#!/usr/bin/env bash
# GKE 클러스터 소프트웨어 설치 — Tekton + 큐 컨트롤러 (kind_setup.sh 의 GKE판)
#
#   실행(컨트롤러 저장소 루트에서):  bash scripts/gke_setup.sh [--tag v0.2.0]
#
# 전제: gke_provision.sh 로 자원 생성 완료 + get-credentials 로 컨텍스트 연결 완료.
# 이 스크립트가 다루는 범위: Tekton Pipelines, 네임스페이스, Webhook TLS, 컨트롤러 이미지 빌드·푸시·배포.
# 이 스크립트가 다루지 **않는** 범위(수동/별도): Gitea·Harbor 시딩, Prometheus, maven-settings 시크릿
#   → GKE_SETUP.md 참조. 파이프라인이 이들을 참조하므로 실험 전 반드시 구성해야 한다.
#
# 이미지 빌드 방식: 기본은 Cloud Build(로컬 docker 불필요). docker 가 있으면 --docker 로 로컬 빌드.

set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-us-central1-a}"
REGION="${ZONE%-*}"
REPO="${REPO:-tekton-queue}"
TAG="${TAG:-v0.2.0}"                    # 계측 코드 반영 위해 매 빌드 새 태그 부여
NAMESPACE="tekton-pipelines"
EXP_NS="${EXP_NS:-default-cicd}"        # 실험 네임스페이스
SVC_NAME="tekton-queue-controller"
TEKTON_VERSION="${TEKTON_VERSION:-v1.9.2}"
BUILD_MODE="cloudbuild"
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)    TAG="$2"; shift 2 ;;
    --docker) BUILD_MODE="docker"; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;   # 비대화 실행(백그라운드·CI). stdin 이 없으면 read 가 EOF 로 죽는다.
    *) echo "알 수 없는 인자: $1"; exit 1 ;;
  esac
done

IMG="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SVC_NAME}:${TAG}"

echo "══════════════════════════════════════════════════════════"
echo "  컨텍스트: $(kubectl config current-context)"
echo "  이미지  : ${IMG}   (빌드 방식: ${BUILD_MODE})"
echo "══════════════════════════════════════════════════════════"
if [[ $ASSUME_YES -eq 1 ]]; then
  echo "  (--yes 지정 — 확인 생략)"
else
  read -r -p "위 컨텍스트에 설치합니다. 계속하려면 'yes' 입력: " CONFIRM
  [[ "$CONFIRM" == "yes" ]] || { echo "취소됨."; exit 1; }
fi

# ── 1. Tekton Pipelines (버전 고정 — 재현성) ──────────────────────
echo ">>> [1/6] Tekton Pipelines ${TEKTON_VERSION} 설치"
# ※ 설치 URL 은 **GitHub 릴리스 자산**을 쓴다. 구 GCS 버킷 경로
#   (storage.googleapis.com/tekton-releases/pipeline/previous/<ver>/release.yaml) 는 404 다(2026-07-29 재확인).
#   `latest` 포인터는 낡은 v1.6.0 을 가리키므로 절대 쓰지 말 것.
kubectl apply -f "https://github.com/tektoncd/pipeline/releases/download/${TEKTON_VERSION}/release.yaml"
kubectl wait --for=condition=available --timeout=300s \
  deployment/tekton-pipelines-controller -n "$NAMESPACE"

# ── 2. 실험 네임스페이스 ──────────────────────────────────────────
echo ">>> [2/6] 실험 네임스페이스 생성 (${EXP_NS})"
kubectl create namespace "$EXP_NS" --dry-run=client -o yaml | kubectl apply -f -

# ── 3. Webhook TLS 인증서 (kind_setup.sh 와 동일 로직) ────────────
echo ">>> [3/6] Webhook TLS 인증서 생성"
TLS_DIR="$(mktemp -d)"
trap 'rm -rf "$TLS_DIR"' EXIT
SVC_FQDN="${SVC_NAME}.${NAMESPACE}.svc"

cat > "${TLS_DIR}/csr.conf" <<EOF
[req]
req_extensions = v3_req
distinguished_name = req_distinguished_name
[req_distinguished_name]
[v3_req]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${SVC_FQDN}
DNS.2 = ${SVC_FQDN}.cluster.local
EOF

# Git Bash(MSYS/Cygwin)는 "/CN=..." 인자를 Windows 경로로 변환해버린다
# ("C:/Program Files/Git/CN=..." 가 되어 openssl 이 거부). 슬래시를 하나 더 붙이면
# MSYS 가 이스케이프로 인식해 변환하지 않는다. Linux(배스천)에서는 그대로 "/CN=...".
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) SUBJ="//CN=${SVC_FQDN}" ;;
  *)                    SUBJ="/CN=${SVC_FQDN}"  ;;
esac

# ※ 오류를 숨기지 않는다. 예전에 2>/dev/null 로 덮어 두어 위 경로 변환 실패가
#   "조용한 중단"으로 나타났다(2026-07-29). genrsa 의 진행 표시만 버린다.
openssl genrsa -out "${TLS_DIR}/ca.key" 2048 2>/dev/null
openssl req -new -x509 -days 365 -key "${TLS_DIR}/ca.key" \
  -subj "$SUBJ" -out "${TLS_DIR}/ca.crt"
openssl genrsa -out "${TLS_DIR}/tls.key" 2048 2>/dev/null
openssl req -new -key "${TLS_DIR}/tls.key" \
  -subj "$SUBJ" -out "${TLS_DIR}/tls.csr"
openssl x509 -req -days 365 \
  -in "${TLS_DIR}/tls.csr" \
  -CA "${TLS_DIR}/ca.crt" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
  -extensions v3_req -extfile "${TLS_DIR}/csr.conf" \
  -out "${TLS_DIR}/tls.crt"
[[ -s "${TLS_DIR}/tls.crt" && -s "${TLS_DIR}/ca.crt" ]] || { echo "TLS 인증서 생성 실패"; exit 1; }
CA_BUNDLE=$(base64 < "${TLS_DIR}/ca.crt" | tr -d '\n')

# ── 4. CRD · Secret · GlobalLimit ─────────────────────────────────
echo ">>> [4/6] CRD·Secret·GlobalLimit 배포"
kubectl apply -f install/crd.yaml
kubectl create secret tls tekton-queue-cacerts \
  --cert="${TLS_DIR}/tls.crt" --key="${TLS_DIR}/tls.key" \
  -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f install/limit-setting.yaml
echo "    GlobalLimit 확인 (T_a=300, maxPipelines=30 이어야 함):"
kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.maxPipelines}{" / agingIntervalSec="}{.spec.agingIntervalSec}{"\n"}' || true

# ── 5. 컨트롤러 이미지 빌드·푸시 ──────────────────────────────────
echo ">>> [5/6] 컨트롤러 이미지 빌드·푸시 (${BUILD_MODE})"
if [[ "$BUILD_MODE" == "cloudbuild" ]]; then
  gcloud services enable cloudbuild.googleapis.com --project "$PROJECT"
  # 신규 GCP 프로젝트는 기본 컴퓨트 SA 에 역할을 자동 부여하지 않는다. Cloud Build 는 이 계정을
  # 빌드 주체로 쓰므로 소스 tarball(GCS) 조차 못 읽어 403 이 난다(2026-07-29 실측).
  # roles/cloudbuild.builds.builder = 스토리지·레지스트리·로깅 권한 묶음.
  PROJ_NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${PROJ_NUM}-compute@developer.gserviceaccount.com" \
    --role="roles/cloudbuild.builds.builder" --condition=None --quiet >/dev/null
  # API 활성화·역할 부여 직후에는 전파 지연으로 첫 호출이 실패할 수 있다.
  sleep 15
  # ※ `--tag` 축약형은 소스 루트의 ./Dockerfile 을 찾는다. 이 저장소는 docker/Dockerfile 이므로
  #    빌드 설정을 임시 생성해 -f 로 경로를 지정한다(Makefile 의 docker build 와 동일한 컨텍스트).
  CB_CFG="$(mktemp -t cloudbuild-XXXX.yaml)"
  cat > "$CB_CFG" <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ['build', '-t', '${IMG}', '-f', 'docker/Dockerfile', '.']
images:
- '${IMG}'
EOF
  gcloud builds submit --config "$CB_CFG" --project "$PROJECT" .
  rm -f "$CB_CFG"
else
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
  make push REGISTRY="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}" IMAGE_TAG="${TAG}"
fi

# ── 6. 컨트롤러 배포 (이미지·caBundle 주입) ───────────────────────
echo ">>> [6/6] 컨트롤러 배포"
sed \
  -e "s|docker.io/${SVC_NAME}:v0.1.0|${IMG}|g" \
  -e "s|<BASE64_ENCODED_CA_CERT_HERE>|${CA_BUNDLE}|g" \
  install/deploy.yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" rollout status deploy/${SVC_NAME} --timeout=180s

# ── 6b. 시스템 컴포넌트를 system 노드에 고정 ──────────────────────
# build 노드에는 taint 를 걸지 않으므로(파이프라인 파드에 toleration 이 없다) Tekton·큐 컨트롤러가
# build 노드로 스케줄될 수 있다. 그 상태에서 A0-NR(requests 미설정 → 노드 OOM)을 돌리면
# **관측 장비가 관측 대상과 함께 죽는다**. 논문 §3.3.1 의 노드 역할 분리와도 어긋난다.
echo ">>> [6b] Tekton·큐 컨트롤러를 infra-role=control 노드에 고정"
for d in tekton-pipelines-controller tekton-pipelines-webhook tekton-events-controller tekton-dashboard "$SVC_NAME"; do
  kubectl -n "$NAMESPACE" patch deploy "$d" --type=strategic \
    -p '{"spec":{"template":{"spec":{"nodeSelector":{"node-role":"system","infra-role":"control"}}}}}' >/dev/null 2>&1 || true
done
# remote-resolvers 는 별도 네임스페이스에 있다.
kubectl -n tekton-pipelines-resolvers patch deploy tekton-pipelines-remote-resolvers --type=strategic \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"node-role":"system","infra-role":"control"}}}}}' >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" rollout status deploy/${SVC_NAME} --timeout=180s

cat <<EOF

══════════════════════════════════════════════════════════
  Tekton + 큐 컨트롤러 설치 완료
══════════════════════════════════════════════════════════
  확인:
    kubectl get pods -n ${NAMESPACE}
    kubectl logs -n ${NAMESPACE} -l app=tekton-queue --tail=30

  ⚠️ 아직 남은 필수 구성 (GKE_SETUP.md):
     · Gitea (devops-tools) + spring-petclinic 리포 시딩
     · Harbor (harbor) + 기반 이미지·Trivy DB 미러
     · maven-settings / harbor-kaniko-config 시크릿
     · Prometheus (resource_collect.py 가 참조)
  이들 없이는 파이프라인이 실행되지 않는다.

  이후 순서: 파라미터 실측(param_measure) → requests 파이프라인 작성
             → dry-run(resource_collect --check) → 캠페인
══════════════════════════════════════════════════════════
EOF
