"""
지표 수집기.
실험 종료 후 해당 네임스페이스의 PipelineRun 목록을 읽어
대기 시간·동시 실행 수·완료율을 CSV로 저장한다.

측정 지표:
  - wait_sec      : spec.status=PipelineRunPending 해제까지 걸린 시간
  - total_sec     : 생성~완료까지 총 시간
  - max_concurrent: 측정 구간 내 최대 동시 실행 수
  - completion_rate: 완료된 PR / 전체 PR
"""
import csv
import os
import sys
import datetime
from kubernetes import client, config

config.load_kube_config()
custom = client.CustomObjectsApi()


def collect(namespace: str, output_csv: str):
    prs = custom.list_namespaced_custom_object(
        group="tekton.dev", version="v1",
        namespace=namespace, plural="pipelineruns"
    )["items"]

    rows = []
    for pr in prs:
        meta = pr["metadata"]
        status = pr.get("status", {})
        conditions = status.get("conditions", [])
        succeeded = next((c for c in conditions if c["type"] == "Succeeded"), None)

        name       = meta["name"]
        tier       = meta.get("labels", {}).get("queue.tekton.dev/tier", "?")
        env        = meta.get("labels", {}).get("env", "?")
        created_at = meta.get("creationTimestamp")
        start_time = status.get("startTime")
        end_time   = status.get("completionTime")
        result     = succeeded["status"] if succeeded else "Unknown"

        wait_sec  = _diff_sec(created_at, start_time)
        total_sec = _diff_sec(created_at, end_time)

        rows.append({
            "name": name, "env": env, "tier": tier,
            "created_at": created_at, "start_time": start_time,
            "end_time": end_time, "result": result,
            "wait_sec": wait_sec, "total_sec": total_sec,
        })

    if not rows:
        print("수집된 PipelineRun 없음")
        return

    # 동시 실행 수 계산 (1초 단위 슬라이딩)
    max_concurrent = _calc_max_concurrent(rows)

    total       = len(rows)
    completed   = sum(1 for r in rows if r["result"] == "True")
    completion_rate = completed / total * 100 if total else 0

    # 원본 덮어쓰기 금지(프로젝트 안전 규칙).
    # 회차 번호를 잘못 넘기거나 청크를 재실행하면 **이미 받은 회차 파일을 조용히 덮어쓴다.**
    # 캠페인은 재실행이 불가능하므로, 존재하면 쓰지 않고 중단한다.
    if os.path.exists(output_csv):
        raise SystemExit(
            f"[중단] 결과 파일이 이미 있습니다: {output_csv}\n"
            f"        덮어쓰면 원본이 사라집니다. 회차 번호를 확인하거나,\n"
            f"        의도한 재측정이면 기존 파일을 다른 이름으로 옮긴 뒤 다시 실행하세요.")

    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"저장: {output_csv}")
    print(f"전체: {total}  완료: {completed}  완료율: {completion_rate:.1f}%")
    print(f"최대 동시 실행: {max_concurrent}")

    for env_name in ["prod", "stg", "dev"]:
        subset = [r for r in rows if r["env"] == env_name and r["wait_sec"] is not None]
        if subset:
            waits = [r["wait_sec"] for r in subset]
            print(f"  {env_name}: 건수={len(subset)}  평균대기={sum(waits)/len(waits):.1f}s  최대대기={max(waits):.1f}s")


def _diff_sec(t1_str, t2_str):
    if not t1_str or not t2_str:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        t1 = datetime.datetime.strptime(t1_str, fmt)
        t2 = datetime.datetime.strptime(t2_str, fmt)
        return max(0.0, (t2 - t1).total_seconds())
    except Exception:
        return None


def _calc_max_concurrent(rows):
    events = []
    for r in rows:
        if r["start_time"]:
            events.append((r["start_time"], +1))
        if r["end_time"]:
            events.append((r["end_time"], -1))
    events.sort()
    cur = max_c = 0
    for _, delta in events:
        cur += delta
        max_c = max(max_c, cur)
    return max_c


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    collect(args.namespace, args.output)
