#!/bin/bash
# V-phantom — phantom 캐시 경로 검증 (named PR)
# A1'와 동일하게 admitted 카운터를 끈 채 named 부하를 인가 → phantom 이 유일 브리지.
# 기대: max_concurrent <= L_max (초과 미발생). A1'(generateName)의 overshoot 와 짝 대조.
# ※ named PR 사용(--generate-name 미부여)이 핵심. 메인 매트릭스는 generateName 통일이나 이 건만 예외.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
NAMESPACE="default-cicd"
OUTDIR="../results/v_phantom_named"
mkdir -p "$OUTDIR"

echo "=== V-phantom Named PR | run=$RUN ==="
echo "  L_max=30 유지, disableAdmittedCounter=true, named 부하(phantom 경로)"

# ⚠️ 원복은 반드시 trap 으로 건다.
#   말미에서만 원복하면 중단·오류(웹훅 스톨 등)로 죽었을 때 disableAdmittedCounter=true 가 남아
#   **이후 전 회차에서 순간 상한 초과 억제가 사라진다**(조용한 오염).
restore_counter() {
  kubectl patch globallimit tekton-queue-limit --type merge     -p '{"spec":{"disableAdmittedCounter":false}}' >/dev/null 2>&1 || true
  local now
  now=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.disableAdmittedCounter}' 2>/dev/null)
  if [ "$now" != "true" ]; then
    echo "  admitted 카운터 원복 완료"
  else
    echo "  [경고] admitted 카운터 원복 실패 — 다음 회차 전에 수동 확인할 것!"
  fi
}
trap restore_counter EXIT

echo "[0/4] admitted 카운터 비활성화 (disableAdmittedCounter=true)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"disableAdmittedCounter":true}}' 2>/dev/null || echo "[경고] GlobalLimit 패치 실패"

echo "[1/4] 90개 burst 생성 (named, 0.33초 간격)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count 90 --interval 0.33 --env dev

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

echo "[후처리] admitted 카운터 원복 (disableAdmittedCounter=false)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"disableAdmittedCounter":false}}' 2>/dev/null

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
