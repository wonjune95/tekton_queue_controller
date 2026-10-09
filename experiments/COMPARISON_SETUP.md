# 비교군 환경 구성 (GKE 캠페인 사전 설치)

> 비교군 실행 스크립트(`cmp_tekton_kueue/`, `cmp_volcano/`)는 아래 오퍼레이터가 **설치돼 있다고 가정**한다.
> 설치는 캠페인 초기 환경 구성 단계에서 1회 수행한다. 출처·버전은 PoC(`poc_log.md`)에서 검증됨.

## 공통 전제
- Tekton Pipelines **v1.9.2** (GitHub 릴리스 자산):
  `kubectl apply -f https://github.com/tektoncd/pipeline/releases/download/v1.9.2/release.yaml`
- 내 컨트롤러(tekton_queue_controller) 배포됨(비교군 실행 시 스크립트가 일시 비활성).
- 부하 파이프라인·시크릿(gitea-basic-auth, harbor-kaniko-config)·maven-settings ConfigMap 준비됨.

## tekton-kueue (cmp_tekton_kueue)
설치 조합: **Kueue v0.16.6 + cert-manager v1.19.2 + tekton-kueue(konflux)**.
```bash
# cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.19.2/cert-manager.yaml
# Kueue v0.16.6
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.6/manifests.yaml
# 외부 프레임워크 pipelineruns.tekton.dev 활성화 (integrations.externalFrameworks)
# ↓ 2026-07-31 URL 확정·검증: HTTP 200, externalFrameworks 에 pipelineruns.tekton.dev 포함,
#   Configuration apiVersion 이 config.kueue.x-k8s.io/v1beta2 로 Kueue v0.16.6 과 일치함을 확인.
kubectl apply -f https://raw.githubusercontent.com/konflux-ci/tekton-kueue/main/hack/kueue-config.yaml
kubectl rollout restart deployment/kueue-controller-manager -n kueue-system
# tekton-kueue 애드온 (이미지 접두어 quay.io 교정)
kubectl apply -k "https://github.com/konflux-ci/tekton-kueue//config/default?ref=main" --server-side
kubectl set image deployment/tekton-kueue-controller-manager manager=quay.io/konflux-ci/tekton-kueue:latest -n tekton-kueue
kubectl set image deployment/tekton-kueue-webhook            webhook=quay.io/konflux-ci/tekton-kueue:latest -n tekton-kueue
```
- 큐 리소스는 실행 스크립트가 적용: `cmp_tekton_kueue/kueue-resources.yaml`(nominalQuota=30).
- 실행: `bash cmp_tekton_kueue/run.sh <s1|s2|s3> <run>` (내 컨트롤러만 일시 비활성).

## Volcano (cmp_volcano)
설치: **Volcano v1.15.0**.
```bash
kubectl apply -f https://raw.githubusercontent.com/volcano-sh/volcano/v1.15.0/installer/volcano-development.yaml
```
- 큐 capability·cpu요청 파이프라인은 실행 스크립트가 설정(capability cpu=3, `petclinic-build-volcano`).
- 실행: `bash cmp_volcano/run.sh <s1|s2> <run>` (내 컨트롤러 + tekton-kueue 웹훅 둘 다 일시 비활성).

## 상호 배제 (중요)
세 게이팅 시스템이 동시에 PR 을 가로채면 비교가 오염된다. 각 실행 스크립트가 **자기 것만 남기고 나머지를 일시 비활성**한다:
| 실행 | 내 컨트롤러 | tekton-kueue 웹훅 | Volcano |
|---|---|---|---|
| 본실험/에이블레이션 | **활성** | 비활성(미설치 or 웹훅 off) | 무관(schedulerName 미주입) |
| cmp_tekton_kueue | 비활성 | **활성** | 무관 |
| cmp_volcano | 비활성 | 비활성 | **활성**(schedulerName=volcano) |

- 웹훅 비활성 방식: failurePolicy=Ignore + 해당 webhook Deployment replicas=0 → fail-open 으로 통과.
- 스크립트는 `trap ... EXIT` 로 종료 시 원복(중단돼도 복구).

## 사전 검증 (2026-07-31, 클러스터 미접촉 상태에서 수행)

| 항목 | 결과 |
|---|---|
| 설치 URL 4종(cert-manager·Kueue·Volcano·Tekton) | HTTP 200, 크기 정상 |
| konflux `hack/kueue-config.yaml` | URL 확정. `externalFrameworks: pipelineruns.tekton.dev` 포함 |
| Kueue v0.16.6 ↔ konflux 설정 apiVersion | 양쪽 다 `config.kueue.x-k8s.io/v1beta2` — **호환** |
| tekton-kueue 리소스 이름 | `kubectl kustomize` 로 렌더해 대조 — 아래 목록과 **일치** |

**실행 스크립트에 가동 확인(중단형) 추가** — 전제를 주석으로만 두면, 미설치 상태에서
`cmp_*/run.sh` 가 **내 컨트롤러를 끈 채 부하를 인가**해 "비교군이 동시 실행을 제한하지 못했다"는
**허위 결과**를 조용히 만든다(우리에게 유리한 방향이라 더 위험하다).
이제 어긋나면 **내 컨트롤러를 끄기 전에** 중단한다.

- `cmp_tekton_kueue`: 웹훅 설정·웹훅 Deployment·Kueue 컨트롤러·ClusterQueue·externalFrameworks 5종 확인
- `cmp_volcano`: volcano-scheduler Ready·queue `default` 존재·파이프라인 파일 존재 확인,
  큐 capability 는 **패치 후 반영값까지 대조**(패치가 받아들여져도 미반영이면 상한 없음과 같다)

## 스크립트가 가정하는 리소스 이름 (설치 후 상이하면 조정)
- 내 컨트롤러: mutatingwebhookconfiguration `tekton-queue-mutator`, deploy `tekton-queue-controller`(ns tekton-pipelines).
- tekton-kueue: mutatingwebhookconfiguration `tekton-kueue-mutating-webhook-configuration`, deploy `tekton-kueue-webhook`(ns tekton-kueue).
- Volcano: queue `default`.
