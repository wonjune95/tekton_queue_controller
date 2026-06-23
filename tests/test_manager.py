"""manager.py 단위 테스트 — 스케줄링 루프 로직."""
import time
from unittest.mock import patch, MagicMock, call
from kubernetes.client.rest import ApiException

import src.state as state
import src.cache as cache
import src.config as config
from src.workers.manager import manager_loop
from tests.conftest import make_pr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) 리더 체크
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerLeaderCheck:
    @patch('src.workers.manager.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.manager.load_crd_config')
    def test_skips_when_not_leader(self, mock_crd, mock_sleep):
        """리더가 아니면 스케줄링하지 않는다."""
        state.is_leader = False
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_crd.assert_not_called()

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.cache.core_api')
    def test_runs_when_leader(self, mock_api, mock_crd, mock_sleep):
        from src.workers.manager import manager_loop as ml
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        try:
            ml()
        except InterruptedError:
            pass
        mock_crd.assert_called()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) 스케줄링 로직
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerScheduling:
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_schedules_pending(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """가용 슬롯이 있으면 Pending PR을 스케줄링한다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_api.patch_namespaced_custom_object.assert_called_once_with(
            'tekton.dev', 'v1', 'test-cicd', 'pipelineruns', 'p1',
            {'spec': {'status': None}}
        )

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=1)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_respects_limit(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """쿼터가 꽉 차면 스케줄링하지 않는다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_api.patch_namespaced_custom_object.assert_not_called()

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_patch_failure_continues(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """패치 실패해도 Manager 루프가 크래시하지 않는다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        mock_api.patch_namespaced_custom_object.side_effect = ApiException(status=500)
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        # 크래시 없이 완료

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=2)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_schedules_only_available_slots(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """슬롯 수만큼만 스케줄링한다 (초과 방지)."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        for i in range(5):
            cache.local_cache[f"test-cicd/p{i}"] = make_pr(
                name=f"p{i}", spec_status="PipelineRunPending",
                tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        assert mock_api.patch_namespaced_custom_object.call_count == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3) admitted 카운터와 슬롯 계산 통합
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerAdmittedCounter:
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=3)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_admitted_reduces_available_slots(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """running(1) + admitted(2) = limit(3) → 가용 슬롯 없어 스케줄링 안 함."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '2'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending", tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_api.patch_namespaced_custom_object.assert_not_called()

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=5)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_admitted_partial_blocks_extra_slots(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """running(1) + admitted(2) = 3, limit=5 → 슬롯 2개만 스케줄링한다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '2'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        for i in range(5):
            cache.local_cache[f"test-cicd/p{i}"] = make_pr(
                name=f"p{i}", spec_status="PipelineRunPending", tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        assert mock_api.patch_namespaced_custom_object.call_count == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4) 스케줄링 후 로컬 캐시 업데이트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerCacheUpdate:
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_cache_updated_after_scheduling(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """스케줄링 성공 후 로컬 캐시의 spec.status가 None으로 갱신된다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending", tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        assert cache.local_cache["test-cicd/p1"]['spec']['status'] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5) 우선순위 기반 스케줄링 순서
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerSchedulingPriority:
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=2)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_higher_tier_scheduled_first(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """슬롯 1개일 때 Tier 1이 Tier 3보다 먼저 스케줄링된다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")  # running=1, slot=1
        cache.local_cache["test-cicd/low"] = make_pr(
            name="low", spec_status="PipelineRunPending", tier=3, managed=True, creation_ts=now)
        cache.local_cache["test-cicd/high"] = make_pr(
            name="high", spec_status="PipelineRunPending", tier=1, managed=True, creation_ts=now)
        try:
            manager_loop()
        except InterruptedError:
            pass
        calls = mock_api.patch_namespaced_custom_object.call_args_list
        assert len(calls) == 1
        assert calls[0][0][4] == 'high'

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_fifo_within_same_tier(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """같은 Tier 내에서는 먼저 생성된 PR이 먼저 스케줄링된다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/old"] = make_pr(
            name="old", spec_status="PipelineRunPending", tier=2, managed=True,
            creation_ts="2025-01-01T00:00:00Z")
        cache.local_cache["test-cicd/new"] = make_pr(
            name="new", spec_status="PipelineRunPending", tier=2, managed=True,
            creation_ts="2025-01-01T01:00:00Z")
        try:
            manager_loop()
        except InterruptedError:
            pass
        calls = mock_api.patch_namespaced_custom_object.call_args_list
        assert calls[0][0][4] == 'old'
        assert calls[1][0][4] == 'new'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5-1) admitted 쿼터 누수 자가 치유(self-healing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerAdmittedSelfHealing:
    def _time_counter(self, step=100.0):
        """호출마다 step초씩 증가하는 시각을 반환하는 side_effect."""
        state_box = {'t': 0.0}
        def _tick():
            state_box['t'] += step
            return state_box['t']
        return _tick

    @patch('src.workers.manager._reset_global_admitted')
    @patch('src.workers.manager.time.time')
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_leak_healed_after_threshold(self, mock_cache_api, mock_api, mock_crd,
                                         mock_sleep, mock_time, mock_reset):
        """running=0 인데 admitted>0 상태가 임계 시간 이상 지속되면 카운터를 보정한다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '2'}  # 붕 뜬 카운터 (실행 중 PR은 0개)
        mock_cache_api.read_namespaced_config_map.return_value = cm
        mock_time.side_effect = self._time_counter(step=100.0)  # 매 호출 +100s
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_reset.assert_called_once()

    @patch('src.workers.manager._reset_global_admitted')
    @patch('src.workers.manager.time.time')
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_leak_not_healed_before_threshold(self, mock_cache_api, mock_api, mock_crd,
                                              mock_sleep, mock_time, mock_reset):
        """임계 시간 미만(짧은 in-flight)에는 보정하지 않는다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '2'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        mock_time.side_effect = self._time_counter(step=1.0)  # 매 호출 +1s (누적 < 30s)
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_reset.assert_not_called()

    @patch('src.workers.manager._reset_global_admitted')
    @patch('src.workers.manager.time.time')
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_no_heal_when_running_present_and_slots_free(self, mock_cache_api, mock_api, mock_crd,
                                                         mock_sleep, mock_time, mock_reset):
        """실행 중 PR이 있고 슬롯도 남으며 대기 작업이 없으면 보정하지 않는다 (정상 상태)."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '2'}  # running(1)+admitted(2)=3 < limit(10), pending 없음
        mock_cache_api.read_namespaced_config_map.return_value = cm
        mock_time.side_effect = self._time_counter(step=100.0)
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")  # running=1
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_reset.assert_not_called()

    @patch('src.workers.manager._reset_global_admitted')
    @patch('src.workers.manager.time.time')
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_partial_leak_starvation_healed(self, mock_cache_api, mock_api, mock_crd,
                                            mock_sleep, mock_time, mock_reset):
        """부분 누수: running>0 이어도 누수로 슬롯이 막히고 대기 작업이 있으면 보정한다.

        running(2) + admitted(8) = 10 = limit → available_slots=0, pending 대기.
        running != 0 이므로 idle 게이트는 안 열리지만, starvation 게이트가 잡아야 한다.
        """
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '8'}  # 누수된 카운터 (실제 그 8개는 존재하지 않음)
        mock_cache_api.read_namespaced_config_map.return_value = cm
        mock_time.side_effect = self._time_counter(step=100.0)
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")  # running
        cache.local_cache["test-cicd/r2"] = make_pr(name="r2")  # running (총 running=2)
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending", tier=2, managed=True)  # pending
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_reset.assert_called_once()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6) 일반 예외 처리
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerGenericException:
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', side_effect=RuntimeError("unexpected error"))
    def test_generic_exception_no_crash(self, mock_crd, mock_sleep):
        """CRD 로드 중 일반 예외가 발생해도 Manager 루프가 크래시하지 않는다."""
        state.is_leader = True
        try:
            manager_loop()
        except InterruptedError:
            pass
