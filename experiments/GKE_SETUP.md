# GKE 실험 환경 구축 순서

> 자원 생성부터 캠페인 착수까지의 전체 순서. 각 단계의 산출물이 다음 단계의 전제다.
> ⚠️ **이 절차는 GKE 실환경에서 아직 미검증이다**(로컬 PoC·파일럿 기록 기반으로 작성).
> 실행하며 어긋나는 부분은 이 문서를 갱신할 것.

경로: 컨트롤러 저장소 = 이 저장소의 루트, 실험 하니스 = `experiments/`

---

## 0. 사전 확인 (비용 0)

```bash
gcloud config get-value account project     # 실행 계정·프로젝트
gcloud billing projects describe <PROJECT>  # billingEnabled: true 확인
```

계정·크레딧·쿼터 상태는 `HANDOVER.md §0` 참조.

## 1. 자원 생성 (과금 시작 — 사용자 승인 필요)

```bash
cd <저장소 루트>
bash scripts/gke_provision.sh --dry-run          # 먼저 명령만 확인
bash scripts/gke_provision.sh                    # 'yes' 입력 후 생성
```

생성물: GKE zonal 클러스터(default-pool(system) 2 + build-pool 3, `node-role` 라벨), 배스천 VM, Artifact Registry.
리전 기본값은 `us-central1-a`(논문 §3.3.1과 일치). 서울로 바꾸려면 `--zone asia-northeast3-a` +
**논문 §3.3.1 리전 표기도 함께 수정**할 것.

## 2. 컨텍스트 연결

```bash
gcloud container clusters get-credentials tekton-cluster --zone us-central1-a --project <PROJECT>
kubectl config current-context      # 이 값을 CLAUDE.md 의 컨텍스트 TODO 에 기입
kubectl get nodes -L node-role      # build 3 / system 2 확인
```

## 3. Tekton + 큐 컨트롤러 설치

```bash
cd <저장소 루트>
bash scripts/gke_setup.sh --tag v0.2.0
```

Tekton v1.9.2(버전 고정), 네임스페이스 `default-cicd`, Webhook TLS, CRD·GlobalLimit,
컨트롤러 이미지 빌드(Cloud Build)·푸시·배포까지 수행한다.
**GlobalLimit 이 `maxPipelines=30`, `agingIntervalSec=300` 인지 출력에서 확인**한다(논문 값과 정합).

## 4. 파이프라인 의존물 구성 — **스크립트 1개로 일괄**

```bash
cd <저장소 루트>
bash scripts/gke_bootstrap_deps.sh        # Gitea·Harbor·시크릿·Prometheus 전부
```

Gitea 설치 + `gitea-service` 별칭 + **petclinic 시딩(Job)**, Harbor 설치 + **기반이미지·TrivyDB 미러(crane Job)**,
`maven-settings`(ConfigMap)·`harbor-kaniko-config`(Secret), Prometheus 까지 수행한다.
시딩·미러링은 클러스터 내 Job 으로 하므로 배스천에 docker/git 을 깔 필요가 없다.

> ⚠️ **`maven-settings` 는 Secret 이 아니라 ConfigMap 이다.** 파이프라인이 `configMap:` 으로 마운트한다
> (`petclinic-build-experiment.yaml` 76–79행). Secret 으로 만들면 code-build 단계가 실패한다.

아래는 스크립트가 무엇을 만드는지에 대한 참조(수동 복구 시 사용).

### 참조: 파이프라인이 요구하는 의존물

`pipeline/petclinic-build-experiment.yaml` 이 참조하는 것들:

| 의존물 | 참조 주소 | 용도 |
|---|---|---|
| Gitea | `gitea-service.devops-tools.svc:3000/admin/spring-petclinic.git` | 소스 클론 |
| Harbor | `harbor.harbor.svc:80/library/eclipse-temurin:17-jre-alpine` | 기반 이미지 |
| Harbor | `harbor.harbor.svc:80/library/trivy-db:2`, `trivy-java-db:1` | Trivy DB 미러 |
| **ConfigMap** | `maven-settings` | Maven 미러 설정(Central 레이트리밋 회피) — Secret 아님 |
| Secret | `harbor-kaniko-config` | Kaniko·Trivy 의 Harbor 인증 |
| ttl.sh | `ttl.sh/petclinic-<env>-<tag>:2h` | 빌드 결과 push(외부, 설치 불필요) |

### 4.1 Gitea

```bash
kubectl create namespace devops-tools
helm repo add gitea-charts https://dl.gitea.com/charts/ && helm repo update
helm install gitea gitea-charts/gitea -n devops-tools \
  --set service.http.port=3000 --set persistence.size=10Gi
# 서비스명이 gitea-service 가 아니면 파이프라인 기본 param 과 어긋난다 →
#   서비스명을 맞추거나 pipeline yaml 의 repo-url 기본값을 수정할 것.
```

시딩: `admin` 사용자 생성 → `spring-petclinic` 리포 생성 → 업스트림 소스를 push.
(배스천에서 clone 후 Gitea 로 push. Gitea 접근은 `kubectl port-forward` 사용 — 외부 IP 절약.)

### 4.2 Harbor

```bash
kubectl create namespace harbor
helm repo add harbor https://helm.goharbor.io && helm repo update
helm install harbor harbor/harbor -n harbor \
  --set expose.type=clusterIP --set persistence.persistentVolumeClaim.registry.size=50Gi
```

미러링 필요 이미지: `eclipse-temurin:17-jre-alpine`, `aquasec/trivy-db:2`, `aquasec/trivy-java-db:1`
→ `library` 프로젝트로 push. (배스천에서 pull → tag → push)

### 4.3 ConfigMap · Secret (스크립트가 자동 생성)

```bash
# Maven 미러 — ★ ConfigMap 이다 (파이프라인이 configMap 으로 마운트)
kubectl create configmap maven-settings -n default-cicd --from-file=settings.xml=<경로>
# Harbor 인증 (kaniko/trivy 공용, key 이름은 config.json 고정)
kubectl create secret generic harbor-kaniko-config -n default-cicd --from-file=config.json=<경로>
```

### 4.4 Prometheus (자원 지표 수집)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update
helm install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
# 접근은 port-forward (외부 IP 절약). resource_collect.py 기본값이 localhost:9090
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 &
```

## 5. 스모크 테스트 ★ 최대 관문 (약 5분)

```bash
cd experiments
bash smoke_test.sh default-cicd
```

의존물 6종 점검 → 파이프라인 적용 → 파이프라인런 1건 생성 → 웹훅 Tier 부여 확인 →
5단계 완료 대기 → 태스크별 결과. 실패 시 **어느 의존물이 원인인지 진단 힌트**를 출력한다.

**여기서 통과해야 이후 단계가 의미 있다.**

## 6. 파라미터 실측 → requests 파이프라인 생성

```bash
bash param_measure/run.sh 5 45                       # 단계별 피크 메모리 실측
python3 common/make_requests_pipeline.py \
    --input results/param/mem_peak.csv \
    --output pipeline/petclinic-build-requests.yaml  # requests=P95, limits=max x1.2 주입
```

생성기가 **빌드 노드 수용량까지 점검**한다. requests 가 과하면
"컨트롤러가 막은 것"과 "스케줄러가 막은 것"이 뒤섞여 L_max 실험이 오염되므로,
경고가 뜨면 `--cpu-request` 를 낮추거나 조건을 재검토할 것.

생성 후 **클러스터에 적용하고 캠페인 기본 파이프라인으로 지정**한다:

```bash
kubectl apply -f pipeline/petclinic-build-requests.yaml -n default-cicd
export EXP_PIPELINE=petclinic-build-requests     # ★ 캠페인 세션마다 반드시 export
```

> ⚠️ **`EXP_PIPELINE` 을 빠뜨리면 본실험이 requests 없이 돌아간다**(HANDOVER §4 확정과 어긋남).
> `pr_create.py` 는 이 환경변수를 기본 파이프라인으로 쓰며, 미설정 시 `petclinic-build`(requests 없음)로 동작한다.
> `run_all.sh` 는 자동으로 설정하고 파이프라인 존재 여부까지 확인하지만,
> **S0·비교군처럼 개별 실행하는 스크립트는 셸에 export 가 되어 있어야 한다.**
> 예외(의도적으로 requests 를 배제하는 조건)는 스크립트가 `--pipeline` 을 명시해 영향을 받지 않는다:
> **A0-NR**(`petclinic-build`), **Volcano**(`petclinic-build-volcano`).

산출물은 A0-R·요인 실험 2×2·비교군 memory requests 통일에 사용한다.

## 7. dry-run (캠페인 전 필수)

```bash
python3 common/resource_collect.py --check      # Prometheus 연결·쿼리 검증
bash common/cleanup.sh default-cicd             # 초기화 동작 확인
python3 common/pr_create.py --plan ...          # 부하 계획만 출력
```

단일 캠페인 원칙상 **dry-run 을 건너뛰지 않는다**(재실행 기회가 없다).

## 8. 캠페인 실행

**청크로 나눠 실행**한다(클러스터는 삭제하지 말고 `--scale-zero` 로만 재움 — 환경 동일성 유지).

```bash
# 비파괴 청크 (예: 조건 일부만)
setsid bash run_all.sh --only s1,s2 > results/run_all.log 2>&1 &
setsid bash run_all.sh --only s3,a2,a3,v >> results/run_all.log 2>&1 &

# 파괴적 조건 — 반드시 마지막, 감독 가능한 시간에만
ALLOW_DESTRUCTIVE=1 bash run_all.sh --only a1,a0nr
```

- 조건별 반복수는 `run_all.sh` 상단 `N_*` 변수(HANDOVER §5 매트릭스와 일치).
- `--max-run N` 으로 반복 회차를 나눠 이어서 실행할 수 있다.
- **파괴적 조건(A1·A0-NR)은 `ALLOW_DESTRUCTIVE=1` 없이는 실행되지 않는다**(안전 규칙 코드화).
- run_all.sh 담당 31회 + S0 3회 + 비교군 11회 + 요인 6회 = **총 51회**.

청크를 나눠 해도 되는 근거: `cleanup.sh` 가 매 실행마다 PipelineRun·PVC·admitted 카운터·빌드노드 이미지 캐시를
초기화하므로 모든 런이 동일한 초기 상태에서 시작하며, 노드 auto-upgrade/repair 를 꺼 두어 청크 간 노드 사양이 고정된다.

## 9. 비용 관리 (상시)

```bash
bash scripts/gke_teardown.sh --scale-zero   # 실험 안 돌릴 때 (필수 습관)
bash scripts/gke_teardown.sh --scale-up     # 재개
bash scripts/gke_teardown.sh --delete-all   # 캠페인 종료 후 (결과 회수 확인 뒤)
```

비용은 실행 시간이 아니라 **클러스터가 켜져 있는 총 시간**이 결정한다(`gke_cost_estimate.md §5`).
