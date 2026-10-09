#!/usr/bin/env bash
# GKE 실험 자원 비용 절감·정리 스크립트
#
#   bash scripts/gke_teardown.sh --scale-zero    # 노드 0 + 배스천 정지 (자원 유지, 과금 최소화) ★평소 사용
#   bash scripts/gke_teardown.sh --scale-up      # 실험 재개 (노드 복구 + 배스천 기동)
#   bash scripts/gke_teardown.sh --delete-all    # 클러스터·배스천 완전 삭제 (캠페인 종료 후)
#
# 비용은 "실험 실행 시간"이 아니라 "클러스터가 켜져 있는 총 시간"이 결정한다
# (gke_cost_estimate.md §5). 실행 사이에 --scale-zero 를 습관화할 것.
#
# ⚠️ --delete-all 은 되돌릴 수 없다. 실험 결과 CSV(results/)와 Prometheus 데이터를
#    로컬로 회수했는지 반드시 먼저 확인한다. 원본 데이터 삭제 금지(프로젝트 안전 규칙).

set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER="${CLUSTER:-tekton-cluster}"
BASTION_NAME="${BASTION_NAME:-bastion}"
SYSTEM_NODES=2
BUILD_NODES=3

ACTION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scale-zero) ACTION="scale-zero"; shift ;;
    --scale-up)   ACTION="scale-up";   shift ;;
    --delete-all) ACTION="delete-all"; shift ;;
    --zone)       ZONE="$2"; shift 2 ;;
    *) echo "알 수 없는 인자: $1"; exit 1 ;;
  esac
done

[[ -n "$ACTION" ]] || {
  echo "사용법: bash scripts/gke_teardown.sh [--scale-zero|--scale-up|--delete-all]"; exit 1; }

echo "프로젝트=$PROJECT 존=$ZONE 클러스터=$CLUSTER"

# 노드풀이 실제로 존재할 때만 resize 한다.
# (프로비저닝이 중간에 실패해 build-pool 이 없는 상태에서도 scale-zero 가 동작해야 한다 —
#  쿼터 부족으로 build-pool 생성이 실패한 이력이 있다.)
resize_pool() {   # resize_pool <pool> <count>
  local pool="$1" count="$2"
  if gcloud container node-pools describe "$pool" --cluster "$CLUSTER" \
       --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud container clusters resize "$CLUSTER" --node-pool="$pool" --num-nodes="$count" \
      --zone "$ZONE" --project "$PROJECT" --quiet
  else
    echo "    (노드풀 '$pool' 없음 — 건너뜀)"
  fi
}

case "$ACTION" in
  scale-zero)
    echo ">>> 노드풀 0 으로 축소 (클러스터·디스크 설정은 유지)"
    resize_pool system-pool 0
    resize_pool default-pool 0
    resize_pool build-pool 0
    echo ">>> 배스천 VM 정지"
    gcloud compute instances stop "$BASTION_NAME" --zone "$ZONE" --project "$PROJECT" --quiet 2>/dev/null || true
    echo "완료. 컴퓨트 과금이 멈춘다(부팅 디스크·PVC 스토리지 요금은 소액 유지)."
    ;;

  scale-up)
    echo ">>> 노드풀 복구 (system=${SYSTEM_NODES}, build=${BUILD_NODES})"
    resize_pool system-pool "$SYSTEM_NODES"
    resize_pool default-pool "$SYSTEM_NODES"
    resize_pool build-pool "$BUILD_NODES"
    echo ">>> 배스천 VM 기동"
    gcloud compute instances start "$BASTION_NAME" --zone "$ZONE" --project "$PROJECT" --quiet 2>/dev/null || true
    echo ">>> 노드 준비 대기"
    kubectl wait --for=condition=Ready nodes --all --timeout=300s 2>/dev/null || true

    # ── infra-role 라벨 복원 ────────────────────────────────────────
    # ⚠️ resize 0→N 은 노드 VM 을 **새로 만든다**. node-role 은 노드풀 설정이라 살아남지만
    #   infra-role 은 프로비저닝 때 kubectl 로 붙인 라벨이라 사라진다.
    #   그러면 Harbor·Gitea(registry)·Tekton·큐 컨트롤러(control) 가 nodeSelector 를 만족하는
    #   노드를 못 찾아 **전부 Pending** 이 된다(2026-07-30 실측: 인프라 파드 18개 Pending).
    echo ">>> infra-role 라벨 복원"
    mapfile -t SYS_NODES < <(kubectl get nodes -l node-role=system \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | sort)
    if [[ ${#SYS_NODES[@]} -ge 2 ]]; then
      kubectl label node "${SYS_NODES[0]}" infra-role=registry --overwrite
      kubectl label node "${SYS_NODES[1]}" infra-role=control  --overwrite
    else
      echo "    [경고] node-role=system 노드가 2대 미만이다. infra-role 을 붙이지 못했다."
      echo "           이 상태로 실험하면 인프라 파드가 Pending 이다. 수동 확인 필요."
    fi

    kubectl get nodes -L node-role,infra-role
    echo "완료. 실험 재개 전 'bash common/cleanup.sh default-cicd' 로 상태를 초기화할 것."
    ;;

  delete-all)
    cat <<EOF

⚠️  경고: 클러스터 '${CLUSTER}' 와 배스천 '${BASTION_NAME}' 를 **완전 삭제**한다.
    되돌릴 수 없다. 삭제 전 확인:
      · results/ 의 실험 CSV 를 로컬로 회수했는가?
      · Prometheus 메트릭 덤프가 필요하면 받아두었는가?
      · 캠페인이 정말 끝났는가? (재실행 필요 시 --scale-zero 를 쓸 것)
EOF
    read -r -p "완전 삭제하려면 'delete' 를 입력: " CONFIRM
    [[ "$CONFIRM" == "delete" ]] || { echo "취소됨."; exit 1; }
    gcloud container clusters delete "$CLUSTER" --zone "$ZONE" --project "$PROJECT" --quiet
    gcloud compute instances delete "$BASTION_NAME" --zone "$ZONE" --project "$PROJECT" --quiet 2>/dev/null || true
    echo "삭제 완료. Artifact Registry 저장소는 남아 있다(이미지 보관용, 소액)."
    echo "저장소도 지우려면: gcloud artifacts repositories delete tekton-queue --location=${ZONE%-*}"
    ;;
esac
