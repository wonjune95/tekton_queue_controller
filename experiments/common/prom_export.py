# -*- coding: utf-8 -*-
"""Prometheus 시계열 로컬 회수 (클러스터 삭제 전 1회용).

resource_collect.py 가 조회하지 않았던 지표까지 포함해, 보존 기간 안에 남아 있는
시계열을 CSV 로 통째 내려받는다. 클러스터를 지우면 다시 얻을 수 없다.

사용:
    kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
    python3 common/prom_export.py --start 2026-07-29 --end 2026-08-09

출력: results/prom_export/<metric>.csv  (+ _manifest.json)
기존 파일이 있으면 덮어쓰지 않고 중단한다(프로젝트 안전 규칙).
"""
import argparse, csv, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

DEFAULT_BASE = "http://localhost:9090"
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "prom_export")

# 회수 대상. (파일명, PromQL)
#  ※ 앞의 4개가 이번 회수의 핵심 — 축출 1건의 원인과 노드 압박 조건.
QUERIES = [
    ("node_memory_pressure",  'kube_node_status_condition{condition="MemoryPressure"}'),
    ("node_disk_pressure",    'kube_node_status_condition{condition="DiskPressure"}'),
    ("node_pid_pressure",     'kube_node_status_condition{condition="PIDPressure"}'),
    ("pod_evicted",           'kube_pod_status_reason{reason="Evicted"}'),

    ("node_ready",            'kube_node_status_condition{condition="Ready"}'),
    ("pod_oomkilled",         'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}'),
    ("pod_phase",             'kube_pod_status_phase'),
    ("node_mem_util",         '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'),
    ("node_cpu_util",         '1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[2m]))'),
    ("node_mem_available",    'node_memory_MemAvailable_bytes'),

    ("queue_running",         'tekton_queue_running_total'),
    ("queue_pending",         'tekton_queue_pending_total'),
    ("queue_limit",           'tekton_queue_limit'),
    ("queue_promotion",       'tekton_queue_promotion_total'),
    ("webhook_latency_count", 'tekton_queue_webhook_latency_seconds_count'),
    ("webhook_latency_bucket",'tekton_queue_webhook_latency_seconds_bucket'),
]


def http_get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def query_range(base, expr, start, end, step):
    q = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    d = http_get(f"{base.rstrip('/')}/api/v1/query_range?{q}")
    if d.get("status") != "success":
        raise RuntimeError(f"query failed: {d.get('error')}")
    return d["data"]["result"]


def to_epoch(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--step", default="30s")
    ap.add_argument("--outdir", default=OUTDIR)
    args = ap.parse_args()

    start, end = to_epoch(args.start), to_epoch(args.end)
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(),
                "range": [args.start, args.end], "step": args.step, "series": {}}
    total_pts = 0

    for name, expr in QUERIES:
        path = os.path.join(outdir, f"{name}.csv")
        if os.path.exists(path):
            print(f"[중단] 이미 존재: {path}  (덮어쓰기 금지 — 다른 이름으로 옮긴 뒤 재실행)")
            sys.exit(1)

        try:
            res = query_range(args.base, expr, start, end, args.step)
        except Exception as e:
            print(f"  ! {name}: {e}")
            manifest["series"][name] = {"expr": expr, "error": str(e)}
            continue

        n = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "labels", "value"])
            for s in res:
                lbl = json.dumps(s["metric"], ensure_ascii=False, sort_keys=True)
                for ts, val in s["values"]:
                    w.writerow([ts, lbl, val]); n += 1
        total_pts += n
        manifest["series"][name] = {"expr": expr, "series": len(res), "points": n}
        flag = "  <== 핵심" if name in ("node_memory_pressure", "pod_evicted") else ""
        print(f"  {name:24s} 계열 {len(res):3d}  표본 {n:7d}{flag}")

    with open(os.path.join(outdir, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n완료: {outdir}  (총 표본 {total_pts:,})")


if __name__ == "__main__":
    main()
