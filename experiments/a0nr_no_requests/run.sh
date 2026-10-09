#!/bin/bash
# A0-NR — 컨트롤러 비활성 + requests 무설정 (요인 2x2의 (off, unset) 셀)
# 큐 컨트롤러도 requests도 없는 최악 조건 → 노드 자원 고갈(OOMKilled·NotReady) 노출.
#
# ※※ 파괴적 조건 ※※  CLAUDE.md 안전 규칙: 임의 실행 금지. 사용자 명시 지시 시에만, 캠페인 최후에 수행.
# no-requests 파이프라인을 명시 적용해 (off, unset) 을 보장한다.
# ※ generateName 통일.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
NAMESPACE="default-cicd"
PIPELINE_FILE="../pipeline/petclinic-build-experiment.yaml"   # 현재 requests 없음 = unset
OUTDIR="../results/a0nr_no_requests"
mkdir -p "$OUTDIR"

echo "=== A0-NR No Controller + No Requests | run=$RUN ==="
echo "  [파괴적] 컨트롤러 비활성 + requests 무설정, 90개 generateName burst"

echo "[0/5] requests 무설정 파이프라인 적용 (off,unset 보장)..."
# ※ `| tail` 로 파이프하면 종료 코드가 tail 것이 되어 set -e 가 **적용 실패를 놓친다**.
#   (2026-07-29 param_measure 에서 실제로 겪음: 적용 거부 → 없는 파이프라인 참조 PR 생성 → 빈 결과)
kubectl apply -f "$PIPELINE_FILE" -n "$NAMESPACE"

# ⚠️ 복구는 반드시 trap 으로 건다 — **파괴적 조건이라 더 중요하다.**
#   A0-NR 은 노드 OOM·NotReady 를 유발하므로 중단될 확률 자체가 높다.
#   말미에서만 복구하면 그때 컨트롤러가 꺼진 채 남아 **이후 전 회차가 무제어**로 수행된다.
# ── tekton-kueue 상호 배제 ───────────────────────────────────────
# ⚠️ 비교군 실험을 위해 tekton-kueue 를 설치하면 그 웹훅이 **모든 네임스페이스의 모든
#   PipelineRun CREATE 를 가로챈다**(namespaceSelector 없음, failurePolicy=Fail).
#   그대로 두면 A1 의 파이프라인런까지 Kueue 가 게이팅해 **측정 대상이 뒤바뀐다**(조용한 오염).
#   설치돼 있을 때만 끄고, 종료 시 원복한다.
KUEUE_WH="tekton-kueue-mutating-webhook-configuration"
have_kueue() { kubectl get mutatingwebhookconfiguration "$KUEUE_WH" >/dev/null 2>&1; }
disable_kueue() {
  have_kueue || { echo "  (tekton-kueue 미설치 — 건너뜀)"; return 0; }
  echo "  tekton-kueue 웹훅 비활성(Ignore + replicas=0)..."
  kubectl patch mutatingwebhookconfiguration "$KUEUE_WH" \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=0 >/dev/null 2>&1 || true
  kubectl wait --for=delete pod -l app.kubernetes.io/name=tekton-kueue -n tekton-kueue --timeout=60s >/dev/null 2>&1 || true
}
restore_kueue() {
  have_kueue || return 0
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration "$KUEUE_WH" \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  echo "  tekton-kueue 웹훅 원복"
}

restore_controller() {
  echo "[후처리] 큐 컨트롤러 복구..."
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  kubectl wait --for=condition=ready pod -l app=tekton-queue \
    -n tekton-pipelines --timeout=180s >/dev/null 2>&1 || true
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
cleanup_all() { restore_controller; restore_kueue; }
trap cleanup_all EXIT

disable_kueue

echo "[1/5] 큐 컨트롤러 비활성화..."
# 웹훅 failurePolicy → Ignore (webhook 없어도 PR 생성 통과)
kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
  --type=json \
  -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]'
# 컨트롤러 Pod 중단
kubectl scale deployment tekton-queue-controller \
  -n tekton-pipelines --replicas=0
kubectl wait --for=delete pod \
  -l app=tekton-queue \
  -n tekton-pipelines --timeout=60s 2>/dev/null || true
echo "  컨트롤러 중단 완료"

echo "[2/5] 90개 burst 생성 (generateName, 0.33초 간격)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count 90 --interval 0.33 --env dev --generate-name \
  --pipeline petclinic-build
# ※ --pipeline 명시: A0-NR 은 requests 무설정 조건이므로 EXP_PIPELINE(캠페인 기본=requests 파이프라인)의
#    영향을 받으면 안 된다. 기본 파이프라인 이름을 고정한다.

echo "[3/5] 완료 대기 (최대 40분)..."
TIMEOUT=2400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  실행 중: ${PENDING}개 (${ELAPSED}s 경과)"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[4/5] 지표 수집 (자원 고갈 포함)..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_resource.json"

# [5/5] 컨트롤러 복구는 trap(restore_controller)이 담당한다. 여기서 중복 호출하지 않는다.

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
