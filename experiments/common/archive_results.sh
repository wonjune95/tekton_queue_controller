#!/bin/bash
# 실험 결과 스냅샷 보관.
#
# 캠페인은 재실행이 불가능하므로 결과 원본을 **복사본으로 이중화**하고,
# 무결성 검증용 체크섬과 환경 지문(어떤 구성에서 나온 값인지)을 함께 남긴다.
#
#   bash common/archive_results.sh [라벨]
#
# - results/ 를 results_archive/<날짜시각>[_라벨]/ 로 **복사**한다(이동 아님, 원본 유지).
# - MANIFEST.md 에 파일별 SHA256 + 회차 목록 + 환경 지문을 기록한다.
# - 기존 아카이브는 절대 지우거나 덮어쓰지 않는다(프로젝트 안전 규칙).
set -u
cd "$(cd "$(dirname "$0")/.." && pwd)"

LABEL="${1:-}"
STAMP="$(date '+%Y%m%d_%H%M%S')"
DEST="results_archive/${STAMP}${LABEL:+_$LABEL}"

if [[ ! -d results ]]; then
  echo "[중단] results/ 가 없습니다."; exit 1
fi
if [[ -e "$DEST" ]]; then
  echo "[중단] 대상이 이미 존재합니다: $DEST"; exit 1
fi

mkdir -p "$DEST"
cp -r results/. "$DEST"/
echo "복사 완료: results/ → $DEST"

MAN="$DEST/MANIFEST.md"
{
  echo "# 실험 결과 스냅샷 — ${STAMP}${LABEL:+ ($LABEL)}"
  echo
  echo "생성: $(date '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "> 이 디렉터리는 \`results/\` 의 복사본이다. **삭제·덮어쓰기 금지.**"
  echo "> 재처리·분석은 이 사본이 아니라 \`results/\` 또는 별도 작업 사본에서 한다."
  echo
  echo "## 회차 목록"
  echo
  echo "| 시나리오 | 파일 | 크기 | 수정시각 |"
  echo "|---|---|---|---|"
  find "$DEST" -name '*.csv' -o -name '*.json' | sort | while read -r f; do
    rel="${f#"$DEST"/}"
    scen="$(dirname "$rel")"
    printf '| %s | %s | %s | %s |\n' \
      "$scen" "$(basename "$rel")" \
      "$(du -h "$f" 2>/dev/null | cut -f1)" \
      "$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null)"
  done
  echo
  echo "## 환경 지문"
  echo
  echo '```'
  echo "kubectl 컨텍스트: $(kubectl config current-context 2>/dev/null)"
  echo
  echo "[노드]"
  kubectl get nodes -L node-role,infra-role --no-headers 2>/dev/null \
    | awk '{print "  "$1"  "$2"  "$6"  "$7}'
  echo
  echo "[GlobalLimit]"
  kubectl get globallimit -A --no-headers 2>/dev/null | awk '{print "  "$0}'
  echo
  echo "[Tekton / 컨트롤러 이미지]"
  kubectl get deploy -n tekton-pipelines -o jsonpath='{range .items[*]}  {.metadata.name}{"  "}{.spec.template.spec.containers[0].image}{"\n"}{end}' 2>/dev/null
  echo
  echo "[파이프라인]"
  kubectl get pipeline -n default-cicd --no-headers 2>/dev/null | awk '{print "  "$1}'
  echo '```'
  echo
  echo "## SHA256"
  echo
  echo '```'
  ( cd "$DEST" && find . -type f ! -name 'MANIFEST.md' -print0 \
      | sort -z | xargs -0 sha256sum 2>/dev/null )
  echo '```'
} > "$MAN"

N=$(find "$DEST" -name '*.csv' | wc -l)
echo "MANIFEST 작성: $MAN  (CSV ${N}개)"
echo
echo "검증하려면:  cd $DEST && sha256sum -c <(sed -n '/^## SHA256/,\$p' MANIFEST.md | grep -E '^[0-9a-f]{64}')"
