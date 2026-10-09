#!/bin/bash
# A3 — 단일 FIFO (Tier 구분 제거)
# tierRules 를 단일 규칙(전 env→Tier 1)으로 패치 → 유효 Tier 차등 소멸 → enqueue_time 순 FIFO.
# S3(우선순위 검증)와 동일 부하를 인가해 Tier 제거 시 우선순위 차등 소멸을 관찰한다.
# 제어는 CRD 패치만으로 수행(코드 수정 없음). 원복은 canonical 4규칙으로(다른 필드 불변).
# ※ 비파괴 조건. generateName 통일.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
# (run_all.sh 는 미리 cd 하지만 개별 실행 시 경로가 어긋난다 — 2026-07-31 S1 재실행이 여기서 실패했다.)
cd "$(cd "$(dirname "$0")" && pwd)"
RUN=${1:-"1"}
RATE=${2:-"3"}            # S3 와 동일 (S0 실측 후 조정 시 S3 와 동기화)
NAMESPACE="default-cicd"
OUTDIR="../results/a3_single_fifo"
mkdir -p "$OUTDIR"

echo "=== A3 Single FIFO | run=$RUN rate=$RATE/분 ==="
echo "  tierRules 단일화(전 env→Tier 1), L_max=30 유지"

# ⚠️ 원복은 반드시 trap 으로 건다.
#   말미에서만 원복하면 중단·오류(웹훅 스톨 등)로 죽었을 때 tierRules 가 단일 규칙으로 남아
#   **이후 전 회차에서 Tier 차등이 사라진다**(조용한 오염).
CANONICAL_TIERS='{"spec":{"tierRules":[{"tier":0,"matchType":"label","labelKey":"queue.tekton.dev/urgent","pattern":"true"},{"tier":1,"matchType":"env","pattern":"prod"},{"tier":2,"matchType":"env","pattern":"stg"},{"tier":3,"matchType":"env","pattern":"*"}]}}'
restore_tiers() {
  kubectl patch globallimit tekton-queue-limit --type merge \
    -p "$CANONICAL_TIERS" >/dev/null 2>&1 || true
  local n
  n=$(kubectl get globallimit tekton-queue-limit -o jsonpath='{.spec.tierRules[*].tier}' 2>/dev/null)
  if [ "$n" = "0 1 2 3" ]; then
    echo "  tierRules 원복 완료 (Tier $n)"
  else
    echo "  [경고] tierRules 원복 실패 — 현재 '$n'. 다음 회차 전에 수동 확인할 것!"
  fi
}
trap restore_tiers EXIT

echo "[0/3] Tier 차등 제거 (tierRules → 전 env Tier 1)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"tierRules":[{"tier":1,"matchType":"env","pattern":"*"}]}}' \
  2>/dev/null || echo "[경고] GlobalLimit tierRules 패치 실패"

echo "[1/3] 부하 인가 30분 (λ=${RATE}/분, 혼합 env, generateName)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate "$RATE" --duration 30 \
  --arrival poisson --seed "$RUN" --generate-name

echo "[2/3] 잔여 대기열 소화 대기 (최대 30분)..."
TIMEOUT=1800
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  PENDING=$(kubectl get pipelinerun -n "$NAMESPACE" \
    --no-headers 2>/dev/null | grep -c "Running\|Pending" || true)
  [ "$PENDING" -eq 0 ] && break
  echo "  대기 중: ${PENDING}개 (${ELAPSED}s 경과)"
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

echo "[후처리] tierRules 원복 (canonical 4규칙, maxPipelines 등 불변)..."
kubectl patch globallimit tekton-queue-limit --type merge \
  -p '{"spec":{"tierRules":[{"tier":0,"matchType":"label","labelKey":"queue.tekton.dev/urgent","pattern":"true"},{"tier":1,"matchType":"env","pattern":"prod"},{"tier":2,"matchType":"env","pattern":"stg"},{"tier":3,"matchType":"env","pattern":"*"}]}}' \
  2>/dev/null || echo "[경고] tierRules 원복 실패 — 수동 확인 필요"

echo "=== 완료: $OUTDIR/run${RUN}.csv ==="
