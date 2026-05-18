#!/usr/bin/env python3
"""
S1~S4 스케줄링 시뮬레이션.

K8s 없이 src 코드를 그대로 사용해 스케줄링 알고리즘을 검증한다.
각 시나리오는 논문 2.3.2 실험 시나리오에 대응한다.

실행: python -m tests.simulate_scenarios
     (프로젝트 루트에서 실행)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# -- K8s mock (src import 전 필수) --------------------------------
from unittest.mock import MagicMock
import kubernetes.config as _k8s_cfg
_k8s_cfg.load_incluster_config = MagicMock(side_effect=_k8s_cfg.ConfigException("sim"))
_k8s_cfg.load_kube_config = MagicMock()

import datetime, time, threading
from collections import defaultdict

import src.state as state
import src.cache as cache
import src.config as cfg
from src.cache import local_cache, cache_lock, get_queue_status_from_cache

# ConfigMap 없이 per-pod fallback 사용
cache.core_api = MagicMock()
cache.core_api.read_namespaced_config_map.side_effect = Exception("sim")

# -- 공통 헬퍼 ----------------------------------------------------
NS = "test-cicd"
_pr_counter = 0
_pr_lock    = threading.Lock()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_name():
    global _pr_counter
    with _pr_lock:
        _pr_counter += 1
        return f"pr-{_pr_counter:04d}"


def _reset(l_max, aging_interval_sec, aging_min_tier):
    global _pr_counter
    _pr_counter = 0
    state.is_leader       = True
    state.initial_sync_done = True
    with cache_lock:
        local_cache.clear()
    cache.webhook_admitted_count = 0
    cfg.crd_config.update({
        "max_pipelines":      l_max,
        "aging_interval_sec": aging_interval_sec,
        "aging_min_tier":     aging_min_tier,
        "tier_rules":         cfg.DEFAULT_TIER_RULES,
        "namespace_patterns": ["*-cicd"],
        "managed_sa_patterns": list(cfg.MANAGED_SA_PATTERNS),
    })


def _create_running(exec_sec):
    """슬롯을 소비하는 실행 중 PR. exec_sec 후 자동 완료."""
    name = _next_name()
    pr = {
        'metadata': {
            'namespace': NS, 'name': name, 'labels': {},
            'creationTimestamp': _ts(_now()), 'resourceVersion': '100',
        },
        'spec': {}, 'status': {},
    }
    with cache_lock:
        local_cache[f"{NS}/{name}"] = pr
    threading.Thread(target=_complete, args=(name, exec_sec), daemon=True).start()
    return name


def _create_pending(tier, backdated_sec=0):
    """대기열에 PR 추가. backdated_sec > 0 이면 생성 시각을 과거로 설정 (Aging 시뮬레이션용)."""
    name       = _next_name()
    created_at = _now() - datetime.timedelta(seconds=backdated_sec)
    pr = {
        'metadata': {
            'namespace': NS, 'name': name,
            'labels': {
                cfg.TIER_LABEL_KEY:  str(tier),
                cfg.MANAGED_LABEL_KEY: cfg.MANAGED_LABEL_VAL,
            },
            'creationTimestamp': _ts(created_at), 'resourceVersion': '100',
        },
        'spec': {'status': 'PipelineRunPending'}, 'status': {},
    }
    with cache_lock:
        local_cache[f"{NS}/{name}"] = pr
    return name, created_at


def _complete(name, delay_sec):
    """delay_sec 후 PR completionTime 설정."""
    time.sleep(delay_sec)
    key = f"{NS}/{name}"
    with cache_lock:
        if key in local_cache:
            local_cache[key]['status']['completionTime'] = _ts(_now())


def _schedule_once(scheduled_at_map, exec_sec):
    """1회 스케줄링: 가용 슬롯만큼 Pending → Running 전환."""
    l_max = cfg.crd_config["max_pipelines"]
    running, pending = get_queue_status_from_cache()
    available = l_max - running
    now = _now()
    for target in pending[:max(0, available)]:
        name = target['metadata']['name']
        key  = f"{NS}/{name}"
        with cache_lock:
            if key in local_cache:
                local_cache[key]['spec']['status'] = None
        if name not in scheduled_at_map:
            scheduled_at_map[name] = now
            threading.Thread(target=_complete, args=(name, exec_sec), daemon=True).start()


def _print_header(title, l_max, aging_interval_sec, exec_sec):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"  L_max={l_max} | aging={aging_interval_sec}s | exec={exec_sec}s/PR")
    print(f"{'='*60}")


def _print_result(scenario, created, scheduled, l_max, exec_sec):
    """Tier별 대기 시간 집계 및 슬롯 위반 여부 출력."""
    metrics = defaultdict(list)
    for name, (tier, created_at) in created.items():
        if name in scheduled:
            wait = (scheduled[name] - created_at).total_seconds()
            metrics[tier].append(wait)

    # 슬롯 위반 검사: 스케줄 이벤트 기반 최대 동시 실행 수 계산
    events = []
    for name, sched_at in scheduled.items():
        events.append((sched_at, +1))
        events.append((sched_at + datetime.timedelta(seconds=exec_sec), -1))
    events.sort(key=lambda x: x[0])
    cur = max_concurrent = 0
    for _, delta in events:
        cur += delta
        max_concurrent = max(max_concurrent, cur)

    slot_ok = max_concurrent <= l_max
    print(f"\n  -- {scenario} 결과 --")
    for tier in sorted(metrics):
        waits = metrics[tier]
        avg   = sum(waits) / len(waits)
        mx    = max(waits)
        print(f"  Tier {tier}: 건수={len(waits):2d} | 평균 대기={avg:5.1f}s | 최대 대기={mx:5.1f}s")
    miss = len(created) - len(scheduled)
    mark = "OK" if slot_ok else f"NG 위반!"
    print(f"  스케줄됨: {len(scheduled)}/{len(created)} | "
          f"최대 동시 실행: {max_concurrent}/{l_max} [{mark}]"
          + (f" | 미스케줄: {miss}건" if miss else ""))
    return metrics, slot_ok


# -- S1: Baseline -------------------------------------------------
def run_s1():
    L_MAX = 8; AGING = 180; EXEC = 3
    _reset(L_MAX, AGING, 1)
    _print_header("S1 Baseline - 정상 부하 (λ=1개/2s, Tier 1:2:3=1:2:7)", L_MAX, AGING, EXEC)

    created    = {}
    scheduled  = {}
    tier_seq   = [1] + [2]*2 + [3]*7  # 10개, 비율 1:2:7

    def _creator():
        for tier in tier_seq:
            name, ts = _create_pending(tier)
            created[name] = (tier, ts)
            time.sleep(2)

    def _scheduler():
        deadline = time.time() + len(tier_seq) * 2 + EXEC * 3 + 5
        while time.time() < deadline:
            _schedule_once(scheduled, EXEC)
            time.sleep(0.3)

    t1 = threading.Thread(target=_creator,   daemon=True)
    t2 = threading.Thread(target=_scheduler, daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join(timeout=60)

    _print_result("S1", created, scheduled, L_MAX, EXEC)


# -- S2a: Priority - Tier1이 Tier3보다 먼저 스케줄 -----------------
def run_s2a():
    L_MAX = 3; AGING = 180; EXEC = 4
    _reset(L_MAX, AGING, 1)
    _print_header("S2a Priority - Tier3 5개 대기 중, Tier1 2개 진입 후 스케줄 순서 확인",
                  L_MAX, AGING, EXEC)

    created   = {}
    scheduled = {}

    # 슬롯 3개 채우기
    for _ in range(L_MAX):
        _create_running(EXEC)

    # Tier3 5개 + Tier1 2개 대기열 진입 (거의 동시)
    for _ in range(5):
        name, ts = _create_pending(3)
        created[name] = (3, ts)
    time.sleep(0.05)
    for _ in range(2):
        name, ts = _create_pending(1)
        created[name] = (1, ts)

    print(f"  대기열 구성: Tier3×5, Tier1×2 / 실행 중: {L_MAX}개 (슬롯 가득)")

    def _scheduler():
        deadline = time.time() + EXEC * 3 + 5
        while time.time() < deadline:
            _schedule_once(scheduled, EXEC)
            time.sleep(0.2)

    t = threading.Thread(target=_scheduler, daemon=True)
    t.start(); t.join(timeout=30)

    # 스케줄 순서 확인
    order = sorted(scheduled.items(), key=lambda x: x[1])
    print("\n  스케줄 순서 (앞 7건):")
    for i, (name, sched_at) in enumerate(order[:7]):
        tier = created[name][0]
        print(f"    {i+1}. {name} Tier{tier}")

    tier1_positions = [i+1 for i, (n, _) in enumerate(order) if created[n][0] == 1]
    tier3_positions = [i+1 for i, (n, _) in enumerate(order) if created[n][0] == 3]
    priority_ok = max(tier1_positions) < min(tier3_positions) if tier1_positions and tier3_positions else False
    mark = "OK Tier1이 Tier3보다 먼저 스케줄됨" if priority_ok else "NG 우선순위 역전 발생"
    print(f"\n  우선순위 검증: [{mark}]")
    _print_result("S2a", created, scheduled, L_MAX, EXEC)


# -- S2b: Aging - 오래 대기한 Tier3이 Tier2보다 먼저 스케줄 ----------
def run_s2b():
    L_MAX = 2; AGING = 10; EXEC = 4  # aging_interval=10s (빠른 시뮬레이션)
    _reset(L_MAX, AGING, 1)
    _print_header("S2b Aging - Tier3 (25s 대기 → 유효 Tier1) vs Tier2 (신규)",
                  L_MAX, AGING, EXEC)

    created   = {}
    scheduled = {}

    # 슬롯 2개 채우기
    for _ in range(L_MAX):
        _create_running(EXEC)

    # Tier3: 25초 전에 생성된 것처럼 backdated (aging_bonus=25//10=2, effective=max(1,3-2)=1)
    for _ in range(3):
        name, ts = _create_pending(3, backdated_sec=25)
        created[name] = (3, ts)

    # Tier2: 방금 생성
    for _ in range(3):
        name, ts = _create_pending(2)
        created[name] = (2, ts)

    print(f"  대기열: Tier3×3 (25s 대기, effective Tier1), Tier2×3 (신규)")
    print(f"  aging_interval={AGING}s → aging_bonus=25//{AGING}=2 → effective_tier=max(1,3-2)=1")

    def _scheduler():
        deadline = time.time() + EXEC * 3 + 5
        while time.time() < deadline:
            _schedule_once(scheduled, EXEC)
            time.sleep(0.2)

    t = threading.Thread(target=_scheduler, daemon=True)
    t.start(); t.join(timeout=30)

    order = sorted(scheduled.items(), key=lambda x: x[1])
    print("\n  스케줄 순서 (앞 6건):")
    for i, (name, sched_at) in enumerate(order[:6]):
        tier = created[name][0]
        created_at = created[name][1]
        wait = (sched_at - created_at).total_seconds()
        print(f"    {i+1}. {name} Tier{tier} (대기 {wait:.1f}s)")

    # Tier3(aging 적용)이 Tier2보다 먼저 스케줄됐는지 확인
    t3_positions = [i+1 for i, (n, _) in enumerate(order) if created[n][0] == 3]
    t2_positions = [i+1 for i, (n, _) in enumerate(order) if created[n][0] == 2]
    aging_ok = (t3_positions and t2_positions and
                min(t3_positions) < min(t2_positions))
    mark = "OK Aging 효과 확인 (Tier3→Tier1 승격)" if aging_ok else "NG Aging 미동작"
    print(f"\n  Aging 검증: [{mark}]")
    _print_result("S2b", created, scheduled, L_MAX, EXEC)


# -- S3: Release Burst ---------------------------------------------
def run_s3():
    L_MAX = 8; AGING = 180; EXEC = 3
    _reset(L_MAX, AGING, 1)
    _print_header("S3 Release Burst - 24개 동시 생성 (L_max의 3배), 슬롯 상한 준수 여부",
                  L_MAX, AGING, EXEC)

    created   = {}
    scheduled = {}

    for _ in range(24):
        name, ts = _create_pending(3)
        created[name] = (3, ts)
    print(f"  24개 동시 생성 완료.")

    def _scheduler():
        deadline = time.time() + EXEC * 5 + 10
        while time.time() < deadline:
            _schedule_once(scheduled, EXEC)
            time.sleep(0.2)

    t = threading.Thread(target=_scheduler, daemon=True)
    t.start(); t.join(timeout=60)

    _, slot_ok = _print_result("S3", created, scheduled, L_MAX, EXEC)
    if slot_ok:
        print("  → 동시 실행 수가 L_max=8을 초과하지 않았습니다. (가설 1 지지)")


# -- S4: Adversarial ----------------------------------------------
def run_s4():
    L_MAX = 4; AGING = 180; EXEC = 4  # μ = 4/4 = 1개/s
    _reset(L_MAX, AGING, 1)
    LAMBDA   = 3.0  # 개/s (λ = 3μ)
    DURATION = 15   # 초
    _print_header(
        f"S4 Adversarial - λ={LAMBDA}/s > μ={L_MAX/EXEC:.1f}/s, "
        f"{DURATION}초간 부하 인가 후 대기열 적체 관찰",
        L_MAX, AGING, EXEC,
    )

    created   = {}
    scheduled = {}
    stop_flag = threading.Event()

    def _creator():
        interval = 1.0 / LAMBDA
        end      = time.time() + DURATION
        while time.time() < end:
            name, ts = _create_pending(3)
            created[name] = (3, ts)
            time.sleep(interval)
        stop_flag.set()

    def _scheduler():
        while not stop_flag.is_set() or any(
            local_cache.get(f"{NS}/{n}", {}).get('spec', {}).get('status') == 'PipelineRunPending'
            for n in created
        ):
            _schedule_once(scheduled, EXEC)
            time.sleep(0.2)

    t1 = threading.Thread(target=_creator,   daemon=True)
    t2 = threading.Thread(target=_scheduler, daemon=True)
    t1.start(); t2.start()
    t1.join()
    t2.join(timeout=EXEC * 3 + 5)

    total    = len(created)
    sched    = len(scheduled)
    backlog  = total - sched
    print(f"\n  부하 종료 후 대기열 잔여: {backlog}건 / 전체 {total}건")
    print(f"  → λ({LAMBDA}/s) > μ({L_MAX/EXEC:.1f}/s) 조건에서 대기열이 적체됩니다.")
    _print_result("S4", created, scheduled, L_MAX, EXEC)


# -- 전체 실행 -----------------------------------------------------
if __name__ == "__main__":
    print("Tekton Queue Controller - 스케줄링 시뮬레이션")
    print("논문 2.3.2 S1~S4 시나리오 대응\n")

    run_s1()
    run_s2a()
    run_s2b()
    run_s3()
    run_s4()

    print(f"\n{'='*60}")
    print("  시뮬레이션 완료")
    print(f"{'='*60}")
