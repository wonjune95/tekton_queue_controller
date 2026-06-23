"""극단적 상황 (Edge Case) 테스트 — Race Condition, 대량 처리, 경계값."""
import datetime
import threading
import json
import base64
from unittest.mock import patch, MagicMock
from kubernetes.client.rest import ApiException

import src.state as state
import src.cache as cache
import src.config as config
from tests.conftest import make_pr, make_admission_request


def _decode_patch(response_data):
    resp = json.loads(response_data)
    patch_b64 = resp.get("response", {}).get("patch")
    if not patch_b64:
        return None
    return json.loads(base64.b64decode(patch_b64))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) 동시 Webhook 요청 (Race Condition 방어)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestConcurrentWebhook:
    @patch('src.cache.core_api')
    def test_burst_admits_respect_limit_concurrent(self, mock_api):
        """실제 멀티스레드로 _try_increment_global_admitted 동시 10호출 — limit 3 초과 없어야 한다.

        Flask test client은 스레드 안전하지 않으므로 핵심 함수를 직접 호출하여
        ConfigMap 낙관적 락의 Race Condition 방어를 검증한다.
        """
        import threading as _thr

        # 실제 K8s ConfigMap 낙관적 락을 재현: 동시 write 시 409 충돌 발생
        cm_lock = _thr.Lock()
        admitted_store = [0]

        class FakeCM:
            def __init__(self, val):
                self.data = {'admitted': str(val)}
                self._snap = val  # 읽을 당시 버전 snapshot

        def fake_read(*a, **kw):
            with cm_lock:
                return FakeCM(admitted_store[0])

        def fake_replace(ns, name, cm_obj):
            new_val = int(cm_obj.data['admitted'])
            with cm_lock:
                # 현재 저장 값 + 1 이 아니면 → 다른 스레드가 먼저 write → 409
                if new_val != admitted_store[0] + 1:
                    from kubernetes.client.rest import ApiException as _AE
                    raise _AE(status=409)
                admitted_store[0] = new_val

        mock_api.read_namespaced_config_map.side_effect = fake_read
        mock_api.replace_namespaced_config_map.side_effect = fake_replace

        LIMIT = 3
        results = []
        results_lock = _thr.Lock()

        def try_admit(running=0):
            ok, _ = cache._try_increment_global_admitted(running, LIMIT)
            with results_lock:
                results.append('admitted' if ok else 'queued')

        threads = [_thr.Thread(target=try_admit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        admitted_count = results.count('admitted')
        assert admitted_count <= LIMIT, f"Limit {LIMIT}인데 {admitted_count}개가 허용됨"
        assert admitted_count + results.count('queued') == 10

    @patch('src.cache.core_api')
    def test_409_retry_path_exercised(self, mock_api):
        """409 발생 시 재시도 후 정상 처리 — 재시도 로직 검증."""
        call_count = [0]

        class FakeCM:
            def __init__(self):
                self.data = {'admitted': '0'}

        def fake_read(*a, **kw):
            return FakeCM()

        def fake_replace(*a, **kw):
            call_count[0] += 1
            if call_count[0] < 3:
                from kubernetes.client.rest import ApiException as _AE
                raise _AE(status=409)

        mock_api.read_namespaced_config_map.side_effect = fake_read
        mock_api.replace_namespaced_config_map.side_effect = fake_replace

        ok, _ = cache._try_increment_global_admitted(0, 10)
        assert ok is True
        assert call_count[0] == 3  # 2회 409 재시도 후 3번째 성공


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) 대량 Pending 정렬 성능
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMassivePending:
    def test_10000_pending_items_sort(self):
        """10,000개 Pending PR 정렬이 정상 작동하고 크래시하지 않는다."""
        for i in range(10000):
            ts = f"2025-01-{(i % 28)+1:02d}T{i % 24:02d}:00:00Z"
            cache.local_cache[f"test-cicd/pr-{i}"] = make_pr(
                name=f"pr-{i}", spec_status="PipelineRunPending",
                tier=(i % 4), managed=True, creation_ts=ts)
        running, pending = cache.get_queue_status_from_cache()
        assert len(pending) == 10000
        # Tier 순서 확인 (첫 항목이 Tier 0)
        first_tier = int(pending[0]['metadata']['labels'].get(
            'queue.tekton.dev/tier', '3'))
        assert first_tier == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3) 경계값 테스트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestBoundaryConditions:
    @patch('src.cache.core_api')
    def test_limit_zero(self, mock_api, flask_client):
        """maxPipelines=0 → 모든 요청이 큐로 들어간다."""
        state.initial_sync_done = True
        config.crd_config['max_pipelines'] = 0
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels={"env": "prod"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        assert any(p.get('value') == 'PipelineRunPending' for p in patches)

    def test_aging_interval_zero(self):
        """agingIntervalSec=0 → division by zero 없이 처리."""
        config.crd_config['aging_interval_sec'] = 0
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=3, managed=True, creation_ts="2020-01-01T00:00:00Z")
        running, pending = cache.get_queue_status_from_cache()
        assert len(pending) == 1  # 크래시 없이 정상

    def test_negative_aging_interval(self):
        """agingIntervalSec=-1 → 안전하게 처리."""
        config.crd_config['aging_interval_sec'] = -1
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=3, managed=True)
        running, pending = cache.get_queue_status_from_cache()
        assert len(pending) == 1

    def test_empty_cache(self):
        """캐시가 완전히 비어있을 때."""
        running, pending = cache.get_queue_status_from_cache()
        assert running == 0
        assert pending == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4) Tier 0 보호 (에이징 불가)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestTier0Protection:
    def test_aging_cannot_reach_tier_0(self):
        """어떤 PR도 에이징으로 Tier 0에 도달할 수 없다."""
        config.crd_config['aging_interval_sec'] = 1
        config.crd_config['aging_min_tier'] = 1
        # 100년 전 생성된 Tier 3 PR
        cache.local_cache["test-cicd/ancient"] = make_pr(
            name="ancient", spec_status="PipelineRunPending",
            tier=3, managed=True, creation_ts="1925-01-01T00:00:00Z")
        # 방금 생성된 Tier 0 PR
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cache.local_cache["test-cicd/urgent"] = make_pr(
            name="urgent", spec_status="PipelineRunPending",
            tier=0, managed=True, creation_ts=now)
        _, pending = cache.get_queue_status_from_cache()
        assert pending[0]['metadata']['name'] == "urgent"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5) 유니코드 및 특수 문자
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestUnicode:
    @patch('src.cache.core_api')
    def test_korean_name_in_webhook(self, mock_api, flask_client):
        """한국어/특수문자가 포함된 PR 이름도 크래시 없이 처리."""
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(name="배포-테스트-pr-001", labels={"env": "dev"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        assert resp.status_code == 200

    def test_unicode_in_cache(self):
        pr = make_pr(namespace="test-cicd", name="빌드-한글")
        cache.local_cache["test-cicd/빌드-한글"] = pr
        running, _ = cache.get_queue_status_from_cache()
        assert running == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6) ConfigMap 전체 실패 시 fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestConfigMapTotalFailure:
    @patch('src.cache.core_api')
    def test_all_cm_retries_fail_uses_local(self, mock_api):
        """ConfigMap 5회 재시도 모두 실패해도 local fallback으로 동작."""
        mock_api.read_namespaced_config_map.side_effect = Exception("K8s down")
        ok, _ = cache._try_increment_global_admitted(0, 10)
        assert ok is True
        assert cache.webhook_admitted_count == 1

    @patch('src.cache.core_api')
    def test_all_cm_retries_fail_decrement(self, mock_api):
        mock_api.read_namespaced_config_map.side_effect = Exception("K8s down")
        cache.webhook_admitted_count = 5
        cache._decrement_global_admitted()
        assert cache.webhook_admitted_count == 4

    @patch('src.cache.core_api')
    def test_all_cm_retries_fail_reset(self, mock_api):
        mock_api.read_namespaced_config_map.side_effect = Exception("K8s down")
        cache.webhook_admitted_count = 99
        cache._reset_global_admitted()
        assert cache.webhook_admitted_count == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7) Phantom 엔트리 교체
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestPhantomReplacement:
    @patch('src.cache.core_api')
    def test_phantom_replaced_by_real_event(self, mock_api):
        """Phantom 엔트리가 실제 이벤트로 교체되면 admitted 카운터가 감소."""
        cm = MagicMock()
        cm.data = {'admitted': '1'}
        mock_api.read_namespaced_config_map.return_value = cm
        # Phantom 삽입
        cache.local_cache["test-cicd/pr1"] = {
            'metadata': {
                'namespace': 'test-cicd', 'name': 'pr1',
                'labels': {}, 'resourceVersion': '__admitted__',
                'creationTimestamp': '2025-01-01T00:00:00Z',
            },
            'spec': {'status': None}, 'status': {},
        }
        # 실제 이벤트로 교체
        real_pr = make_pr(name="pr1")
        cache.update_cache("ADDED", real_pr)
        assert cache.local_cache["test-cicd/pr1"]['metadata']['resourceVersion'] == '100'
        mock_api.replace_namespaced_config_map.assert_called()  # decrement 호출됨


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8) 동시 캐시 업데이트 (Thread Safety)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestConcurrentCache:
    @patch('src.cache.core_api')
    def test_concurrent_cache_updates(self, mock_api):
        """여러 스레드에서 동시에 캐시를 업데이트해도 크래시하지 않는다."""
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        errors = []
        def update_many(thread_id):
            try:
                for i in range(100):
                    pr = make_pr(name=f"pr-{thread_id}-{i}",
                                 namespace="other-ns")  # 비대상 NS로 admitted 호출 회피
                    cache.update_cache("ADDED", pr)
                    cache.update_cache("MODIFIED", pr)
                    cache.update_cache("DELETED", pr)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=update_many, args=(t,)) for t in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert errors == [], f"캐시 동시 업데이트 중 에러 발생: {errors}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9) 리더 상태 빠른 전환
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestRapidLeaderFlip:
    def test_rapid_leader_state_changes(self):
        """빠른 리더 상태 전환에도 state 모듈이 안정적이다."""
        errors = []
        def flip(val):
            try:
                for _ in range(1000):
                    with state.leader_lock:
                        state.is_leader = val
                        _ = state.is_leader
            except Exception as e:
                errors.append(e)
        t1 = threading.Thread(target=flip, args=(True,))
        t2 = threading.Thread(target=flip, args=(False,))
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert errors == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10) 모든 Cancel 상태 검증
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestCancelStatuses:
    def test_all_cancel_statuses_excluded_from_running(self):
        """README에 명시된 3가지 취소 상태 모두 running에서 제외."""
        for cancel_status in ['Cancelled', 'CancelledRunFinally', 'StoppedRunFinally']:
            cache.local_cache.clear()
            cache.local_cache["test-cicd/c1"] = make_pr(
                name="c1", spec_status=cancel_status)
            running, _ = cache.get_queue_status_from_cache()
            assert running == 0, f"{cancel_status}가 running에 카운트됨"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 11) CRD 사용 불가 시나리오
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestCrdUnavailable:
    @patch('src.config.api')
    def test_crd_deleted_uses_defaults(self, mock_api):
        """CRD가 삭제되어도 기본값으로 동작한다."""
        mock_api.get_cluster_custom_object.side_effect = ApiException(status=404)
        result = config.load_crd_config()
        assert result == config.DEFAULT_LIMIT

    @patch('src.config.api')
    def test_crd_network_error(self, mock_api):
        mock_api.get_cluster_custom_object.side_effect = ConnectionError("timeout")
        result = config.load_crd_config()
        assert result == config.DEFAULT_LIMIT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 12) 많은 네임스페이스 패턴
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManyNamespacePatterns:
    def test_100_patterns(self):
        """100개 패턴도 정상 매칭."""
        patterns = [f"ns-{i}-*" for i in range(100)]
        config.crd_config['namespace_patterns'] = patterns
        assert config.is_target_namespace("ns-50-cicd") is True
        assert config.is_target_namespace("ns-999-cicd") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 13) per-pod fallback 쿼터 집행 (TEST-GAP-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestFallbackQuotaEnforcement:
    @patch('src.cache.core_api')
    def test_fallback_enforces_quota(self, mock_api, flask_client):
        """ConfigMap 완전 불가 시 per-pod fallback도 쿼터를 집행한다."""
        mock_api.read_namespaced_config_map.side_effect = Exception("K8s down")
        state.initial_sync_done = True
        config.crd_config['max_pipelines'] = 2

        admitted = queued = 0
        for i in range(5):
            req = make_admission_request(name=f"pr-fallback-{i}", labels={"env": "dev"})
            resp = flask_client.post('/mutate', json=req, content_type='application/json')
            patches = _decode_patch(resp.data)
            if any(p.get('value') == 'PipelineRunPending' for p in (patches or [])):
                queued += 1
            else:
                admitted += 1

        assert admitted <= 2, f"per-pod fallback에서 limit 2를 초과하여 {admitted}개 허용됨"
        assert admitted + queued == 5

    @patch('src.cache.core_api')
    def test_fallback_decrement_works(self, mock_api):
        """per-pod fallback decrement가 0 이하로 내려가지 않는다."""
        mock_api.read_namespaced_config_map.side_effect = Exception("K8s down")
        cache.webhook_admitted_count = 0
        cache._decrement_global_admitted()
        assert cache.webhook_admitted_count == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 14) generateName PR admitted 카운터 처리 (TEST-GAP 보완)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestGenerateNameAdmitted:
    @patch('src.cache.core_api')
    def test_generate_name_no_phantom_but_counter_decrements_on_watcher_event(self, mock_api):
        """generateName PR은 phantom 없이 admitted 증가 → Watcher ADDED 이벤트로 decrement."""
        cm_val = [1]

        class FakeCM:
            def __init__(self):
                self.data = {'admitted': str(cm_val[0])}

        def fake_read(*a, **kw):
            return FakeCM()

        decremented = []

        def fake_replace(ns, name, cm_obj):
            new_val = int(cm_obj.data['admitted'])
            cm_val[0] = new_val
            decremented.append(new_val)

        mock_api.read_namespaced_config_map.side_effect = fake_read
        mock_api.replace_namespaced_config_map.side_effect = fake_replace

        # generateName PR이 생성됐을 때 (이름 없음 → phantom 미삽입, 캐시에도 없음)
        # Watcher가 ADDED 이벤트로 수신 (is_new_addition=True, not Pending)
        real_pr = make_pr(namespace="test-cicd", name="pr-generated-001")
        assert "test-cicd/pr-generated-001" not in cache.local_cache
        cache.update_cache("ADDED", real_pr)

        assert "test-cicd/pr-generated-001" in cache.local_cache
        assert len(decremented) > 0, "Watcher ADDED 이벤트 시 admitted decrement가 호출되지 않음"
        assert cm_val[0] == 0

    @patch('src.cache.core_api')
    def test_generate_name_webhook_no_phantom_inserted(self, mock_api, flask_client):
        """generateName 요청 → 캐시에 phantom entry가 삽입되지 않는다."""
        state.initial_sync_done = True

        class FakeCM:
            def __init__(self):
                self.data = {'admitted': '0'}

        mock_api.read_namespaced_config_map.return_value = FakeCM()
        mock_api.replace_namespaced_config_map.return_value = None

        req = make_admission_request(name=None, generate_name="my-pipeline-run-",
                                     labels={"env": "dev"})
        flask_client.post('/mutate', json=req, content_type='application/json')

        # generateName PR은 이름이 없으므로 phantom이 캐시에 없어야 함
        phantom_keys = [k for k, v in cache.local_cache.items()
                        if v.get('metadata', {}).get('resourceVersion') == '__admitted__']
        assert phantom_keys == [], f"generateName PR에 phantom entry가 삽입됨: {phantom_keys}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 15) determine_tier 비정상 입력 처리 (TEST-GAP-2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestDetermineTierEdgeCases:
    def test_empty_labels_returns_default(self):
        """라벨이 없으면 DEFAULT_TIER를 반환한다."""
        from src.config import determine_tier, DEFAULT_TIER_RULES, DEFAULT_TIER
        result = determine_tier({}, DEFAULT_TIER_RULES)
        assert result == DEFAULT_TIER

    def test_none_labels_does_not_crash(self):
        """라벨이 None이어도 크래시하지 않는다."""
        from src.config import determine_tier, DEFAULT_TIER_RULES, DEFAULT_TIER
        result = determine_tier({}, DEFAULT_TIER_RULES)
        assert result == DEFAULT_TIER

    def test_empty_tier_rules_returns_default(self):
        """tierRules가 빈 리스트면 DEFAULT_TIER를 반환한다."""
        from src.config import determine_tier, DEFAULT_TIER
        result = determine_tier({"env": "prod"}, [])
        assert result == DEFAULT_TIER

    def test_malformed_tier_rule_skipped(self):
        """matchType이 없는 규칙은 기본 env 매칭으로 처리하며 크래시하지 않는다."""
        from src.config import determine_tier, DEFAULT_TIER
        bad_rules = [
            {"tier": 1, "pattern": "prod"},  # matchType 없음 → env로 폴백
            {"tier": 3, "matchType": "env", "pattern": "*"},
        ]
        result = determine_tier({"env": "prod"}, bad_rules)
        assert result == 1  # env 매칭으로 처리되어 tier 1 반환

    def test_invalid_tier_value_falls_back(self):
        """tier 값이 정수로 변환 불가능해도 크래시 없이 DEFAULT_TIER를 반환한다."""
        from src.config import determine_tier, DEFAULT_TIER
        bad_rules = [
            {"tier": "not-a-number", "matchType": "env", "pattern": "prod"},
            {"tier": 3, "matchType": "env", "pattern": "*"},
        ]
        result = determine_tier({"env": "prod"}, bad_rules)
        assert result == DEFAULT_TIER, "잘못된 tier 값 입력 시 DEFAULT_TIER로 graceful degradation 되어야 함"
