#!/bin/bash
# D1 실험용 비교군 설치 — tekton-kueue + Volcano 를 한 번에.
#
#   사용법: bash setup_comparison.sh
#
# COMPARISON_SETUP.md 의 설치 절차를 D1 실험에 필요한 만큼만 묶은 것이다.
# 캠페인 때와 같은 버전을 쓴다(Kueue v0.16.6 · cert-manager v1.19.2 · Volcano v1.15.0).
#
# ⚠️ 설치만 한다. 활성/비활성 전환은 run.sh 가 회차마다 수행한다.
# ⚠️ 설치 직후에는 세 계층이 모두 살아 있다 — 그대로 부하를 주면 서로 가로채 오염된다.
#    반드시 run.sh 를 통해서만 부하를 준다.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"

echo "=== D1 비교군 설치 ==="
echo "컨텍스트: $(kubectl config current-context)"
echo ""

# ── 1. cert-manager (tekton-kueue 웹훅 인증서) ───────────────────
echo "[1/6] cert-manager v1.19.2..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.19.2/cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s

# ── 2. Kueue ─────────────────────────────────────────────────────
echo "[2/6] Kueue v0.16.6..."
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.6/manifests.yaml
kubectl -n kueue-system rollout status deploy/kueue-controller-manager --timeout=300s

# ── 3. externalFrameworks 활성화 ─────────────────────────────────
# 이것이 없으면 Kueue 가 파이프라인런을 대기 항목으로 잡지 않는다.
# 그 상태로 K 조건을 돌리면 「tekton-kueue 는 템플릿을 건드리지 않았다」는 허위 결과가 된다.
echo "[3/6] externalFrameworks: pipelineruns.tekton.dev 활성화..."
kubectl apply -f https://raw.githubusercontent.com/konflux-ci/tekton-kueue/main/hack/kueue-config.yaml
kubectl rollout restart deployment/kueue-controller-manager -n kueue-system
kubectl -n kueue-system rollout status deploy/kueue-controller-manager --timeout=300s
kubectl get cm kueue-manager-config -n kueue-system -o yaml | grep -q "pipelineruns.tekton.dev" \
  || { echo "[중단] externalFrameworks 반영 실패 — 이대로 돌리면 허위 결과가 된다."; exit 1; }
echo "  OK   externalFrameworks 반영 확인"

# ── 4. tekton-kueue 애드온 ───────────────────────────────────────
echo "[4/6] tekton-kueue 애드온..."
kubectl apply -k "https://github.com/konflux-ci/tekton-kueue//config/default?ref=main" --server-side
# 이미지 접두어 교정(기본 매니페스트의 이미지 경로가 quay.io 가 아니다)
kubectl set image deployment/tekton-kueue-controller-manager \
  manager=quay.io/konflux-ci/tekton-kueue:latest -n tekton-kueue
kubectl set image deployment/tekton-kueue-webhook \
  webhook=quay.io/konflux-ci/tekton-kueue:latest -n tekton-kueue
kubectl -n tekton-kueue rollout status deploy/tekton-kueue-webhook --timeout=300s
kubectl -n tekton-kueue rollout status deploy/tekton-kueue-controller-manager --timeout=300s

# ── 5. 큐 리소스 (nominalQuota=30 = L_max) ───────────────────────
# 쿼터가 L_max 와 같아야 동등 비교가 된다.
echo "[5/6] Kueue 큐 리소스 (nominalQuota=30)..."
kubectl apply -f ../cmp_tekton_kueue/kueue-resources.yaml

# ── 6. Volcano ───────────────────────────────────────────────────
echo "[6/6] Volcano v1.15.0..."
# ⚠️ GKE 전용 선행 조치 (2026-08-22 재구축에서 겪음)
#   Volcano 의 scheduler·controllers·admission 은 priorityClassName: system-cluster-critical 을 쓴다.
#   GKE 는 kube-system 밖에서 critical PriorityClass 를 쓰려면 **그 네임스페이스에 해당 스코프의
#   ResourceQuota** 를 요구한다. 없으면 Deployment 는 만들어지는데 파드가 하나도 생성되지 않고
#   `0/1  0  0` 에서 멈춘다(ReplicaFailure=True/FailedCreate).
#     Error creating: insufficient quota to match these scopes:
#       [{PriorityClass In [system-node-critical system-cluster-critical]}]
#   rollout status 로는 타임아웃으로만 보여 원인이 드러나지 않으므로 **먼저** 넣는다.
kubectl create namespace volcano-system --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f volcano-critical-quota.yaml
kubectl apply -f https://raw.githubusercontent.com/volcano-sh/volcano/v1.15.0/installer/volcano-development.yaml
# 쿼터를 먼저 넣었으므로 정상 경로에서는 그대로 뜬다. 혹시 백오프에 걸렸으면 재시작으로 푼다.
kubectl -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s || {
  echo "  [복구] ReplicaSet 백오프 해제를 위해 재시작..."
  for d in volcano-admission volcano-controllers volcano-scheduler; do
    kubectl -n volcano-system rollout restart deploy/$d >/dev/null 2>&1 || true
  done
  kubectl -n volcano-system rollout status deploy/volcano-scheduler --timeout=300s
}

echo ""
echo "=== 설치 확인 ==="
for x in "kueue-controller-manager:kueue-system" \
         "tekton-kueue-webhook:tekton-kueue" \
         "tekton-kueue-controller-manager:tekton-kueue" \
         "volcano-scheduler:volcano-system"; do
  d="${x%%:*}"; n="${x##*:}"
  r=$(kubectl -n "$n" get deploy "$d" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  printf "  %-34s ready=%s\n" "$d" "${r:-0}"
done
kubectl get clusterqueue -o name 2>/dev/null | head -3
kubectl get queues.scheduling.volcano.sh default -o name 2>/dev/null || echo "  [경고] Volcano queue default 없음"

echo ""
echo "⚠️ 지금은 세 계층이 모두 살아 있다. 부하는 반드시 run.sh 를 통해서만 준다."
echo "   (run.sh 가 회차마다 자기 것만 남기고 나머지를 비활성한다)"
