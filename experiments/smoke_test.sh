#!/usr/bin/env bash
# 스모크 테스트 — 파이프라인런 1건이 5단계를 끝까지 통과하는지 검증
#
#   실행: bash smoke_test.sh [namespace]
#
# 이 테스트를 통과해야 이후 단계(파라미터 실측·dry-run·캠페인)가 의미 있다.
# 실패 시 어느 의존물이 빠졌는지 진단 출력한다.
# 소요: 약 4~6분(파이프라인 1건 = clone 10s + maven 90s + kaniko 60s + trivy 60s + 오버헤드).

set -uo pipefail
NS="${1:-default-cicd}"
TIMEOUT=900

pass() { echo "  [OK]   $*"; }
fail() { echo "  [FAIL] $*"; FAILED=1; }
FAILED=0

echo "════════════════════════════════════════════════════"
echo " 스모크 테스트  namespace=${NS}"
echo " 컨텍스트: $(kubectl config current-context)"
echo "════════════════════════════════════════════════════"

# ── 1. 사전 의존물 점검 ───────────────────────────────────────────
echo ""
echo ">>> [1/5] 의존물 점검"
kubectl -n "$NS" get configmap maven-settings >/dev/null 2>&1 \
  && pass "ConfigMap maven-settings" \
  || fail "ConfigMap maven-settings 없음 (※ Secret 아니라 ConfigMap 이어야 함) → gke_bootstrap_deps.sh"
kubectl -n "$NS" get secret harbor-kaniko-config >/dev/null 2>&1 \
  && pass "Secret harbor-kaniko-config" \
  || fail "Secret harbor-kaniko-config 없음 → gke_bootstrap_deps.sh"
kubectl -n devops-tools get svc gitea-service >/dev/null 2>&1 \
  && pass "Gitea 서비스" || fail "gitea-service 없음 → gke_bootstrap_deps.sh"
kubectl -n harbor get svc harbor >/dev/null 2>&1 \
  && pass "Harbor 서비스" || fail "harbor 서비스 없음 → gke_bootstrap_deps.sh"
kubectl get nodes -l node-role=build --no-headers 2>/dev/null | grep -q . \
  && pass "빌드 노드(node-role=build) 존재" \
  || fail "node-role=build 라벨 노드 없음 → 파이프라인 파드가 스케줄되지 않는다"
kubectl -n tekton-pipelines get deploy tekton-queue-controller >/dev/null 2>&1 \
  && pass "큐 컨트롤러 배포됨" || fail "큐 컨트롤러 없음 → gke_setup.sh"

if [[ $FAILED -eq 1 ]]; then
  echo ""
  echo "의존물이 빠졌다. 위 항목을 채운 뒤 다시 실행할 것."
  exit 1
fi

# ── 2. 파이프라인 적용 ────────────────────────────────────────────
echo ""
echo ">>> [2/5] 파이프라인 적용"
kubectl apply -f "$(dirname "$0")/pipeline/petclinic-build-experiment.yaml" -n "$NS" 2>&1 | tail -1

# ── 3. 파이프라인런 1건 생성 ──────────────────────────────────────
echo ""
echo ">>> [3/5] 파이프라인런 1건 생성 (generateName, env=dev)"
BEFORE=$(kubectl get pipelinerun -n "$NS" --no-headers 2>/dev/null | wc -l)
python3 "$(dirname "$0")/common/pr_create.py" \
  --namespace "$NS" --mode burst --count 1 --interval 1 --env dev --generate-name
sleep 5
PR=$(kubectl get pipelinerun -n "$NS" --sort-by=.metadata.creationTimestamp \
      --no-headers 2>/dev/null | tail -1 | awk '{print $1}')
[[ -n "$PR" ]] || { echo "  [FAIL] 파이프라인런이 생성되지 않았다 (웹훅 거부 여부 확인)"; exit 1; }
pass "생성됨: $PR"

# 웹훅이 Tier 라벨을 부여했는지(컨트롤러 동작 확인)
TIER=$(kubectl get pipelinerun "$PR" -n "$NS" -o jsonpath='{.metadata.labels.queue\.tekton\.dev/tier}' 2>/dev/null)
[[ -n "$TIER" ]] && pass "웹훅 Tier 부여됨 (tier=$TIER)" \
                 || echo "  [경고] Tier 라벨 없음 — 웹훅이 개입하지 않았을 수 있다"

# ── 4. 완료 대기 ──────────────────────────────────────────────────
echo ""
echo ">>> [4/5] 완료 대기 (최대 $((TIMEOUT/60))분)"
ELAPSED=0
while [[ $ELAPSED -lt $TIMEOUT ]]; do
  STATUS=$(kubectl get pipelinerun "$PR" -n "$NS" \
    -o jsonpath='{.status.conditions[0].status}' 2>/dev/null)
  REASON=$(kubectl get pipelinerun "$PR" -n "$NS" \
    -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null)
  case "$STATUS" in
    True)  pass "완료: $REASON"; break ;;
    False) fail "실패: $REASON"; FAILED=1; break ;;
    *)     echo "  진행 중... ${REASON:-Pending} (${ELAPSED}s)"; ;;
  esac
  sleep 15; ELAPSED=$((ELAPSED+15))
done
[[ $ELAPSED -ge $TIMEOUT ]] && { fail "타임아웃"; FAILED=1; }

# ── 5. 단계별 결과 + 진단 ─────────────────────────────────────────
echo ""
echo ">>> [5/5] 태스크별 결과"
kubectl get taskrun -n "$NS" -l tekton.dev/pipelineRun="$PR" \
  -o custom-columns=TASK:.metadata.labels.'tekton\.dev/pipelineTask',STATUS:.status.conditions[0].reason \
  --no-headers 2>/dev/null || true

if [[ $FAILED -eq 1 ]]; then
  cat <<EOF

════════════════════════════════════════════════════
 스모크 테스트 실패 — 진단 힌트
════════════════════════════════════════════════════
  실패 태스크 로그:
    kubectl logs -n ${NS} -l tekton.dev/pipelineRun=${PR} --all-containers --tail=50

  흔한 원인:
   · code-fetch  실패 → Gitea 시딩 안 됨 (리포 없음/인증)
   · code-build  실패 → maven-settings ConfigMap 누락 또는 미러 접근 불가
   · image-build 실패 → harbor-kaniko-config 시크릿, Harbor 기반 이미지 미러 확인
   · image-scan  실패 → Trivy DB 미러(library/trivy-db:2) 확인
   · 전부 Pending → node-role=build 노드 부재 또는 자원 부족
════════════════════════════════════════════════════
EOF
  exit 1
fi

cat <<EOF

════════════════════════════════════════════════════
 ✅ 스모크 테스트 통과 — 환경이 실험 가능 상태다
════════════════════════════════════════════════════
 다음 단계:
   1) 파라미터 실측:  bash param_measure/run.sh 5 45
   2) requests 파이프라인 생성:
      python3 common/make_requests_pipeline.py --input results/param/mem_peak.csv
   3) dry-run:        python3 common/resource_collect.py --check
   4) 정리:           bash common/cleanup.sh ${NS}
════════════════════════════════════════════════════
EOF
