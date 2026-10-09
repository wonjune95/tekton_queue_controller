#!/bin/bash
# 전체 실험 자동 실행 오케스트레이터
#
#   bash run_all.sh                      # 비파괴 전 조건, 조건별 계획 반복수까지
#   bash run_all.sh --only s1,s2         # 청크 실행(일부 조건만)
#   bash run_all.sh --max-run 2          # 이번 세션에서 반복 1~2회분만 (이어하기)
#   ALLOW_DESTRUCTIVE=1 bash run_all.sh --only a1,a0nr   # 파괴적 조건(명시 승인 필요)
#
# ■ 조건별 반복수(N)를 개별 지정한다. 예전에는 단일 REPEATS 를 모든 조건에 적용해
#   파괴적 조건(A0-NR)까지 계획(N3)보다 많이 실행되는 문제가 있었다.
# ■ 반복 라운드로빈 유지: 한 반복 안에서 모든 조건을 한 번씩 → 시간에 따른 환경 드리프트가
#   특정 조건에 몰리지 않는다(조건 간 비교의 교란 완화).
# ■ 비파괴 먼저 → 파괴적 최후. 파괴적 조건은 ALLOW_DESTRUCTIVE=1 없이는 실행되지 않는다
#   (프로젝트 안전 규칙: 파괴적 실험 임의 실행 금지).
# ■ 메인 부하는 generateName 통일(운영 정합). 예외: V-phantom 만 named(phantom 경로 노출).
# ※ S0(baseline)은 signature가 달라(run.sh [mode] [run]) 자동 루프에서 제외 — 별도 수행(N3).
# ※ 구 A1(L_max=999)은 A0-R 과 주장 중복으로 제외(2026-07-28). A1 = admitted 카운터 격리(구 A1′).

set -e
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
COMMON="$TESTDIR/common"
RESULTS="$TESTDIR/results"
LOG="$RESULTS/run_all.log"

# ── 조건별 계획 반복수 (HANDOVER §5 매트릭스와 일치시킬 것) ────────
N_S1=5; N_S2=5; N_S3=5; N_A2=5; N_A3=3; N_V=2; N_A1=3; N_A0NR=3

# ── 캠페인 파이프라인 (HANDOVER §4 확정: 본실험·비교군 전부 requests 설정 조건) ──
# pr_create.py 가 EXP_PIPELINE 을 기본 파이프라인으로 사용한다. 이를 지정하지 않으면
# requests 없는 기본 파이프라인으로 돌아가 A0-NR 과 조건이 뒤섞인다(조용한 오염).
export EXP_PIPELINE="${EXP_PIPELINE:-petclinic-build-requests}"

ONLY=""; MAX_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)    ONLY=",$2,"; shift 2 ;;
    --max-run) MAX_RUN="$2"; shift 2 ;;
    *) echo "알 수 없는 인자: $1"; exit 1 ;;
  esac
done

# 조건 실행 여부: 필터 통과 + 계획 반복수 이내 + (파괴적이면) 명시 승인
should_run() {         # should_run <key> <planN> <run> [destructive]
  local key="$1" planN="$2" run="$3" destructive="${4:-0}"
  [[ -n "$ONLY" && "$ONLY" != *",$key,"* ]] && return 1
  [[ "$run" -gt "$planN" ]] && return 1
  if [[ "$destructive" == "1" && "${ALLOW_DESTRUCTIVE:-0}" != "1" ]]; then
    [[ "$run" -eq 1 ]] && log "[건너뜀] $key 는 파괴적 조건 — ALLOW_DESTRUCTIVE=1 필요"
    return 1
  fi
  return 0
}

# ── 파일럿 원본 보호 ──────────────────────────────────────────────
# results/ 에 기존 CSV(파일럿 원본)가 있으면 삭제·덮어쓰기하지 않고 백업으로 이동한다.
#
# ⚠️ 이 통째 이동은 **캠페인 시작 전 1회**만 유효하다.
#   캠페인은 청크로 나눠 여러 날에 걸쳐 돌리므로, 두 번째 호출부터는 이 로직이
#   **이미 받아 둔 캠페인 결과와 INVALID 증거 디렉터리까지 통째로 옮겨** 데이터가 흩어진다.
#   (2026-07-30: S0 3회를 받은 뒤 `--only s1,s2` 를 호출하려다 발견.)
#   마커 파일이 있으면 캠페인 진행 중으로 보고 건너뛴다.
MARKER="$RESULTS/.campaign_started"
if [[ -f "$MARKER" ]]; then
  echo "[보호] 캠페인 진행 중($(cat "$MARKER")) — 기존 결과를 옮기지 않는다."
elif ls "$RESULTS"/*/*.csv >/dev/null 2>&1; then
  BACKUP="$TESTDIR/results_pilot_$(date +%Y%m%d_%H%M%S)"
  echo "[보호] 기존 결과 발견 → $BACKUP 로 이동(원본 보존, 덮어쓰기 금지)"
  mkdir -p "$BACKUP"
  mv "$RESULTS"/* "$BACKUP"/ 2>/dev/null || true
fi
mkdir -p "$RESULTS"
[[ -f "$MARKER" ]] || date '+%Y-%m-%d %H:%M:%S 캠페인 시작' > "$MARKER"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

cleanup() {
  log "=== 네임스페이스 초기화 ==="
  bash "$COMMON/cleanup.sh" default-cicd 2>&1 | tee -a "$LOG"
  # 초기화가 실패하면 다음 회차에 이전 산출물이 섞인다(조용한 오염). 반드시 드러낸다.
  [[ ${PIPESTATUS[0]} -eq 0 ]] || log "⚠️ 초기화 실패 — 다음 회차가 오염될 수 있다. 잔여 확인 필요."
  sleep 10
}

FAILED_RUNS=()

run_experiment() {
  local dir="$1"; shift
  local name="$(basename "$dir")"
  log ">>> 실험 시작: $name (인자: $*)"
  cd "$TESTDIR/$dir"
  # ⚠️ `... | tee` 는 **파이프의 종료 코드(=tee)** 를 반환해 run.sh 의 실패를 가린다.
  #   2026-07-31 S1 run3 이 웹훅 EOF 로 중단돼 결과 파일이 하나도 없는데도
  #   "<<< 실험 완료" 가 찍히고 다음 조건으로 넘어갔다. 캠페인이 정상 완주한 것처럼 보인다.
  #   PIPESTATUS[0] 로 run.sh 의 실제 종료 코드를 확인한다.
  bash run.sh "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  cd "$TESTDIR"
  if [[ $rc -ne 0 ]]; then
    log "!!! 실험 실패: $name (인자: $*) 종료코드=$rc — 결과가 없거나 불완전하다. 재실행 대상."
    FAILED_RUNS+=("$name($*) rc=$rc")
  else
    log "<<< 실험 완료: $name"
  fi
}

# ── 반복 라운드로빈 (RUN = 반복 번호 = 시드) ──────────────────────
REPEATS=$N_S1
for n in $N_S2 $N_S3 $N_A2 $N_A3 $N_V $N_A1 $N_A0NR; do
  [[ "$n" -gt "$REPEATS" ]] && REPEATS=$n
done
[[ "$MAX_RUN" -gt 0 && "$MAX_RUN" -lt "$REPEATS" ]] && REPEATS=$MAX_RUN

# 캠페인 파이프라인이 클러스터에 존재하는지 먼저 확인한다(없으면 조용히 잘못된 조건으로 도는 것을 방지).
if ! kubectl get pipeline "$EXP_PIPELINE" -n default-cicd >/dev/null 2>&1; then
  echo "[중단] 파이프라인 '$EXP_PIPELINE' 이 default-cicd 에 없습니다."
  echo "       본실험은 requests 설정 조건이어야 합니다(HANDOVER §4). 먼저 생성하세요:"
  echo "         bash param_measure/run.sh 5 45"
  echo "         python3 common/make_requests_pipeline.py --input results/param/mem_peak.csv \\"
  echo "                 --output pipeline/petclinic-build-requests.yaml"
  echo "         kubectl apply -f pipeline/petclinic-build-requests.yaml -n default-cicd"
  echo "       (의도적으로 requests 없이 돌리려면 EXP_PIPELINE=petclinic-build 를 지정하세요.)"
  exit 1
fi

log "계획: S1×$N_S1 S2×$N_S2 S3×$N_S3 A2×$N_A2 A3×$N_A3 V×$N_V A1×$N_A1 A0NR×$N_A0NR"
log "파이프라인: $EXP_PIPELINE (A0-NR 은 petclinic-build 고정)"
[[ -n "$ONLY" ]]                        && log "필터: ${ONLY//,/ }"
[[ "${ALLOW_DESTRUCTIVE:-0}" == "1" ]]  && log "⚠️ 파괴적 조건 실행 승인됨(ALLOW_DESTRUCTIVE=1)"

# ⚠️ 본 루프 **진입 전** 초기화.
#   cleanup 은 각 실험 **뒤에** 호출되므로, 청크의 첫 회차는 네임스페이스에 남아 있던
#   이전 산출물을 그대로 안고 시작한다. metrics_collect.py 는 파이프라인런을
#   시간·이름 필터 없이 전량 수집하므로 이월분이 측정에 섞인다.
#   (2026-07-30: S1 run1 에 S0 run3 의 25건이 유입되어 무효 처리했다.)
log "=== 청크 시작 전 초기화 ==="
cleanup

# Harbor 실험 이미지 정리.
# ttl.sh 와 달리 Harbor 는 자동 삭제되지 않는다. 파이프라인런마다 고유 저장소가 생기므로
# 청크를 거듭하면 registry PVC(200Gi)가 찬다. 청크 **시작 시** 지우고 GC 해 누적을 끊는다.
# (측정 전에 끝나므로 회차에 영향이 없다. GC 중 레지스트리가 잠깐 느려질 수 있어 실행 전에 둔다.)
log "=== Harbor 실험 이미지 정리 ==="
bash "$COMMON/harbor_cleanup.sh" --gc 2>&1 | tee -a "$LOG"
[[ ${PIPESTATUS[0]} -eq 0 ]] || log "⚠️ Harbor 정리 실패 — registry 용량을 직접 확인할 것."

for RUN in $(seq 1 "$REPEATS"); do
  log "########## 반복 $RUN / $REPEATS ##########"
  # ── 비파괴 ──
  should_run s1     "$N_S1"   "$RUN" && { run_experiment s1_peak_hour     "$RUN";   cleanup; }
  should_run s2     "$N_S2"   "$RUN" && { run_experiment s2_release_burst "$RUN";   cleanup; }
  should_run s3     "$N_S3"   "$RUN" && { run_experiment s3_priority   "$RUN" 3; cleanup; }  # 3=λ, S0 실측 후 조정
  should_run a2     "$N_A2"   "$RUN" && { run_experiment a2_no_aging      "$RUN";   cleanup; }
  should_run a3     "$N_A3"   "$RUN" && { run_experiment a3_single_fifo   "$RUN" 3; cleanup; }  # 3=λ, S3 와 동기화
  # phantom 경로 검증(named, 카운터 off): 비파괴 기대(초과 미발생)이나 카운터 격리 → 비파괴 말미
  should_run v      "$N_V"    "$RUN" && { run_experiment v_phantom_named  "$RUN";   cleanup; }
  # ── 파괴적(순간 상한 초과·OOM 유발) — 최후 배치 + 명시 승인 필요 ──
  should_run a1      "$N_A1" "$RUN" 1 && { run_experiment a1_counter_isolation "$RUN"; cleanup; }
  should_run a0nr    "$N_A0NR" "$RUN" 1 && { run_experiment a0nr_no_requests       "$RUN"; cleanup; }  # 최악(OOM), 맨 끝
done
# ※ 구 A1(deprecated_a1_lmax999/): 실행하지 말 것 — 매트릭스에서 제외됨.
# ※ A0-R(a0r_no_controller/): requests 파이프라인 확정 후 요인 실험에서 개별 수행.

# ── 보고서 생성 ────────────────────────────────────────────────────
log "=== 보고서 생성 중 ==="
python3 "$TESTDIR/common/make_report.py" \
  --results-dir "$RESULTS" \
  --output "$RESULTS/report.md" 2>&1 | tee -a "$LOG"

log "======================================"
log "전체 실험 완료 ($REPEATS회 반복). 보고서: $RESULTS/report.md"

# 실패한 회차를 마지막에 다시 모아 보여 준다.
# 수백 줄 로그 중간에 묻히면 놓치고, 캠페인이 정상 완주한 것으로 오인한다.
if [[ ${#FAILED_RUNS[@]} -gt 0 ]]; then
  log "⚠️ 실패한 회차 ${#FAILED_RUNS[@]}건 — 결과가 없거나 불완전하다. 재실행할 것:"
  for f in "${FAILED_RUNS[@]}"; do log "     · $f"; done
else
  log "실패한 회차 없음."
fi
log "======================================"
