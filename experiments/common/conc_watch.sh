#!/bin/bash
# conc_watch.sh — 동시 실행 수를 실시간으로 표본화해 CSV 로 남긴다.
#
#   사용법: bash conc_watch.sh <namespace> <출력csv> [간격초]
#   중지  : kill <pid>  (호출 측에서 trap 으로 정리)
#
# metrics_collect.py 의 max_concurrent 는 완료 후 타임스탬프로 «재구성»한 값이다.
# 이 감시기는 실행 «중»에 관측한 독립 계열이라, 재구성값과 어긋나면 그 자체가 신호가 된다.
# (E3 리더 전환처럼 «특정 구간»의 거동을 봐야 하는 조건에서는 이 계열이 근거가 된다.)
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
  PENDING=$(printf '%s\n' "$SNAP" | grep -cE "^(Pending|Started)$" || true)
  TOTAL=$(printf '%s\n' "$SNAP" | grep -c . || true)
  printf '%s,%s,%s,%s,%s\n' "$(date +%s)" "$(date -Iseconds)" \
      "$RUNNING" "$PENDING" "$TOTAL" >> "$OUT"
  sleep "$INTERVAL"
done
