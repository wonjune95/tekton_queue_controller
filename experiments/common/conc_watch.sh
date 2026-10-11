#!/bin/bash
# conc_watch.sh — 동시 실행 수를 실시간으로 표본화해 CSV 로 남긴다.
#
#   사용법: bash conc_watch.sh <namespace> <출력csv> [간격초]
#   중지  : kill <pid>  (호출 측에서 trap 으로 정리)
#
# metrics_collect.py 의 max_concurrent 는 완료 후 타임스탬프로 «재구성»한 값이다.
# 이 감시기는 실행 «중»에 관측한 독립 계열이라, 재구성값과 어긋나면 그 자체가 신호가 된다.
# (E3 리더 전환처럼 «특정 구간»의 거동을 봐야 하는 조건에서는 이 계열이 근거가 된다.)
#
# ⚠️ running 은 첫 조건의 사유가 정확히 `Running` 인 건만 센다. 시작 직후의 `Started` 등은 빠지므로
#    시작~완료 시각으로 센 동시 실행 수(metrics_collect.py)보다 좁은 하한 계열이다(논문 5.10절).
# ⚠️ pending 정규식은 캠페인 당시 `^(Pending|Started)$` 여서 보류 사유 `PipelineRunPending` 을 세지 못해
#    늘 0 이었다(논문에는 이 열을 쓰지 않았다). 2026-10-11 실제 사유로 바로잡았다.
set -u
NS="${1:-default-cicd}"
OUT="${2:-/tmp/conc.csv}"
INTERVAL="${3:-2}"

echo "ts_epoch,ts_iso,running,pending,total" > "$OUT"
while true; do
  # 한 번의 조회로 세 값을 만든다(조회를 나누면 시점이 어긋난다).
  SNAP=$(kubectl get pipelinerun -n "$NS" \
      -o jsonpath='{range .items[*]}{.status.conditions[0].reason}{"\n"}{end}' 2>/dev/null || true)
  RUNNING=$(printf '%s\n' "$SNAP" | grep -c "^Running$" || true)
  PENDING=$(printf '%s\n' "$SNAP" | grep -c "^PipelineRunPending$" || true)
  TOTAL=$(printf '%s\n' "$SNAP" | grep -c . || true)
  printf '%s,%s,%s,%s,%s\n' "$(date +%s)" "$(date -Iseconds)" \
      "$RUNNING" "$PENDING" "$TOTAL" >> "$OUT"
  sleep "$INTERVAL"
done
