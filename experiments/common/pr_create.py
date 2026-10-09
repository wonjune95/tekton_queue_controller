"""
PipelineRun 생성기 (부하 발생기).

논문 실험에서 부하 발생기(배스천)가 직접 K8s API로 PipelineRun을 생성한다.
env 라벨로 Tier가 결정된다: prod→Tier1, stg→Tier2, 그 외→Tier3.

재현성(TODO 6):
  - --seed 로 난수 고정 → Tier 혼합·도착 시각 시퀀스가 실행 간 동일.
  - --arrival {periodic,poisson}: 도착 모델. periodic=등간격(기존), poisson=지수 간격(M/M/c 정합).
  - --env-dist "prod:0.1,stg:0.2,dev:0.7": Tier 혼합비 명시.
  - --plan: 실제 생성 없이 스케줄(오프셋·env)만 출력 → 동일 시드 재현·dry-run 검증용.
  - 실행 시작 시 설정(seed/arrival/dist/rate)을 로그에 남겨 파라미터를 기록한다.
"""
import os
import sys
import time
import argparse
import datetime
import random

from kubernetes.client.exceptions import ApiException

ENV_DIST_DEFAULT = {"prod": 0.1, "stg": 0.2, "dev": 0.7}

# 웹훅 일시 실패 재시도 설정. create_pr() 주석 참조.
# 실측 스톨은 30초 규모였으므로 2·4·8·16초 백오프(누적 30초)로 충분히 넘긴다.
WEBHOOK_RETRIES = int(os.environ.get("WEBHOOK_RETRIES", "5"))
WEBHOOK_BACKOFF = float(os.environ.get("WEBHOOK_BACKOFF", "2.0"))

# Gitea 소스 URL (클러스터 내부 DNS)
GITEA_URL = "http://gitea-service.devops-tools.svc:3000/admin/spring-petclinic.git"
# 기본 파이프라인. 캠페인에서는 requests 주석 파이프라인으로 통일한다
# (HANDOVER §4 확정: 본실험·비교군 전부 requests 설정 조건).
#   export EXP_PIPELINE=petclinic-build-requests
# A0-NR(requests 무설정 조건)과 Volcano(cpu 노브 파이프라인)는 --pipeline 으로 명시 지정하여
# 이 환경변수의 영향을 받지 않는다.
PIPELINE_NAME = os.environ.get("EXP_PIPELINE", "petclinic-build")
GITEA_SECRET = "gitea-basic-auth"

# 웹훅은 GlobalLimit.managedSAPatterns 에 매칭되는 출처의 PipelineRun 만 큐에 넣는다.
# 그 외 출처는 Pending + managed 라벨 없음 → 스케줄링에서 제외되어 영구 보류된다(webhook.py:93~104).
# 부하 발생기는 kubeconfig 사용자(사람 계정) 자격이므로 그대로 두면 전부 보류된다.
# → Dashboard SA 를 임퍼소네이트해 **운영 경로(Dashboard 발) 와 동일한 출처**로 생성한다.
#   논문 §3.2.2 "Dashboard SA 출처에만 적용" 서술과 실제 동작을 일치시키는 설정이기도 하다.
#   비관리 출처(보류 분기) 검증이 필요하면 IMPERSONATE_SA="" 로 두고 1건만 생성해 비교한다.
IMPERSONATE_SA = os.environ.get(
    "IMPERSONATE_SA", "system:serviceaccount:tekton-pipelines:tekton-dashboard")

_custom = None


def _get_custom():
    """K8s 클라이언트 지연 초기화 (--plan 모드는 클러스터 불필요)."""
    global _custom
    if _custom is None:
        from kubernetes import client, config
        config.load_kube_config()
        api_client = client.ApiClient()
        if IMPERSONATE_SA:
            api_client.set_default_header("Impersonate-User", IMPERSONATE_SA)
        _custom = client.CustomObjectsApi(api_client)
    return _custom


def parse_env_dist(s: str) -> dict:
    """'prod:0.1,stg:0.2,dev:0.7' → {'prod':0.1,...}. 합이 1이 아니면 정규화."""
    if not s:
        return dict(ENV_DIST_DEFAULT)
    d = {}
    for part in s.split(","):
        k, v = part.split(":")
        d[k.strip()] = float(v)
    total = sum(d.values())
    if total <= 0:
        raise ValueError("env-dist 합이 0 이하")
    return {k: v / total for k, v in d.items()}


def plan_schedule(rate_per_min: float, duration_min: float, env_dist: dict,
                  arrival: str, seed: int) -> list:
    """생성 스케줄을 결정론적으로 산출한다. 반환: [(offset_sec, env), ...].

    동일 (rate, duration, env_dist, arrival, seed) → 동일 스케줄(재현성).
    - periodic: 등간격(60/rate) 도착.
    - poisson : 지수 분포 간격(평균 60/rate)의 누적 → M/M/c 정합.
    """
    rng = random.Random(seed)
    interval = 60.0 / rate_per_min
    horizon = duration_min * 60.0
    envs = list(env_dist.keys())
    weights = list(env_dist.values())

    schedule = []
    if arrival == "periodic":
        n = int(round(rate_per_min * duration_min))
        for i in range(n):
            env = rng.choices(envs, weights=weights)[0]
            schedule.append((i * interval, env))
    elif arrival == "poisson":
        t = 0.0
        while True:
            t += rng.expovariate(1.0 / interval)
            if t >= horizon:
                break
            env = rng.choices(envs, weights=weights)[0]
            schedule.append((t, env))
    else:
        raise ValueError(f"알 수 없는 arrival: {arrival}")
    return schedule


def _build_pipelinerun(namespace: str, name: str, env: str, urgent: bool,
                       generate_name: bool = False, pipeline: str = PIPELINE_NAME,
                       scheduler_name: str = "") -> dict:
    labels = {"env": env}
    if urgent:
        labels["queue.tekton.dev/urgent"] = "true"

    # Volcano 비교군: 태스크 파드에 schedulerName 을 주입 → Volcano 가 파드 스케줄링을 담당.
    pod_template = {"nodeSelector": {"node-role": "build"}}
    if scheduler_name:
        pod_template["schedulerName"] = scheduler_name

    # generate_name=True: metadata.name 대신 generateName 사용 → 서버가 이름 확정.
    # 이 경우 웹훅이 phantom 캐시를 넣지 못해 admitted 카운터가 유일한 슬롯 브리지가 된다
    # (A1' 격리 조건에서 watch 지연 창의 순간 상한 초과를 노출하려면 이 모드가 필요).
    if generate_name:
        metadata = {"generateName": f"{name}-", "namespace": namespace, "labels": labels}
    else:
        metadata = {"name": name, "namespace": namespace, "labels": labels}

    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": metadata,
        "spec": {
            "pipelineRef": {"name": pipeline},
            "taskRunTemplate": {
                "podTemplate": pod_template
            },
            "params": [
                {"name": "git-url", "value": GITEA_URL},
                {"name": "git-revision", "value": "main"},
                {"name": "env", "value": env},
                {"name": "image-tag", "value": name},
            ],
            "workspaces": [
                {
                    "name": "shared-data",
                    "volumeClaimTemplate": {
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "storageClassName": "standard",
                            "resources": {"requests": {"storage": "1Gi"}},
                        }
                    },
                },
            ],
        },
    }


def create_pr(namespace: str, name_prefix: str, env: str = "dev",
              urgent: bool = False, idx: int = None, generate_name: bool = False,
              pipeline: str = PIPELINE_NAME, scheduler_name: str = "") -> str:
    ts = datetime.datetime.now().strftime("%H%M%S%f")
    # 버스트 시 동일 마이크로초 충돌 방지: 인덱스 suffix
    suffix = ts if idx is None else f"{ts}-{idx}"
    name = f"{name_prefix}-{suffix}"
    body = _build_pipelinerun(namespace, name, env, urgent, generate_name, pipeline,
                              scheduler_name)

    # ── 웹훅 일시 실패 재시도 ──────────────────────────────────────
    # 큐 컨트롤러 웹훅은 드물게(실측 1,129건 중 1건, 0.09%) 5초 타임아웃을 넘긴다.
    # `failurePolicy: Fail` + 복제본 1 이라 그 순간 생성이 500 으로 거부되고,
    # 재시도가 없으면 **부하 발생기가 죽어 회차 전체가 날아간다**
    # (2026-07-30 S1 run3: EOF / 2026-07-31 S0 run1: context deadline exceeded — 둘 다 회차 소실).
    #
    # 이 재시도는 **부하 발생기 쪽 견고성**이며 측정 대상(컨트롤러)을 바꾸지 않는다.
    # 실제 CI 클라이언트도 API 서버의 500 은 재시도한다.
    # 재시도 건수는 stderr 에 남겨 사후에 집계한다(웹훅 가용성은 그 자체가 §5 관측 대상).
    last_exc = None
    for attempt in range(1, WEBHOOK_RETRIES + 1):
        try:
            resp = _get_custom().create_namespaced_custom_object(
                group="tekton.dev", version="v1", namespace=namespace,
                plural="pipelineruns", body=body,
            )
            if attempt > 1:
                print(f"[재시도 성공] {attempt}번째 시도에서 생성됨", file=sys.stderr, flush=True)
            break
        except ApiException as e:
            # 5xx 만 재시도한다. 4xx(스키마 오류·권한 등)는 재시도해도 같으므로 즉시 올린다.
            if e.status is None or e.status < 500:
                raise
            last_exc = e
            if attempt == WEBHOOK_RETRIES:
                print(f"[재시도 소진] {WEBHOOK_RETRIES}회 모두 실패", file=sys.stderr, flush=True)
                raise
            wait = WEBHOOK_BACKOFF * (2 ** (attempt - 1))
            print(f"[웹훅 일시 실패] status={e.status} {attempt}/{WEBHOOK_RETRIES} "
                  f"— {wait:.1f}초 후 재시도", file=sys.stderr, flush=True)
            time.sleep(wait)

    actual = resp.get("metadata", {}).get("name", name) if generate_name else name
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] created {namespace}/{actual} env={env}", flush=True)
    return actual


def burst(namespace: str, count: int, env: str = "dev", interval_sec: float = 0.0,
          generate_name: bool = False, pipeline: str = PIPELINE_NAME,
          scheduler_name: str = "", parallel: int = 1, urgent: bool = False):
    """count개를 interval_sec 간격으로 생성한다(단일 env).

    parallel > 1 이면 **동시 요청**으로 생성한다(A1′ 전용).

    ※ 왜 병렬 옵션이 필요한가 (2026-08-03 진단):
      생성은 웹훅을 **동기 호출**하고, 인가 판정(인포머 캐시 조회)이 그 웹훅 안에서 일어난다.
      따라서 직렬 생성에서는 다음 요청이 도착하기 전에 이전 인가가 이미 끝나 있어
      **«인포머 지연 창»이 요청 사이에 존재할 수 없다.** interval 을 아무리 줄여도 마찬가지다
      (--interval 0 실측: 건당 244ms, 초당 4.1건, 동시 실행 최대 30 = 초과 없음).
      순간 상한 초과를 노출하려면 **여러 요청이 동시에 웹훅에 도달**해야 한다.
      기본값 1 은 기존 동작과 동일하므로 다른 조건의 회차는 영향받지 않는다.
    """
    if parallel <= 1:
        for i in range(count):
            create_pr(namespace, "pr", env=env, idx=i, generate_name=generate_name,
                      pipeline=pipeline, scheduler_name=scheduler_name, urgent=urgent)
            if interval_sec > 0 and i < count - 1:
                time.sleep(interval_sec)
        return

    from concurrent.futures import ThreadPoolExecutor
    print(f"# 병렬 생성: parallel={parallel} (동시 인가 요청으로 경쟁 조건 노출)", flush=True)

    def _one(i):
        try:
            return create_pr(namespace, "pr", env=env, idx=i, generate_name=generate_name,
                             pipeline=pipeline, scheduler_name=scheduler_name, urgent=urgent)
        except Exception as e:          # 개별 실패가 전체를 죽이지 않도록
            print(f"[생성 실패] idx={i}: {e}", flush=True)
            return None

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        list(ex.map(_one, range(count)))


def steady(namespace: str, rate_per_min: float, duration_min: float,
           env_dist: dict = None, arrival: str = "periodic", seed: int = 42,
           generate_name: bool = False, pipeline: str = PIPELINE_NAME,
           scheduler_name: str = ""):
    """스케줄을 산출한 뒤 오프셋에 맞춰 생성한다(재현성)."""
    if env_dist is None:
        env_dist = dict(ENV_DIST_DEFAULT)
    schedule = plan_schedule(rate_per_min, duration_min, env_dist, arrival, seed)
    print(f"[부하발생기] seed={seed} arrival={arrival} rate={rate_per_min}/분 "
          f"duration={duration_min}분 dist={env_dist} generateName={generate_name} "
          f"pipeline={pipeline} scheduler={scheduler_name or '-'} "
          f"→ {len(schedule)}개 예정", flush=True)
    start = time.time()
    for i, (offset, env) in enumerate(schedule):
        due = start + offset
        wait = due - time.time()
        if wait > 0:
            time.sleep(wait)
        create_pr(namespace, "pr", env=env, idx=i, generate_name=generate_name,
                  pipeline=pipeline, scheduler_name=scheduler_name)


def _print_plan(schedule, env_dist):
    from collections import Counter
    counts = Counter(env for _, env in schedule)
    print(f"# 예정 {len(schedule)}개 | env 분포: {dict(counts)}")
    for i, (offset, env) in enumerate(schedule):
        print(f"{i:4d}  t={offset:8.3f}s  env={env}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="default-cicd")
    parser.add_argument("--mode", choices=["burst", "steady"], required=True)
    parser.add_argument("--count", type=int, default=10, help="burst: 생성 개수")
    parser.add_argument("--interval", type=float, default=0.0, help="burst: PR 간 간격(초)")
    parser.add_argument("--parallel", type=int, default=1,
                        help="burst: 동시 생성 스레드 수(기본 1=직렬, 기존 동작). "
                             ">1 이면 동시 인가 요청을 만든다 — A1′ 순간 상한 초과 관측용")
    parser.add_argument("--rate", type=float, default=1.0, help="steady: 분당 생성 수")
    parser.add_argument("--duration", type=float, default=30.0, help="steady: 지속 시간(분)")
    parser.add_argument("--env", default="dev", help="burst: env 라벨")
    parser.add_argument("--seed", type=int, default=42, help="난수 시드(재현성)")
    parser.add_argument("--urgent", action="store_true",
                        help="burst: queue.tekton.dev/urgent=true 라벨 부여 → Tier 0(긴급). "
                             "기본값 off 이므로 기존 회차의 동작은 바뀌지 않는다")
    parser.add_argument("--arrival", choices=["periodic", "poisson"], default="periodic",
                        help="steady: 도착 모델")
    parser.add_argument("--env-dist", default="", help="steady: 'prod:0.1,stg:0.2,dev:0.7'")
    parser.add_argument("--plan", action="store_true",
                        help="steady: 실제 생성 없이 스케줄만 출력(dry-run·재현 검증)")
    parser.add_argument("--generate-name", action="store_true",
                        help="burst·steady 공통: metadata.name 대신 generateName 사용"
                             "(phantom 미삽입 → 운영 정합·A1' 격리용)")
    parser.add_argument("--pipeline", default=PIPELINE_NAME,
                        help="pipelineRef 이름(측정용: petclinic-build-measure, Volcano: petclinic-build-volcano)")
    parser.add_argument("--scheduler-name", default="",
                        help="태스크 파드 schedulerName 주입(Volcano 비교군: volcano)")
    args = parser.parse_args()

    if args.mode == "burst":
        if args.plan:
            print(f"# burst {args.count}개 env={args.env} interval={args.interval}s parallel={args.parallel} "
                  f"generateName={args.generate_name} pipeline={args.pipeline} "
                  f"scheduler={args.scheduler_name or '-'}")
        else:
            burst(args.namespace, args.count, args.env, args.interval, args.generate_name,
                  args.pipeline, args.scheduler_name, args.parallel, args.urgent)
    else:
        env_dist = parse_env_dist(args.env_dist)
        if args.plan:
            sched = plan_schedule(args.rate, args.duration, env_dist, args.arrival, args.seed)
            _print_plan(sched, env_dist)
        else:
            steady(args.namespace, args.rate, args.duration, env_dist, args.arrival, args.seed,
                   args.generate_name, args.pipeline, args.scheduler_name)
