"""
Trivy ttl.sh 리다이렉트 오류로 실패한 PipelineRun을 True로 정정한다.

판정 기준:
  - result=False인 PipelineRun 중
  - 실패한 TaskRun이 image-scan 단계이고
  - 해당 Pod 로그에 "stopped after 10 redirects" 포함

사용법:
  python3 fix_trivy_redirect.py --csv results/a2_no_aging/run1.csv [--dry-run]
"""
import csv
import argparse
import subprocess
import json
import sys
from pathlib import Path


def kubectl(*args):
    # encoding 을 명시하지 않으면 Windows 에서 cp949 로 디코딩해 한글이 섞인 출력에
    # UnicodeDecodeError 가 난다. 2026-08-04 에 이 누락으로 사전 점검이 오판돼
    # 실험 한 청크가 통째로 건너뛰어졌다. errors="replace" 로 깨진 바이트도 흘려보낸다.
    result = subprocess.run(["kubectl"] + list(args), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip(), result.returncode


def get_failed_taskrun(pr_name, namespace):
    """PipelineRun에서 실패한 TaskRun 이름과 step 반환."""
    out, rc = kubectl("get", "pipelinerun", pr_name, "-n", namespace, "-o", "json")
    if rc != 0:
        return None, None
    pr = json.loads(out)
    for ref in pr.get("status", {}).get("childReferences", []):
        if ref.get("kind") == "TaskRun":
            tr_name = ref["name"]
            tr_out, _ = kubectl("get", "taskrun", tr_name, "-n", namespace, "-o", "json")
            if not tr_out:
                continue
            tr = json.loads(tr_out)
            conditions = tr.get("status", {}).get("conditions", [])
            succeeded = next((c for c in conditions if c["type"] == "Succeeded"), None)
            if succeeded and succeeded.get("status") == "False":
                # pipelineTaskName으로 어느 단계인지 확인
                task_label = ref.get("pipelineTaskName", "")
                return tr_name, task_label
    return None, None


def check_redirect_error(tr_name, namespace):
    """TaskRun Pod 로그에 redirect 오류 포함 여부 확인."""
    out, rc = kubectl("get", "taskrun", tr_name, "-n", namespace, "-o",
                      "jsonpath={.status.podName}")
    if rc != 0 or not out:
        return False
    pod_name = out.strip()
    log_out, _ = kubectl("logs", pod_name, "-n", namespace,
                         "-c", "step-trivy-scan", "--tail=50")
    return "stopped after 10 redirects" in log_out or "10 redirects" in log_out


def fix_csv(csv_path: Path, namespace: str, dry_run: bool):
    rows = []
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    fieldnames = list(rows[0].keys()) if rows else []
    fixed = 0
    skipped = 0

    for row in rows:
        if row.get("result") != "False":
            continue

        pr_name = row["name"]
        print(f"[검사] {pr_name} ...", end=" ", flush=True)

        tr_name, task_label = get_failed_taskrun(pr_name, namespace)
        if not tr_name:
            print(f"TaskRun 없음 — 건너뜀")
            skipped += 1
            continue

        if "image-scan" not in task_label:
            print(f"실패 단계={task_label} (Trivy 아님) — 유지")
            skipped += 1
            continue

        if check_redirect_error(tr_name, namespace):
            print(f"redirect 오류 확인 → True 정정" + (" [dry-run]" if dry_run else ""))
            if not dry_run:
                row["result"] = "True"
            fixed += 1
        else:
            print(f"image-scan 실패이나 redirect 아님 — 유지")
            skipped += 1

    if not dry_run and fixed > 0:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n저장 완료: {csv_path}")

    print(f"\n결과: {fixed}개 정정, {skipped}개 유지" + (" (dry-run)" if dry_run else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="대상 CSV 파일 경로")
    parser.add_argument("--namespace", default="default-cicd")
    parser.add_argument("--dry-run", action="store_true", help="실제 변경 없이 확인만")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"파일 없음: {csv_path}", file=sys.stderr)
        sys.exit(1)

    fix_csv(csv_path, args.namespace, args.dry_run)
