# resource_collect.py — 자원 지표 수집기 (캠페인 계측 보강)

파일럿의 `metrics_collect.py`는 대기·완료·동시성만 측정했고 **CPU/메모리는 0건**이었다
(HANDOVER 문제 #4). 논문 핵심 주장이 "자원 안정성(H1)"이므로, GKE 캠페인에서는
노드/파드 자원을 반드시 함께 수집해야 한다. 본 스크립트가 그 공백을 메운다.

## 수집 지표 (논문 §3.4 '자원 안정성' 대응)
- 노드별 메모리 사용률·사용 바이트, CPU 사용률
- 네임스페이스 파드 메모리·CPU 합
- 최대 동시 실행 파드 수(빌드 파드 근사)
- OOMKilled 건수(창 누적), 노드 Ready(0 구간=NotReady), 축출(Evicted) 건수

측정 창은 대상 네임스페이스 PipelineRun 의 `[min(created), max(completion)]`에서 자동 도출한다
(metrics_collect.py 와 동일 창).

## 사전 준비 (GKE, kube-prometheus-stack)
```bash
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
# 서비스명은 설치 릴리스명에 따라 다를 수 있음 (kubectl -n monitoring get svc | grep prometheus)
```

## 사용
```bash
# 1) 캠페인 dry-run: 연결·지표 존재 점검 (반드시 먼저)
python3 resource_collect.py --namespace default-cicd --check

# 2) 실제 수집 (run.sh 의 metrics_collect 직후)
python3 resource_collect.py --namespace default-cicd \
  --output  "$OUTDIR/${MODE}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/${MODE}_run${RUN}_resource.json"
```

## run.sh 통합 (한 줄 추가)
각 시나리오 `run.sh` 의 `metrics_collect.py` 호출 **직후**에 아래를 추가:
```bash
python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/${MODE}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/${MODE}_run${RUN}_resource.json"
```

## ⚠ 반드시 지킬 것 (단일 캠페인 원칙)
- **로컬에서 실동작 검증 불가**(Prometheus 미기동). GKE 캠페인 **dry-run 에서 `--check` 로 필수 검증**.
  `--check` 는 각 지표의 시리즈 개수를 보고하며, 0개인 지표는 라벨명/지표 존재 문제다.
- kube-prometheus-stack 버전에 따라 라벨명(`node` vs `instance`)·지표 존재가 다를 수 있다.
  문제 시 스크립트 상단 `QUERIES` 딕셔너리에서 PromQL 을 직접 조정한다.
- 웹훅 지연 p50/p99(§3.4 '제어 계층 부하')는 **컨트롤러에 히스토그램 추가 완료(2026-07-25)** 로
  `tekton_queue_webhook_latency_seconds_bucket` 을 수집기가 이미 질의한다. 단 컨트롤러 :9090 이
  Prometheus 스크레이프 대상이어야 한다(ServiceMonitor/annotation 확인).

## 산출물
- `*_resource.csv` : long-format 시계열 (metric, timestamp, label, value)
- `*_resource.json`: 요약 (지표별 peak, OOMKilled/Evicted 누적, 창 정보)
