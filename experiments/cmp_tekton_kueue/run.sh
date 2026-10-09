#!/bin/bash
# 비교군: tekton-kueue | 사용법: bash run.sh <scenario> <run>   scenario ∈ {s1,s2,s3}
# 내 컨트롤러만 비활성 → tekton-kueue 가 게이팅. 부하는 본실험과 동일(푸아송·generateName).
# 전제: Tekton+cert-manager+Kueue v0.16.6+tekton-kueue 설치됨(comparison_setup.md).
# ※ 비파괴.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
SCENARIO=${1:-"s1"}
RUN=${2:-"1"}
RATE=${3:-"3"}                   # s3 용 λ (S3 와 동기화)
NAMESPACE="default-cicd"
OUTDIR="../results/cmp_tekton_kueue"
mkdir -p "$OUTDIR"

echo "=== 비교군 tekton-kueue | scenario=$SCENARIO run=$RUN ==="

# ── 비교 대상 가동 확인 (중단형) ─────────────────────────────────
# ⚠️ 이 스크립트는 **내 컨트롤러를 끄고** 부하를 인가한다.
#   tekton-kueue 가 설치돼 있지 않거나 웹훅이 게이팅하지 않으면
#   **아무 제어 없이 90건이 쏟아지고**, 그 결과가 "tekton-kueue 는 동시 실행을 제한하지 못했다"로
#   기록된다. 즉 **비교군에 불리하고 우리에게 유리한 방향의 허위 결과**가 조용히 만들어진다.
#   전제를 주석으로만 두지 말고 실행 시점에 검증해 어긋나면 중단한다.
require() {   # require <설명> <실제> <기대>
  if [ "$2" = "$3" ]; then
    echo "  OK   $1 ($2)"
  else
    echo "  [중단] $1: '$2' (기대 '$3')"
    echo "         COMPARISON_SETUP.md 의 tekton-kueue 설치를 먼저 완료하세요."
    exit 1
  fi
}
echo "[사전] tekton-kueue 가동 확인..."
require "웹훅 설정 존재" \
  "$(kubectl get mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration -o name 2>/dev/null | wc -l | tr -d ' ')" "1"
require "웹훅 Deployment Ready" \
  "$(kubectl get deploy tekton-kueue-webhook -n tekton-kueue -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" "1"
require "Kueue 컨트롤러 Ready" \
  "$(kubectl get deploy kueue-controller-manager -n kueue-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" "1"
# 쿼터가 L_max 와 같아야 동등 비교가 된다(kueue-resources.yaml: nominalQuota=30).
CQ=$(kubectl get clusterqueue -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -n "$CQ" ] || { echo "  [중단] ClusterQueue 가 없습니다. kueue-resources.yaml 적용 필요."; exit 1; }
echo "  OK   ClusterQueue ($CQ)"
# externalFrameworks 에 pipelineruns.tekton.dev 가 활성화돼 있어야 파이프라인런을 가로챈다.
kubectl get cm kueue-manager-config -n kueue-system -o yaml 2>/dev/null \
  | grep -q "pipelineruns.tekton.dev" \
  || { echo "  [중단] Kueue 설정에 externalFrameworks: pipelineruns.tekton.dev 가 없습니다."; exit 1; }
echo "  OK   externalFrameworks 에 pipelineruns.tekton.dev 활성"

disable_my_controller() {
  echo "[준비] 내 컨트롤러 비활성(웹훅 Ignore + scale 0)..."
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' 2>/dev/null || true
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=0 2>/dev/null || true
  kubectl wait --for=delete pod -l app=tekton-queue -n tekton-pipelines --timeout=60s 2>/dev/null || true
}
restore_my_controller() {
  echo "[후처리] 내 컨트롤러 복구..."
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=1 2>/dev/null || true
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' 2>/dev/null || true
  kubectl wait --for=condition=ready pod -l app=tekton-queue -n tekton-pipelines --timeout=120s 2>/dev/null || true
}
trap restore_my_controller EXIT

echo "[1/4] Kueue 큐 리소스 적용(nominalQuota=30)..."
# ※ `| tail` 로 파이프하면 종료 코드가 tail 것이 되어 set -e 가 **적용 실패를 놓친다**.
#   (2026-07-29 param_measure 에서 실제로 겪음: 적용 거부 → 없는 파이프라인 참조 PR 생성 → 빈 결과)
# ⚠️ 스크립트 상단에서 이미 자기 디렉터리로 cd 했으므로 파일명만 쓴다.
#   `$(dirname "$0")/...` 를 쓰면 호출 형태(`cmp_tekton_kueue/run.sh`)에 따라
#   `cmp_tekton_kueue/cmp_tekton_kueue/...` 로 이중 결합되어 적용에 실패한다
#   (2026-08-02 실측: 비교군 첫 회차가 32초 만에 중단).
kubectl apply -f kueue-resources.yaml

disable_my_controller

echo "[2/4] 부하 인가 (scenario=$SCENARIO, generateName)..."
PC="python3 ../common/pr_create.py --namespace $NAMESPACE"
case "$SCENARIO" in
  s1)  # peak-hour: 저부하 10분 + 피크 10분
    $PC --mode steady --rate 1  --duration 10 --arrival poisson --seed "$RUN"        --generate-name
    $PC --mode steady --rate 10 --duration 10 --arrival poisson --seed "$((RUN+100))" --generate-name ;;
  s2)  # release burst: 90개
    $PC --mode burst --count 90 --interval 0.33 --env dev --generate-name ;;
  s3)  # adversarial steady: λ=RATE 30분
    $PC --mode steady --rate "$RATE" --duration 30 --arrival poisson --seed "$RUN" --generate-name ;;
  *) echo "알 수 없는 scenario: $SCENARIO (s1|s2|s3)"; exit 1 ;;
esac

echo "[3/4] 완료 대기 (최대 40분)..."
TIMEOUT=2400; ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s)"; sleep 15; ELAPSED=$((ELAPSED+15))
done

echo "[4/4] 지표 수집..."
python3 ../common/metrics_collect.py --namespace "$NAMESPACE" \
  --output "$OUTDIR/${SCENARIO}_run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/${SCENARIO}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/${SCENARIO}_run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/${SCENARIO}_run${RUN}.csv ==="
