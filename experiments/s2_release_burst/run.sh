#!/bin/bash
# S2 — Release Burst 실험
# 30초 내 90개(Lmax×3) 일괄 생성

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 동작하게 한다.
cd "$(cd "$(dirname "$0")" && pwd)"

RUN=${1:-"1"}
NAMESPACE="default-cicd"
OUTDIR="../results/s2_release_burst"
mkdir -p "$OUTDIR"

# ── 사전 상태 가드 ────────────────────────────────────────────────
# metrics_collect.py 는 **네임스페이스의 모든 PipelineRun** 을 수집한다(시간·이름 필터 없음).
# 이전 실행분이 남아 있으면 이번 회차 CSV 에 섞여 들어간다(조용한 오염).
# run_all.sh 는 시나리오 사이에 cleanup.sh 를 호출하지만, 개별 실행에는 그런 보호가 없다.
LEFT=$(kubectl get pipelinerun -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$LEFT" -ne 0 ]; then
  echo "[중단] 네임스페이스에 이전 PipelineRun 이 ${LEFT}건 남아 있습니다."
  echo "       그대로 실행하면 이번 회차 지표에 섞입니다. 먼저 초기화하세요:"
  echo "         bash ../common/cleanup.sh $NAMESPACE"
  exit 1
fi

echo "=== S2 Release Burst | run=$RUN ==="
echo "  Lmax=30, 90개 burst (0.33초 간격)"

echo "[1/3] 90개 burst 생성..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode burst \
  --count 90 --interval 0.33 --env dev --generate-name

# 전체 완료까지 대기 (최대 20분)
# Running/Pending 상태 PR이 0이 될 때까지 polling
echo "[2/3] 완료 대기 (최대 40분)..."
TIMEOUT=2400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 실행/대기 (${ELAPSED}s 경과)"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo "[3/3] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/run${RUN}_resource.csv" \
  --summary "$OUTDIR/run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
