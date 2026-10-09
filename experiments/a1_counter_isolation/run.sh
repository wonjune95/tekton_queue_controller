#!/bin/bash
# A1 — admitted 카운터 격리 (disableAdmittedCounter=true)
# L_max=30 유지 + admitted 카운터만 비활성 → watch 지연 창의 순간 상한 초과(30+delta) 측정.
# 상한(L_max=30)은 유지하고 카운터의 기여만 격리한다. 짝 조건 V(named)와 대조.
# ※ generateName 부하 필수(named PR 은 phantom 이 슬롯을 즉시 반영해 초과 미발생).
# ※ 파괴적 조건: 비파괴 조건 완료 후 최후에 수행.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
NAMESPACE="default-cicd"
OUTDIR="../results/a1_counter_isolation"
mkdir -p "$OUTDIR"

echo "=== A1' Disable Admitted Counter | run=$RUN ==="
echo "  L_max=30 유지, disableAdmittedCounter=true, generateName 부하"

# ⚠️ 원복은 반드시 trap 으로 건다.
#   말미에서만 원복하면 중단·오류(웹훅 스톨 등)로 죽었을 때 disableAdmittedCounter=true 가 남아
#   **이후 전 회차에서 순간 상한 초과 억제가 사라진다**(조용한 오염).
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
cleanup_all() { restore_counter; restore_kueue; }
trap cleanup_all EXIT

disable_kueue

echo "[0/4] admitted 카운터 비활성화 (disableAdmittedCounter=true)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"disableAdmittedCounter":true}}' 2>/dev/null || echo "[경고] GlobalLimit 패치 실패"

echo "[1/4] 90개 burst 생성 (generateName, 0.33초 간격)..."
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

echo "[후처리] admitted 카운터 원복 (disableAdmittedCounter=false)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"disableAdmittedCounter":false}}' 2>/dev/null

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
