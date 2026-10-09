"""
스테이지별 피크 메모리 수집기 (파라미터 실측).

petclinic-build-measure 파이프라인이 각 셸 스텝 말미에 출력한
  MEMPEAK stage=<name> bytes=<N>
마커를 PipelineRun 파드 로그에서 파싱해 CSV로 정리한다.

- PR 피크 = 스테이지 피크의 최댓값(태스크 순차 실행 → 동시 파드 1개, 합 아님).
- requests(P95)·limits(피크×1.2) 산정 입력 = param_measurement.md §6.

사용:
  python mem_peak_collect.py --namespace default-cicd --output ../results/param/mem_peak.csv
  # 특정 PR 접두사만: --pr-prefix pr-
의존성 없음(kubectl + 표준 라이브러리).
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

MARKER = re.compile(r"MEMPEAK\s+stage=(\S+)\s+bytes=(\d+|NA)")
STAGES = ["clone", "maven", "kaniko", "trivy"]


def kubectl(args):
    """kubectl 호출 → stdout 문자열. 실패 시 빈 문자열.

    ※ encoding 을 UTF-8 로 명시한다. text=True 만 쓰면 시스템 로캘로 디코딩하는데,
      한국어 Windows(cp949)에서 kubectl 의 UTF-8 출력을 만나면 UnicodeDecodeError 로
      죽고 None 이 반환돼 호출부에서 TypeError 가 난다(2026-07-29 실측).
      errors='replace' 로 일부 깨진 바이트가 있어도 MEMPEAK 마커 파싱은 계속되게 한다.
    """
    try:
        out = subprocess.run(["kubectl"] + args, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        return out.stdout or ""
    except Exception as e:
        print(f"[경고] kubectl {' '.join(args)} 실패: {e}", file=sys.stderr)
        return ""


def list_measure_pods(namespace, pr_prefix):
    """measure 파이프라인의 PipelineRun 파드 목록 → [(pr_name, pod_name)]."""
    raw = kubectl(["get", "pods", "-n", namespace,
                   "-l", "tekton.dev/pipelineRun",
                   "-o", "json"])
    if not raw.strip():
        return []
    data = json.loads(raw)
    pods = []
    for item in data.get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        pr = labels.get("tekton.dev/pipelineRun", "")
        pod = item.get("metadata", {}).get("name", "")
        if not pr or not pod:
            continue
        if pr_prefix and not pr.startswith(pr_prefix):
            continue
        pods.append((pr, pod))
    return pods


def collect(namespace, pr_prefix):
    """PR별 stage→peak_bytes 딕셔너리 반환."""
    pods = list_measure_pods(namespace, pr_prefix)
    if not pods:
        print(f"[안내] {namespace} 에서 measure 파드를 찾지 못함.", file=sys.stderr)
    # pr -> {stage: bytes}
    result = defaultdict(dict)
    for pr, pod in pods:
        logs = kubectl(["logs", "-n", namespace, pod, "--all-containers", "--prefix=false"])
        for m in MARKER.finditer(logs):
            stage, val = m.group(1), m.group(2)
            if val == "NA":
                continue
            b = int(val)
            # 같은 스테이지가 여러 번(재시도) 나오면 최댓값 유지
            result[pr][stage] = max(result[pr].get(stage, 0), b)
    return result


def write_csv(result, output):
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    rows = []
    for pr in sorted(result):
        stages = result[pr]
        pr_peak = max(stages.values()) if stages else 0
        row = {"pipelinerun": pr, "pr_peak_mib": round(pr_peak / 1048576, 1)}
        for s in STAGES:
            b = stages.get(s)
            row[f"{s}_mib"] = round(b / 1048576, 1) if b else ""
        rows.append(row)

    fields = ["pipelinerun", "pr_peak_mib"] + [f"{s}_mib" for s in STAGES]
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # 요약: 스테이지별 최댓값 + PR 피크 중앙값 후보
    print(f"[완료] {len(rows)}개 PR → {output}")
    if rows:
        for s in STAGES:
            vals = [r[f"{s}_mib"] for r in rows if r[f"{s}_mib"] != ""]
            if vals:
                print(f"  {s:7s}: max={max(vals):7.1f} MiB  (n={len(vals)})")
        peaks = [r["pr_peak_mib"] for r in rows]
        peaks.sort()
        p95 = peaks[min(len(peaks) - 1, int(round(0.95 * (len(peaks) - 1))))]
        print(f"  PR피크 : max={max(peaks):7.1f}  P95={p95:7.1f} MiB "
              f"→ requests≈P95, limit≈max×1.2={max(peaks)*1.2:.1f} MiB")


if __name__ == "__main__":
    # 한국어 Windows 콘솔(cp949)에서 '≈' 같은 문자를 출력하다 UnicodeEncodeError 로
    # 죽는 것을 막는다. CSV 는 이미 쓰인 뒤라 데이터 손실은 없었지만 종료 코드가 1이 되어
    # 호출 스크립트가 실패로 판단한다(2026-07-29 실측).
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="default-cicd")
    ap.add_argument("--pr-prefix", default="", help="특정 PR 접두사만 수집(예: pr-)")
    ap.add_argument("--output", default="../results/param/mem_peak.csv")
    args = ap.parse_args()
    res = collect(args.namespace, args.pr_prefix)
    write_csv(res, args.output)
