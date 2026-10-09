#!/usr/bin/env bash
# 파이프라인 의존물 일괄 구성 — Gitea·Harbor·시크릿·Prometheus
#
#   실행: bash scripts/gke_bootstrap_deps.sh [--skip-prometheus]
#
# 전제: gke_provision.sh + gke_setup.sh 완료(클러스터·Tekton·컨트롤러 준비됨), helm 설치됨.
#
# petclinic-build-experiment.yaml 이 참조하는 것들을 전부 만든다:
#   · Gitea   : gitea-service.devops-tools.svc:3000/admin/spring-petclinic.git
#   · Harbor  : harbor.harbor.svc:80/library/{eclipse-temurin:17-jre-alpine,trivy-db:2,trivy-java-db:1}
#   · ConfigMap maven-settings (※ Secret 아님 — 파이프라인이 configMap 으로 마운트)
#   · Secret    harbor-kaniko-config (key: config.json)
#
# 시딩·미러링은 **클러스터 내 Job** 으로 수행한다(배스천에 docker/git/crane 설치 불필요).
# ⚠️ GKE 실환경 미검증. 실패 지점은 GKE_SETUP.md 에 반영할 것.

set -euo pipefail

EXP_NS="${EXP_NS:-default-cicd}"
GITEA_NS="devops-tools"
HARBOR_NS="harbor"
MON_NS="monitoring"
GITEA_USER="${GITEA_USER:-admin}"
GITEA_PW="${GITEA_PW:-Admin12345!}"          # 실험 전용 로컬 자격증명(외부 노출 없음)
HARBOR_PW="${HARBOR_PW:-Harbor12345}"
PETCLINIC_UPSTREAM="${PETCLINIC_UPSTREAM:-https://github.com/spring-projects/spring-petclinic.git}"
# 재현성: 업스트림 main 은 계속 움직인다(현재 4.0.0-SNAPSHOT / Spring Boot 4.1.0 의존).
# 캠페인 도중·이후 재시딩해도 **동일한 소스**가 되도록 커밋을 고정한다.
# 바꾸려면 빌드 소요시간·의존성이 달라지므로 논문 §3.3.1 기술과 함께 검토할 것.
PETCLINIC_REF="${PETCLINIC_REF:-f182358d02e4a68e52bdbabf55ca7800288511e7}"
SKIP_PROM=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --skip-prometheus) SKIP_PROM=1 ;;
    --yes|-y)          ASSUME_YES=1 ;;   # 비대화 실행(백그라운드). stdin 없으면 read 가 EOF 로 죽는다.
  esac
done

echo "컨텍스트: $(kubectl config current-context)"
if [[ $ASSUME_YES -eq 1 ]]; then
  echo "  (--yes 지정 — 확인 생략)"
else
  read -r -p "위 클러스터에 의존물을 설치합니다. 'yes' 입력: " C
  [[ "$C" == "yes" ]] || { echo "취소됨."; exit 1; }
fi

command -v helm >/dev/null || { echo "helm 이 필요합니다."; exit 1; }

# ── 1. Gitea ──────────────────────────────────────────────────────
echo ">>> [1/6] Gitea 설치 (${GITEA_NS})"
kubectl create namespace "$GITEA_NS" --dry-run=client -o yaml | kubectl apply -f -
helm repo add gitea-charts https://dl.gitea.com/charts/ >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install gitea gitea-charts/gitea -n "$GITEA_NS" \
  --set gitea.admin.username="$GITEA_USER" \
  --set gitea.admin.password="$GITEA_PW" \
  --set gitea.admin.email="admin@example.com" \
  --set gitea.config.server.OFFLINE_MODE=true \
  --set redis-cluster.enabled=false --set postgresql.enabled=false \
  --set postgresql-ha.enabled=false --set valkey-cluster.enabled=false \
  --set valkey.enabled=false \
  --set gitea.config.database.DB_TYPE=sqlite3 \
  --set persistence.size=10Gi \
  --set persistence.storageClass=standard \
  --set nodeSelector."node-role"=system \
  --set nodeSelector."infra-role"=registry \
  --wait --timeout 10m

# 파이프라인이 기대하는 이름(gitea-service:3000)으로 별칭 서비스 생성.
# 차트 버전에 따라 서비스명이 달라지므로 셀렉터 기반 별칭이 더 안전하다.
echo ">>> [2/6] gitea-service 별칭 서비스 생성"
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: gitea-service
  namespace: ${GITEA_NS}
spec:
  selector:
    app.kubernetes.io/name: gitea
    app.kubernetes.io/instance: gitea
  ports:
  - name: http
    port: 3000
    targetPort: 3000
EOF
kubectl -n "$GITEA_NS" get endpoints gitea-service

# ── 3. petclinic 리포 시딩 (Job) ──────────────────────────────────
echo ">>> [3/6] spring-petclinic 시딩 Job"
kubectl -n "$GITEA_NS" delete job gitea-seed --ignore-not-found >/dev/null 2>&1 || true
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: gitea-seed
  namespace: ${GITEA_NS}
spec:
  backoffLimit: 2
  template:
    spec:
      # ※ Never 로 둔다. OnFailure 는 backoffLimit 초과 시 파드를 지워 로그가 사라져
      #   실패 원인을 볼 수 없다(2026-07-29 시딩 실패 진단 때 두 번 겪음).
      restartPolicy: Never
      nodeSelector: { node-role: system, infra-role: registry }
      containers:
      - name: seed
        image: alpine/git:latest
        env:
        - { name: GITEA_USER,  value: "${GITEA_USER}" }
        - { name: GITEA_PW,    value: "${GITEA_PW}" }
        - { name: UPSTREAM,    value: "${PETCLINIC_UPSTREAM}" }
        - { name: PC_REF,      value: "${PETCLINIC_REF}" }
        command: [/bin/sh, -c]
        args:
        - |
          set -e
          BASE=http://gitea-service.${GITEA_NS}.svc:3000
          echo "Gitea 기동 대기..."
          for i in \$(seq 1 60); do
            wget -q -O- "\$BASE/api/v1/version" >/dev/null 2>&1 && break
            sleep 5
          done
          # ※ alpine/git 의 wget 은 BusyBox 판이라 --auth-no-challenge/--user/--password 를
          #   지원하지 않는다(2026-07-29 실측). Basic 인증은 --header 로 직접 넣는다.
          AUTH=\$(printf '%s:%s' "\$GITEA_USER" "\$GITEA_PW" | base64 | tr -d '\n')
          echo "리포 생성(이미 있으면 무시)"
          wget -q -O- --header="Content-Type: application/json" \
            --header="Authorization: Basic \$AUTH" \
            --post-data='{"name":"spring-petclinic","private":false}' \
            "\$BASE/api/v1/user/repos" >/dev/null 2>&1 || echo "  (이미 존재하거나 생성 생략)"
          # 생성 결과를 반드시 확인한다. 예전에는 실패를 '|| echo' 로 덮어 두어
          # 다음 단계인 git push 에서야 죽었다(원인 추적이 어려웠다).
          wget -q -O- --header="Authorization: Basic \$AUTH" \
            "\$BASE/api/v1/repos/\$GITEA_USER/spring-petclinic" >/dev/null 2>&1 || {
              echo "[실패] Gitea 리포 spring-petclinic 이 없다. 자격증명·API 응답 확인 필요."; exit 1; }
          echo "업스트림 클론 → Gitea push (ref=\$PC_REF)"
          rm -rf /tmp/pc && git clone "\$UPSTREAM" /tmp/pc
          cd /tmp/pc
          git checkout -q "\$PC_REF"
          echo "  고정된 커밋: \$(git rev-parse HEAD)"
          # ※ --depth 1 로 받은 얕은 이력은 Gitea 가 push 를 거부한다
          #   ("shallow update not allowed", 2026-07-29 실측).
          #   실험에는 이력이 필요 없으므로 .git 을 새로 만들어 단일 커밋으로 올린다.
          rm -rf .git
          git init -q -b main
          git config user.email admin@example.com && git config user.name admin
          git add -A
          git commit -q -m "seed: spring-petclinic snapshot"
          git remote add gitea "http://\$GITEA_USER:\$GITEA_PW@gitea-service.${GITEA_NS}.svc:3000/\$GITEA_USER/spring-petclinic.git"
          git push -f -q gitea main
          echo "시딩 완료"
EOF
kubectl -n "$GITEA_NS" wait --for=condition=complete job/gitea-seed --timeout=15m \
  || { echo "[실패] 시딩 로그:"; kubectl -n "$GITEA_NS" logs job/gitea-seed --tail=50; exit 1; }

# ── 4. Harbor ─────────────────────────────────────────────────────
# ⚠️ **nodeSelector 는 컴포넌트별 키로 줘야 한다.** Harbor 차트는 최상위 `nodeSelector` 를
#   대부분의 컴포넌트에 전파하지 않는다. 2026-07-29 에 `--set nodeSelector.*` 만 주었더니
#   Harbor 7개 파드가 **전부 build 노드**에 떴고, 버스트 중 CPU 포화(99.8%)로
#   harbor-database 의 liveness probe(타임아웃 1초)가 실패해 kubelet 이 컨테이너를 죽였다
#   → 토큰 검증 실패 → image-build/image-scan 21/90 실패.
#   build 노드는 파이프라인런 전용이어야 한다(측정 대상 오염 + 인프라 동반 사망 방지).
#
# ※ PVC 크기는 용량이 아니라 **성능** 때문에 키운다.
#   pd-standard 는 용량에 비례해 성능이 정해진다(GB당 0.75 IOPS, 0.12 MB/s).
#    · registry 200Gi → 24 MB/s : 동시 30 파드의 기반 이미지 pull 처리량
#    · database 200Gi → 150 IOPS: Harbor 는 pull 마다 감사로그·pull시각을 DB 에 쓴다.
#      기본값 1Gi 는 약 1 IOPS 라 PostgreSQL 이 버티지 못한다 — 2026-07-29 S2 버스트(동시 30)에서
#      harbor-database 가 exit=1 로 죽고 복구 모드에 진입, 토큰 검증 실패로 image-build 9/90 실패.
#   ⚠️ 주석을 helm 명령의 백슬래시 연결 **안쪽**에 넣지 말 것. 줄이 합쳐진 뒤 '#' 이후가 통째로
#      주석 처리되어 이후 --set 이 사라진다(bash -n 은 문법상 유효하므로 잡지 못한다).
echo ">>> [4/6] Harbor 설치 (${HARBOR_NS})"
kubectl create namespace "$HARBOR_NS" --dry-run=client -o yaml | kubectl apply -f -
helm repo add harbor https://helm.goharbor.io >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install harbor harbor/harbor -n "$HARBOR_NS" \
  --set expose.type=clusterIP \
  --set expose.tls.enabled=false \
  --set externalURL="http://harbor.${HARBOR_NS}.svc:80" \
  --set harborAdminPassword="$HARBOR_PW" \
  --set persistence.persistentVolumeClaim.registry.size=200Gi \
  --set persistence.persistentVolumeClaim.database.size=200Gi \
  --set persistence.persistentVolumeClaim.redis.size=50Gi \
  --set persistence.persistentVolumeClaim.jobservice.jobLog.size=50Gi \
  --set persistence.persistentVolumeClaim.registry.storageClass=standard \
  --set persistence.persistentVolumeClaim.jobservice.jobLog.storageClass=standard \
  --set persistence.persistentVolumeClaim.jobservice.storageClass=standard \
  --set persistence.persistentVolumeClaim.database.storageClass=standard \
  --set persistence.persistentVolumeClaim.redis.storageClass=standard \
  --set persistence.persistentVolumeClaim.trivy.storageClass=standard \
  --set trivy.enabled=false --set notary.enabled=false \
  --set core.nodeSelector."node-role"=system --set core.nodeSelector."infra-role"=registry \
  --set database.nodeSelector."node-role"=system --set database.nodeSelector."infra-role"=registry \
  --set jobservice.nodeSelector."node-role"=system --set jobservice.nodeSelector."infra-role"=registry \
  --set nginx.nodeSelector."node-role"=system --set nginx.nodeSelector."infra-role"=registry \
  --set portal.nodeSelector."node-role"=system --set portal.nodeSelector."infra-role"=registry \
  --set redis.nodeSelector."node-role"=system --set redis.nodeSelector."infra-role"=registry \
  --set registry.nodeSelector."node-role"=system --set registry.nodeSelector."infra-role"=registry \
  --set exporter.nodeSelector."node-role"=system --set exporter.nodeSelector."infra-role"=registry \
  --wait --timeout 15m

# ── 5. 기반 이미지·Trivy DB 미러 (crane Job) ──────────────────────
echo ">>> [5/6] Harbor 미러링 Job (crane)"
kubectl -n "$HARBOR_NS" delete job harbor-mirror --ignore-not-found >/dev/null 2>&1 || true
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: harbor-mirror
  namespace: ${HARBOR_NS}
spec:
  backoffLimit: 2
  template:
    spec:
      # ※ Never 로 둔다. OnFailure 는 backoffLimit 초과 시 파드를 지워 로그가 사라져
      #   실패 원인을 볼 수 없다(2026-07-29 시딩 실패 진단 때 두 번 겪음).
      restartPolicy: Never
      nodeSelector: { node-role: system, infra-role: registry }
      containers:
      - name: crane
        image: gcr.io/go-containerregistry/crane:debug
        command: [/busybox/sh, -c]
        args:
        - |
          set -e
          H=harbor.${HARBOR_NS}.svc:80
          echo "Harbor 로그인"
          crane auth login \$H -u admin -p '${HARBOR_PW}' --insecure
          echo "기반 이미지 미러"
          crane copy docker.io/library/eclipse-temurin:17-jre-alpine \$H/library/eclipse-temurin:17-jre-alpine --insecure
          echo "Trivy DB 미러"
          crane copy ghcr.io/aquasecurity/trivy-db:2 \$H/library/trivy-db:2 --insecure
          crane copy ghcr.io/aquasecurity/trivy-java-db:1 \$H/library/trivy-java-db:1 --insecure
          echo "미러링 완료"
EOF
kubectl -n "$HARBOR_NS" wait --for=condition=complete job/harbor-mirror --timeout=30m \
  || { echo "[실패] 미러링 로그:"; kubectl -n "$HARBOR_NS" logs job/harbor-mirror --tail=50; exit 1; }

# ── 6. 실험 네임스페이스 ConfigMap·Secret ─────────────────────────
echo ">>> [6/6] maven-settings(ConfigMap) · harbor-kaniko-config(Secret)"
kubectl create namespace "$EXP_NS" --dry-run=client -o yaml | kubectl apply -f -

# ※ 파이프라인은 maven-settings 를 **ConfigMap** 으로 마운트한다(Secret 아님).
#   Maven Central 레이트리밋 회피용 미러 설정.
#
# 미러는 **Google 이 GCS 로 호스팅하는 Maven Central 미러**를 쓴다.
#   · 구 설정(aliyun-public)은 최신 Spring Boot 아티팩트가 없어 502 로 빌드가 실패했다
#     (2026-07-29 스모크 테스트 실측: spring-boot-jpa-test:4.1.0 → 502 Bad Gateway).
#   · 클러스터가 us-central1 이므로 중국 미러는 지리적으로도 부적합하다.
#   · GCS 미러는 Central 전량을 담고 있고 같은 구글 네트워크라 빠르며 레이트리밋이 없다
#     (미러를 쓰는 본래 목적인 Central 레이트리밋 회피도 그대로 달성).
cat <<'EOF' > /tmp/settings.xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>google-maven-central</id>
      <name>GCS Maven Central mirror</name>
      <url>https://maven-central.storage-download.googleapis.com/maven2/</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
</settings>
EOF
kubectl create configmap maven-settings -n "$EXP_NS" \
  --from-file=settings.xml=/tmp/settings.xml \
  --dry-run=client -o yaml | kubectl apply -f -

# Kaniko/Trivy 가 Harbor 에 접근할 docker config (key 이름은 config.json 고정)
AUTH=$(printf 'admin:%s' "$HARBOR_PW" | base64 | tr -d '\n')
cat > /tmp/config.json <<EOF
{"auths":{"harbor.${HARBOR_NS}.svc:80":{"auth":"${AUTH}"}}}
EOF
kubectl create secret generic harbor-kaniko-config -n "$EXP_NS" \
  --from-file=config.json=/tmp/config.json \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/settings.xml /tmp/config.json

# ── Prometheus (자원 지표) ────────────────────────────────────────
if [[ $SKIP_PROM -eq 0 ]]; then
  echo ">>> [추가] Prometheus 설치 (${MON_NS})"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null
  helm upgrade --install kps prometheus-community/kube-prometheus-stack -n "$MON_NS" \
    --create-namespace \
    --set grafana.enabled=false \
    --set prometheus.prometheusSpec.nodeSelector."node-role"=system --set prometheus.prometheusSpec.nodeSelector."infra-role"=registry \
    --set prometheusOperator.nodeSelector."node-role"=system --set prometheusOperator.nodeSelector."infra-role"=registry \
    --set kube-state-metrics.nodeSelector."node-role"=system --set kube-state-metrics.nodeSelector."infra-role"=registry \
    --set alertmanager.alertmanagerSpec.nodeSelector."node-role"=system --set alertmanager.alertmanagerSpec.nodeSelector."infra-role"=registry \
    --wait --timeout 15m
fi

cat <<EOF

══════════════════════════════════════════════════════════
  의존물 구성 완료
══════════════════════════════════════════════════════════
  확인:
    kubectl -n ${GITEA_NS} get svc gitea-service
    kubectl -n ${HARBOR_NS} get pods
    kubectl -n ${EXP_NS} get cm maven-settings secret harbor-kaniko-config

  Prometheus 접근(외부 IP 절약 — port-forward):
    kubectl -n ${MON_NS} port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 &

  다음: 스모크 테스트
    cd ../test && bash smoke_test.sh
══════════════════════════════════════════════════════════
EOF
