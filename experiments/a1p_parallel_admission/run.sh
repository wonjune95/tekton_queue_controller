#!/bin/bash
# A1′ — 동시 인가 요청 하에서의 admitted 카운터 효과
#
#   사용법: bash run.sh {off|on} <run>
#     off = disableAdmittedCounter=true  (카운터 격리 — 초과 관측 대상)
#     on  = disableAdmittedCounter=false (대조군 — 같은 부하에서 상한이 지켜지는지)
#
# ── 왜 A1 과 별도 조건인가 (2026-08-03) ─────────────────────────────
# 기존 A1(90건 / 0.33초 간격 = 초당 3건)은 3회 모두 상한 초과가 없었다. 원인은 컨트롤러가 아니라
# **부하 발생기의 구조**였다: 생성이 웹훅을 동기 호출하고 인가 판정이 그 웹훅 안에서 일어나므로,
# **직렬 생성에서는 다음 요청 전에 이전 인가가 이미 끝나 있어 «인포머 지연 창»이 존재할 수 없다.**
#   실측: --interval 0(직렬 최대, 건당 244ms · 초당 4.1건) 에서도 최대 동시 실행 30 = 초과 없음.
#   병렬 12 로 바꾸자 **최대 37(+7)** 이 166초간 지속됐다.
# → 도착 간격이 아니라 **동시성**이 이 현상의 조건이다. 그래서 부하 형태가 다른 별도 조건으로 둔다.
#   기존 A1 결과(N=3, 초과 없음)는 유효하며 그대로 보고한다.
#
# ⚠️ 초과는 «순간»이 아니다. 초과 인가된 실행이 슬롯을 점유하는 동안 상한 위반이 **지속**된다.
#
# ※ generateName 부하 필수(named PR 은 phantom 이 슬롯을 즉시 반영해 초과 미발생 — 짝 조건 V 참조).

set -e
cd "$(cd "$(dirname "$0")" && pwd)"
MODE=${1:-"off"}
RUN=${2:-"1"}
NAMESPACE="default-cicd"
PARALLEL="${A1P_PARALLEL:-12}"
OUTDIR="../results/a1p_parallel_admission"
mkdir -p "$OUTDIR"

case "$MODE" in
  off) WANT="true";  LABEL="카운터 격리(OFF)" ;;
  on)  WANT="false"; LABEL="대조군(ON)" ;;
  *)   echo "[중단] MODE 는 off 또는 on"; exit 1 ;;
esac

echo "=== A1' 동시 인가 | mode=$MODE ($LABEL) | parallel=$PARALLEL | run=$RUN ==="

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
# ⚠️ 원복은 trap 으로. 중단·오류로 죽었을 때 카운터가 꺼진 채 남으면 이후 전 회차가 오염된다.
restore_counter() {
  kubectl patch globallimit tekton-queue-limit --type merge \
    -p '{"spec":{"disableAdmittedCounter":false}}' >/dev/null 2>&1 || true
  local now
  now=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.disableAdmittedCounter}' 2>/dev/null)
  [ "$now" = "true" ] \
    && echo "  [경고] admitted 카운터 원복 실패 — 다음 회차 전에 수동 확인할 것!" \
    || echo "  admitted 카운터 원복 완료"
}
cleanup_all() { restore_counter; restore_kueue; }
trap cleanup_all EXIT

disable_kueue

echo "[0/4] admitted 카운터 설정 (disableAdmittedCounter=$WANT)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p "{\"spec\":{\"disableAdmittedCounter\":$WANT}}" >/dev/null 2>&1 || true
# 적용 결과를 되읽어 확인한다. 반영되지 않으면 조건이 뒤바뀐 채 기록된다(허위 결과).
NOW=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.disableAdmittedCounter}' 2>/dev/null)
[ -z "$NOW" ] && NOW="false"
[ "$NOW" = "$WANT" ] || { echo "[중단] 카운터 설정 미반영 (현재 '$NOW', 기대 '$WANT')."; exit 1; }
echo "  확인: disableAdmittedCounter=$NOW"
# 매니저 루프가 CRD 를 다시 읽을 시간(루프 주기 ~5초).
sleep 15

echo "[1/4] 90개 burst 생성 (generateName, 병렬 $PARALLEL = 동시 인가 요청)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count 90 --interval 0 --parallel "$PARALLEL" --env dev --generate-name

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
  --output "$OUTDIR/${MODE}_run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/${MODE}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/${MODE}_run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/${MODE}_run${RUN}.csv ==="
