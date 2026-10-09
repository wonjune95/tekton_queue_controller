#!/bin/bash
# T_a 민감도 스윕 — S1(피크) 부하를 에이징 주기 T_a 를 바꿔가며 인가한다.
#
# 사용: bash run.sh <T_a초> <run>      예) bash run.sh 150 1
#
# ── 왜 필요한가 (2026-08-03) ────────────────────────────────────────
# 캠페인 결과 에이징의 «거래»가 T_a=300 한 점에서만 평가돼 있다. 그 한 점에서는:
#   · Tier3 중앙 대기 1210 → 844초 (30.3% 감소, p=0.008 유의)      ← 얻는 것
#   · Tier1 **최대** 대기 214 → 2181초 (10배 증가, p=0.008 유의)   ← 잃는 것
#   · Tier3 **최대** 대기 감소는 7.4% 로 **유의하지 않다**(p=0.238) ← 논문 H3 가 쓴 지표
# 한 점만 있으면 "그 거래가 남는 장사냐"에 답할 수 없고, 에이징이 나쁜 설계로 보인다.
# {150, 300, 600} 곡선이 있어야 **T_a 가 교환 비율을 조절하는 정책 손잡이**라고 쓸 수 있다.
#
# 부하·시드를 S1 과 동일하게 맞춘다 → A2(에이징 off) / 150 / 300(=S1) / 600 의 4점 비교가 성립한다.
#
# ⚠️ GlobalLimit 을 패치하므로 **반드시 원복**한다. 실패하면 이후 전 회차가 잘못된 T_a 로 돈다(조용한 오염).
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

TA=${1:?"사용법: bash run.sh <T_a초> <run>"}
RUN=${2:-"1"}
NAMESPACE="default-cicd"
DEFAULT_TA=300
OUTDIR="../results/ta_sweep"
mkdir -p "$OUTDIR"

case "$TA" in
  ''|*[!0-9]*) echo "[중단] T_a 는 정수여야 합니다: '$TA'"; exit 1 ;;
esac

restore_ta() {
  kubectl patch globallimit tekton-queue-limit \
    --type merge -p "{\"spec\":{\"agingIntervalSec\":${DEFAULT_TA}}}" >/dev/null 2>&1 || true
  local now
  now=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.agingIntervalSec}' 2>/dev/null)
  if [ "$now" = "$DEFAULT_TA" ]; then
    echo "  T_a 원복 완료 ($now)"
  else
    echo "  [경고] T_a 원복 실패 — 현재 '$now'. 다음 회차 전에 수동 확인할 것!"
  fi
}

# ── tekton-kueue 상호 배제 (2026-08-04 추가) ──────────────────────
# ⚠️ tekton-kueue 웹훅은 failurePolicy=Fail 이고 namespaceSelector 가 비어 있어
#   **모든 파이프라인런을 가로챈다.** 파이프라인런마다 Kueue Workload 를 만들고,
#   ClusterQueue 가 인가하지 않은 건은 spec.status=StoppedRunFinally 로 중단시킨다.
#   내 컨트롤러가 대기열에서 푼 건을 tekton-kueue 가 도로 중단시켜 «대량 실패» 로 보인다.
#   (2026-08-04 실측: 실패율 52~62%. S1 은 0.9~7.5%. 3회분 무효 처리.)
#   원래 S1(2026-07-31)에는 tekton-kueue 가 설치돼 있지 않았으므로, 비교하려면 반드시 꺼야 한다.
KUEUE_WH="tekton-kueue-mutating-webhook-configuration"
have_kueue() { kubectl get mutatingwebhookconfiguration "$KUEUE_WH" >/dev/null 2>&1; }
restore_kueue() {
  have_kueue || return 0
  kubectl scale deployment tekton-kueue-controller-manager -n tekton-kueue --replicas=1 >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration "$KUEUE_WH"     --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  echo "  tekton-kueue 원복"
}
disable_kueue() {
  have_kueue || { echo "  (tekton-kueue 미설치 — 건너뜀)"; return 0; }
  echo "  tekton-kueue 비활성 (상호 배제)"
  kubectl patch mutatingwebhookconfiguration "$KUEUE_WH"     --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=0 >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-controller-manager -n tekton-kueue --replicas=0 >/dev/null 2>&1 || true
  sleep 8
}

cleanup_all() { restore_ta; restore_kueue; }
trap cleanup_all EXIT
disable_kueue

echo "=== T_a 스윕 | T_a=${TA}s run=$RUN ==="

LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$LEFT" != "0" ]; then
  echo "[중단] 네임스페이스에 이전 PipelineRun 이 ${LEFT}건 남아 있습니다. cleanup 후 실행하세요."
  exit 1
fi
# L_max 가 기본값이어야 S1 과 비교된다.
LM=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}' 2>/dev/null)
[ "$LM" = "30" ] || { echo "[중단] L_max 가 30 이 아닙니다 (현재 '$LM'). S1 과 비교 불가."; exit 1; }

echo "[0/5] T_a 를 ${TA}초로 설정..."
kubectl patch globallimit tekton-queue-limit \
  --type merge -p "{\"spec\":{\"agingIntervalSec\":${TA}}}"
APPLIED=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.agingIntervalSec}' 2>/dev/null)
[ "$APPLIED" = "$TA" ] || { echo "[중단] T_a 적용 실패 (현재 '$APPLIED')"; exit 1; }
echo "  적용 확인: agingIntervalSec=$APPLIED"
# 컨트롤러 매니저 루프가 CRD 를 다시 읽을 여유(폴링 ~5초)
sleep 15

# ── 부하: S1 과 동일 (저부하 10분 → 피크 10분, 푸아송, 시드도 동일) ──
echo "[1/5] 저부하 구간 10분 (λ=1/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate 1 --duration 10 \
  --arrival poisson --seed "$RUN" --generate-name

echo "[2/5] 피크 구간 10분 (λ=10/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate 10 --duration 10 \
  --arrival poisson --seed "$((RUN + 100))" --generate-name

echo "[3/5] 잔여 파이프라인 완료 대기 (최대 40분)..."
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

echo "[4/5] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/ta${TA}_run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/ta${TA}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/ta${TA}_run${RUN}_resource.json"

echo "[5/5] 완료: $OUTDIR/ta${TA}_run${RUN}.csv"
