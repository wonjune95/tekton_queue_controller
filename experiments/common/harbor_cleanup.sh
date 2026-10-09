#!/bin/bash
# Harbor 에 쌓인 실험 이미지 정리.
#
# 왜 필요한가: ttl.sh 는 2시간 뒤 자동 삭제됐지만 **Harbor 는 지우지 않는다.**
#   파이프라인런마다 고유 저장소(`petclinic-<env>-<pr-id>`)를 만들므로
#   남은 캠페인(약 3,200건 × 고유 레이어 ~50MB)이면 registry PVC(200Gi)를 위협한다.
#
#   bash common/harbor_cleanup.sh          # 저장소 삭제만(빠름)
#   bash common/harbor_cleanup.sh --gc     # 삭제 + 가비지 컬렉션(실제 공간 회수)
#
# ⚠️ 베이스 이미지·trivy DB 는 지우지 않는다(삭제 대상은 `petclinic-` 접두사만).
#    이들을 지우면 다음 회차가 전부 실패한다.
set -u
NS_TOOL="${NS_TOOL:-devops-tools}"
HARBOR="${HARBOR:-http://harbor.harbor.svc:80}"
CRED="${HARBOR_CRED:-admin:Harbor12345}"
DO_GC=0
[[ "${1:-}" == "--gc" ]] && DO_GC=1

echo "=== Harbor 실험 이미지 정리 ==="

run_curl() {   # 클러스터 안에서만 Harbor 에 닿으므로 임시 파드로 호출한다
  kubectl run "harbor-cl-$RANDOM" --rm -i --restart=Never \
    --image=curlimages/curl:8.10.1 -n "$NS_TOOL" --timeout=180s --quiet -- "$@" 2>/dev/null
}

BEFORE=$(run_curl sh -c "curl -s -u '$CRED' '$HARBOR/api/v2.0/statistics'" \
         | python3 -c "import sys,json;print(json.load(sys.stdin)['total_storage_consumption'])" 2>/dev/null || echo 0)

# petclinic- 로 시작하는 저장소를 모두 지운다(페이지네이션 처리).
# ⚠️ **페이지를 넘기면서 삭제하면 안 된다.** 삭제할 때마다 목록이 앞으로 밀려
#   page=2 로 넘어가는 순간 한 페이지 분량을 통째로 건너뛴다.
#   (2026-08-02 실측: 한 번 실행에 1642 → 810 → 405 개씩 절반씩만 지워졌다.)
#   → **항상 page=1 만 읽고 지우기를, 대상이 없을 때까지 반복**한다.
DELETED=$(run_curl sh -c "
  n=0; round=0
  while [ \$round -lt 200 ]; do
    body=\$(curl -s -u '$CRED' '$HARBOR/api/v2.0/projects/library/repositories?page_size=100&page=1')
    names=\$(echo \"\$body\" | tr ',' '\n' | grep '\"name\"' | sed 's/.*\"name\":\"library\///;s/\".*//' | grep '^petclinic-')
    [ -z \"\$names\" ] && break
    for r in \$names; do
      curl -s -o /dev/null -X DELETE -u '$CRED' \"$HARBOR/api/v2.0/projects/library/repositories/\$r\"
      n=\$((n+1))
    done
    round=\$((round+1))
  done
  echo \$n
" | tail -1)

echo "  삭제한 저장소: ${DELETED:-0}개"

if [[ "$DO_GC" == "1" ]]; then
  # 저장소를 지워도 blob 은 GC 전까지 공간을 차지한다.
  echo "  가비지 컬렉션 실행(수 분 소요)..."
  run_curl sh -c "curl -s -o /dev/null -X POST -u '$CRED' \
    -H 'Content-Type: application/json' \
    -d '{\"schedule\":{\"type\":\"Manual\"},\"parameters\":{\"delete_untagged\":true}}' \
    '$HARBOR/api/v2.0/system/gc/schedule'" >/dev/null
  # 완료를 기다린다(최대 10분).
  for _ in $(seq 1 60); do
    ST=$(run_curl sh -c "curl -s -u '$CRED' '$HARBOR/api/v2.0/system/gc?page_size=1'" \
         | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['job_status'] if d else '')" 2>/dev/null)
    [[ "$ST" == "Success" || "$ST" == "Error" || "$ST" == "Stopped" ]] && break
    sleep 10
  done
  echo "  GC 상태: ${ST:-알 수 없음}"
fi

AFTER=$(run_curl sh -c "curl -s -u '$CRED' '$HARBOR/api/v2.0/statistics'" \
        | python3 -c "import sys,json;print(json.load(sys.stdin)['total_storage_consumption'])" 2>/dev/null || echo 0)
python3 -c "
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
b,a=${BEFORE:-0},${AFTER:-0}
print(f'  사용량: {b/2**30:.2f} GiB -> {a/2**30:.2f} GiB')
" 2>/dev/null
echo "=== 정리 완료 ==="
