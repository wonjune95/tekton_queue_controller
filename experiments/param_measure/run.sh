#!/bin/bash
# 파라미터 실측 — 단일 PR 스테이지별 피크 메모리 측정
# petclinic-build-measure 파이프라인으로 N개 PR을 돌려 memory.peak 을 수집한다.
# 산출: results/param/mem_peak.csv (스테이지별·PR별 피크, P95·limit 후보 요약).
# 근거·후속: param_measurement.md.
#
# 사용: bash run.sh [count] [interval_sec]
#   기본 5개, 45초 간격(모던 동시성 낮게 유지 → 자연 피크 관측, 노드 압박 최소화).
# ※ 비파괴. 측정은 넉넉한 limit(파이프라인 param mem-limit 기본 4Gi)에서 수행.
#   JVM 힙 캘리브레이션(-Xmx 스윕)은 파이프라인 param java-opts 로 GKE에서 별도 수행.

set -e
# 이 스크립트는 자기 디렉터리 기준 상대경로(../pipeline, ../common, ../results)를 쓴다.
# 문서에 `bash param_measure/run.sh` 로 안내돼 있어 test/ 에서 호출하면 경로가 어긋난다
# (2026-07-29 실측). 호출 위치와 무관하게 동작하도록 자기 디렉터리로 이동한다.
cd "$(cd "$(dirname "$0")" && pwd)"

COUNT=${1:-"5"}
INTERVAL=${2:-"45"}
NAMESPACE="default-cicd"
PIPELINE_FILE="../pipeline/petclinic-build-measure.yaml"
OUTDIR="../results/param"
mkdir -p "$OUTDIR"

echo "=== 파라미터 실측 | count=$COUNT interval=${INTERVAL}s ==="

echo "[1/4] 측정 파이프라인 적용..."
# ※ `| tail -1` 로 파이프하면 종료 코드가 tail 것이 되어 **적용 실패를 삼킨다**.
#   2026-07-29: 파이프라인 적용이 거부됐는데도 그대로 진행해 없는 파이프라인을 참조하는
#   PR 5건을 만들고(CouldntGetPipeline) 빈 CSV 를 남겼다. 실패하면 즉시 중단한다.
kubectl apply -f "$PIPELINE_FILE" -n "$NAMESPACE"
kubectl get pipeline petclinic-build-measure -n "$NAMESPACE" >/dev/null 2>&1 || {
  echo "[중단] 측정 파이프라인이 클러스터에 없다. 위 오류를 먼저 해결할 것."; exit 1; }

echo "[2/4] ${COUNT}개 PR 생성 (measure 파이프라인, ${INTERVAL}s 간격)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count "$COUNT" --interval "$INTERVAL" --env dev \
  --pipeline petclinic-build-measure

echo "[3/4] 완료 대기 (최대 40분)..."
TIMEOUT=2400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  RUNNING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$RUNNING" -eq 0 ] && break
  echo "  실행/대기 중: ${RUNNING}개 (${ELAPSED}s 경과)"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[4/4] 스테이지별 피크 메모리 수집..."
python3 ../common/mem_peak_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/mem_peak.csv"

echo "=== 완료: $OUTDIR/mem_peak.csv ==="
echo "※ 파드가 GC 되기 전(로그 보존 중)에 수집해야 함. 미수집 시 재실행 대신 파드 잔존 상태에서 수집기만 재호출."
