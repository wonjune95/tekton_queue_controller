#!/bin/bash
# 실험 전 네임스페이스 초기화
# 이전 실험의 PipelineRun, PVC를 삭제하고 큐 상태를 초기화한다.

NAMESPACE="${1:-default-cicd}"

# ── 빌드 노드 목록: 라벨로 동적 조회 ──────────────────────────────
# ⚠️ 예전에는 노드 이름이 하드코딩("gke-...-default-pool-62e9947e" + "m396 m844 pxae")되어 있었다.
#   GKE 클러스터를 새로 만들면 이름이 달라지므로 prune 파드가 **존재하지 않는 노드**에 배정되어
#   영원히 Pending 이 되고, 이미지 정리가 **한 번도 실행되지 않는다**.
#   그 상태로 S2(90건 버스트)를 돌린 결과 빌드 노드 디스크가 90~94% 까지 차올라
#   DiskPressure 로 파드가 축출됐다(2026-07-29). 로그에는 "백그라운드 시작"만 찍혀 정상처럼 보였다.
BUILD_NODE_NAMES=$(kubectl get nodes -l node-role=build \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
if [ -z "$BUILD_NODE_NAMES" ]; then
  echo "[경고] node-role=build 라벨을 가진 노드를 찾지 못했습니다. 이미지 prune 을 건너뜁니다."
fi

echo "=== 네임스페이스 초기화: $NAMESPACE ==="

# PipelineRun 전체 삭제
COUNT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l)
if [ "$COUNT" -gt 0 ]; then
  echo "[1/3] PipelineRun ${COUNT}개 삭제..."
  kubectl delete pipelinerun --all -n "$NAMESPACE" --wait=false
fi

# PVC 삭제 (volumeClaimTemplate으로 생성된 것들)
PVC_COUNT=$(kubectl get pvc -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l)
if [ "$PVC_COUNT" -gt 0 ]; then
  echo "[2/3] PVC ${PVC_COUNT}개 삭제..."
  kubectl delete pvc --all -n "$NAMESPACE" --wait=false
fi

# 큐 컨트롤러 admitted count 리셋
echo "[3/3] 큐 컨트롤러 상태 리셋..."
kubectl patch configmap tekton-queue-admitted-count \
  -n tekton-pipelines \
  --type merge \
  -p '{"data":{"admitted_count":"0"}}' 2>/dev/null || true

# 빌드 노드 이미지 prune (백그라운드 실행 - 대기 없음)
for NODE in $BUILD_NODE_NAMES; do
  NODE_SUFFIX="${NODE##*-}"
  kubectl delete pod "prune-$NODE_SUFFIX" -n kube-system --ignore-not-found=true --wait=false 2>/dev/null
  kubectl run "prune-$NODE_SUFFIX" -n kube-system \
    --image=alpine --restart=Never \
    --overrides="{
      \"spec\": {
        \"nodeName\": \"${NODE}\",
        \"hostPID\": true,
        \"tolerations\": [{\"operator\": \"Exists\"}],
        \"containers\": [{
          \"name\": \"prune\",
          \"image\": \"alpine\",
          \"command\": [\"nsenter\", \"-t\", \"1\", \"-m\", \"--\", \"crictl\", \"rmi\", \"--prune\"],
          \"securityContext\": {\"privileged\": true}
        }]
      }
    }" 2>/dev/null && echo "  prune-$NODE_SUFFIX 시작" || true
done

# prune 완료까지 대기.
# ⚠️ 예전에는 prune 을 백그라운드로 띄우고 10초 뒤 바로 측정을 시작했다.
#   이미지 삭제 디스크 I/O 가 **측정 창 앞부분과 겹쳐** 초기 파이프라인런의 소요에 섞인다.
#   측정 전에 끝내면 조건 간 비교가 깨끗해진다(모든 시나리오에 동일 적용).
if [ -n "$BUILD_NODE_NAMES" ]; then
  echo "[4/4] 이미지 prune 완료 대기 (최대 300초)..."
  for NODE in $BUILD_NODE_NAMES; do
    NODE_SUFFIX="${NODE##*-}"
    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded \
      "pod/prune-$NODE_SUFFIX" -n kube-system --timeout=300s >/dev/null 2>&1 \
      || echo "  [경고] prune-$NODE_SUFFIX 가 300초 내에 끝나지 않았다(계속 진행)."
  done
  echo "  prune 완료"
fi

echo "=== 초기화 완료 ==="
