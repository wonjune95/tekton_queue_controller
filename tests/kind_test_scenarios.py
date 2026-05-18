#!/usr/bin/env python3
"""
Kind 클러스터 S1~S4 실험 테스트.
kubectl로 PipelineRun을 생성하고 status.startTime - creationTimestamp 로 대기 시간을 측정한다.
실행: python -m tests.kind_test_scenarios
"""
import subprocess, json, time, datetime, sys
from collections import defaultdict

CONTEXT = "kind-tekton-test"
NS      = "test-cicd"

# ── kubectl 래퍼 ────────────────────────────────────────────────
def kubectl(*args, stdin=None):
    cmd = ["kubectl", "--context", CONTEXT] + list(args)
    r   = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    return r.stdout, r.stderr, r.returncode


def patch_globallimit(max_pipelines, aging_interval_sec, managed_sa_patterns=None):
    if managed_sa_patterns is None:
        managed_sa_patterns = ["*"]
    payload = json.dumps({"spec": {
        "maxPipelines":      max_pipelines,
        "agingIntervalSec":  aging_interval_sec,
        "managedSAPatterns": managed_sa_patterns,
    }})
    kubectl("patch", "globallimit", "tekton-queue-limit",
            "--type=merge", "-p", payload)
    time.sleep(7)   # 컨트롤러가 CRD 변경을 읽는 주기 대기


# ── PipelineRun 생성 ─────────────────────────────────────────────
PR_MANIFEST = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: {prefix}-
  namespace: {ns}
  labels:
    env: {env}
    type: kind-test
spec:
  pipelineSpec:
    tasks:
    - name: run
      taskSpec:
        steps:
        - name: sleep
          image: alpine:3.18
          command: ["sh", "-c", "sleep {sleep_sec}"]
"""

def create_pr(env, prefix="t", sleep_sec=20):
    manifest = PR_MANIFEST.format(
        prefix=prefix, ns=NS, env=env, sleep_sec=sleep_sec
    )
    out, err, rc = kubectl("create", "-f", "-", stdin=manifest)
    if rc == 0:
        name = out.strip().split("/")[1].split(" ")[0]
        return name
    print(f"  [오류] PR 생성 실패: {err.strip()[:80]}")
    return None


def reset_admitted_counter():
    """admitted ConfigMap 카운터를 0으로 리셋."""
    kubectl("patch", "configmap", "tekton-queue-admitted-count",
            "-n", "tekton-pipelines", "--type=merge",
            "-p", '{"data":{"admitted":"0"}}')
    time.sleep(2)


def delete_test_prs():
    kubectl("delete", "pipelineruns", "-n", NS,
            "-l", "type=kind-test", "--ignore-not-found=true")
    reset_admitted_counter()
    time.sleep(3)


# ── 상태 수집 ───────────────────────────────────────────────────
def _get_pr_items(names):
    out, _, _ = kubectl("get", "pipelineruns", "-n", NS,
                        "-l", "type=kind-test", "-o", "json")
    if not out:
        return {}
    all_items = json.loads(out).get("items", [])
    return {it["metadata"]["name"]: it for it in all_items
            if it["metadata"]["name"] in names}


def wait_all(names, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pr_map = _get_pr_items(names)
        done    = sum(1 for pr in pr_map.values() if _is_done(pr))
        pending = sum(1 for pr in pr_map.values() if _is_pending(pr))
        running = len(names) - done - pending
        print(f"\r  Running:{running:2d}  Pending:{pending:2d}  Done:{done:2d}/{len(names)}",
              end="", flush=True)
        if done == len(names):
            print()
            return pr_map
        time.sleep(5)
    print()
    return _get_pr_items(names)


def _is_done(pr):
    status = pr.get("status", {})
    if status.get("completionTime"):
        return True
    for c in status.get("conditions", []):
        if c.get("type") == "Succeeded" and c.get("status") in ("True", "False"):
            return True
    return False


def _is_pending(pr):
    return pr.get("spec", {}).get("status") == "PipelineRunPending"


def _parse_ts(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            return dt.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


# ── 메트릭 계산 ────────────────────────────────────────────────
def calc_metrics(pr_map, tier_map):
    """tier -> [wait_sec] 딕셔너리 반환."""
    metrics = defaultdict(list)
    for name, pr in pr_map.items():
        tier     = tier_map.get(name, "?")
        created  = _parse_ts(pr["metadata"].get("creationTimestamp"))
        started  = _parse_ts(pr["status"].get("startTime"))
        if created and started:
            wait = (started - created).total_seconds()
            metrics[tier].append(wait)
    return metrics


def max_concurrent(pr_map, exec_sec):
    """스케줄 이벤트 기반 최대 동시 실행 수 계산."""
    events = []
    for pr in pr_map.values():
        s = _parse_ts(pr["status"].get("startTime"))
        if s:
            events.append((s, +1))
            events.append((s + datetime.timedelta(seconds=exec_sec), -1))
    events.sort(key=lambda x: x[0])
    cur = peak = 0
    for _, d in events:
        cur  += d
        peak  = max(peak, cur)
    return peak


def print_metrics(scenario, pr_map, tier_map, l_max, exec_sec):
    metrics  = calc_metrics(pr_map, tier_map)
    peak     = max_concurrent(pr_map, exec_sec)
    done_cnt = sum(1 for pr in pr_map.values() if _is_done(pr))
    stuck    = sum(1 for pr in pr_map.values() if _is_pending(pr))

    print(f"\n  -- {scenario} 결과 --")
    for tier in sorted(metrics):
        ws  = metrics[tier]
        avg = sum(ws) / len(ws)
        mx  = max(ws)
        print(f"  Tier {tier}: 건수={len(ws):2d} | 평균 대기={avg:6.1f}s | 최대 대기={mx:6.1f}s")
    slot_mark = "OK" if peak <= l_max else f"NG (위반: {peak})"
    print(f"  완료:{done_cnt}/{len(pr_map)} | 최대 동시:{peak}/{l_max} [{slot_mark}]"
          + (f" | 미완료(Pending):{stuck}" if stuck else ""))
    return metrics, peak, done_cnt, stuck


# ── 시나리오 ─────────────────────────────────────────────────────

def run_s1(results):
    """S1 Baseline: 정상 부하 λ≈1/3s, Tier 1:2:3=1:2:7, L_max=5"""
    L_MAX   = 5
    EXEC    = 20
    AGING   = 180
    print(f"\n{'='*58}")
    print(f"  S1 Baseline | L_max={L_MAX} | Tier 1:2:3=1:2:7 | exec={EXEC}s")
    print(f"{'='*58}")

    delete_test_prs()
    patch_globallimit(L_MAX, AGING)

    tier_seq = [("prod",1)]*1 + [("stg",2)]*2 + [("dev",3)]*7
    tier_map = {}
    names    = []
    for env, tier in tier_seq:
        name = create_pr(env, prefix="s1", sleep_sec=EXEC)
        if name:
            names.append(name)
            tier_map[name] = tier
            print(f"  [생성] {name} Tier{tier}")
        time.sleep(3)

    print(f"\n  {len(names)}개 생성 완료. 완료 대기 중...")
    pr_map = wait_all(names, timeout=EXEC*4+60)
    metrics, peak, done, stuck = print_metrics("S1", pr_map, tier_map, L_MAX, EXEC)
    results["S1"] = {"metrics": metrics, "peak": peak, "done": done,
                     "total": len(names), "stuck": stuck, "l_max": L_MAX}


def run_s2(results):
    """S2 Priority: Tier3 5개 대기 → Tier1 3개 진입, 스케줄 순서 확인"""
    L_MAX   = 3
    EXEC    = 25
    AGING   = 180
    print(f"\n{'='*58}")
    print(f"  S2 Priority | L_max={L_MAX} | Tier3×5 대기 후 Tier1×3 진입")
    print(f"{'='*58}")

    delete_test_prs()
    patch_globallimit(L_MAX, AGING)

    tier_map = {}
    names    = []

    # Tier3 5개 먼저 생성 (슬롯 채우기 + 대기열)
    for _ in range(5):
        name = create_pr("dev", prefix="s2", sleep_sec=EXEC)
        if name:
            names.append(name)
            tier_map[name] = 3
            print(f"  [생성] {name} Tier3")
        time.sleep(0.5)

    print(f"  5초 후 Tier1 3개 진입...")
    time.sleep(5)

    # Tier1 3개 진입
    for _ in range(3):
        name = create_pr("prod", prefix="s2", sleep_sec=EXEC)
        if name:
            names.append(name)
            tier_map[name] = 1
            print(f"  [생성] {name} Tier1")
        time.sleep(0.5)

    print(f"\n  {len(names)}개 생성 완료. 완료 대기 중...")
    pr_map = wait_all(names, timeout=EXEC*5+60)

    # 스케줄 순서 확인
    schedule_order = []
    for name, pr in pr_map.items():
        started = _parse_ts(pr["status"].get("startTime"))
        if started:
            schedule_order.append((started, name, tier_map.get(name, "?")))
    schedule_order.sort()
    print("\n  스케줄 순서:")
    for i, (_, name, tier) in enumerate(schedule_order):
        print(f"    {i+1}. {name} Tier{tier}")

    tier1_pos = [i+1 for i, (_, n, t) in enumerate(schedule_order) if t == 1]
    tier3_pos = [i+1 for i, (_, n, t) in enumerate(schedule_order) if t == 3]
    priority_ok = (tier1_pos and tier3_pos and max(tier1_pos) < min(tier3_pos))
    print(f"\n  우선순위 검증: [{'OK - Tier1이 Tier3보다 먼저 스케줄됨' if priority_ok else 'NG - 역전 발생'}]")

    metrics, peak, done, stuck = print_metrics("S2", pr_map, tier_map, L_MAX, EXEC)
    results["S2"] = {"metrics": metrics, "peak": peak, "done": done,
                     "total": len(names), "stuck": stuck, "l_max": L_MAX,
                     "priority_ok": priority_ok, "order": schedule_order}


def run_s3(results):
    """S3 Release Burst: 15개 동시 생성(L_max×3), 슬롯 상한 준수 여부"""
    L_MAX   = 5
    EXEC    = 20
    AGING   = 180
    BURST   = 15
    print(f"\n{'='*58}")
    print(f"  S3 Release Burst | L_max={L_MAX} | {BURST}개 동시 생성 (×{BURST//L_MAX})")
    print(f"{'='*58}")

    delete_test_prs()
    patch_globallimit(L_MAX, AGING)

    tier_map = {}
    names    = []
    for _ in range(BURST):
        name = create_pr("dev", prefix="s3", sleep_sec=EXEC)
        if name:
            names.append(name)
            tier_map[name] = 3
    print(f"  {len(names)}개 동시 생성 완료. 완료 대기 중...")
    pr_map = wait_all(names, timeout=EXEC*4+60)
    metrics, peak, done, stuck = print_metrics("S3", pr_map, tier_map, L_MAX, EXEC)
    slot_ok = peak <= L_MAX
    if slot_ok:
        print(f"  -> 동시 실행 상한({L_MAX}) 준수 확인. (가설 1 지지)")
    results["S3"] = {"metrics": metrics, "peak": peak, "done": done,
                     "total": len(names), "stuck": stuck, "l_max": L_MAX,
                     "slot_ok": slot_ok}


def run_s4(results):
    """S4 Adversarial: λ > μ 포화 조건, 대기열 적체 관찰"""
    L_MAX     = 3
    EXEC      = 20    # μ = 3/20 = 0.15/s
    AGING     = 180
    INTERVAL  = 4     # λ = 1/4 = 0.25/s > μ
    TOTAL     = 12
    print(f"\n{'='*58}")
    print(f"  S4 Adversarial | L_max={L_MAX} | λ=1/{INTERVAL}s={1/INTERVAL:.2f}/s "
          f"> μ={L_MAX}/{EXEC}={L_MAX/EXEC:.2f}/s")
    print(f"{'='*58}")

    delete_test_prs()
    patch_globallimit(L_MAX, AGING)

    tier_map = {}
    names    = []
    for i in range(TOTAL):
        name = create_pr("dev", prefix="s4", sleep_sec=EXEC)
        if name:
            names.append(name)
            tier_map[name] = 3
            print(f"  [생성 {i+1:2d}/{TOTAL}] {name}")
        time.sleep(INTERVAL)

    print(f"\n  생성 종료. 잔여 대기열 소진 대기 중 (최대 {EXEC*3}s)...")
    pr_map = wait_all(names, timeout=EXEC*3+60)

    done_cnt  = sum(1 for pr in pr_map.values() if _is_done(pr))
    stuck_cnt = sum(1 for pr in pr_map.values() if _is_pending(pr))
    print(f"  부하 종료 후 잔여 대기: {stuck_cnt}건 / 전체 {TOTAL}건")
    print(f"  -> λ({1/INTERVAL:.2f}/s) > μ({L_MAX/EXEC:.2f}/s): 대기열 적체 {'확인됨' if stuck_cnt > 0 else '미확인(모두 완료)'}")

    metrics, peak, done, stuck = print_metrics("S4", pr_map, tier_map, L_MAX, EXEC)
    results["S4"] = {"metrics": metrics, "peak": peak, "done": done,
                     "total": len(names), "stuck": stuck, "l_max": L_MAX,
                     "lambda": round(1/INTERVAL, 3), "mu": round(L_MAX/EXEC, 3)}


# ── 메인 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    results = {}

    print("Tekton Queue Controller - Kind 클러스터 실험 테스트")
    print("GlobalLimit 초기 패치: managedSAPatterns=[*] (모든 kubectl 요청 허용)\n")

    # 전처리: managedSAPatterns를 ["*"]로 열어야 kubectl 생성 PR이 큐를 통해 실행됨
    patch_globallimit(10, 180, managed_sa_patterns=["*"])

    run_s1(results)
    run_s2(results)
    run_s3(results)
    run_s4(results)

    # 원복
    patch_globallimit(10, 300,
                      managed_sa_patterns=["system:serviceaccount:tekton-pipelines:tekton-dashboard"])
    print("\n[완료] GlobalLimit 원복 완료.")

    # 결과 요약 저장
    with open("tests/kind_test_results.json", "w", encoding="utf-8") as f:
        # datetime 직렬화 처리
        def _serial(obj):
            if isinstance(obj, datetime.datetime):
                return obj.isoformat()
            raise TypeError(str(type(obj)))
        json.dump(results, f, ensure_ascii=False, indent=2, default=_serial)

    print(f"\n결과 저장: tests/kind_test_results.json")
    print("='='*29")
