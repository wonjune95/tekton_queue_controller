# 부록: 실험 환경 및 방법론

> ⚠️ **이 문서는 파일럿(1차) 실험 기록이다. 현행 계획과 값이 다르다.**
> 옛 값: Tekton v0.65.0 / e2-standard-4 / `agingIntervalSec: 180` / A0~A2 3종 / 단일 실행.
> **현행**: Tekton v1.9.2 / e2-custom-8-16384 / `agingIntervalSec: 300`(W_max=600) /
> S0–S3 + A0-R·A0-NR·A1·A2·A3·V + 비교군 2종 / 51회.
> 현행 기준은 `HANDOVER.md §5`(매트릭스·명명표)와 `GKE_SETUP.md`(구축 순서)를 볼 것.
> 이 문서의 옛 실험명(`a1_no_admitted` 등)은 **파일럿 결과 경로와 일치시키기 위해 그대로 둔다**(원본 보존).

## A. 실험 환경

### A.1 클러스터 구성

| 항목 | 사양 |
|------|------|
| 플랫폼 | Google Kubernetes Engine (GKE) |
| 클러스터 이름 | tekton-cluster |
| 리전 | us-central1-a |
| 노드 수 | 5 (컨트롤 플레인 1 + 워커 4) |
| 노드 머신 타입 | e2-standard-4 (vCPU 4, 메모리 16 GB) |
| 노드 디스크 | 200 GB SSD (각 노드) |
| Kubernetes 버전 | 1.31 |

**노드 역할 분리**

| 역할 | 노드 수 | 라벨 | 설명 |
|------|--------|------|------|
| 빌드 노드 | 3 | `node-role=build` | PipelineRun TaskRun 전용 |
| 인프라 노드 | 1 | — | Gitea, Harbor, Tekton 컨트롤 플레인 |
| 배스천 노드 | 1 | — | 실험 오케스트레이터, 부하 생성기 실행 |

### A.2 소프트웨어 구성

| 컴포넌트 | 버전 | 네임스페이스 | 역할 |
|----------|------|-------------|------|
| Tekton Pipelines | v0.65.0 | tekton-pipelines | PipelineRun 실행 엔진 |
| Tekton Queue Controller | 자체 구현 | tekton-pipelines | 동시 실행 제어·우선순위 큐 |
| Gitea | 1.21 | devops-tools | 소스 저장소 (spring-petclinic) |
| Harbor | 2.10 | harbor | 기반 이미지 레지스트리 캐시 |
| Traefik | v2.10 | kube-system | 인그레스 컨트롤러 |

### A.3 큐 컨트롤러 설정 (기준값)

```yaml
apiVersion: queue.tekton.dev/v1alpha1
kind: GlobalLimit
metadata:
  name: tekton-queue-limit
spec:
  maxPipelines: 30          # Lmax: 빌드 노드 3대 × 10
  agingIntervalSec: 180     # 에이징 주기 3분
  tierRules:
    - tier: 0  matchType: label   labelKey: queue.tekton.dev/urgent  pattern: "true"  description: 긴급
    - tier: 1  matchType: env     pattern: prod                       description: 운영
    - tier: 2  matchType: env     pattern: stg                        description: 검증
    - tier: 3  matchType: env     pattern: "*"                        description: 개발
```

---

## B. 실험 파이프라인

실험에 사용한 파이프라인(`petclinic-build`)은 실제 CI 워크로드를 모사한 5단계 구조다.

```
code-fetch → code-build → write-dockerfile → image-build → image-scan
```

| 단계 | 작업 | 이미지 | 소요 시간 (평균) |
|------|------|--------|----------------|
| code-fetch | Gitea에서 spring-petclinic 클론 | alpine/git | ~10초 |
| code-build | Maven 패키지 빌드 (`-DskipTests`) | maven:3.9-eclipse-temurin-17 | ~90초 |
| write-dockerfile | Dockerfile 생성 | alpine:3.19 | ~5초 |
| image-build | Kaniko 이미지 빌드 & ttl.sh 푸시 | gcr.io/kaniko-project/executor:v1.23.2 | ~60초 |
| image-scan | Trivy 취약점 스캔 (HIGH/CRITICAL) | aquasec/trivy:0.57.0 | ~60초 |

**Maven 의존성 미러**: Maven Central 레이트 리밋 회피를 위해 Alibaba 퍼블릭 미러(`https://maven.aliyun.com/repository/public`) 사용.

**이미지 전략**: 기반 이미지는 Harbor 내부 캐시에서 pull, 빌드 결과 이미지는 ttl.sh(임시 레지스트리, 2시간 TTL)에 push하여 Harbor 스토리지 누적 방지.

---

## C. 부하 생성기 (`common/pr_create.py`)

배스천 노드에서 Kubernetes Python 클라이언트로 PipelineRun을 직접 생성한다.

### 동작 모드

| 모드 | 설명 | 주요 파라미터 |
|------|------|-------------|
| `steady` | 지정 속도(λ/분)로 지속 생성 | `--rate`, `--duration` |
| `burst` | 지정 개수를 짧은 간격으로 일괄 생성 | `--count`, `--interval` |

### env 분포 (steady 모드 기본값)

| env | 비율 | Tier |
|-----|------|------|
| prod | 10% | 1 |
| stg | 20% | 2 |
| dev | 70% | 3 |

PipelineRun 생성 시 `env` 라벨을 부착하면, 큐 컨트롤러의 mutating webhook이 이를 읽어 `queue.tekton.dev/tier` 라벨을 자동으로 추가한다.

---

## D. 지표 수집기 (`common/metrics_collect.py`)

실험 종료 후 네임스페이스의 모든 PipelineRun을 조회하여 CSV로 저장한다.

### 수집 필드

| 필드 | 설명 |
|------|------|
| `name` | PipelineRun 이름 |
| `env` | env 라벨 (prod / stg / dev) |
| `tier` | 큐 컨트롤러가 부여한 Tier (0–3) |
| `created_at` | 생성 시각 (ISO 8601) |
| `start_time` | 실행 시작 시각 |
| `end_time` | 완료 시각 |
| `result` | True / False / Unknown |
| `wait_sec` | 대기 시간 = start\_time − created\_at (초) |
| `total_sec` | 총 소요 시간 = end\_time − created\_at (초) |

`result=Unknown`은 지표 수집 시점에 아직 실행 중인 PipelineRun을 의미한다. 분석 시 완료(True/False) 항목만 사용한다.

**최대 동시 실행 수** 계산: 모든 PipelineRun의 `start_time`(+1)과 `end_time`(−1)을 이벤트로 정렬 후 슬라이딩 카운터의 최댓값.

---

## E. 실험 시나리오

### E.1 시나리오 개요

| ID | 이름 | 부하 패턴 | 목적 |
|----|------|----------|------|
| S0 | Baseline | λ=1/분, 30분 | 정상 부하 기준선 확립 |
| S1 | Peak-hour | λ=1/분 10분 → λ=10/분 10분 | 피크 시 차등 서비스·에이징 효과 측정 |
| S2 | Release Burst | 90개 일괄 생성 (0.33초 간격) | Lmax 초과 방지 검증 |
| S3 | Adversarial | λ=3/분, 30분 (λ≥μ) | 포화 상태에서 대기시간 상한 검증 |

### E.2 S0 — Baseline

**목적**: 정상 부하(λ=1/분)에서 큐 컨트롤러 기준선 수립.

**절차**:
1. λ=1/분으로 30분 동안 PipelineRun 생성
2. 전체 완료 후 지표 수집

**주요 측정 지표**: env별 평균 대기시간, 완료율, 최대 동시 실행 수

---

### E.3 S1 — Peak-hour

**목적**: 저부하→피크 전환 시 Tier별 대기시간 차이와 에이징에 의한 기아 완화 효과 측정.

**절차**:
1. **저부하 구간**: λ=1/분, 10분
2. **피크 구간**: λ=10/분, 10분
3. 잔여 PipelineRun 전체 완료 대기 (최대 40분)
4. 지표 수집

**부하 조건 요약**:

| 구간 | λ (건/분) | 지속 시간 |
|------|----------|---------|
| 저부하 | 1 | 10분 |
| 피크 | 10 | 10분 |

---

### E.4 S2 — Release Burst

**목적**: Lmax×3(=90개)을 30초 내 일괄 생성하여 동시 실행 수가 Lmax=30을 초과하지 않는지 검증.

**절차**:
1. 90개 PipelineRun을 0.33초 간격으로 일괄 생성 (전량 dev/Tier3)
2. 전체 완료 대기 (최대 40분)
3. 지표 수집

**판정 기준**: 측정 구간 내 최대 동시 실행 수 ≤ 30

---

### E.5 S3 — Adversarial

**목적**: 도착률이 처리율을 초과(λ≥μ)하는 포화 조건에서 대기 시간이 수렴하는지 확인.

**절차**:
1. λ=3/분으로 30분 지속 생성
2. 잔여 대기열 소화 대기 (최대 30분)
3. 지표 수집

**μ 추정**: S0 결과에서 파이프라인 1건당 평균 소요 시간 ≈ 4분, 빌드 슬롯 30개 기준 μ ≈ 7.5건/분. λ=3/분은 λ<μ이나 Tier3 누적으로 포화 효과 관찰 가능.

---

## F. 절제 실험 (Ablation Study)

큐 컨트롤러의 핵심 컴포넌트를 각각 제거하여 기여도를 검증한다.

### F.1 절제 실험 개요

| ID | 제거 컴포넌트 | 변경 사항 | 부하 패턴 |
|----|-------------|----------|----------|
| A0 | 컨트롤러 전체 | Deployment scale=0, webhook failurePolicy=Ignore | S2와 동일 (90개 burst) |
| A1 | Lmax 제약 | `maxPipelines: 999` | S2와 동일 (90개 burst) |
| A2 | 에이징 | `agingIntervalSec: 9999` | S1와 동일 (저부하→피크) |

### F.2 A0 — 컨트롤러 완전 제거

**목적**: 컨트롤러 없이 90개 burst 실행 시 동시 실행 수 및 노드 안정성 확인.

**절차**:
1. `tekton-queue-mutator` webhook의 `failurePolicy`를 `Ignore`로 변경 (webhook 없어도 PipelineRun 생성 허용)
2. `tekton-queue-controller` Deployment를 replicas=0으로 축소
3. 90개 PipelineRun burst 생성
4. 완료 대기 후 지표 수집
5. 컨트롤러 복구 (`failurePolicy=Fail`, replicas=1)

**예상 결과**: 모든 PipelineRun이 즉시 실행 → 최대 동시 실행 수 >> 30 → 빌드 노드 자원 고갈

---

### F.3 A1 — Lmax 제약 제거

**목적**: `maxPipelines` 값을 999로 설정하여 사실상 동시 실행 상한을 제거한 효과 측정.

**절차**:
1. `GlobalLimit.spec.maxPipelines = 999` 패치
2. 90개 PipelineRun burst 생성
3. 완료 대기 후 지표 수집
4. `maxPipelines = 30` 원복

**A0와의 차이**: 컨트롤러는 동작하나 슬롯 제한이 없음. 티어·에이징 로직은 유지.

---

### F.4 A2 — 에이징 제거

**목적**: 에이징을 비활성화하여 저우선순위(dev) PipelineRun의 기아 현상 발생 여부 확인.

**절차**:
1. `GlobalLimit.spec.agingIntervalSec = 9999` 패치 (에이징 사실상 비활성화)
2. S1와 동일 부하: λ=1/분 10분 → λ=10/분 10분
3. 완료 대기 후 지표 수집
4. `agingIntervalSec = 180` 원복

**판정 기준**: 피크 구간 dev PipelineRun의 평균 대기시간이 S1 대비 유의하게 증가하는지 확인.

---

## G. 실험 실행 절차

### G.1 전체 실험 순서

```
S0 → S1 → S2 → S3 → A0 → A1 → A2
```

각 실험 사이에 네임스페이스 초기화(`cleanup.sh`)를 실행하여 이전 실험의 PipelineRun·PVC를 제거하고 큐 상태를 리셋한다.

### G.2 네임스페이스 초기화 (`common/cleanup.sh`)

각 실험 종료 후 수행하는 초기화 절차:

1. **PipelineRun 전체 삭제** (`kubectl delete pipelinerun --all`)
2. **PVC 전체 삭제** (`kubectl delete pvc --all`, volumeClaimTemplate 생성분)
3. **큐 컨트롤러 admitted count 리셋** (ConfigMap `tekton-queue-admitted-count` → `admitted_count: "0"`)
4. **빌드 노드 이미지 prune** (백그라운드 실행, nsenter + crictl rmi --prune)

### G.3 자동화 오케스트레이터 (`run_all.sh`)

전체 실험을 자동으로 순차 실행하고, 완료 후 `make_report.py`로 결과 보고서를 생성한다.

```bash
setsid bash run_all.sh > results/run_all.log 2>&1 &
```

`setsid`를 사용하여 새 세션에서 실행함으로써 터미널 종료 시에도 실험이 중단되지 않도록 한다.

---

## H. 결과 파일 구조

```
test/results/
├── s1_baseline/
│   └── run1.csv
├── s2_peak_hour/
│   └── run1.csv
├── s3_release_burst/
│   └── run1.csv
├── s4_adversarial/
│   └── run1_rate3.csv
├── a0_no_controller/
│   └── run1.csv
├── a1_no_admitted/
│   └── run1.csv
├── a2_no_aging/
│   └── run1.csv
└── report.md
```

각 CSV 파일은 실험 단위의 PipelineRun 레코드를 포함하며, `make_report.py`가 전체 파일을 읽어 시나리오별 비교 보고서를 생성한다.
