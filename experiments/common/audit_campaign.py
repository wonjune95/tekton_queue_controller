#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""캠페인 종료 후 «놓친 것» 전수 점검.

사람이 눈으로 찾지 않게 만든 감사 도구다. 2026-08-03 기준으로 실제로 겪은 누락 유형을 전부 검사한다.
  · 논문 3장이 약속한 반복 수를 4장이 못 채우는 «장 간 불일치» (Volcano 6회·C-K N=5 에서 두 번 발생)
  · 요약 JSON 이 지표를 원리적으로 놓치는 경우 (node_ready 를 peak 으로 집계 → NotReady 가 안 보임)
  · 무효 처리한 회차가 실제로 대체되지 않은 경우 (Harbor 고갈분)
  · 부분 산출물이 정상분에 섞인 경우

사용:
    python3 common/audit_campaign.py                 # results/ 를 점검
    python3 common/audit_campaign.py --root results  # 경로 지정
종료 코드: 지적 사항이 하나라도 있으면 1.
"""
import argparse
import csv
import glob
import json
import math
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── 계획 매트릭스 ────────────────────────────────────────────────
# HANDOVER §«핵심 실험» + 2026-08-02 두 차례 복귀 결정(Volcano 6회·C-K 보강 4회) 기준.
# (조건 디렉터리, 표시명, 측정 CSV 글롭, 논문이 인가한 최소 반복 수, 근거)
PLAN = [
    ("s0_baseline",          "S0 정상부하",        "experiment_run*.csv", 3,  "μ 추정·기준 소요"),
    ("s1_peak_hour",         "S1 피크",            "run*.csv",            5,  "본실험"),
    ("s2_release_burst",     "S2 릴리스 버스트",   "run*.csv",            5,  "본실험"),
    ("s3_priority",          "S3 우선순위",        "run*.csv",            5,  "본실험"),
    ("a2_no_aging",          "A2 에이징 off",      "run*.csv",            5,  "에이블레이션"),
    ("a3_single_fifo",       "A3 단일 FIFO",       "run*.csv",            3,  "에이블레이션"),
    ("v_phantom_named",      "V 유령 이름",        "run*.csv",            2,  "검증"),
    ("a0r_no_controller",    "A0-R 컨트롤러 없음", "*.csv",               6,  "요인 2x2"),
    ("lmax_sweep",           "L_max 민감도",       "lmax*_run*.csv",      6,  "2026-07-31 복귀"),
    ("cmp_tekton_kueue",     "C-K tekton-kueue",   "s*_run*.csv",        15,  "논문 3장: S1~S3 각 N>=5"),
    ("cmp_volcano",          "C-V Volcano",        "s*_run*.csv",         6,  "논문 3장: S1·S2 각 N>=3"),
    ("a1_counter_isolation", "A1 카운터 격리",     "run*.csv",            3,  "파괴적"),
    ("a0nr_no_requests",     "A0-NR 무설정",       "run*.csv",            3,  "파괴적"),
]

# 최소값이 의미를 갖는 지표 — peak 집계로는 이상을 볼 수 없다.
MIN_MATTERS = {"node_ready"}

# 내 컨트롤러를 **의도적으로 끄는** 조건. 웹훅이 호출되지 않으므로 웹훅 지연 지표가 없는 것이 정상이다.
#   (이 예외가 없으면 감사가 24건을 결측으로 지적해 진짜 문제를 가린다.)
CONTROLLER_OFF = {"a0r_no_controller", "cmp_tekton_kueue", "cmp_volcano", "a0nr_no_requests"}
WEBHOOK_METRICS = {"webhook_latency_p50_seconds", "webhook_latency_p99_seconds"}

# 도착이 **푸아송**인 조건 — 시드마다 인가 건수가 달라지는 것이 설계다(고정 버스트가 아니다).
#   따라서 건수 편차를 조건 흔들림으로 지적하면 안 된다.
POISSON_ARRIVAL = {"s0_baseline", "s1_peak_hour", "s3_priority", "a2_no_aging",
                   "a3_single_fifo", "a0r_no_controller", "cmp_tekton_kueue", "cmp_volcano"}


def measure_csvs(root, d, pat):
    """측정 CSV 만 (자원 시계열 CSV 는 제외)."""
    return sorted(
        f for f in glob.glob(os.path.join(root, d, pat))
        if not f.endswith("_resource.csv")
    )


def rows_of(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="results")
    args = ap.parse_args()
    root = args.root
    issues = []          # (심각도, 메시지)
    def bad(msg):  issues.append(("지적", msg))
    def warn(msg): issues.append(("주의", msg))

    if not os.path.isdir(root):
        print(f"[중단] 결과 디렉터리가 없다: {root}")
        return 2

    # ── 1. 매트릭스 대조 ──────────────────────────────────────
    print("=" * 78)
    print("1. 계획 매트릭스 대조 — 논문이 인가한 반복 수를 채웠는가")
    print("=" * 78)
    print(f'{"조건":22s} {"실제":>4s} {"계획":>4s}  {"판정":6s} 근거')
    print("-" * 78)
    total_got = total_plan = 0
    for d, label, pat, n, why in PLAN:
        files = measure_csvs(root, d, pat)
        got = len(files)
        total_got += got
        total_plan += n
        if got == 0:
            verdict = "미수행"
            bad(f"{label}: 회차가 하나도 없다 (계획 {n}회, 근거: {why})")
        elif got < n:
            verdict = "부족"
            bad(f"{label}: {got}/{n}회 — {n - got}회 부족 (근거: {why})")
        else:
            verdict = "충족"
        print(f"{label:22s} {got:4d} {n:4d}  {verdict:6s} {why}")
    print("-" * 78)
    print(f'{"합계":22s} {total_got:4d} {total_plan:4d}')

    # ── 2. 파일 쌍 무결성 ────────────────────────────────────
    print()
    print("=" * 78)
    print("2. 산출물 무결성 — 측정 CSV 마다 자원 시계열·요약이 짝지어 있는가")
    print("=" * 78)
    n_ok = 0
    for d, label, pat, _n, _why in PLAN:
        for f in measure_csvs(root, d, pat):
            base = f[:-4]
            rcsv, rjson = base + "_resource.csv", base + "_resource.json"
            tag = os.path.relpath(f, root)
            if not os.path.exists(rcsv):
                bad(f"{tag}: 자원 시계열 CSV 없음")
                continue
            if not os.path.exists(rjson):
                bad(f"{tag}: 자원 요약 JSON 없음")
                continue
            try:
                rows = rows_of(f)
            except Exception as e:
                bad(f"{tag}: 측정 CSV 읽기 실패 — {e}")
                continue
            if not rows:
                bad(f"{tag}: 측정 CSV 가 비어 있다 (부분 산출물일 수 있다)")
                continue
            if os.path.getsize(rcsv) < 1000:
                bad(f"{tag}: 자원 시계열이 지나치게 작다 ({os.path.getsize(rcsv)} B)")
                continue
            n_ok += 1
    print(f"  정상 짝: {n_ok}건")

    # ── 3. 지표 결측 ─────────────────────────────────────────
    print()
    print("=" * 78)
    print("3. 지표 결측 — 요약 JSON 의 peak 이 None/NaN 인 항목")
    print("=" * 78)
    missing = defaultdict(list)
    expected = defaultdict(int)   # 컨트롤러 off 조건의 웹훅 지표 — 없는 것이 정상
    for jf in sorted(glob.glob(os.path.join(root, "*", "*_resource.json"))):
        if "INVALID" in jf or ".orig." in jf or ".prewarmupfix." in jf:
            continue
        cond = os.path.basename(os.path.dirname(jf))
        try:
            j = json.load(open(jf, encoding="utf-8"))
        except Exception as e:
            bad(f"{os.path.relpath(jf, root)}: 요약 JSON 읽기 실패 — {e}")
            continue
        for k, v in (j.get("peaks") or {}).items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                if k in WEBHOOK_METRICS and cond in CONTROLLER_OFF:
                    expected[k] += 1     # 컨트롤러를 껐으니 웹훅 호출이 없다 — 정상
                else:
                    missing[k].append(os.path.relpath(jf, root))
    if not missing:
        print("  설명되지 않는 결측 없음")
    for k, v in sorted(missing.items(), key=lambda x: -len(x[1])):
        print(f"  {k:34s} {len(v):3d}건 결측  예: {v[0]}")
        warn(f"지표 {k} 가 {len(v)}건에서 결측 — 4장에 쓸 지표면 원자료 CSV 에서 재계산할 것")
    for k, c in sorted(expected.items()):
        print(f"  (정상) {k:28s} {c:3d}건 — 컨트롤러를 끈 조건이라 웹훅 호출 자체가 없다")

    # ── 4. peak 으로는 못 보는 지표 (최소값 재검) ────────────
    print()
    print("=" * 78)
    print("4. 최소값이 의미 있는 지표 재검 — peak 집계로는 이상이 안 보인다")
    print("=" * 78)
    for metric in sorted(MIN_MATTERS):
        hits = []
        for rc in sorted(glob.glob(os.path.join(root, "*", "*_resource.csv"))):
            if "INVALID" in rc:
                continue
            per = defaultdict(list)
            try:
                for r in csv.DictReader(open(rc, encoding="utf-8")):
                    if r.get("metric") == metric:
                        per[r["label"]].append(float(r["value"]))
            except Exception:
                continue
            if not per:
                continue
            for node, vals in per.items():
                low = [x for x in vals if x < 1]
                if low:
                    hits.append((os.path.relpath(rc, root), node, len(low)))
        print(f"  [{metric}] 이상 관측 {len(hits)}건")
        for tag, node, cnt in hits:
            print(f"    {tag:44s} {node:46s} {cnt}표본")
        if hits:
            warn(f"{metric}: {len(hits)}건에서 이상 구간 — 요약 JSON 만 보면 놓친다. 4장 서술에 반영할 것")

    # ── 5. 회차 간 부하·결과 일관성 ──────────────────────────
    print()
    print("=" * 78)
    print("5. 회차 간 일관성 — 부하량·실패율이 회차마다 크게 흔들리지 않는가")
    print("=" * 78)
    print(f'{"조건":22s} {"N":>3s} {"인가 건수":>16s} {"완료율%":>16s}')
    print("-" * 78)
    for d, label, pat, _n, _why in PLAN:
        files = measure_csvs(root, d, pat)
        if not files:
            continue
        ns, comps = [], []
        for f in files:
            try:
                rows = rows_of(f)
            except Exception:
                continue
            if not rows:
                continue
            ns.append(len(rows))
            c = Counter(r.get("result") for r in rows)
            comps.append(100 * c.get("True", 0) / len(rows))
        if not ns:
            continue
        nrange = f"{min(ns)}~{max(ns)}"
        crange = f"{min(comps):.1f}~{max(comps):.1f}"
        note = "푸아송" if d in POISSON_ARRIVAL else "고정"
        print(f"{label:22s} {len(ns):3d} {nrange:>16s} {crange:>16s}  {note}")
        # 고정 버스트인데 건수가 흔들리면 조건이 어긋난 것이다.
        # 푸아송 도착은 시드마다 건수가 달라지는 것이 설계이므로 지적하지 않는다.
        if d not in POISSON_ARRIVAL and min(ns) and max(ns) != min(ns):
            bad(f"{label}: 고정 부하인데 인가 건수가 회차마다 다르다({nrange}) — 부하 인가 실패 의심")

    # ── 6. 무효 격리분이 대체됐는가 ──────────────────────────
    print()
    print("=" * 78)
    print("6. 무효 처리분 — 격리한 회차가 실제로 다시 수행됐는가")
    print("=" * 78)
    inv_dirs = sorted(glob.glob(os.path.join(root, "*INVALID*"))) + \
               sorted(glob.glob(os.path.join(os.path.dirname(root) or ".", "results_invalid", "*")))
    if not inv_dirs:
        print("  격리 폴더 없음")
    for p in inv_dirs:
        cnt = len([f for f in glob.glob(os.path.join(p, "**", "*.csv"), recursive=True)
                   if not f.endswith("_resource.csv")])
        print(f"  {os.path.relpath(p):58s} 측정 CSV {cnt}건")
    print("  ※ 위 회차들이 정상 폴더에서 같은 번호로 다시 채워졌는지는 1번 표의 «충족» 여부로 판단한다.")

    # ── 결과 ────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("감사 결과")
    print("=" * 78)
    if not issues:
        print("  지적 사항 없음.")
        return 0
    for sev, msg in issues:
        print(f"  [{sev}] {msg}")
    n_bad = sum(1 for s, _ in issues if s == "지적")
    print()
    print(f"  지적 {n_bad}건 / 주의 {len(issues) - n_bad}건")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
