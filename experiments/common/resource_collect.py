# -*- coding: utf-8 -*-
"""
자원 지표 수집기 (Prometheus 기반).

파일럿 metrics_collect.py 는 대기/완료/동시성만 측정한다(메모리 0건).
본 스크립트는 논문 §3.4 '자원 안정성' 지표를 kube-prometheus-stack 에서 시계열로 수집한다.
  - 노드별 메모리 사용률 / CPU 사용률
  - 네임스페이스 파드 메모리·CPU 합
  - OOMKilled 건수, 노드 NotReady 지속시간, 축출(Evicted) 건수
  - 최대 동시 실행 파드 수

측정 창은 대상 네임스페이스 PipelineRun 의 [min(created), max(completion)] 로 자동 도출한다
(metrics_collect.py 와 동일 창). --start/--end 로 직접 지정도 가능.

사용:
  # 사전: kube-prometheus-stack Prometheus 로 포트포워드
  #   kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
  python3 resource_collect.py --namespace default-cicd --output res.csv
  python3 resource_collect.py --namespace default-cicd --check   # 연결·지표 존재 자체 점검(캠페인 dry-run)

주의:
  - **로컬에서 검증 불가(Prometheus 미기동). GKE 캠페인 dry-run 에서 반드시 --check 로 검증할 것.**
  - 라벨명(node vs instance)·지표 존재는 kube-prometheus-stack 버전에 따라 다를 수 있다.
    QUERIES 딕셔너리에서 바로 조정한다. --check 가 시리즈 0개인 지표를 알려준다.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── PromQL 정의 ────────────────────────────────────────────────────────────
# {ns}=네임스페이스, {win}=측정 창(예: "40m"), {rate}=rate 윈도우
# range 질의 결과의 라벨은 series() 로 요약해 long-format CSV 로 저장한다.
QUERIES = {
    # 자원 안정성 — 노드
    "node_mem_util_ratio":
        '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)',
    "node_mem_used_bytes":
        'node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes',
    "node_cpu_util_ratio":
        '1 - avg by(instance)(rate(node_cpu_seconds_total{{mode="idle"}}[{rate}]))',
    # 자원 안정성 — 네임스페이스 파드 합
    "ns_pod_mem_working_set_bytes":
        'sum(container_memory_working_set_bytes{{namespace="{ns}",container!="",container!="POD"}})',
    "ns_pod_cpu_cores":
        'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container!="",container!="POD"}}[{rate}]))',
    # 동시 실행 파드 수(빌드 파드 근사) — kube-state-metrics
    # ※ count() 가 아니라 sum() 이어야 한다. kube_pod_status_phase 는 파드마다 **모든 phase 에 대해**
    #   시리즈를 내보내며 값이 0/1 이다. count() 는 시리즈 개수(=존재하는 전체 파드 수)를 세어
    #   실제 동시 실행 수와 무관한 값이 나온다(2026-07-29 S0: count=130 vs 실제 Running=0).
    "ns_running_pods":
        'sum(kube_pod_status_phase{{namespace="{ns}",phase="Running"}}) or vector(0)',
    # 노드 Ready 상태(1=Ready). 0 구간 = NotReady — kube-state-metrics
    "node_ready":
        'kube_node_status_condition{{condition="Ready",status="true"}}',
    # 제어 계층 부하 — 웹훅 /mutate 지연 p50/p99 (컨트롤러 :9090 히스토그램)
    # ※ rate 윈도우가 짧으면 관측이 드문 구간에서 모든 버킷이 0 이 되어 histogram_quantile 이 NaN 을 낸다
    #   (2026-07-29 S0: λ=1/분 → 2m·5m 창에서 NaN, 10m 부터 값이 나옴).
    #   CPU rate 창({rate})을 늘리면 자원 지표가 과평활화되므로 히스토그램 전용 창({whist})을 따로 둔다.
    "webhook_latency_p50_seconds":
        'histogram_quantile(0.50, sum(rate(tekton_queue_webhook_latency_seconds_bucket[{whist}])) by (le))',
    "webhook_latency_p99_seconds":
        'histogram_quantile(0.99, sum(rate(tekton_queue_webhook_latency_seconds_bucket[{whist}])) by (le))',
}

# 순간(instant) 질의 — 창 전체 누적/집계
INSTANT_QUERIES = {
    "oomkilled_total":
        'sum(increase(kube_pod_container_status_terminated_reason'
        '{{namespace="{ns}",reason="OOMKilled"}}[{win}])) or vector(0)',
    "evicted_pods":
        'sum(kube_pod_status_reason{{namespace="{ns}",reason="Evicted"}}) or vector(0)',
}


def _http_get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def prom_query_range(base, promql, start, end, step):
    q = urllib.parse.urlencode({
        "query": promql, "start": start, "end": end, "step": step})
    data = _http_get(f"{base.rstrip('/')}/api/v1/query_range?{q}")
    if data.get("status") != "success":
        raise RuntimeError(f"query 실패: {data}")
    return data["data"]["result"]


def prom_query_instant(base, promql, at=None):
    params = {"query": promql}
    if at:  # 빈 값이면 time 생략 → Prometheus 가 now 사용
        params["time"] = at
    q = urllib.parse.urlencode(params)
    data = _http_get(f"{base.rstrip('/')}/api/v1/query?{q}")
    if data.get("status") != "success":
        raise RuntimeError(f"query 실패: {data}")
    return data["data"]["result"]


def series_label(metric):
    """결과 시리즈의 대표 라벨(node/instance/pod) 추출."""
    for k in ("node", "instance", "nodename", "pod"):
        if k in metric:
            return metric[k]
    return metric.get("__name__", "series")


def derive_window(namespace, buffer_sec=60):
    """대상 네임스페이스 PipelineRun 의 [min(created)-buffer, max(completion)+buffer]."""
    from kubernetes import client, config
    config.load_kube_config()
    custom = client.CustomObjectsApi()
    prs = custom.list_namespaced_custom_object(
        group="tekton.dev", version="v1",
        namespace=namespace, plural="pipelineruns")["items"]
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    created, completed = [], []
    for pr in prs:
        c = pr["metadata"].get("creationTimestamp")
        e = pr.get("status", {}).get("completionTime")
        if c:
            created.append(dt.datetime.strptime(c, fmt))
        if e:
            completed.append(dt.datetime.strptime(e, fmt))
    if not created:
        raise SystemExit(f"[오류] {namespace} 에 PipelineRun 이 없어 창을 도출할 수 없음. --start/--end 지정.")
    start = min(created) - dt.timedelta(seconds=buffer_sec)
    end = (max(completed) if completed else max(created)) + dt.timedelta(seconds=buffer_sec)
    return start.replace(tzinfo=dt.timezone.utc), end.replace(tzinfo=dt.timezone.utc)


def fmt_win(start, end):
    mins = int((end - start).total_seconds() // 60) + 1
    return f"{mins}m"


def do_check(base, ns, rate, whist="10m"):
    """연결·지표 존재 점검(캠페인 dry-run 용). 각 지표의 시리즈 개수를 보고."""
    print(f"[check] Prometheus: {base}")
    try:
        up = prom_query_instant(base, "up", "")  # time 생략 시 now
    except Exception as e:
        print(f"[check][실패] Prometheus 연결 불가: {e}")
        return 1
    print(f"[check] 연결 OK (up 시리즈 {len(up)}개)")
    now = ""
    problems = 0
    for name, tmpl in {**QUERIES, **INSTANT_QUERIES}.items():
        promql = tmpl.format(ns=ns, rate=rate, win="10m", whist=whist)
        try:
            res = prom_query_instant(base, promql, now)
            n = len(res)
            flag = "OK" if n > 0 else "⚠ 0개"
            if n == 0:
                problems += 1
            print(f"[check] {name:32s} 시리즈 {n:3d}개  {flag}")
        except Exception as e:
            problems += 1
            print(f"[check] {name:32s} 오류: {e}")
    if problems:
        print(f"[check] ⚠ {problems}개 지표가 0개/오류 — 라벨명·지표 존재를 QUERIES 에서 조정 필요.")
    else:
        print("[check] 모든 지표 정상. 수집 준비 완료.")
    return 0


def main():
    p = argparse.ArgumentParser(description="Prometheus 기반 자원 지표 수집기")
    p.add_argument("--namespace", required=True)
    p.add_argument("--prometheus-url", default="http://localhost:9090",
                   help="기본: 포트포워드된 localhost:9090")
    p.add_argument("--output", help="시계열 long-format CSV 경로")
    p.add_argument("--summary", help="요약 JSON 경로(생략 시 stdout)")
    p.add_argument("--start", help="ISO8601(UTC). 생략 시 PR 에서 자동 도출")
    p.add_argument("--end", help="ISO8601(UTC). 생략 시 PR 에서 자동 도출")
    p.add_argument("--step", default="15s", help="range 질의 간격")
    p.add_argument("--rate", default="2m", help="rate() 윈도우(자원 지표용)")
    p.add_argument("--hist-window", default="10m",
                   help="웹훅 지연 히스토그램 전용 rate 윈도우. 짧으면 관측 희소 구간에서 NaN.")
    p.add_argument("--check", action="store_true", help="연결·지표 존재만 점검")
    args = p.parse_args()

    base = args.prometheus_url
    if args.check:
        sys.exit(do_check(base, args.namespace, args.rate, args.hist_window))

    # 측정 창
    if args.start and args.end:
        start = dt.datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    else:
        start, end = derive_window(args.namespace)
    win = fmt_win(start, end)
    start_ts, end_ts = start.timestamp(), end.timestamp()
    print(f"측정 창: {start.isoformat()} ~ {end.isoformat()} ({win}), step={args.step}")

    # 원본 덮어쓰기 금지(프로젝트 안전 규칙).
    # 회차 번호 착오나 청크 재실행으로 **이미 받은 회차 파일을 조용히 덮어쓰는** 사고를 막는다.
    # 질의 전에 검사해 즉시 중단한다(수 분짜리 질의를 헛돌리지 않도록).
    for path in (args.output, args.summary):
        if path and os.path.exists(path):
            raise SystemExit(
                f"[중단] 결과 파일이 이미 있습니다: {path}\n"
                f"        덮어쓰면 원본이 사라집니다. 회차 번호를 확인하거나,\n"
                f"        의도한 재측정이면 기존 파일을 다른 이름으로 옮긴 뒤 다시 실행하세요.")

    rows = []      # (metric, ts_iso, label, value)
    summary = {"namespace": args.namespace, "window": win,
               "start": start.isoformat(), "end": end.isoformat(), "peaks": {}}

    # 히스토그램 기반 지표의 워밍업 구간.
    # ⚠️ `rate(...[10m])` 는 각 시점에서 **10분을 되돌아본다.** 따라서 측정 창 시작 직후의 값에는
    #   **창 이전의 트래픽이 섞인다.** 회차 사이 간격은 cleanup 포함 몇 분뿐이라
    #   매 회차 초반이 **직전 회차의 트래픽**으로 오염된다.
    #   (2026-07-31 S0 run1 실측: 직전 실패 회차의 30초 스톨이 창 시작~+10분 구간에 유입돼
    #    p99 peak 가 5000ms 로 기록됐다. 해당 구간을 제외한 실제 p99 는 중앙 98ms·최대 236ms.)
    #   → 창 시작부터 rate 창 길이만큼은 peak 집계에서 제외한다. 원자료(CSV)에는 그대로 남긴다.
    _unit = {"s": 1, "m": 60, "h": 3600}
    try:
        hist_warmup = int(args.hist_window[:-1]) * _unit[args.hist_window[-1]]
    except (ValueError, KeyError):
        print(f"[경고] --hist-window 형식을 해석하지 못했다: {args.hist_window} — 워밍업 제외 생략")
        hist_warmup = 0

    # range 질의
    for name, tmpl in QUERIES.items():
        promql = tmpl.format(ns=args.namespace, rate=args.rate, win=win, whist=args.hist_window)
        # 히스토그램 분위수만 워밍업 제외 대상이다(다른 지표는 순간값이라 되돌아보지 않는다).
        warmup_until = start_ts + hist_warmup if "histogram_quantile" in tmpl else start_ts
        try:
            result = prom_query_range(base, promql, start_ts, end_ts, args.step)
        except Exception as e:
            print(f"[경고] {name} 질의 실패: {e}")
            continue
        # ⚠️ NaN 은 peak 집계에서 제외한다.
        #   histogram_quantile(rate(...[10m])) 는 측정 창 **시작 부근**에서 되돌아볼 관측이 없어
        #   rate=0 → NaN 을 낸다. 그런데 파이썬 max() 는 NaN 과의 비교가 전부 False 라
        #   **첫 원소가 NaN 이면 NaN 을 그대로 반환**한다.
        #   그 결과 시계열 CSV 에는 유효값이 131개 들어 있는데도 요약 JSON 의
        #   webhook_latency_p50/p99 가 NaN 으로 기록됐다(2026-07-30 S0 run1 실측).
        #   JSON 관점에서도 NaN 은 표준이 아니라 엄격한 파서가 읽지 못한다.
        #   원자료(rows)에는 NaN 을 그대로 남기고, 요약에서만 제외한다.
        peak = None
        nan_count = 0
        warm_skipped = 0
        for s in result:
            label = series_label(s["metric"])
            for ts, val in s["values"]:
                try:
                    v = float(val)
                except ValueError:
                    continue
                tsf = float(ts)
                rows.append((name, dt.datetime.utcfromtimestamp(tsf).isoformat() + "Z", label, v))
                if math.isnan(v) or math.isinf(v):
                    nan_count += 1
                    continue
                if tsf < warmup_until:      # 창 이전 트래픽이 섞이는 구간
                    warm_skipped += 1
                    continue
                peak = v if peak is None else max(peak, v)
        if peak is not None:
            summary["peaks"][name] = peak
        elif nan_count or warm_skipped:
            # 전 구간이 제외되면 창 설정이 잘못된 것이므로 조용히 넘기지 않는다.
            summary["peaks"][name] = None
            print(f"  [경고] {name} 은 유효 표본이 없다 — 측정 창이 --hist-window({args.hist_window})보다 "
                  f"짧은지 확인할 것.")
        notes = []
        if nan_count:
            notes.append(f"NaN {nan_count}개")
        if warm_skipped:
            notes.append(f"워밍업 {warm_skipped}개")
        suffix = f"  ({', '.join(notes)} 제외)" if notes else ""
        print(f"  {name:32s} 시리즈 {len(result):3d}  peak={peak}{suffix}")

    # instant 질의(창 누적)
    for name, tmpl in INSTANT_QUERIES.items():
        promql = tmpl.format(ns=args.namespace, rate=args.rate, win=win, whist=args.hist_window)
        try:
            res = prom_query_instant(base, promql, end_ts)
            val = float(res[0]["value"][1]) if res else 0.0
        except Exception as e:
            print(f"[경고] {name} 질의 실패: {e}")
            val = None
        summary[name] = val
        print(f"  {name:32s} = {val}")

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "timestamp", "label", "value"])
            w.writerows(rows)
        print(f"저장(시계열): {args.output}  ({len(rows)} 행)")

    sj = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(sj)
        print(f"저장(요약): {args.summary}")
    else:
        print("=== 요약 ===")
        print(sj)


if __name__ == "__main__":
    main()
