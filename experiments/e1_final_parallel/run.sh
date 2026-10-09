#!/bin/bash
# E1 — 최종 구성 + 실부하 + 동시 도착을 «전체 규모»에서 동시에 건 조건
#
#   사용법: bash run.sh <run>            (기본 3회: run 1,2,3)
#
# ── 왜 이 조건이 필요한가 ────────────────────────────────────────
# 논문 §5.6 의 인가 경로 대조는 **축소 구성(L_max=5, 단일 노드)** 에서 수행했다.
# 전체 규모(L_max=30, GKE 5노드)에서 동시 도착을 건 관측은 a1p 회차뿐인데,
# 그 회차는 **웹훅 시점 판정 + admitted 카운터** 구성이었다(현 최종 구성이 아니다).
# 즉 「최종 구성 × 실부하 × 동시 도착 × 전체 규모」 칸이 비어 있었다 — 이 조건이 그 칸이다.
#
#   판정: 측정 창 내 최대 동시 실행 수가 L_max(=30) 이하인가.
#   부하: a1p 와 동일(90건 · 간격 0 · 병렬 12 · generateName) — 초과가 관측됐던 그 부하.
#
# ※ generateName 필수 — named PR 은 phantom 항목이 슬롯을 즉시 반영해 초과가 발생하지 않는다(조건 V).
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

RUN=${1:-1}
NAMESPACE="${EXP_NS:-default-cicd}"
PARALLEL="${E1_PARALLEL:-12}"
COUNT="${E1_COUNT:-90}"
OUTDIR="../results/e1_final_parallel"
mkdir -p "$OUTDIR"

echo "=== E1 최종 구성 동시 도착 | parallel=$PARALLEL | count=$COUNT | run=$RUN ==="
echo "컨텍스트: $(kubectl config current-context)"

# ── [0/5] 선행 조건 검증 — 하나라도 어긋나면 조건이 뒤바뀐 채 기록된다 ──
LMAX=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}')
CNTR=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.disableAdmittedCounter}')
[ -z "$CNTR" ] && CNTR="false"
REPL=$(kubectl -n tekton-pipelines get deploy/tekton-queue-controller -o jsonpath='{.spec.replicas}')

echo "[0/5] 선행 조건: L_max=$LMAX · disableAdmittedCounter=$CNTR · replicas=$REPL"
[ "$LMAX" = "30" ]   || { echo "[중단] L_max 가 30 이 아니다 ($LMAX)."; exit 1; }
[ "$CNTR" = "false" ] || { echo "[중단] 카운터가 꺼져 있다 — 최종 구성이 아니다."; exit 1; }
[ "$REPL" = "1" ]    || { echo "[중단] 복제본이 1 이 아니다 ($REPL) — E3 뒤라면 원복할 것."; exit 1; }

for WH in tekton-kueue-mutating-webhook-configuration volcano-admission-service-pods-mutate; do
  kubectl get mutatingwebhookconfiguration "$WH" >/dev/null 2>&1 \
    && { echo "[중단] 비교군 웹훅이 살아 있다 ($WH) — 상호 배제 위반."; exit 1; }
done
echo "  비교군 웹훅 없음 확인"

# ── [1/5] 동시 실행 수 감시 시작 ──
WATCH_CSV="$OUTDIR/run${RUN}_conc.csv"
bash ../common/conc_watch.sh "$NAMESPACE" "$WATCH_CSV" 2 &
WATCH_PID=$!
trap 'kill $WATCH_PID 2>/dev/null || true' EXIT
echo "[1/5] 동시 실행 감시 시작 (pid=$WATCH_PID)"

# ── [2/5] 부하 ──
echo "[2/5] ${COUNT}건 burst 생성 (generateName, 병렬 $PARALLEL)..."
START_EPOCH=$(date +%s)
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count "$COUNT" --interval 0 --parallel "$PARALLEL" --env dev --generate-name

# ── [3/5] 소화 대기 ──
echo "[3/5] 완료 대기 (최대 40분)..."
TIMEOUT=2400; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s)"
  sleep 15; ELAPSED=$((ELAPSED + 15))
done

kill $WATCH_PID 2>/dev/null || true

# ── [4/5] 수집 ──
echo "[4/5] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" --output "$OUTDIR/run${RUN}.csv"
python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_resource.json" || echo "  [경고] 자원 수집 실패(계속)"

# ── [5/5] 판정 ──
OBS=$(awk -F, 'NR>1 && $3+0>m {m=$3+0} END {print m+0}' "$WATCH_CSV")
echo "[5/5] 판정"
echo "  감시 계열 최대 동시 실행: $OBS (상한 $LMAX)"
if [ "$OBS" -le "$LMAX" ]; then
  echo "  ✅ 상한 유지 (초과 없음)"
else
  echo "  ⚠️ 상한 초과 관측 — 원자료를 보존하고 조건을 재확인할 것"
fi
echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
