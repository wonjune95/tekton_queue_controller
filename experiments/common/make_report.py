"""
실험 결과 CSV들을 읽어 Markdown 보고서를 생성한다.
"""
import csv
import os
import argparse
import datetime
from pathlib import Path


# ⚠️ 키는 `results/` 아래 **실제 디렉터리명**이어야 한다.
#   예전 목록은 s3_adversarial·a0_no_controller·a1_no_admitted 로 되어 있었는데
#   실제 디렉터리는 s3_priority·a0r_no_controller·a1_counter_isolation 이다.
#   그 결과 **데이터가 있어도 보고서에 계속 '—' 로 나왔다**(2026-07-31 발견: S3 1회 실행분이 누락).
#   시나리오를 추가·개명하면 이 목록도 함께 고칠 것.
EXPERIMENTS = [
    ("s0_baseline",         "S0 Baseline (λ=1/분, 여유 ρ=0.12)"),
    ("s1_peak_hour",        "S1 Peak-hour (저부하↔피크 교차, 피크 ρ=1.20)"),
    ("s2_release_burst",    "S2 Release Burst (90건/30초 일괄)"),
    ("s3_priority",         "S3 Priority (λ=3/분 지속, 안정 ρ=0.36)"),
    ("a2_no_aging",         "A2 에이징 제거"),
    ("a3_single_fifo",      "A3 단일 FIFO (Tier 차등 제거)"),
    ("v_phantom_named",     "V phantom 검증 (카운터 off, named)"),
    ("a0r_no_controller",   "A0-R 컨트롤러 없음 (requests 설정)"),
    ("a0nr_no_requests",    "A0-NR 컨트롤러·requests 모두 없음 (파괴적)"),
    ("a1_counter_isolation","A1 admitted 카운터 격리 (파괴적)"),
    ("lmax_sweep",          "L_max 민감도 스윕"),
    ("cmp_tekton_kueue",    "비교군 tekton-kueue"),
    ("cmp_volcano",         "비교군 Volcano"),
]


def load_csv(path: Path):
    if not path.exists():
        return []
    # 인코딩을 명시하지 않으면 Windows 에서 로캘(cp949)로 읽어 UTF-8 CSV 에서 깨진다.
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def stats(rows, field):
    vals = [float(r[field]) for r in rows if r.get(field) not in (None, "", "None")]
    if not vals:
        return None, None, None
    return sum(vals) / len(vals), min(vals), max(vals)


def summarize(rows):
    if not rows:
        return None
    total = len(rows)
    completed = sum(1 for r in rows if r.get("result") == "True")
    rate = completed / total * 100 if total else 0

    wait_avg, wait_min, wait_max = stats(rows, "wait_sec")
    total_avg, _, _ = stats(rows, "total_sec")

    # max_concurrent: sliding window from start/end times
    events = []
    for r in rows:
        if r.get("start_time") and r["start_time"] not in ("None", ""):
            events.append((r["start_time"], +1))
        if r.get("end_time") and r["end_time"] not in ("None", ""):
            events.append((r["end_time"], -1))
    events.sort()
    cur = max_c = 0
    for _, d in events:
        cur += d
        max_c = max(max_c, cur)

    # per-tier wait
    tier_stats = {}
    for env in ["prod", "stg", "dev"]:
        subset = [r for r in rows if r.get("env") == env and r.get("wait_sec") not in (None, "", "None")]
        if subset:
            waits = [float(r["wait_sec"]) for r in subset]
            tier_stats[env] = (len(subset), sum(waits) / len(waits), max(waits))

    return {
        "total": total,
        "completed": completed,
        "completion_rate": rate,
        "wait_avg": wait_avg,
        "wait_min": wait_min,
        "wait_max": wait_max,
        "total_avg": total_avg,
        "max_concurrent": max_c,
        "tier_stats": tier_stats,
    }


def fmt(v, unit="", digits=1):
    if v is None:
        return "N/A"
    return f"{v:.{digits}f}{unit}"


def make_report(results_dir: Path, output: Path):
    lines = []
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines += [
        f"# Tekton Queue Controller 실험 결과 보고서",
        f"",
        f"생성 시각: {now}  ",
        f"클러스터: nonmoon (GKE 5-node)  ",
        f"Lmax=30, Aging=300s, 4-tier 우선순위",
        f"",
        f"---",
        f"",
    ]

    # Summary table
    lines += [
        "## 요약 테이블",
        "",
        "| 실험 | PR수 | 완료율 | 평균대기(s) | 최대대기(s) | 최대동시실행 | 평균파이프라인(s) |",
        "|------|------|--------|------------|------------|------------|----------------|",
    ]

    summaries = {}
    for key, label in EXPERIMENTS:
        # 파일명 변형 탐색: run1.csv → experiment_run1.csv → run1_rate*.csv
        result_dir = results_dir / key
        csv_path = result_dir / "run1.csv"
        if not csv_path.exists():
            # `*_resource.csv` 는 스키마가 다른 자원 시계열이다. 이걸 집으면
            # summarize() 가 빈 결과를 내고 조용히 '—' 가 찍힌다. 반드시 제외한다.
            candidates = sorted(p for p in result_dir.glob("*.csv")
                                if not p.name.endswith("_resource.csv")) \
                if result_dir.exists() else []
            csv_path = candidates[0] if candidates else csv_path
        rows = load_csv(csv_path)
        s = summarize(rows)
        summaries[key] = (label, s)
        if s:
            lines.append(
                f"| {label} | {s['total']} | {fmt(s['completion_rate'], '%')} "
                f"| {fmt(s['wait_avg'], 's')} | {fmt(s['wait_max'], 's')} "
                f"| {s['max_concurrent']} | {fmt(s['total_avg'], 's')} |"
            )
        else:
            lines.append(f"| {label} | — | — | — | — | — | — |")

    lines += ["", "---", ""]

    # Per-experiment detail
    lines += ["## 실험별 상세 결과", ""]
    for key, label in EXPERIMENTS:
        label_full, s = summaries[key]
        lines += [f"### {label_full}", ""]
        if not s:
            lines += ["결과 파일 없음 (실험 미완료)", ""]
            continue
        lines += [
            f"- **PipelineRun 총 수**: {s['total']}",
            f"- **완료율**: {fmt(s['completion_rate'], '%')}  ({s['completed']}/{s['total']})",
            f"- **최대 동시 실행**: {s['max_concurrent']}  (Lmax=30)",
            f"- **대기 시간**: 평균 {fmt(s['wait_avg'], 's')}, 최대 {fmt(s['wait_max'], 's')}",
            f"- **파이프라인 소요**: 평균 {fmt(s['total_avg'], 's')}",
            "",
        ]
        if s["tier_stats"]:
            lines += ["**Tier별 대기 시간:**", ""]
            lines += ["| Tier | 건수 | 평균대기(s) | 최대대기(s) |"]
            lines += ["|------|------|------------|------------|"]
            for env, (cnt, avg, mx) in s["tier_stats"].items():
                lines.append(f"| {env} | {cnt} | {avg:.1f} | {mx:.1f} |")
            lines += [""]

    # Hypothesis checks
    lines += [
        "---",
        "",
        "## 가설 검증 요약",
        "",
        "| 가설 | 측정 지표 | 결과 |",
        "|------|----------|------|",
    ]

    s0 = summaries.get("s0_baseline", (None, None))[1]
    s1 = summaries.get("s1_peak_hour", (None, None))[1]
    s2 = summaries.get("s2_release_burst", (None, None))[1]
    a1 = summaries.get("a1_no_admitted", (None, None))[1]
    a2 = summaries.get("a2_no_aging", (None, None))[1]
    a0 = summaries.get("a0_no_controller", (None, None))[1]

    def check(cond):
        return "✅ 충족" if cond else "❌ 미충족"

    h1 = check(s0 and s0["max_concurrent"] <= 30)
    h1a = check(a1 and a1["max_concurrent"] > 30)
    h2 = check(s2 and s2["max_concurrent"] <= 30)
    h3_prod = None
    h3_dev = None
    if s1:
        ts = s1["tier_stats"]
        if "prod" in ts and "dev" in ts:
            h3_prod = ts["prod"][1]
            h3_dev = ts["dev"][1]
    h3 = check(h3_prod is not None and h3_dev is not None and h3_prod < h3_dev)
    h4 = check(a0 and s2 and a0["max_concurrent"] > s2["max_concurrent"])

    mc_s0 = s0["max_concurrent"] if s0 else "N/A"
    mc_a1 = a1["max_concurrent"] if a1 else "N/A"
    mc_s2 = s2["max_concurrent"] if s2 else "N/A"
    mc_a0 = a0["max_concurrent"] if a0 else "N/A"

    lines += [
        f"| H1: max_concurrent ≤ Lmax(30) | max_concurrent={mc_s0} | {h1} |",
        f"| H1 대조: Lmax 제거시 초과 | max_concurrent={mc_a1} | {h1a} |",
        f"| H2: Burst 시에도 Lmax 유지 | max_concurrent={mc_s2} | {h2} |",
        f"| H3: prod 대기 < dev 대기 | prod={fmt(h3_prod,'s')} dev={fmt(h3_dev,'s')} | {h3} |",
        f"| H4: 컨트롤러 없으면 동시실행 폭증 | no-ctrl={mc_a0} vs ctrl={mc_s2} | {h4} |",
        "",
        "---",
        "",
        f"*자동 생성: make_report.py*",
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ write_text 는 인코딩 미지정 시 Windows 로캘(cp949)을 쓴다.
    #   본문에 em dash 등 cp949 에 없는 문자가 있으면 UnicodeEncodeError 로 **보고서가 생성되지 않는다**
    #   (2026-07-31 청크 종료 시 실측). 측정 데이터와는 무관하나 요약이 통째로 날아간다.
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"보고서 저장: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    make_report(Path(args.results_dir), Path(args.output))
