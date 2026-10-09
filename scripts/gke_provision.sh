#!/usr/bin/env bash
# GKE 실험 자원 생성 스크립트 (kind_setup.sh 의 GKE 대응물)
#
#   실행:  bash scripts/gke_provision.sh [--dry-run] [--zone <zone>]
#
# ⚠️ 이 스크립트는 **과금이 시작되는 자원**(GKE 클러스터·노드풀·배스천 VM·디스크)을 만든다.
#    실행 전 요약을 출력하고 사용자 확인(yes 입력)을 받는다. --dry-run 으로 명령만 확인 가능.
#
# 사전 요구: gcloud 인증 완료, 결제 계정 연결, CPU/디스크 쿼터 확보
#   확인:  gcloud config get-value account / gcloud billing projects describe <PROJECT>
#
# 설계 의도 (실험 재현성 — 변경 시 논문 §3.3.1 과의 정합 확인 필요)
#   · 노드 자동 업그레이드·자동 복구 **끄기**: 캠페인 도중 노드 재생성이 실험을 오염시키는 것을 방지.
#     특히 파괴적 조건(A0-NR)은 노드 NotReady 전이 자체가 관측 대상이므로 자동 복구가 개입하면 안 된다.
#   · 클러스터 오토스케일링 **끄기**: 고정 자원 상한(L_max) 실험이므로 노드가 늘어나면 안 된다.
#   · zonal 단일 클러스터: GKE 관리 수수료 무료 구간 유지(빌링 계정당 zonal 1개).
#   · 외부 IP 는 노드 5 + 배스천 1 = 6개 사용(기본 쿼터 8) → LoadBalancer 대신 port-forward 로 접근할 것.

set -euo pipefail

# ── 설정값 ────────────────────────────────────────────────────────
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-us-central1-a}"          # 논문 §3.3.1 = us-central1. 서울로 바꾸려면 asia-northeast3-a
CLUSTER="${CLUSTER:-tekton-cluster}"
NODE_MACHINE="e2-custom-8-16384"       # 8 vCPU / 16 GB
BASTION_MACHINE="e2-custom-2-4096"     # 2 vCPU / 4 GB
SYSTEM_NODES=2                         # Gitea·Harbor·Tekton 컨트롤러·Prometheus
BUILD_NODES=3                          # PipelineRun 전용 (node-role=build)
DISK_TYPE="pd-balanced"                # system 노드용
NODE_DISK=100                          # GB/system 노드
# build 노드는 **pd-standard 500GB**. 이유 두 가지:
#  1) 용량 — 90건 버스트 한 회에서 완료 파드의 임시 저장소가 쌓여 100GB 노드가 94% 까지 차고
#     DiskPressure 로 파드가 축출됐다(2026-07-29 S2). 실측 최대 88GB → 500GB 면 18%.
#  2) 성능 — GCP PD 는 용량 비례다. pd-balanced 100GB(600 IOPS, 28MB/s) 보다
#     pd-standard 500GB(375/750 IOPS, 60MB/s) 가 처리량·쓰기 IOPS 에서 오히려 낫다.
#     SSD_TOTAL_GB 증설은 거부됐고, HDD 쿼터(DISKS_TOTAL_GB=4096)는 여유가 크다.
BUILD_DISK_TYPE="pd-standard"
BUILD_DISK=500                         # GB/build 노드
# 배스천은 pd-standard(HDD). 노드 5대 x 100GB 로 SSD_TOTAL_GB(500) 를 정확히 소진하므로
# pd-balanced 로 만들면 쿼터 초과로 실패한다(2026-07-29 실측). 배스천은 부하 발생기(API 호출)만
# 돌리므로 디스크 성능이 실험에 영향을 주지 않는다. HDD 쿼터 DISKS_TOTAL_GB=4096 은 여유.
BASTION_DISK_TYPE="pd-standard"
BASTION_DISK=50
REPO="${REPO:-tekton-queue}"           # Artifact Registry 저장소
BASTION_NAME="${BASTION_NAME:-bastion}"
# 노드 이미지 pull·로그 기록용 최소 스코프
NODE_SCOPES="https://www.googleapis.com/auth/devstorage.read_only,https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring"

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --zone)    ZONE="$2"; shift 2 ;;
    *) echo "알 수 없는 인자: $1"; exit 1 ;;
  esac
done
REGION="${ZONE%-*}"

run() {
  echo "  \$ $*"
  [[ $DRY_RUN -eq 1 ]] && return 0
  "$@"
}

# ── 실행 전 요약 + 확인 ───────────────────────────────────────────
TOTAL_VCPU=$(( (SYSTEM_NODES + BUILD_NODES) * 8 + 2 ))
TOTAL_DISK=$(( (SYSTEM_NODES + BUILD_NODES) * NODE_DISK + BASTION_DISK ))
cat <<EOF

══════════════════════════════════════════════════════════
  GKE 실험 자원 생성 — 생성 목록 (과금 시작)
══════════════════════════════════════════════════════════
  프로젝트   : ${PROJECT}
  존/리전    : ${ZONE} (${REGION})
  클러스터   : ${CLUSTER} (zonal)
    · default-pool : ${NODE_MACHINE} x ${SYSTEM_NODES}  (node-role=system, 초기 풀은 이름 지정 불가)
    · build-pool  : ${NODE_MACHINE} x ${BUILD_NODES}  (node-role=build)
  배스천 VM  : ${BASTION_NAME} (${BASTION_MACHINE})
  레지스트리 : Artifact Registry '${REPO}' (${REGION})
  디스크     : system ${DISK_TYPE} $(( SYSTEM_NODES * NODE_DISK )) GB (SSD 쿼터)
               build  ${BUILD_DISK_TYPE} $(( BUILD_NODES * BUILD_DISK )) GB (HDD 쿼터)
               배스천 ${BASTION_DISK_TYPE} ${BASTION_DISK} GB (HDD 쿼터)
  ---------------------------------------------------------
  합계 vCPU  : ${TOTAL_VCPU}
  개략 비용  : 컴퓨트 ≈ \$1.16/시간 (gke_cost_estimate.md)
               실행 사이 'gke_teardown.sh --scale-zero' 로 가동 시간을 압축할 것
══════════════════════════════════════════════════════════
EOF

if [[ $DRY_RUN -eq 1 ]]; then
  echo ">>> DRY-RUN: 아래 명령을 출력만 하고 실행하지 않습니다."
else
  read -r -p "위 자원을 생성합니다. 계속하려면 'yes' 입력: " CONFIRM
  [[ "$CONFIRM" == "yes" ]] || { echo "취소됨. 자원을 만들지 않았습니다."; exit 1; }
fi

# ── 1. API 활성화 (무료) ──────────────────────────────────────────
echo ">>> [1/5] 필요한 API 활성화 (무료)"
run gcloud services enable container.googleapis.com artifactregistry.googleapis.com \
  compute.googleapis.com --project "$PROJECT"

# ── 2. Artifact Registry (컨트롤러 이미지) ────────────────────────
echo ">>> [2/5] Artifact Registry 저장소 생성"
if [[ $DRY_RUN -eq 0 ]] && gcloud artifacts repositories describe "$REPO" \
     --location="$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  echo "    이미 존재. 건너뜁니다."
else
  run gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location="$REGION" \
    --description="tekton queue controller images" --project "$PROJECT"
fi

# ── 3. GKE 클러스터 (system 노드풀) ───────────────────────────────
echo ">>> [3/5] GKE 클러스터 생성 (default-pool=system 역할 ${SYSTEM_NODES}대)"
if [[ $DRY_RUN -eq 0 ]] && gcloud container clusters describe "$CLUSTER" \
     --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "    이미 존재. 건너뜁니다."
else
  run gcloud container clusters create "$CLUSTER" \
    --project "$PROJECT" --zone "$ZONE" \
    --release-channel=None \
    --num-nodes="$SYSTEM_NODES" \
    --machine-type="$NODE_MACHINE" \
    --disk-type="$DISK_TYPE" --disk-size="$NODE_DISK" \
    --node-labels=node-role=system \
    --no-enable-autoupgrade --no-enable-autorepair \
    --scopes="$NODE_SCOPES" \
    --enable-ip-alias
fi

# ── 4. build 노드풀 (PipelineRun 전용) ────────────────────────────
echo ">>> [4/5] build 노드풀 생성 (${BUILD_NODES}대, node-role=build)"
# taint 는 걸지 않는다: pr_create.py 가 nodeSelector(node-role=build)만 주입하고
# toleration 은 넣지 않으므로, taint 를 걸면 파이프라인 파드가 스케줄되지 않는다.
if [[ $DRY_RUN -eq 0 ]] && gcloud container node-pools describe build-pool \
     --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "    이미 존재. 건너뜁니다."
else
  run gcloud container node-pools create build-pool \
    --cluster "$CLUSTER" --project "$PROJECT" --zone "$ZONE" \
    --num-nodes="$BUILD_NODES" \
    --machine-type="$NODE_MACHINE" \
    --disk-type="$BUILD_DISK_TYPE" --disk-size="$BUILD_DISK" \
    --node-labels=node-role=build \
    --no-enable-autoupgrade --no-enable-autorepair \
    --scopes="$NODE_SCOPES"
fi

# ── 4b. system 노드 세분 라벨 (infra-role) ────────────────────────
# system 노드 2대를 역할로 나눈다. **측정 대상(제어 계층)을 조용한 노드에 격리**하는 것이 목적이다.
#   · infra-role=registry : Harbor·Gitea·Prometheus  (I/O·CPU 가 튀는 인프라 + 측정 도구)
#   · infra-role=control  : Tekton·큐 컨트롤러·Dashboard (웹훅 지연 p50/p99 의 측정 대상)
# 같은 노드에 두면 Harbor 가 30 동시 이미지 전송을 처리하는 동안 웹훅 지연이 함께 늘어
# **우리 시스템의 특성이 아닌 옆 워크로드의 부하가 논문 지표에 섞인다**(2026-07-29 확인).
if [[ $DRY_RUN -eq 0 ]]; then
  echo ">>> [4b] system 노드 infra-role 라벨"
  mapfile -t SYS_NODES < <(kubectl get nodes -l node-role=system \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  if [[ ${#SYS_NODES[@]} -ge 2 ]]; then
    run kubectl label node "${SYS_NODES[0]}" infra-role=registry --overwrite
    run kubectl label node "${SYS_NODES[1]}" infra-role=control  --overwrite
  else
    echo "    ⚠️ system 노드가 2대 미만 — 라벨 생략(수동 확인 필요)"
  fi
else
  echo ">>> [4b] system 노드 infra-role 라벨 (registry/control) — dry-run 생략"
fi

# ── 5. 배스천 VM (부하 발생기) ────────────────────────────────────
echo ">>> [5/5] 배스천 VM 생성"
if [[ $DRY_RUN -eq 0 ]] && gcloud compute instances describe "$BASTION_NAME" \
     --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "    이미 존재. 건너뜁니다."
else
  run gcloud compute instances create "$BASTION_NAME" \
    --project "$PROJECT" --zone "$ZONE" \
    --machine-type="$BASTION_MACHINE" \
    --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
    --boot-disk-size="${BASTION_DISK}GB" --boot-disk-type="$BASTION_DISK_TYPE" \
    --scopes=cloud-platform
fi

# ── 완료 안내 ─────────────────────────────────────────────────────
cat <<EOF

══════════════════════════════════════════════════════════
  자원 생성 완료. 다음 단계:
══════════════════════════════════════════════════════════
  1) kubectl 컨텍스트 연결:
     gcloud container clusters get-credentials ${CLUSTER} --zone ${ZONE} --project ${PROJECT}
     kubectl config current-context     # CLAUDE.md 의 컨텍스트 TODO 를 이 값으로 채울 것

  2) 클러스터 소프트웨어 설치 (Tekton·Gitea·Harbor·Prometheus·컨트롤러):
     GKE_SETUP.md 의 순서를 따를 것

  3) 배스천 접속 (부하 발생기 실행 위치):
     gcloud compute ssh ${BASTION_NAME} --zone ${ZONE} --project ${PROJECT}

  ⚠️ 비용: 실험을 돌리지 않는 시간에는 반드시 스케일다운
     bash scripts/gke_teardown.sh --scale-zero
══════════════════════════════════════════════════════════
EOF
