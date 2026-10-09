#!/bin/bash
# E3 — 리더 전환 구간에서 상한이 유지되는가 (복제본 2, 부하 중 리더 강제 종료)
#
#   사용법: bash run.sh <run>
#
# ── 왜 필요한가 ──────────────────────────────────────────────────
# 캠페인은 **복제본 1개**만 계측했다. 리더 선출은 구현돼 있으나(src/workers/leader.py)
# **전환 구간의 거동은 측정된 적이 없다** — 논문의 한계 중 하나다.
# 임차 파라미터는 LEASE_DURATION_SEC=15 · LEASE_RETRY_PERIOD_SEC=2 이므로
# 이론상 15~17초 안에 후임이 인수해야 한다.
#
#   판정 ① 전환 지연: 리더 소멸 → 새 보유자 등재까지 걸린 시간
#        ② 상한 유지: 전환 구간을 포함한 측정 창 전체에서 최대 동시 실행 ≤ L_max
#        ③ 유실 없음: 투입 건수 == 최종 관측 건수 (인가 대기 중 항목이 버려지지 않음)
#
# ⚠️ 종료 후 복제본을 1 로 원복한다(trap). 원복 실패 시 이후 회차가 다른 구성으로 기록된다.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

RUN=${1:-1}
NAMESPACE="${EXP_NS:-default-cicd}"
CTRL_NS="tekton-pipelines"
DEPLOY="tekton-queue-controller"
LEASE="${LEASE_NAME:-tekton-queue-controller-leader}"
COUNT="${E3_COUNT:-90}"
KILL_AFTER="${E3_KILL_AFTER:-90}"   # 부하 시작 후 리더를 종료할 시점(초)
OUTDIR="../results/e3_leader_failover"
mkdir -p "$OUTDIR"

echo "=== E3 리더 페일오버 | count=$COUNT | kill_after=${KILL_AFTER}s | run=$RUN ==="
echo "컨텍스트: $(kubectl config current-context)"

LMAX=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}')
[ "$LMAX" = "30" ] || { echo "[중단] L_max 가 30 이 아니다 ($LMAX)."; exit 1; }

restore_replicas() {
  kubectl -n "$CTRL_NS" scale deploy/"$DEPLOY" --replicas=1 >/dev/null 2>&1 || true
  local now
  now=$(kubectl -n "$CTRL_NS" get deploy/"$DEPLOY" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  [ "$now" = "1" ] && echo "  복제본 원복 완료(1)" \
                   || echo "  [경고] 복제본 원복 실패(현재 $now) — 다음 회차 전에 수동 확인!"
}
cleanup() { kill ${WATCH_PID:-0} ${LEASE_PID:-0} 2>/dev/null || true; restore_replicas; }
trap cleanup EXIT

# ── [1/7] 복제본 2 로 확장 ──
echo "[1/7] 복제본 2 로 확장..."
kubectl -n "$CTRL_NS" scale deploy/"$DEPLOY" --replicas=2
kubectl -n "$CTRL_NS" rollout status deploy/"$DEPLOY" --timeout=180s
kubectl -n "$CTRL_NS" get pods -l app=tekton-queue -o wide

# ── [2/7] 현재 리더 확인 ──
sleep 10
LEADER=$(kubectl -n "$CTRL_NS" get lease "$LEASE" -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || true)
[ -n "$LEADER" ] || { echo "[중단] 임차($LEASE)에서 보유자를 읽지 못했다 — 이름을 확인할 것."; exit 1; }
echo "[2/7] 현재 리더: $LEADER"

# ── [3/7] 감시 시작 (동시 실행 + 임차 보유자) ──
WATCH_CSV="$OUTDIR/run${RUN}_conc.csv"
LEASE_CSV="$OUTDIR/run${RUN}_lease.csv"
bash ../common/conc_watch.sh "$NAMESPACE" "$WATCH_CSV" 2 &
WATCH_PID=$!
( echo "ts_epoch,holder"
  while true; do
    H=$(kubectl -n "$CTRL_NS" get lease "$LEASE" -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || echo "ERR")
    echo "$(date +%s),$H"
    sleep 1
  done ) > "$LEASE_CSV" &
LEASE_PID=$!
echo "[3/7] 감시 시작 (conc=$WATCH_PID lease=$LEASE_PID)"

# ── [4/7] 부하 ──
echo "[4/7] ${COUNT}건 burst 생성..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count "$COUNT" --interval 0 --parallel 4 --env dev --generate-name

# ── [5/7] 리더 종료 ──
echo "[5/7] ${KILL_AFTER}s 뒤 리더 종료 예정..."
sleep "$KILL_AFTER"
KILL_EPOCH=$(date +%s)
echo "  리더 파드 삭제: $LEADER (t=$KILL_EPOCH)"
kubectl -n "$CTRL_NS" delete pod "$LEADER" --wait=false

# ── [6/7] 소화 대기 ──
echo "[6/7] 완료 대기 (최대 40분)..."
TIMEOUT=2400; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s)"
  sleep 15; ELAPSED=$((ELAPSED + 15))
done
kill $WATCH_PID $LEASE_PID 2>/dev/null || true

# ── [7/7] 수집·판정 ──
echo "[7/7] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" --output "$OUTDIR/run${RUN}.csv"
python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_resource.json" || echo "  [경고] 자원 수집 실패(계속)"

python3 - "$LEASE_CSV" "$WATCH_CSV" "$KILL_EPOCH" "$LEADER" "$LMAX" "$COUNT" \
         "$OUTDIR/run${RUN}.csv" "$OUTDIR/run${RUN}_verdict.txt" <<'PYEOF'
import csv, sys
# Windows 콘솔(cp949)은 이모지를 못 찍어 UnicodeEncodeError 로 죽는다(2026-08-13 실측).
# 파일에는 UTF-8 로 그대로 쓰고, stdout 만 대체 문자로 흘린다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
lease_csv, conc_csv, kill_ep, old, lmax, count, res_csv, out = sys.argv[1:9]
kill_ep, lmax, count = int(kill_ep), int(lmax), int(count)
L = list(csv.DictReader(open(lease_csv, encoding="utf-8")))
# 종료 시점 이후 보유자가 «다른 이름»으로 바뀐 첫 시각
switch = next((int(r["ts_epoch"]) for r in L
               if int(r["ts_epoch"]) >= kill_ep and r["holder"] not in (old, "ERR", "")), None)
lines = [f"리더 종료: {old} (t={kill_ep})"]
lines.append(f"전환 지연: {switch-kill_ep}초" if switch else "전환 지연: 미관측(임차 보유자 변화 없음)")
C = list(csv.DictReader(open(conc_csv, encoding="utf-8")))
mx = max((int(r["running"]) for r in C), default=0)
win = [int(r["running"]) for r in C if kill_ep <= int(r["ts_epoch"]) <= kill_ep + 120]
lines.append(f"최대 동시 실행: 전체 {mx} / 전환 구간(+120s) {max(win) if win else 'n/a'} (상한 {lmax})")
lines.append("상한 유지: " + ("✅ 유지" if mx <= lmax else "⚠️ 초과"))
n = sum(1 for _ in csv.DictReader(open(res_csv, encoding="utf-8")))
lines.append(f"관측 건수 {n} / 투입 {count} → " + ("✅ 유실 없음" if n >= count else "⚠️ 차이 — 원자료 확인"))
open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
PYEOF

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
