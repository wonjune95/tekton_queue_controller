#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D1 관측 — 상주 템플릿과 새 파이프라인런의 상태를 주기적으로 기록한다.

무엇을 보는가
  ① 상주 템플릿이 실행됐는가            — status.startTime 이 생기는가
  ② 새로 만든 것들이 바로 실행됐는가    — 생존 확인(②가 서야 ①을 해석할 수 있다)

파이프라인 «완료» 는 보지 않는다. 판정은 startTime 으로 확정되므로
E[S0]=216초를 기다릴 이유가 없다.

kubectl 에만 의존한다(파이썬 k8s 클라이언트 불필요). 배스천에서 그대로 돈다.
"""
import argparse
import json
import subprocess
import sys
import time

# 출력 인코딩 방어 — 배스천은 UTF-8 이지만 Windows 콘솔(cp949)에서 시험할 때
# 「—」·이모지에서 UnicodeEncodeError 로 죽는다. 검증을 막지 않도록 한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def kubectl_json(args):
    """kubectl 을 호출해 JSON 을 돌려준다. 실패하면 None (관측을 멈추지 않는다)."""
    try:
        out = subprocess.run(["kubectl"] + args + ["-o", "json"],
                             capture_output=True, timeout=30)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.decode("utf-8", "replace"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def workload_names(namespace):
    """Kueue 가 만든 Workload 목록. Kueue 가 없으면 빈 집합."""
    data = kubectl_json(["get", "workloads.kueue.x-k8s.io", "-n", namespace])
    if not data:
        return set()
    return {w["metadata"]["name"] for w in data.get("items", [])}


def snapshot(namespace, template_names):
    """한 시점의 관측값. (템플릿별 상태, 부하 집계) 를 돌려준다."""
    data = kubectl_json(["get", "pipelinerun", "-n", namespace])
    if not data:
        return None, None
    wls = workload_names(namespace)

    templates, load = {}, {"total": 0, "started": 0, "pending": 0}
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        labels = meta.get("labels") or {}
        spec = item.get("spec", {})
        status = item.get("status", {})
        started = status.get("startTime")

        if name in template_names:
            templates[name] = {
                # 이 필드가 사라졌다는 것은 «누군가 인가했다» 는 뜻이다.
                "spec_status": spec.get("status", ""),
                "started": bool(started),
                "started_at": started,
                # 제안 컨트롤러가 붙이는 라벨. 이것이 있어야 대기열의 원소가 된다(cache.py:91).
                "managed": labels.get("queue.tekton.dev/managed", ""),
                "tier": labels.get("queue.tekton.dev/tier", ""),
                # Kueue 가 이 오브젝트를 대기 항목으로 잡았는지
                "workload": any(name in w for w in wls),
            }
        elif labels.get("d1-role") != "template":
            load["total"] += 1
            if started:
                load["started"] += 1
            elif spec.get("status") == "PipelineRunPending":
                load["pending"] += 1
    return templates, load


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="default-cicd")
    p.add_argument("--templates", required=True, help="쉼표로 구분한 템플릿 이름 3개(pre,other-sa,same-sa)")
    p.add_argument("--duration", type=int, default=180)
    p.add_argument("--interval", type=int, default=5)
    p.add_argument("--out", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--condition", required=True)
    p.add_argument("--run", default="1")
    a = p.parse_args()

    names = [n.strip() for n in a.templates.split(",") if n.strip()]
    if len(names) != 3:
        print("[중단] 템플릿 이름은 3개여야 한다(pre,other-sa,same-sa).")
        sys.exit(1)
    roles = dict(zip(names, ["pre", "other-sa", "same-sa"]))

    t0 = time.time()
    first_seen = {}          # 템플릿이 «실행됨» 으로 처음 관측된 시각
    last_tmpl, last_load = {}, {}

    with open(a.out, "w", encoding="utf-8") as f:
        f.write("ts_epoch,elapsed,object,role,spec_status,started,managed,tier,workload,"
                "load_total,load_started,load_pending\n")
        while time.time() - t0 < a.duration:
            tmpl, load = snapshot(a.namespace, set(names))
            now = time.time()
            el = int(now - t0)
            if tmpl is not None:
                last_tmpl, last_load = tmpl, load
                for n in names:
                    st = tmpl.get(n)
                    if st is None:      # 아직 안 보이거나 지워짐
                        continue
                    if st["started"] and n not in first_seen:
                        first_seen[n] = el
                        print(f"  [{el:3d}s] 🔴 {roles[n]} 템플릿이 실행됐다 — {n}")
                    f.write(f"{int(now)},{el},{n},{roles[n]},{st['spec_status']},"
                            f"{int(st['started'])},{st['managed']},{st['tier']},"
                            f"{int(st['workload'])},{load['total']},{load['started']},"
                            f"{load['pending']}\n")
                f.flush()
                if el % 30 == 0:
                    print(f"  [{el:3d}s] 부하 {load['started']}/{load['total']} 실행 · "
                          f"대기 {load['pending']} | 템플릿 실행 {len(first_seen)}/3")
            time.sleep(a.interval)

    state = {
        "condition": a.condition,
        "run": a.run,
        "duration_sec": a.duration,
        "templates": {
            n: {
                "role": roles[n],
                "started": n in first_seen,
                "started_at_sec": first_seen.get(n),
                "spec_status_last": last_tmpl.get(n, {}).get("spec_status", "(사라짐)"),
                "managed_label": last_tmpl.get(n, {}).get("managed", ""),
                "tier_label": last_tmpl.get(n, {}).get("tier", ""),
                "workload": last_tmpl.get(n, {}).get("workload", False),
            } for n in names
        },
        "load": last_load,
    }
    with open(a.state, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"\n관측 종료 ({a.duration}초). 상태: {a.state}")


if __name__ == "__main__":
    main()
