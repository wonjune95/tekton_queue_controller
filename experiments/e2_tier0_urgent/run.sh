#!/bin/bash
# E2 — 포화 상태에서 Tier 0(긴급) 항목이 승격분보다 앞서는가
#
#   사용법: bash run.sh <run>
#
# ── 왜 필요한가 ──────────────────────────────────────────────────
# 논문은 «에이징으로 Tier 1 최대 대기가 2,181초까지 늘 수 있으나, 시간 제약이 있는 긴급 배포는
# 에이징에서 제외되는 Tier 0 경로로 우회할 수 있다»고 서술한다. 그런데 **Tier 0 은 부하에 포함된 적이 없어
# 효과가 검증되지 않았다**(6.2 단서). 이 조건이 그 단서를 실측으로 바꾼다.
#
#   설계: 대기열이 깊어진 뒤(포화) 같은 시점에 두 건을 넣는다.
#           · 긴급  = queue.tekton.dev/urgent=true  → Tier 0
#           · 대조  = env=prod                       → Tier 1 (승격분이 도달하는 최상위 층)
#         두 건의 대기 시간을 비교한다. 설계대로면 긴급이 먼저 인가된다.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

RUN=${1:-1}
NAMESPACE="${EXP_NS:-default-cicd}"
SAT_COUNT="${E2_SAT_COUNT:-90}"
SOAK="${E2_SOAK:-120}"     # 포화 후 대기열이 안정될 때까지
OUTDIR="../results/e2_tier0_urgent"
mkdir -p "$OUTDIR"

echo "=== E2 Tier 0 긴급 우회 | saturate=$SAT_COUNT | run=$RUN ==="
echo "컨텍스트: $(kubectl config current-context)"

LMAX=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}')
AGING=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.agingIntervalSec}')
MINTIER=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.agingMinTier}')
echo "[0/6] 선행 조건: L_max=$LMAX · T_a=${AGING}s · agingMinTier=$MINTIER"
[ "$AGING" = "300" ] || echo "  [경고] T_a 가 300 이 아니다 ($AGING)"
[ "$LMAX" = "30" ] || { echo "[중단] L_max 가 30 이 아니다 ($LMAX)."; exit 1; }

WATCH_CSV="$OUTDIR/run${RUN}_conc.csv"
bash ../common/conc_watch.sh "$NAMESPACE" "$WATCH_CSV" 2 &
WATCH_PID=$!
trap 'kill $WATCH_PID 2>/dev/null || true' EXIT

# ── [1/6] 포화 ──
echo "[1/6] ${SAT_COUNT}건으로 대기열 포화 (dev, Tier 3)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count "$SAT_COUNT" --interval 0 --parallel 1 --env dev --generate-name

# ── [2/6] 대기열이 실제로 깊어졌는지 확인 ──
echo "[2/6] 포화 안정 대기 (${SOAK}s)..."
sleep "$SOAK"
QUEUED=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | grep -c "Pending" || true)
echo "  대기 중: ${QUEUED}건"
[ "$QUEUED" -ge 10 ] || { echo "[중단] 대기열이 얕다(${QUEUED}) — 포화 조건 불성립."; exit 1; }

# ── [3/6] 같은 시점에 긴급 1건 + 대조(prod) 1건 ──
STAMP=$(date +%s)
echo "[3/6] 긴급(Tier 0)·대조(Tier 1) 동시 투입 (t=$STAMP)"
python3 ../common/pr_create.py --namespace "$NAMESPACE" --mode burst \
  --count 1 --parallel 1 --env prod --urgent --generate-name &
python3 ../common/pr_create.py --namespace "$NAMESPACE" --mode burst \
  --count 1 --parallel 1 --env prod --generate-name &
wait
echo "  투입 완료"

# ── [4/6] 소화 대기 ──
echo "[4/6] 완료 대기 (최대 40분)..."
TIMEOUT=2400; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s)"
  sleep 15; ELAPSED=$((ELAPSED + 15))
done
kill $WATCH_PID 2>/dev/null || true

# ── [5/6] 수집 ──
echo "[5/6] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" --output "$OUTDIR/run${RUN}.csv"

# 투입 시점 이후 생성된 Tier 0 / Tier 1 항목만 뽑아 대기 시간을 대조한다.
python3 - "$OUTDIR/run${RUN}.csv" "$STAMP" "$OUTDIR/run${RUN}_verdict.txt" <<'PYEOF'
import csv, sys
src, stamp, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
rows = list(csv.DictReader(open(src, encoding="utf-8")))
def wait_of(r):
    for k in ("wait_seconds", "wait_sec", "wait"):
        if k in r and r[k]:
            return float(r[k])
    return None
urgent = [r for r in rows if r.get("tier") == "0"]
tier1  = [r for r in rows if r.get("tier") == "1"]
lines = [f"Tier 0 건수={len(urgent)} · Tier 1 건수={len(tier1)}"]
if urgent:
    lines.append(f"Tier 0 대기(초): {[wait_of(r) for r in urgent]}")
if tier1:
    lines.append(f"Tier 1 대기(초): {[wait_of(r) for r in tier1]}")
if urgent and tier1:
    u = min(w for w in (wait_of(r) for r in urgent) if w is not None)
    t = min(w for w in (wait_of(r) for r in tier1)  if w is not None)
    lines.append(f"판정: 긴급 {u:.1f}s vs 대조 {t:.1f}s → "
                 + ("✅ 긴급이 먼저" if u < t else "⚠️ 역전 — 원자료 확인"))
open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
PYEOF

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
