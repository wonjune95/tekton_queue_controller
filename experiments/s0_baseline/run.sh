#!/bin/bash
# S0 — Baseline 실험
# 사용법: ./run.sh [control|experiment] [run_number] [seed]
# control   : 컨트롤러 없이 실행 (제어군)
# experiment: 컨트롤러 적용 후 실행 (실험군)
#
# seed 는 생략하면 run_number 를 쓴다(기존 동작 그대로).
# ── 왜 시드를 분리했나 (2026-08-04) ─────────────────────────────────
#   인가 경로 수정본으로 S0 를 다시 잴 때, 기존 run1~3 을 덮어쓰지 않으려면 run4~6 이어야 한다.
#   그런데 시드가 회차 번호에 묶여 있으면 도착열까지 달라져 **버전 차이와 푸아송 잡음이 섞인다.**
#   출력 회차(4·5·6)와 시드(1·2·3)를 분리하면 **같은 도착열에서 버전만 바꾼 짝지은 비교**가 된다.

set -e
# 상대경로(../common, ../results)를 쓰므로 호출 위치와 무관하게 자기 디렉터리에서 동작하게 한다.
cd "$(cd "$(dirname "$0")" && pwd)"

MODE=${1:-"experiment"}   # control | experiment
RUN=${2:-"1"}
SEED=${3:-"$RUN"}
NAMESPACE="default-cicd"
OUTDIR="../results/s0_baseline"
mkdir -p "$OUTDIR"

echo "=== S0 Baseline | mode=$MODE run=$RUN seed=$SEED ==="

# ── control 모드: 큐 컨트롤러를 실제로 비활성화한다 ──────────────────
# 예전에는 MODE 가 **출력 파일명만** 바꿔서, 컨트롤러가 켜진 채 control_runN.csv 가
# 생성될 수 있었다(라벨만 제어군인 데이터 = 조용한 오염). a0r_no_controller 와 동일한 절차를 쓴다.
restore_controller() {
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=1 >/dev/null 2>&1 || true
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator --type=json \
    -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]' >/dev/null 2>&1 || true
  kubectl wait --for=condition=ready pod -l app=tekton-queue -n tekton-pipelines --timeout=120s >/dev/null 2>&1 || true
  echo "  컨트롤러 복구 완료"
}
if [ "$MODE" = "control" ]; then
  echo "[0/3] 큐 컨트롤러 비활성화 (제어군)..."
  trap restore_controller EXIT          # 중단되어도 반드시 복구
  kubectl patch mutatingwebhookconfiguration tekton-queue-mutator --type=json \
    -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]'
  kubectl scale deployment tekton-queue-controller -n tekton-pipelines --replicas=0
  kubectl wait --for=delete pod -l app=tekton-queue -n tekton-pipelines --timeout=60s 2>/dev/null || true
  echo "  컨트롤러 중단 완료"
elif [ "$MODE" != "experiment" ]; then
  echo "[중단] MODE 는 control 또는 experiment 여야 합니다 (받은 값: $MODE)"; exit 1
fi

# 측정
echo "[1/3] 측정 30분 (λ=1/분)..."
python3 ../common/pr_create.py \
  --namespace "$NAMESPACE" --mode steady \
  --rate 1 --duration 30 \
  --arrival poisson --seed "$SEED" --generate-name

# 쿨다운
echo "[2/3] 쿨다운 5분 대기..."
sleep 300

# 지표 수집
echo "[3/3] 지표 수집..."
python3 ../common/metrics_collect.py \
  --namespace "$NAMESPACE" \
  --output "$OUTDIR/${MODE}_run${RUN}.csv"

python3 ../common/resource_collect.py \
  --namespace "$NAMESPACE" \
  --prometheus-url "${PROM_URL:-http://localhost:9090}" \
  --output  "$OUTDIR/${MODE}_run${RUN}_resource.csv" \
  --summary "$OUTDIR/${MODE}_run${RUN}_resource.json"

echo "=== 완료: $OUTDIR/${MODE}_run${RUN}.csv ==="
