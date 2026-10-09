#!/bin/bash
# Harbor registry PVC 사용률 점검 — 임계 초과면 비정상 종료(청크 사전 점검용).
#
# 왜 필요한가: Harbor 는 ttl.sh 와 달리 이미지를 자동 삭제하지 않는다.
#   2026-08-02 비교군 청크에서 정리 배선이 빠져 195.2 GiB/200 GiB 까지 차올랐고,
#   kaniko 가 push 하지 못해 **5회차가 통째로 무효**가 됐다(전멸 4회 + 오염 1회).
#   회차를 돌리기 전에 남은 공간을 확인해 같은 사고를 반복하지 않는다.
#
# Harbor API 대신 **Prometheus 의 PVC 지표**를 쓴다.
#   · 임시 파드를 띄우지 않아 즉시 응답한다(파드 방식은 스케줄 지연으로 타임아웃했다).
#   · 실제로 중요한 것은 registry PVC 의 잔여 공간이다.
#
#   bash common/harbor_check.sh [임계백분율]     기본 임계 70%
set -u
LIMIT_PCT="${1:-70}"
PROM="${PROM_URL:-http://localhost:9090}"

q() {  # q <promql>
  curl -s --max-time 20 "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null \
    | python3 -c "
import sys,json
try:
    r=json.load(sys.stdin)['data']['result']
    print(r[0]['value'][1] if r else '')
except Exception:
    print('')
" 2>/dev/null
}

USED=$(q 'kubelet_volume_stats_used_bytes{persistentvolumeclaim="harbor-registry"}')
CAP=$(q  'kubelet_volume_stats_capacity_bytes{persistentvolumeclaim="harbor-registry"}')

if [ -z "$USED" ] || [ -z "$CAP" ]; then
  # 조회 실패를 통과로 처리하면 **점검이 없는 것만 못하다**(거짓 안심).
  echo "  [중단] Harbor registry PVC 사용량을 조회하지 못했습니다."
  echo "         Prometheus 포트포워드($PROM)와 지표 존재를 확인하세요."
  exit 1
fi

python3 - "$USED" "$CAP" "$LIMIT_PCT" <<'PY'
import sys
used, cap, limit = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
pct = used / cap * 100
print(f"  Harbor registry PVC: {used/2**30:.1f} / {cap/2**30:.1f} GiB ({pct:.1f}%, 임계 {limit:.0f}%)")
if pct >= limit:
    print("  [중단] 임계 초과. 먼저 정리하세요:  bash common/harbor_cleanup.sh --gc")
    sys.exit(1)
PY
