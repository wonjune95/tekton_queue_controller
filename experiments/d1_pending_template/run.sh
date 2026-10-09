#!/bin/bash
# D1 — 보류 필드(spec.status: PipelineRunPending) 이중 용도 상충의 재현
#
#   사용법: bash run.sh <k|p|v> <run>
#     k = tekton-kueue   p = 제안 컨트롤러   v = Volcano(파드 계층)
#
# ── 무엇을 보는가 ────────────────────────────────────────────────
# Tekton 의 spec.status: PipelineRunPending 에는 두 용도가 겹친다.
#   (1) 실행하지 않는 «원본 템플릿» 을 상주시킨다  — CI 구성 관행
#   (2) «대기 중» 임을 표시한다                    — 파이프라인런 계층의 큐잉
# 인가 계층이 둘을 구분하지 못하면 상주 템플릿이 인가되어 실행된다.
#
#   판정 ① 상주 템플릿이 실행됐는가      (주 판정)
#        ② 새로 만든 10건이 Pending 에 머물지 않고 바로 실행됐는가 (생존 확인)
#   ②가 성립할 때만 ①을 해석한다 — 새 것도 안 도는 회차는 상충의 증거가 아니라 구성 실패다.
#
# ── 템플릿을 셋 두는 이유 ────────────────────────────────────────
#   pre       : 인가 계층을 **켜기 전에** 만든다. 실무의 「큐잉 도입 전부터 있던 템플릿」.
#               웹훅을 거치지 않으므로 큐잉이 붙인 라벨이 없다.
#               → 컨트롤러가 «라벨 없는 보류 오브젝트» 까지 잡는지를 본다.
#   other-sa  : 계층 가동 중, **실행본과 다른 계정**(배스천 kubectl). ← **실무 조건·주 판정**
#               실무에서 템플릿은 사람·배포 도구가 만들고 실행본은 트리거가 만든다.
#   same-sa   : 계층 가동 중, **실행본과 같은 계정**. ← **구분 축의 경계·부가 관측**
#               제안 컨트롤러의 구분 축은 요청 출처(SA)이므로 계정이 같으면 원리상 구분할 수 없다.
#               제외 설정을 넣으면 실행본까지 함께 빠지므로 «설정으로 해결» 되지 않는다.
#               이 칸은 주 판정에서 뺀다 — 실무 결과인 것처럼 나란히 놓으면 왜곡이 된다.
#   K·V 에서는 other-sa/same-sa 결과가 같아야 한다(그 시스템들은 출처를 보지 않는다).
#   P 에서만 갈려야 한다. 갈리지 않으면 논문 6.8절 ①의 서술을 고쳐야 한다.
#
# ⚠️ 파이프라인 완료를 기다리지 않는다. 판정은 startTime 으로 확정된다.
# ⚠️ 종료 시 모든 인가 계층을 원복한다(trap). 원복 실패 시 다음 회차가 오염된다.
# ※ 비파괴.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

COND="${1:-}"
RUN="${2:-1}"
NAMESPACE="${EXP_NS:-default-cicd}"
CTRL_NS="tekton-pipelines"
WATCH_SEC="${D1_WATCH_SEC:-180}"
LOAD_COUNT="${D1_LOAD_COUNT:-10}"
MANAGED_SA="${IMPERSONATE_SA:-system:serviceaccount:tekton-pipelines:tekton-dashboard}"
OUTDIR="../results/d1_pending_template"

case "$COND" in
  k|p|v) ;;
  *) echo "사용법: bash run.sh <k|p|v> <run>   (k=tekton-kueue p=제안 v=Volcano)"; exit 1 ;;
esac
mkdir -p "$OUTDIR"

TAG="${COND}_run${RUN}"
T_PRE="d1-pre-${COND}-${RUN}"
T_OTH="d1-other-sa-${COND}-${RUN}"
T_SAME="d1-same-sa-${COND}-${RUN}"
CSV="$OUTDIR/${TAG}_poll.csv"
JSON="$OUTDIR/${TAG}_state.json"

echo "=== D1 보류 템플릿 상충 | 조건=$COND | run=$RUN ==="
echo "컨텍스트: $(kubectl config current-context)"
echo "템플릿: $T_PRE / $T_OTH / $T_SAME"
echo "부하: ${LOAD_COUNT}건 · 관측 ${WATCH_SEC}초"

# ── 공통 선행 확인 (중단형) ──────────────────────────────────────
# ⚠️ 이 스크립트는 인가 계층을 껐다 켠다. 전제가 어긋난 채로 돌면
#   「템플릿이 실행되지 않았다」가 **구성 실패인지 회피인지 구분되지 않는다.**
require() {   # require <설명> <실제> <기대>
  if [ "$2" = "$3" ]; then
    echo "  OK   $1 ($2)"
  else
    echo "  [중단] $1: '$2' (기대 '$3')"
    exit 1
  fi
}

echo "[사전] 공통 전제 확인..."
LMAX=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}' 2>/dev/null || echo "")
require "L_max" "$LMAX" "30"
# 부하 10건 < L_max 30 이어야 대기가 생기지 않는다. 대기가 생기면 큐 길이가 변수로 끼어든다.
[ "$LOAD_COUNT" -lt "$LMAX" ] || { echo "  [중단] 부하($LOAD_COUNT)가 L_max($LMAX) 이상이면 대기가 생긴다."; exit 1; }
echo "  OK   부하 $LOAD_COUNT < L_max $LMAX (대기 없음)"
kubectl get pipeline petclinic-build -n "$NAMESPACE" >/dev/null 2>&1 \
  || { echo "  [중단] 파이프라인 petclinic-build 가 없습니다."; exit 1; }
echo "  OK   파이프라인 petclinic-build 존재"
kubectl auth can-i create pipelinerun -n "$NAMESPACE" --as="$MANAGED_SA" >/dev/null 2>&1 \
  || { echo "  [중단] 임퍼소네이트($MANAGED_SA) 로 파이프라인런을 만들 수 없습니다. loadgen-rbac.yaml 확인."; exit 1; }
echo "  OK   임퍼소네이트 권한"

# ── 조건별 선행 확인 ─────────────────────────────────────────────
case "$COND" in
  k)
    echo "[사전] tekton-kueue 가동 확인..."
    require "웹훅 설정 존재" \
      "$(kubectl get mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration -o name 2>/dev/null | wc -l | tr -d ' ')" "1"
    require "Kueue 컨트롤러 Ready" \
      "$(kubectl get deploy kueue-controller-manager -n kueue-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" "1"
    kubectl get clusterqueue -o name >/dev/null 2>&1 \
      || { echo "  [중단] ClusterQueue 가 없습니다. kueue-resources.yaml 적용 필요."; exit 1; }
    echo "  OK   ClusterQueue 존재"
    kubectl get cm kueue-manager-config -n kueue-system -o yaml 2>/dev/null | grep -q "pipelineruns.tekton.dev" \
      || { echo "  [중단] externalFrameworks 에 pipelineruns.tekton.dev 가 없습니다."; exit 1; }
    echo "  OK   externalFrameworks 활성"
    ;;
  p)
    echo "[사전] 제안 컨트롤러 가동 확인..."
    require "웹훅 설정 존재" \
      "$(kubectl get mutatingwebhookconfiguration tekton-queue-mutator -o name 2>/dev/null | wc -l | tr -d ' ')" "1"
    ;;
  v)
    echo "[사전] Volcano 가동 확인..."
    require "volcano-scheduler Ready" \
      "$(kubectl get deploy volcano-scheduler -n volcano-system -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" "1"
    # ⚠️ 정규 이름을 쓴다. Kueue 의 LocalQueue 도 단축명 `queue` 를 등록한다.
    kubectl get queues.scheduling.volcano.sh default >/dev/null 2>&1 \
      || { echo "  [중단] Volcano queue 'default' 가 없습니다."; exit 1; }
    echo "  OK   Volcano queue default 존재"
    ;;
esac

# ── 계층 제어 ────────────────────────────────────────────────────
mine_off() {
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' >/dev/null 2>&1 || true
  kubectl scale deployment tekton-queue-controller -n "$CTRL_NS" --replicas=0 >/dev/null 2>&1 || true
  kubectl wait --for=delete pod -l app=tekton-queue -n "$CTRL_NS" --timeout=60s >/dev/null 2>&1 || true
}
mine_on() {
  kubectl scale deployment tekton-queue-controller -n "$CTRL_NS" --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  kubectl wait --for=condition=ready pod -l app=tekton-queue -n "$CTRL_NS" --timeout=120s >/dev/null 2>&1 || true
}
kueue_off() {
  kubectl patch mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=0 >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-controller-manager -n tekton-kueue --replicas=0 >/dev/null 2>&1 || true
}
kueue_on() {
  kubectl scale deployment tekton-kueue-webhook -n tekton-kueue --replicas=1 >/dev/null 2>&1 || true
  kubectl scale deployment tekton-kueue-controller-manager -n tekton-kueue --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration \
    --type=json -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  kubectl -n tekton-kueue rollout status deploy/tekton-kueue-webhook --timeout=120s >/dev/null 2>&1 || true
}
vc_cap_on()  { kubectl patch queues.scheduling.volcano.sh default --type=merge \
                 -p '{"spec":{"capability":{"cpu":"3"}}}' >/dev/null 2>&1 || true; }
vc_cap_off() { kubectl patch queues.scheduling.volcano.sh default --type=merge \
                 -p '{"spec":{"capability":null}}' >/dev/null 2>&1 || true; }

restore() {
  echo ""
  echo "[후처리] 인가 계층 원복..."
  mine_on
  kueue_on
  vc_cap_off
  local r
  r=$(kubectl get deploy tekton-queue-controller -n "$CTRL_NS" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  [ "$r" = "1" ] && echo "  제안 컨트롤러 원복(1)" \
                 || echo "  [경고] 제안 컨트롤러 원복 실패(현재 $r) — 다음 회차 전에 수동 확인!"
}
trap restore EXIT

# ── [1/7] 네임스페이스 초기화 ────────────────────────────────────
echo ""
echo "[1/7] 네임스페이스 초기화..."
bash ../common/cleanup.sh "$NAMESPACE" >/dev/null 2>&1 || true
# 지난 회차의 템플릿이 남아 있으면 이름 충돌로 apply 가 «변경 없음» 이 되어 조용히 오염된다.
kubectl delete pipelinerun -n "$NAMESPACE" -l d1-role=template --ignore-not-found >/dev/null 2>&1 || true
sleep 10
# ⚠️ 잔여 파이프라인런은 **부하로 집계된다**(관측 스크립트는 d1-role 라벨이 없는 것을 부하로 센다).
#   남은 채로 진행하면 ② 생존 확인이 거짓 통과할 수 있다 — 정리 실패를 삼키지 않는다.
LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "${LEFT:-0}" -gt 0 ]; then
  echo "  정리 대기 중 (잔여 ${LEFT}건)..."
  for _ in 1 2 3 4 5 6; do
    sleep 10
    LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
    [ "${LEFT:-0}" -eq 0 ] && break
  done
fi
[ "${LEFT:-0}" -eq 0 ] || { echo "  [중단] 파이프라인런 ${LEFT}건이 남아 있다. 부하 집계가 오염된다."; exit 1; }
echo "  OK   네임스페이스 비어 있음"

# ── [2/7] 인가 계층 전부 끄기 ────────────────────────────────────
# pre 템플릿은 «큐잉 도입 전부터 있던 것» 이어야 하므로 어떤 웹훅도 거치면 안 된다.
echo "[2/7] 인가 계층 전부 비활성 (pre 템플릿을 웹훅 없이 만들기 위해)..."
mine_off
kueue_off
vc_cap_off
sleep 5

# ── [3/7] pre 템플릿 생성 ────────────────────────────────────────
mk_template() {   # mk_template <name> [as_sa]
  local f
  f=$(mktemp)
  sed "s/__NAME__/$1/g" pending-template.yaml > "$f"
  if [ -n "${2:-}" ]; then
    kubectl apply -f "$f" --as="$2"
  else
    kubectl apply -f "$f"
  fi
  rm -f "$f"
}
echo "[3/7] pre 템플릿 생성 (웹훅 미경유): $T_PRE"
mk_template "$T_PRE"
PRE_STATUS=$(kubectl get pipelinerun "$T_PRE" -n "$NAMESPACE" -o jsonpath='{.spec.status}' 2>/dev/null)
require "pre 템플릿이 보류 상태" "$PRE_STATUS" "PipelineRunPending"

# ── [4/7] 대상 인가 계층만 켜기 ──────────────────────────────────
echo "[4/7] 대상 계층 활성: $COND"
case "$COND" in
  k) kueue_on ;;
  p) mine_on ;;
  v) vc_cap_on
     CAP=$(kubectl get queues.scheduling.volcano.sh default -o jsonpath='{.spec.capability.cpu}' 2>/dev/null)
     require "Volcano capability 반영" "$CAP" "3" ;;
esac
sleep 10

# ── [5/7] post 템플릿 2건 생성 (계층 가동 중) ────────────────────
echo "[5/7] 계층 가동 중 템플릿 생성"
# 실무 조건 — 템플릿은 사람·배포 도구가 kubectl 로 만들고 실행본은 트리거가 만든다(다른 계정).
echo "  · 실행본과 다른 계정(kubectl):     $T_OTH   ← 실무 조건·주 판정"
mk_template "$T_OTH"
# 경계 확인 — 실행본과 같은 계정으로 만든 경우. SA 를 축으로 삼은 설계는 원리상 구분할 수 없다.
# 제외 설정을 넣으면 실행본까지 함께 빠지므로 «설정으로 해결» 되지 않는다. 주 판정에서는 뺀다.
echo "  · 실행본과 같은 계정(임퍼소네이트): $T_SAME  ← 구분 축의 경계·부가 관측"
mk_template "$T_SAME" "$MANAGED_SA"

# ── [6/7] 새 파이프라인런 생성 ───────────────────────────────────
echo "[6/7] 새 파이프라인런 ${LOAD_COUNT}건 생성..."
PC="python3 ../common/pr_create.py --namespace $NAMESPACE --mode burst --count $LOAD_COUNT --generate-name"
if [ "$COND" = "v" ]; then
  kubectl apply -f ../pipeline/petclinic-build-volcano.yaml -n "$NAMESPACE" >/dev/null
  $PC --pipeline petclinic-build-volcano --scheduler-name volcano
else
  $PC
fi

# ── [7/7] 관측 ───────────────────────────────────────────────────
echo "[7/7] ${WATCH_SEC}초 관측 (5초 주기)..."
python3 watch_templates.py \
  --namespace "$NAMESPACE" \
  --templates "$T_PRE,$T_OTH,$T_SAME" \
  --duration "$WATCH_SEC" \
  --interval 5 \
  --out "$CSV" \
  --state "$JSON" \
  --condition "$COND" \
  --run "$RUN"

echo ""
echo "=== 회차 종료 — 판정 ==="
python3 judge.py --state "$JSON" || true
echo ""
echo "원자료: $CSV"
echo "        $JSON"
