#!/bin/bash
# 비교군: Volcano | 사용법: bash run.sh <scenario> <run>   scenario ∈ {s1,s2}
# 내 컨트롤러 + tekton-kueue 웹훅 둘 다 비활성 → Volcano 만 게이팅(파드 스케줄링 계층).
# PR 파드에 schedulerName=volcano 주입 + cpu 요청량 파이프라인(petclinic-build-volcano).
# 전제: Tekton v1.9.2 + Volcano v1.15.0 설치됨(comparison_setup.md).
# ※ 비파괴. 관찰: 초과분은 PipelineRun 시작된 채 파드만 Pending(부분 실행 위험).

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
SCENARIO=${1:-"s1"}
RUN=${2:-"1"}
NAMESPACE="default-cicd"
PIPELINE_FILE="../pipeline/petclinic-build-volcano.yaml"
OUTDIR="../results/cmp_volcano"
mkdir -p "$OUTDIR"

echo "=== 비교군 Volcano | scenario=$SCENARIO run=$RUN ==="

# ── 비교 대상 가동 확인 (중단형) ─────────────────────────────────
# ⚠️ 이 스크립트는 **내 컨트롤러와 tekton-kueue 웹훅을 모두 끄고** 부하를 인가한다.
#   Volcano 가 없거나 큐 capability 가 적용되지 않으면 **아무 제어 없이 부하가 쏟아지고**,
#   그 결과가 "Volcano 는 동시 실행을 제한하지 못했다"로 기록된다.
#   즉 **비교군에 불리하고 우리에게 유리한 방향의 허위 결과**가 조용히 만들어진다.
echo "[사전] Volcano 가동 확인..."
VC_SCHED=$(kubectl get deploy volcano-scheduler -n volcano-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
[ "$VC_SCHED" = "1" ] || { echo "  [중단] volcano-scheduler 가 Ready 가 아닙니다 (현재 '$VC_SCHED')."; \
  echo "         COMPARISON_SETUP.md 의 Volcano 설치를 먼저 완료하세요."; exit 1; }
echo "  OK   volcano-scheduler Ready"
# ⚠️ 반드시 **정규 이름**(queues.scheduling.volcano.sh)을 쓴다.
#   Kueue 의 LocalQueue 도 단축명 `queue` 를 등록하므로, 짧은 이름은 Kueue 쪽으로 해석된다.
#   (2026-08-03 실제로 겪음: Volcano 가 정상인데도 큐를 못 찾아 회차가 시작되지 않았다.)
kubectl get queues.scheduling.volcano.sh default >/dev/null 2>&1 \
  || { echo "  [중단] Volcano queue 'default' 가 없습니다."; exit 1; }
echo "  OK   Volcano queue default 존재"
[ -f "$PIPELINE_FILE" ] || { echo "  [중단] 파이프라인 파일 없음: $PIPELINE_FILE"; exit 1; }
echo "  OK   파이프라인 파일 존재"

restore() {
  echo "[후처리] 컨트롤러/웹훅 복구 + Volcano 큐 capability 원복..."
  # 내 컨트롤러 복구
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=1 2>/dev/null || true
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' 2>/dev/null || true
  # tekton-kueue 웹훅 복구
  kubectl patch mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' 2>/dev/null || true
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=1 2>/dev/null || true
  # Volcano 큐 capability 원복(무제한)
  kubectl patch queues.scheduling.volcano.sh default --type=merge -p '{"spec":{"capability":null}}' 2>/dev/null || true
}
trap restore EXIT

echo "[1/5] 내 컨트롤러 비활성(웹훅 Ignore + scale 0)..."
kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
  --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' 2>/dev/null || true
kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=0 2>/dev/null || true
kubectl wait --for=delete pod -l app=tekton-queue -n tekton-pipelines --timeout=60s 2>/dev/null || true

echo "[2/5] tekton-kueue 웹훅 비활성(Ignore + scale 0) → Volcano 만 게이팅..."
kubectl patch mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration \
  --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' 2>/dev/null || true
kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=0 2>/dev/null || true

echo "[3/5] Volcano 큐 capability=3(≈30x100m) 설정 + cpu요청 파이프라인 적용..."
kubectl patch queues.scheduling.volcano.sh default --type=merge -p '{"spec":{"capability":{"cpu":"3"}}}' 2>/dev/null \
  || { echo "[중단] Volcano queue capability 패치 실패 — 게이팅 없이 돌면 허위 결과가 된다."; exit 1; }
# 적용 결과를 확인한다. 패치가 받아들여져도 값이 반영되지 않으면 상한이 없는 것과 같다.
CAP=$(kubectl get queues.scheduling.volcano.sh default -o jsonpath='{.spec.capability.cpu}' 2>/dev/null)
[ "$CAP" = "3" ] || { echo "[중단] Volcano queue capability 미반영 (현재 '$CAP', 기대 '3')."; exit 1; }
echo "  capability 적용 확인: cpu=$CAP"
# ※ `| tail` 로 파이프하면 종료 코드가 tail 것이 되어 set -e 가 **적용 실패를 놓친다**.
#   (2026-07-29 param_measure 에서 실제로 겪음: 적용 거부 → 없는 파이프라인 참조 PR 생성 → 빈 결과)
kubectl apply -f "$PIPELINE_FILE" -n "$NAMESPACE"

echo "[4/5] 부하 인가 (scenario=$SCENARIO, schedulerName=volcano, generateName)..."
PC="python3 ../common/pr_create.py --namespace $NAMESPACE --pipeline petclinic-build-volcano --scheduler-name volcano"
case "$SCENARIO" in
  s1)
    $PC --mode steady --rate 1  --duration 10 --arrival poisson --seed "$RUN"        --generate-name
    $PC --mode steady --rate 10 --duration 10 --arrival poisson --seed "$((RUN+100))" --generate-name ;;
  s2)
    $PC --mode burst --count 90 --interval 0.33 --env dev --generate-name ;;
  *) echo "알 수 없는 scenario: $SCENARIO (s1|s2)"; exit 1 ;;
esac

echo "[대기] 완료 대기 (최대 40분)... (Volcano: PR 시작됐어도 파드 Pending 가능 관찰)"
TIMEOUT=2400; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s)"; sleep 15; ELAPSED=$((ELAPSED+15))
done

echo "[5/5] 지표 수집..."
python3 ../common/metrics_collect.py --namespace "$NAMESPACE" \
  --output "$OUTDIR/${SCENARIO}_run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/${SCENARIO}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/${SCENARIO}_run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/${SCENARIO}_run${RUN}.csv ==="
