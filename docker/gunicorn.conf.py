"""
Gunicorn 설정 파일.

workers=1 + gthread worker: 백그라운드 스레드(leader/manager/watcher)가
in-memory 캐시를 공유하므로 멀티 프로세스 방식 사용 불가.
"""
bind = "0.0.0.0:8443"
workers = 1
worker_class = "gthread"
threads = 8
keyfile = "/certs/tls.key"
certfile = "/certs/tls.crt"
timeout = 120
keepalive = 5


def post_fork(server, worker):
    import threading

    from prometheus_client import start_http_server

    from src.config import (
        CANCEL_STATUSES, LEASE_DURATION_SEC, LEASE_NAME, LEASE_NAMESPACE,
        POD_NAME, get_cached_config, load_crd_config, log,
    )
    from src.workers.leader import leader_election_loop
    from src.workers.manager import manager_loop
    from src.workers.watcher import watcher_loop

    log("Tekton Queue Controller 기동 준비 중...")
    log(f"  Pod: {POD_NAME}")
    load_crd_config()
    cfg = get_cached_config()
    log(f"  네임스페이스 패턴: {cfg['namespace_patterns']}")
    log(f"  Limit: {cfg['max_pipelines']}")
    log(f"  Aging: {cfg['aging_interval_sec']}초당 Tier 1 승격 (최소 Tier {cfg['aging_min_tier']})")
    log("  Tier Rules:")
    for rule in cfg['tier_rules']:
        mt    = rule.get('matchType', 'env')
        extra = f", labelKey: {rule['labelKey']}" if mt == 'label' else ""
        log(f"    Tier {rule['tier']} [{mt}{extra}] {rule['pattern']} ({rule.get('description', '')})")
    log(f"  취소 상태 목록: {sorted(CANCEL_STATUSES)}")
    log(f"  관리 SA 패턴: {cfg['managed_sa_patterns']}")
    log(f"  Leader Election: Lease={LEASE_NAMESPACE}/{LEASE_NAME}, "
        f"Duration={LEASE_DURATION_SEC}s, RetryPeriod=2s")

    try:
        start_http_server(9090)
        log("Prometheus metrics server started on port 9090")
    except OSError:
        log("[경고] Prometheus 포트 9090 이미 사용 중. 스킵.")

    threading.Thread(target=leader_election_loop, daemon=True).start()
    threading.Thread(target=manager_loop,         daemon=True).start()
    threading.Thread(target=watcher_loop,         daemon=True).start()
