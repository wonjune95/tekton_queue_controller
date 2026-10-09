#!/bin/bash
# S1 — Peak-hour 실험
# 저부하(λ=1, 10분) → 피크(λ=10, 10분) 교차

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
NAMESPACE="default-cicd"
OUTDIR="../results/s1_peak_hour"
mkdir -p "$OUTDIR"

echo "=== S1 Peak-hour | run=$RUN ==="

echo "[1/4] 저부하 구간 10분 (λ=1/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate 1 --duration 10 \
  --arrival poisson --seed "$RUN" --generate-name

echo "[2/4] 피크 구간 10분 (λ=10/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate 10 --duration 10 \
  --arrival poisson --seed "$((RUN + 100))" --generate-name

echo "[3/4] 잔여 파이프라인 완료 대기 (최대 40분)..."
TIMEOUT=2400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s 경과)"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[4/4] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
