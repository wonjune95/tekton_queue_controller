#!/bin/bash
# S3 — Priority Validation 실험
# λ=3/분 30분 지속 (안정 조건 ρ≈0.3). 우선순위 차등(H2)·W_max 상한 검증.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
RATE=${2:-"3"}            # S0 실측 후 μ 확인해서 조정
NAMESPACE="default-cicd"
OUTDIR="../results/s3_priority"
mkdir -p "$OUTDIR"

echo "=== S3 Adversarial | run=$RUN rate=$RATE/분 ==="

echo "[1/3] 부하 인가 30분 (λ=${RATE}/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate "$RATE" --duration 30 \
  --arrival poisson --seed "$RUN" --generate-name

echo "[2/3] 잔여 대기열 소화 대기 (최대 30분)..."
TIMEOUT=1800
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s 경과)"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[3/3] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/run${RUN}_rate${RATE}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_rate${RATE}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_rate${RATE}_resource.json"

echo "=== 완료: $OUTDIR/run${RUN}_rate${RATE}.csv ==="
