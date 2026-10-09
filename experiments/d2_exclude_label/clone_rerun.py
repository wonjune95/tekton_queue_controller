#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tekton Dashboard 의 rerun 동작을 그대로 재현해 파이프라인런을 복제한다.

왜 Dashboard API 를 직접 부르지 않는가
  rerun 의 본질은 «원본을 통째로 복제하되 실행 상태만 지우는 것» 이다.
  대시보드를 띄우고 인증을 붙이는 것보다 같은 변환을 여기서 하는 편이 정확하고 검증 가능하다.

재현 근거 — tektoncd/dashboard `src/api/pipelineRuns.js` 의 generateNewPipelineRunPayload:
    payload = structuredClone(pipelineRun)
    payload.metadata = { annotations, generateName, labels: labels || {}, namespace }
    payload.metadata.labels['dashboard.tekton.dev/rerunOf'] = name
  제거 대상: status · spec.status · 시스템 라벨(removeSystemLabels) ·
             annotations 중 tekton.dev/v1beta1TaskRuns, kubectl.kubernetes.io/last-applied-configuration

  ⇒ **사용자가 붙인 라벨은 그대로 살아남는다.** 이것이 D2 가 보려는 지점이다.
     템플릿에 붙인 제외 라벨이 복제본에도 따라가면, 복제본 역시 웹훅에서 제외되어
     큐를 타지 않는다 — 회피의 대가가 상한 통제 상실로 나타난다.
"""
import argparse
import json
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# removeSystemLabels 가 걸러내는 접두어(대시보드 구현과 동일한 취지)
SYSTEM_LABEL_PREFIXES = ("tekton.dev/", "app.kubernetes.io/")
DROP_ANNOTATIONS = (
    "tekton.dev/v1beta1TaskRuns",
    "kubectl.kubernetes.io/last-applied-configuration",
)


def build_payload(src: dict, impersonate: str | None) -> dict:
    """대시보드 rerun 과 동일한 변환. 사용자 라벨은 유지한다."""
    name = src["metadata"]["name"]
    labels = {k: v for k, v in (src["metadata"].get("labels") or {}).items()
              if not k.startswith(SYSTEM_LABEL_PREFIXES)}
    labels["dashboard.tekton.dev/rerunOf"] = name          # 대시보드가 붙이는 표시
    annotations = {k: v for k, v in (src["metadata"].get("annotations") or {}).items()
                   if k not in DROP_ANNOTATIONS}

    payload = json.loads(json.dumps(src))                  # structuredClone 대응
    payload.pop("status", None)                            # 실행 상태 제거
    payload["spec"].pop("status", None)                    # 보류 표시 제거 → 실행 대상이 된다
    payload["metadata"] = {
        "generateName": f"{name}-r-",
        "namespace": src["metadata"]["namespace"],
        "labels": labels,
        "annotations": annotations,
    }
    return payload


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="default-cicd")
    p.add_argument("--source", required=True, help="복제할 원본 파이프라인런 이름")
    p.add_argument("--count", type=int, default=12)
    p.add_argument("--impersonate", default="",
                   help="이 계정으로 생성한다(빈 값이면 현재 자격). 제안 컨트롤러의 출처 판정 대상")
    a = p.parse_args()

    got = subprocess.run(
        ["kubectl", "get", "pipelinerun", a.source, "-n", a.namespace, "-o", "json"],
        capture_output=True)
    if got.returncode != 0:
        print(f"[중단] 원본을 읽지 못했다: {got.stderr.decode('utf-8', 'replace')[:200]}")
        sys.exit(1)
    src = json.loads(got.stdout.decode("utf-8", "replace"))

    payload = build_payload(src, a.impersonate or None)
    kept = [k for k in payload["metadata"]["labels"] if k != "dashboard.tekton.dev/rerunOf"]
    print(f"복제 원본: {a.source}")
    print(f"  유지되는 라벨: {kept}")
    print(f"  생성 계정: {a.impersonate or '(현재 자격 — 사람 계정)'}")

    made = 0
    for i in range(a.count):
        cmd = ["kubectl", "create", "-n", a.namespace, "-f", "-"]
        if a.impersonate:
            cmd += [f"--as={a.impersonate}"]
        r = subprocess.run(cmd, input=json.dumps(payload).encode("utf-8"),
                           capture_output=True)
        if r.returncode == 0:
            made += 1
        else:
            err = r.stderr.decode("utf-8", "replace").strip()
            print(f"  [{i+1}] 실패: {err[:160]}")
    print(f"복제 완료: {made}/{a.count}")


if __name__ == "__main__":
    main()
