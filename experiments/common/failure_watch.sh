#!/bin/bash
# 실패한 파이프라인런의 진단 정보를 삭제되기 전에 남긴다.
#
# 왜 필요한가: `metrics_collect.py` 는 result 만 기록하고 **reason/message 를 남기지 않는다.**
#   그리고 `cleanup.sh` 가 수집 직후 파이프라인런을 전량 삭제하므로 **사후 조회가 불가능하다.**
#   (2026-07-30 S2 run1: 90건 중 1건 실패했으나 원인을 확인할 수 없었다.)
#
# 읽기 전용이며 실행 중인 실험 스크립트를 건드리지 않는다.
#   bash common/failure_watch.sh [네임스페이스] [출력파일]
set -u
NS="${1:-default-cicd}"
OUT="${2:-results/failures.log}"
mkdir -p "$(dirname "$OUT")"
SEEN=""

while true; do
  # Succeeded 조건이 False 인 파이프라인런 = 실패
  ROWS=$(kubectl get pipelinerun -n "$NS" -o json 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for it in d.get('items', []):
    for c in it.get('status', {}).get('conditions', []):
        if c.get('type') == 'Succeeded' and c.get('status') == 'False':
            name = it['metadata']['name']
            print('\t'.join([name, c.get('reason',''), (c.get('message','') or '').replace('\n',' ')[:400]]))
" 2>/dev/null)

  if [ -n "$ROWS" ]; then
    while IFS=$'\t' read -r NAME REASON MSG; do
      [ -z "$NAME" ] && continue
      case "$SEEN" in *"|$NAME|"*) continue ;; esac
      SEEN="$SEEN|$NAME|"
      {
        echo "=== $(date '+%Y-%m-%d %H:%M:%S')  $NAME"
        echo "  reason : $REASON"
        echo "  message: $MSG"
        # 실패한 태스크런의 스텝 상태까지 남긴다(어느 단계에서 깨졌는지).
        kubectl get taskrun -n "$NS" \
          -l tekton.dev/pipelineRun="$NAME" \
          -o jsonpath='{range .items[*]}  taskrun {.metadata.name}: {range .status.conditions[*]}{.reason}{" "}{.message}{end}{"\n"}{end}' 2>/dev/null

        # 실패한 태스크런의 **파드 로그**까지 받아 둔다.
        # 조건(reason/message)만으로는 "step-trivy-scan exited with code 1: Error" 이상을 알 수 없고,
        # cleanup 이 파이프라인런과 파드를 지우면 원인을 영영 확인할 수 없다.
        for TR in $(kubectl get taskrun -n "$NS" -l tekton.dev/pipelineRun="$NAME" \
                      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null); do
          ST=$(kubectl get taskrun -n "$NS" "$TR" \
                 -o jsonpath='{range .status.conditions[*]}{.status}{end}' 2>/dev/null)
          [ "$ST" = "False" ] || continue
          POD=$(kubectl get taskrun -n "$NS" "$TR" -o jsonpath='{.status.podName}' 2>/dev/null)
          [ -n "$POD" ] || continue
          echo "  --- 로그: $TR (pod $POD) ---"
          kubectl logs -n "$NS" "$POD" --all-containers --tail=40 2>&1 \
            | sed 's/^/    /' | tail -40
        done
        echo
      } >> "$OUT"
      echo "[$(date '+%H:%M:%S')] 실패 포착: $NAME ($REASON)"
    done <<< "$ROWS"
  fi
  sleep 20
done
