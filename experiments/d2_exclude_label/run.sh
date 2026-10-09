#!/bin/bash
# D2 — 제외 라벨 회피의 대가: 라벨은 복제를 견디는가, 요청 출처는 견디는가
#
#   사용법: bash run.sh <k|p|v> <run>
#     k = tekton-kueue (objectSelector 로 제외 라벨 설정)
#     p = 제안 컨트롤러 (제외 라벨과 무관 — 요청 출처로 판정)
#     v = Volcano (파드 계층 — 파이프라인런은 통제하지 않는다)
#
# ⚠️ Volcano 를 파이프라인런 동시 실행 수로만 재면 불공정하다. 파드 계층에서 제한하기
#   때문이다. 그래서 **파드 수도 함께 관측**한다. 「파이프라인런은 전부 시작되지만
#   파드는 Volcano 가 제한한다」가 정확한 서술이며, 그것이 곧 파이프라인런 계층에
#   통제가 없다는 뜻이다(D1 의 «회피가 아니라 무관심»과 같은 결론).
#
# ── 무엇을 보는가 ────────────────────────────────────────────────
# D1 에서 tekton-kueue 가 보류 템플릿을 실행시키는 것을 확인했다.
# 그 회피 방안이 «템플릿에 제외 라벨을 붙이고 웹훅에서 걸러내는 것» 이다(보고서 4.1절).
#
# 그런데 Dashboard rerun 은 원본을 복제하며 **사용자 라벨을 유지한다**
# (src/api/pipelineRuns.js — status·spec.status·시스템 라벨만 제거).
# 그러면 제외 라벨이 실행본에도 따라가 실행본까지 큐에서 빠진다.
#
#   판정 ① 템플릿이 실행되지 않는가        — 회피가 작동하는가
#        ② 복제 실행본이 상한을 지키는가    — 회피의 대가가 있는가  ← 이것이 핵심
#
# 상한 초과를 적은 건수로 드러내기 위해 **쿼터를 5로 낮춘다.**
# 자기완결 판정이므로 값 자체는 무관하다. ⚠️ 기존 캠페인(L_max=30)과 합산하지 않는다.
#
# ※ 비파괴.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

COND="${1:-}"
RUN="${2:-1}"
NAMESPACE="${EXP_NS:-default-cicd}"
CTRL_NS="tekton-pipelines"
QUOTA="${D2_QUOTA:-5}"
CLONES="${D2_CLONES:-12}"
WATCH_SEC="${D2_WATCH_SEC:-240}"
MANAGED_SA="${IMPERSONATE_SA:-system:serviceaccount:tekton-pipelines:tekton-dashboard}"
OUTDIR="../results/d2_exclude_label"

case "$COND" in k|p|v) ;; *) echo "사용법: bash run.sh <k|p|v> <run>"; exit 1 ;; esac
mkdir -p "$OUTDIR"

TAG="${COND}_run${RUN}"
TMPL="d2-tmpl-${COND}-${RUN}"
CSV="$OUTDIR/${TAG}_conc.csv"
JSON="$OUTDIR/${TAG}_state.json"

echo "=== D2 제외 라벨 회피의 대가 | 조건=$COND | run=$RUN ==="
echo "컨텍스트: $(kubectl config current-context)"
echo "쿼터=$QUOTA · 복제본=$CLONES · 관측=${WATCH_SEC}초"

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
# ── 제외 라벨 설정 (회피 방안의 구현) ────────────────────────────
# 웹훅이 d2-exclude=true 인 오브젝트를 건너뛰게 한다. 보고서 4.1 의 「제외 라벨」이 이것이다.
excl_on() {
  kubectl patch mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration --type=json \
    -p='[{"op":"add","path":"/webhooks/0/objectSelector","value":{"matchExpressions":[{"key":"d2-exclude","operator":"NotIn","values":["true"]}]}}]' \
    >/dev/null 2>&1 || true
}
excl_off() {
  kubectl patch mutatingwebhookconfiguration tekton-kueue-mutating-webhook-configuration --type=json \
    -p='[{"op":"remove","path":"/webhooks/0/objectSelector"}]' >/dev/null 2>&1 || true
}
vc_cap_on()  { kubectl patch queues.scheduling.volcano.sh default --type=merge \
                 -p '{"spec":{"capability":{"cpu":"3"}}}' >/dev/null 2>&1 || true; }
vc_cap_off() { kubectl patch queues.scheduling.volcano.sh default --type=merge \
                 -p '{"spec":{"capability":null}}' >/dev/null 2>&1 || true; }

restore() {
  echo ""
  echo "[후처리] 원복..."
  kill ${WATCH_PID:-} 2>/dev/null || true
  excl_off; mine_on; kueue_on; vc_cap_off
  kubectl patch globallimit tekton-queue-limit --type=merge \
    -p '{"spec":{"maxPipelines":30}}' >/dev/null 2>&1 || true
  echo "  제외 라벨 해제 · 계층 원복 · L_max 30 복원"
}
trap restore EXIT

# ── [1/6] 초기화 ─────────────────────────────────────────────────
echo ""
echo "[1/6] 네임스페이스 초기화..."
bash ../common/cleanup.sh "$NAMESPACE" >/dev/null 2>&1 || true
sleep 10
LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
for _ in 1 2 3 4 5 6; do
  [ "${LEFT:-0}" -eq 0 ] && break
  sleep 10; LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
done
[ "${LEFT:-0}" -eq 0 ] || { echo "  [중단] 파이프라인런 ${LEFT}건 잔여 — 동시 실행 집계가 오염된다."; exit 1; }
echo "  OK   네임스페이스 비어 있음"

# ── [2/6] 계층·쿼터 구성 ─────────────────────────────────────────
echo "[2/6] 조건 구성: $COND (쿼터 $QUOTA)"
case "$COND" in
  k)
    mine_off; kueue_on; excl_on
    # ClusterQueue nominalQuota 를 낮춘다
    CQ=$(kubectl get clusterqueue -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    [ -n "$CQ" ] || { echo "  [중단] ClusterQueue 가 없다."; exit 1; }
    kubectl patch clusterqueue "$CQ" --type=json \
      -p="[{\"op\":\"replace\",\"path\":\"/spec/resourceGroups/0/flavors/0/resources/0/nominalQuota\",\"value\":\"$QUOTA\"}]" \
      >/dev/null 2>&1 || echo "  [경고] 쿼터 패치 실패 — 기본값으로 진행"
    echo "  ClusterQueue=$CQ · 제외 라벨(objectSelector) 설정됨"
    ;;
  v)
    mine_off; kueue_off; vc_cap_on
    kubectl get queues.scheduling.volcano.sh default >/dev/null 2>&1 \
      || { echo "  [중단] Volcano queue default 가 없다."; exit 1; }
    CAP=$(kubectl get queues.scheduling.volcano.sh default -o jsonpath='{.spec.capability.cpu}' 2>/dev/null)
    echo "  Volcano queue capability cpu=$CAP (파드 계층 — 파이프라인런은 통제하지 않는다)"
    ;;
  p)
    kueue_off; mine_on
    kubectl patch globallimit tekton-queue-limit --type=merge \
      -p "{\"spec\":{\"maxPipelines\":$QUOTA}}" >/dev/null
    L=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}')
    [ "$L" = "$QUOTA" ] || { echo "  [중단] L_max 반영 실패 (현재 $L)"; exit 1; }
    echo "  L_max=$L (제안 컨트롤러는 제외 라벨을 보지 않는다)"
    ;;
esac
sleep 10

# ── [3/6] 제외 라벨이 붙은 템플릿 상주 ───────────────────────────
echo "[3/6] 제외 라벨 템플릿 생성: $TMPL"
TF="template-excluded.yaml"
[ "$COND" = "v" ] && TF="template-excluded-volcano.yaml"   # schedulerName=volcano 주입판
T=$(mktemp); sed "s/__NAME__/$TMPL/g" "$TF" > "$T"
kubectl apply -f "$T"; rm -f "$T"
sleep 5
TS=$(kubectl get pipelinerun "$TMPL" -n "$NAMESPACE" -o jsonpath='{.spec.status}' 2>/dev/null)
echo "  템플릿 spec.status=$TS"

# ── [4/6] 동시 실행 감시 시작 ────────────────────────────────────
echo "[4/6] 동시 실행 감시 시작 (2초 주기)..."
bash ../common/conc_watch.sh "$NAMESPACE" "$CSV" 2 &
WATCH_PID=$!
sleep 3

# ── [5/6] rerun 복제 ─────────────────────────────────────────────
# 대시보드 rerun 과 같은 변환으로 복제한다. 제외 라벨이 따라가는지가 핵심이다.
echo "[5/6] 템플릿을 ${CLONES}건 복제 (Dashboard rerun 재현)..."
if [ "$COND" = "p" ]; then
  # 제안 컨트롤러는 요청 출처로 판정하므로 rerun 경로와 같은 계정으로 만든다
  python3 clone_rerun.py --namespace "$NAMESPACE" --source "$TMPL" --count "$CLONES" --impersonate "$MANAGED_SA"
else
  python3 clone_rerun.py --namespace "$NAMESPACE" --source "$TMPL" --count "$CLONES"
fi

# ── [6/6] 관측 ───────────────────────────────────────────────────
echo "[6/6] ${WATCH_SEC}초 관측..."
END=$((SECONDS + WATCH_SEC))
while [ $SECONDS -lt $END ]; do
  sleep 30
  RUNNING=$(kubectl get pipelinerun -n "$NAMESPACE" -o json 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for i in d['items'] if i.get('status',{}).get('startTime') and not any(c.get('type')=='Succeeded' and c.get('status') in ('True','False') for c in i.get('status',{}).get('conditions',[]))))" 2>/dev/null || echo "?")
  echo "  실행 중 $RUNNING (쿼터 $QUOTA)"
done
kill ${WATCH_PID:-} 2>/dev/null || true
sleep 2

# ── 결과 집계 ────────────────────────────────────────────────────
python3 - "$CSV" "$JSON" "$COND" "$RUN" "$QUOTA" "$CLONES" "$TMPL" "$NAMESPACE" <<'PYEOF'
import json, subprocess, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass
csv_p, json_p, cond, run, quota, clones, tmpl, ns = sys.argv[1:9]
peak = peak_total = 0
try:
    with open(csv_p, encoding="utf-8") as f:
        rows = [l.strip().split(",") for l in f.readlines()[1:] if l.strip()]
    # CSV 열: ts_epoch,ts_iso,running,pending,total
    # ⚠️ 상한 준수는 **running** 으로 판정한다. total 을 세면 대기 중인 것까지 포함되어
    #    큐가 정상 동작해도 «초과» 로 잘못 나온다(2026-08-22 실측에서 겪음).
    for r in rows:
        if len(r) >= 5:
            try:
                peak = max(peak, int(r[2]))
                peak_total = max(peak_total, int(r[4]))
            except ValueError:
                pass
except OSError:
    pass
# Volcano 는 파드 계층에서 제한하므로 파드 수도 함께 본다(같은 지표로만 재면 불공정하다)
pods = subprocess.run(["kubectl","get","pods","-n",ns,"--no-headers"], capture_output=True)
pod_total = pod_running = 0
if pods.returncode == 0:
    for line in pods.stdout.decode("utf-8","replace").splitlines():
        if not line.strip():
            continue
        pod_total += 1
        if " Running " in line or line.split()[2] == "Running":
            pod_running += 1
g = subprocess.run(["kubectl","get","pipelinerun","-n",ns,"-o","json"], capture_output=True)
started = tmpl_started = 0
if g.returncode == 0:
    for it in json.loads(g.stdout.decode("utf-8","replace")).get("items", []):
        s = bool(it.get("status", {}).get("startTime"))
        if it["metadata"]["name"] == tmpl:
            tmpl_started = int(s)
        elif s:
            started += 1
state = {"condition": cond, "run": run, "quota": int(quota), "clones": int(clones),
         "peak_concurrent": peak, "clones_started": started, "template_started": bool(tmpl_started),
         "exceeded": peak > int(quota), "peak_total_objects": peak_total,
         "pods_total": pod_total, "pods_running": pod_running}
with open(json_p, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
print(f"\n=== 결과 ===")
print(f"  템플릿 실행: {'예' if tmpl_started else '아니오'}")
print(f"  복제본 실행: {started}/{clones}")
print(f"  최대 동시 실행: {peak}  (쿼터 {quota})")
print(f"  상한 초과: {'*** 예 ***' if peak > int(quota) else '아니오'}")
print(f"  파드: 총 {pod_total} · 실행 중 {pod_running}")
if cond == "v":
    print("  ※ Volcano 는 파드 계층에서 제한한다. 파이프라인런 수 초과를 «통제 실패»로 읽지 말 것 —")
    print("     파이프라인런 계층에 통제가 없다는 뜻이며, 그 대가는 논문 5.5절에 실측돼 있다.")
PYEOF

echo ""
echo "원자료: $CSV / $JSON"
