# Security Guide

## 1. TLS 인증서 생성 및 Secret 등록

### 자체 서명 인증서 생성 (로컬/테스트 환경)
```bash
# 1. 개인 키 및 자체 서명 인증서 생성
openssl req -x509 -newkey rsa:4096 -keyout tls.key -out tls.crt \
  -days 365 -nodes \
  -subj "/CN=tekton-queue-controller.tekton-pipelines.svc" \
  -addext "subjectAltName=DNS:tekton-queue-controller.tekton-pipelines.svc,DNS:tekton-queue-controller.tekton-pipelines.svc.cluster.local"

# 2. K8s Secret 생성
kubectl create secret tls tekton-queue-cacerts \
  --cert=tls.crt --key=tls.key \
  -n tekton-pipelines

# 3. caBundle 값 추출 (deploy.yaml에 주입 필요)
cat tls.crt | base64 | tr -d '\n'
```

### deploy.yaml에 caBundle 주입
`install/deploy.yaml`의 `MutatingWebhookConfiguration` 섹션에서:
```yaml
caBundle: "<위에서 추출한 base64 값>"
```

---

## 2. RBAC 권한 설명

`install/deploy.yaml`에 정의된 `ClusterRole` 권한:

| apiGroup | 리소스 | 동사 | 이유 |
|----------|--------|------|------|
| `tekton.dev` | `pipelineruns` | get, list, watch, patch | 캐시 구축(Watcher), 보류 해제(Manager) |
| `tekton.devops` | `globallimits` | get | 상한 설정 로드 |
| `coordination.k8s.io` | `leases` | get, create, update | Leader Election |

> **최소 권한 원칙**: `src/` 가 실제로 호출하는 API 만 둡니다. PipelineRun 생성·삭제 권한은 없습니다.
> 웹훅 판정 구성(이전 판, 59c66bd)이 쓰던 ConfigMap 인가 카운터 권한은 단일 인가 구조에서 쓰지 않아 제거했습니다.

---

## 3. NetworkPolicy

`install/networkpolicy.yaml`을 적용하면 컨트롤러 Pod의 트래픽을 제한합니다.

```bash
kubectl apply -f install/networkpolicy.yaml
```

- **Ingress**: webhook 포트(8443)만 허용
- **Egress**: K8s API 서버(443) 및 Prometheus(9090)만 허용

---

## 4. Pod 보안 설정

`install/deploy.yaml`의 컨테이너 보안 컨텍스트:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1001
  fsGroup: 1001
containers:
  - securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: [ALL]
      seccompProfile:
        type: RuntimeDefault
```

---

## 5. 보안 점검 체크리스트

- [ ] TLS 인증서 만료 일자 확인 (365일 기준, 갱신 알림 설정)
- [ ] `caBundle` 값이 실제 인증서와 일치하는지 확인
- [ ] `tekton-queue-cacerts` Secret이 올바른 네임스페이스에 있는지 확인
- [ ] ClusterRole 권한이 최소 권한 원칙에 부합하는지 주기적으로 검토
- [ ] NetworkPolicy 적용 후 Webhook가 정상 동작하는지 확인
- [ ] 이미지 취약점 스캔 (`trivy image tekton-queue-controller:local`) 주기 실행
