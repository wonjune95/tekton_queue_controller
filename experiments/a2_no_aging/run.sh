#!/bin/bash
# A2 — 에이징 제거
# GlobalLimit CRD의 agingIntervalSec을 9999로 패치 → 에이징 사실상 비활성화

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
NAMESPACE="default-cicd"
OUTDIR="../results/a2_no_aging"
mkdir -p "$OUTDIR"

echo "=== A2 No Aging | run=$RUN ==="

# ⚠️ 원복은 반드시 trap 으로 건다.
#   예전에는 스크립트 말미에서만 원복해서, 중단·오류(웹훅 스톨 등)로 도중에 죽으면
#   agingIntervalSec=9999 가 그대로 남아 **이후 전 회차가 에이징 없이 수행**된다(조용한 오염).
restore_aging() {
  kubectl patch globallimit tekton-queue-limit \
    --type merge -p '{"spec":{"agingIntervalSec":300}}' >/dev/null 2>&1 || true
  local now
  now=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.agingIntervalSec}' 2>/dev/null)
  if [ "$now" = "300" ]; then
    echo "  에이징 원복 완료 (T_a=$now)"
  else
    echo "  [경고] 에이징 원복 실패 — 현재 '$now'. 다음 회차 전에 수동 확인할 것!"
  fi
}
trap restore_aging EXIT

echo "[0/5] 에이징 비활성화 (agingIntervalSec=9999)..."
kubectl patch globallimit tekton-queue-limit \
  --type merge -p '{"spec":{"agingIntervalSec":9999}}' 2>/dev/null || \
  echo "[경고] GlobalLimit 패치 실패"

echo "[1/4] 저부하 10분 (λ=1/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate 1 --duration 10 \
  --arrival poisson --seed "$RUN" --generate-name

echo "[2/4] 피크 10분 (λ=10/분)..."
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

echo "[후처리] 에이징 원복 (agingIntervalSec=300)..."
kubectl patch globallimit tekton-queue-limit \
  --type merge -p '{"spec":{"agingIntervalSec":300}}' 2>/dev/null

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
