#!/bin/bash
# A0-R — 컨트롤러 비활성 + requests 설정 (요인 2x2의 (off, set) 셀)
# 대조군: 큐 없이 90개 burst → 무제한 동시 실행 확인

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
# 부하 유형: burst = S2 와 동일(90건/30초), peak = S1 과 동일(저부하 10분 → 피크 10분)
# 요인 실험은 두 부하에서 각각 N=3 을 인가한다(HANDOVER §5: "2×2 잔여 셀 + S1×A0-R × N3").
SCENARIO=${2:-"burst"}
NAMESPACE="default-cicd"
PIPELINE="petclinic-build-requests"          # ★ requests 주석 파이프라인 (A0-NR 과의 유일한 차이)
PIPELINE_FILE="../pipeline/petclinic-build-requests.yaml"
OUTDIR="../results/a0r_no_controller"
mkdir -p "$OUTDIR"

case "$SCENARIO" in
  burst) SUFFIX="" ;;
  peak)  SUFFIX="_peak" ;;
  *) echo "[중단] SCENARIO 는 burst 또는 peak 여야 합니다 (받은 값: $SCENARIO)"; exit 1 ;;
esac

echo "=== A0-R (컨트롤러 비활성 + requests 설정) | run=$RUN scenario=$SCENARIO ==="

# ── 전제 확인 ─────────────────────────────────────────────────────
# A0-R 은 "requests 가 설정된" 조건이다. requests 파이프라인 없이 기본 파이프라인으로 돌리면
# 실질적으로 A0-NR 과 동일한 조건이 되어 요인 2x2 (off,set) 셀이 무너진다(조용한 오염).
# 따라서 파이프라인이 없으면 실행하지 않는다.
if [ ! -f "$PIPELINE_FILE" ]; then
  echo "[중단] $PIPELINE_FILE 이 없습니다."
  echo "       A0-R 은 requests 설정 조건이므로 이 파이프라인이 필수입니다."
  echo "       먼저 파라미터 실측 후 생성하세요:"
  echo "         bash ../param_measure/run.sh 5 45"
  echo "         python3 ../common/make_requests_pipeline.py --input ../results/param/mem_peak.csv \\"
  echo "                 --output $PIPELINE_FILE"
  exit 1
fi
# ※ `| tail` 로 파이프하면 종료 코드가 tail 것이 되어 set -e 가 **적용 실패를 놓친다**.
#   (2026-07-29 param_measure 에서 실제로 겪음: 적용 거부 → 없는 파이프라인 참조 PR 생성 → 빈 결과)
kubectl apply -f "$PIPELINE_FILE" -n "$NAMESPACE"

# ⚠️ 복구는 반드시 trap 으로 건다.
#   말미에서만 복구하면 중단·오류(웹훅 스톨 등)로 죽었을 때 **컨트롤러가 꺼진 채로 남아
#   이후 전 회차가 무제어로 수행**된다. 이 프로젝트에서 가장 위험한 오염 경로다.
restore_controller() {
  echo "[후처리] 큐 컨트롤러 복구..."
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  kubectl wait --for=condition=ready pod -l app=tekton-queue \
    -n tekton-pipelines --timeout=120s >/dev/null 2>&1 || true
  local ready fp
  ready=$(kubectl get pods -n tekton-pipelines -l app=tekton-queue \
            -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)
  fp=$(kubectl get mutatingwebhookconfiguration tekton-queue-mutator \
         -o jsonpath='{.webhooks[0].failurePolicy}' 2>/dev/null)
  if [ "$ready" = "true" ] && [ "$fp" = "Fail" ]; then
    echo "  컨트롤러 복구 완료 (Ready=$ready, failurePolicy=$fp)"
  else
    echo "  [경고] 컨트롤러 복구 실패 (Ready='$ready', failurePolicy='$fp') — 다음 회차 전에 수동 확인!"
  fi
}
trap restore_controller EXIT

echo "[0/4] 큐 컨트롤러 비활성화..."
# 웹훅 failurePolicy → Ignore (webhook 없어도 PR 생성 통과)
kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
  --type=json \
  -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]'
# 컨트롤러 Pod 중단
kubectl scale deployment tekton-queue-controller \
  -n tekton-pipelines --replicas=0
# Pod 완전 종료 대기
kubectl wait --for=delete pod \
  -l app=tekton-queue \
  -n tekton-pipelines --timeout=60s 2>/dev/null || true
echo "  컨트롤러 중단 완료"

if [ "$SCENARIO" = "burst" ]; then
  echo "[1/4] 90개 burst 생성 (0.33초 간격, requests 파이프라인)..."
  python3 ../common/pr_create.py \
    --namespace "$NAMESPACE" --mode burst \
    --count 90 --interval 0.33 --env dev --generate-name \
    --pipeline "$PIPELINE"
else
  # S1 과 동일한 부하(저부하 10분 → 피크 10분). 시드도 S1 과 같은 규칙을 쓴다.
  echo "[1/4] 저부하 구간 10분 (λ=1/분, requests 파이프라인)..."
  python3 ../common/pr_create.py \
    --namespace "$NAMESPACE" --mode steady \
    --rate 1 --duration 10 \
    --arrival poisson --seed "$RUN" --generate-name \
    --pipeline "$PIPELINE"
  echo "     피크 구간 10분 (λ=10/분)..."
  python3 ../common/pr_create.py \
    --namespace "$NAMESPACE" --mode steady \
    --rate 10 --duration 10 \
    --arrival poisson --seed "$((RUN + 100))" --generate-name \
    --pipeline "$PIPELINE"
fi

echo "[2/4] 완료 대기 (최대 40분)..."
TIMEOUT=2400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running" || true)
  [ "$PENDING" -eq 0 ] && break
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[3/4] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/run${RUN}${SUFFIX}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}${SUFFIX}_resource.csv" \
  --summary "$OUTDIR/run${RUN}${SUFFIX}_resource.json"

# 컨트롤러 복구는 trap(restore_controller)이 담당한다. 여기서 중복 호출하지 않는다.

echo "=== 완료: $OUTDIR/run${RUN}${SUFFIX}.csv ==="
