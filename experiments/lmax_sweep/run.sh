#!/bin/bash
# L_max 민감도 스윕 — S2(버스트) 부하를 L_max 를 바꿔가며 인가한다.
#
# 사용: bash run.sh <L_max> <run>      예) bash run.sh 18 1
#
# 왜 필요한가: 논문 3장의 L_max 도출 근거가 "노드당 10개 파일럿에서 OOM 미발생"(메모리 기준)인데,
#   2026-07-31 실측에서 build 노드 메모리는 지속 13~15% 로 여유가 크고 **CPU 가 92~99% 로 포화**했다.
#   즉 도출 근거로 삼은 제약이 실제 병목이 아니다. {18, 24, 30} 스윕 곡선이 있어야
#   "30이 적정"이라는 전향적 근거가 선다.
#
# ⚠️ GlobalLimit 을 패치하므로 **반드시 원복**해야 한다. 원복에 실패하면 이후 전 회차가
#   잘못된 L_max 로 수행된다(조용한 오염). trap 으로 중단·오류 시에도 원복한다.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

LMAX=${1:?"사용법: bash run.sh <L_max> <run>"}
RUN=${2:-"1"}
NAMESPACE="default-cicd"
DEFAULT_LMAX=30
OUTDIR="../results/lmax_sweep"
mkdir -p "$OUTDIR"

case "$LMAX" in
  ''|*[!0-9]*) echo "[중단] L_max 는 정수여야 합니다: '$LMAX'"; exit 1 ;;
esac

restore_lmax() {
  kubectl patch globallimit tekton-queue-limit \
    --type merge -p "{\"spec\":{\"maxPipelines\":${DEFAULT_LMAX}}}" >/dev/null 2>&1 || true
  local now
  now=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}' 2>/dev/null)
  if [ "$now" = "$DEFAULT_LMAX" ]; then
    echo "  L_max 원복 완료 ($now)"
  else
    echo "  [경고] L_max 원복 실패 — 현재 '$now'. 다음 회차 전에 수동 확인할 것!"
  fi
}
trap restore_lmax EXIT

echo "=== L_max 스윕 | L_max=$LMAX run=$RUN ==="

# 이전 회차 잔여물이 있으면 metrics_collect 가 전부 수집해 오염된다(S2 와 동일 가드).
LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$LEFT" != "0" ]; then
  echo "[중단] 네임스페이스에 이전 PipelineRun 이 ${LEFT}건 남아 있습니다. cleanup 후 실행하세요."
  exit 1
fi

echo "[0/4] L_max 를 ${LMAX} 로 설정..."
kubectl patch globallimit tekton-queue-limit \
  --type merge -p "{\"spec\":{\"maxPipelines\":${LMAX}}}"
APPLIED=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}' 2>/dev/null)
[ "$APPLIED" = "$LMAX" ] || { echo "[중단] L_max 적용 실패 (현재 '$APPLIED')"; exit 1; }
echo "  적용 확인: maxPipelines=$APPLIED"
# 컨트롤러가 CRD 변경을 읽어들일 여유(폴링 5초)
sleep 15

echo "[1/4] 버스트 인가 (90건 / 30초)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count 90 --interval 0.33 --env dev --generate-name

echo "[2/4] 잔여 파이프라인 완료 대기 (최대 40분)..."
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

echo "[3/4] 쿨다운 5분..."
sleep 300

echo "[4/4] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/lmax${LMAX}_run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/lmax${LMAX}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/lmax${LMAX}_run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/lmax${LMAX}_run${RUN}.csv ==="
