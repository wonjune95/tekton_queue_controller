#!/bin/bash
# [DEPRECATED 2026-07-28] 구 A1 (L_max=999, 상한 제거) — A0-R 과 주장 중복으로 매트릭스에서 제외.
# 실행하지 말 것. 스크립트는 이력 보존용으로만 남긴다. (HANDOVER 5절 축소 감사 참조)
# 결과: max_concurrent >> Lmax=30 → H1 위반 확인

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
NAMESPACE="default-cicd"
OUTDIR="../results/a1_no_admitted"
mkdir -p "$OUTDIR"

echo "=== A1 No Admitted Tracking | run=$RUN ==="
echo "  maxPipelines=999 설정하여 Lmax 제약 제거"

echo "[0/4] Lmax 무력화 (maxPipelines=999)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"maxPipelines":999}}' 2>/dev/null || echo "[경고] GlobalLimit 패치 실패"

echo "[1/4] 90개 burst 생성..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count 90 --interval 0.33 --env dev --generate-name

echo "[2/4] 완료 대기 (최대 40분)..."
TIMEOUT=2400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 실행/대기 (${ELAPSED}s 경과)"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[3/4] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_resource.json"

echo "[후처리] maxPipelines 원복 (30)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"maxPipelines":30}}' 2>/dev/null

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
