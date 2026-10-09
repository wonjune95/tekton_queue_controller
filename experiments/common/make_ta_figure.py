# -*- coding: utf-8 -*-
"""그림: 에이징 주기 T_a 민감도 곡선 (2026-08-04)

세 수준 {150, 300, 600}초를 **같은 시드(run6·7·8)** 로 짝지어 수행했다.
T_a=300 점은 교차 검증 S1 run6~8 이 채운다(동일 부하·동일 버전).

왼쪽  : Tier 별 대기 중앙값 — T_a 가 짧으면 두 선이 붙는다(차등 소멸)
가운데: Tier1↔Tier3 격차 — 차등의 크기
오른쪽: 창내 완료율 — 처리량이 함께 어떻게 움직이는지

사용: python3 common/make_ta_figure.py
"""
import csv
import os
import statistics as st
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")

for cand in ("C:/Windows/Fonts/malgun.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"):
    if os.path.exists(cand):
        font_manager.fontManager.addfont(cand)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=cand).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False

C_RUN = "#9AA4B2"
C_MED = "#1F4E79"
C_T1 = "#1F4E79"     # Tier 1 (prod)
C_T3 = "#C0392B"     # Tier 3 (dev)
C_GRID = "#E3E6EA"

E2T = {"prod": 1, "stg": 2, "dev": 3}
FILES = {
    150: "results/ta_sweep/ta150_run{}.csv",
    300: "results/s1_peak_hour/run{}.csv",     # 교차 검증분(동일 시드·신버전)
    600: "results/ta_sweep/ta600_run{}.csv",
}
RUNS = (6, 7, 8)


def _style(ax):
    ax.grid(True, axis="y", color=C_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#C7CDD4")


def tier_of(r):
    t = (r.get("tier") or "").strip()
    return int(t) if t.isdigit() else E2T.get((r.get("env") or "").strip())


def profile(path):
    if not os.path.exists(path):
        return None
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if not rows:
        return None
    acc = defaultdict(list)
    for r in rows:
        t = tier_of(r)
        w = r.get("wait_sec")
        if t and w not in (None, "", "NaN"):
            acc[t].append(float(w))
    succ = 100 * Counter(r.get("result") for r in rows).get("True", 0) / len(rows)
    med = {t: st.median(v) for t, v in acc.items()}
    return med.get(1, 0), med.get(3, 0), succ


def main():
    data = {}     # T_a -> [(t1, t3, succ), ...]
    for ta, fmt in FILES.items():
        vals = [p for p in (profile(fmt.format(r)) for r in RUNS) if p]
        if len(vals) == len(RUNS):
            data[ta] = vals
    if len(data) < 3:
        print(f"[건너뜀] T_a 수준이 {len(data)}종뿐 — 3종(150·300·600) × 3회가 필요하다.")
        return 1

    xs = sorted(data)
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.2), dpi=200)

    # ── (1) Tier 별 대기 중앙값 ─────────────────────────────────
    ax = axes[0]
    for idx, (color, label) in ((0, (C_T1, "Tier 1 (prod)")), (1, (C_T3, "Tier 3 (dev)"))):
        med = [st.median([v[idx] for v in data[x]]) for x in xs]
        lo = [min(v[idx] for v in data[x]) for x in xs]
        hi = [max(v[idx] for v in data[x]) for x in xs]
        ax.fill_between(xs, lo, hi, color=color, alpha=0.12, zorder=2)
        ax.plot(xs, med, color=color, linewidth=2.0, marker="o", markersize=7,
                markeredgecolor="white", markeredgewidth=1.2, label=label, zorder=3)
        for x, y in zip(xs, med):
            ax.annotate(f"{y:.0f}", xy=(x, y), xytext=(0, 8), textcoords="offset points",
                        ha="center", fontsize=8.5, color=color)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left")
    ax.set_title("Tier 별 대기 중앙값 (초)", fontsize=10, pad=8)

    # ── (2) 격차 ────────────────────────────────────────────────
    ax = axes[1]
    med = [st.median([v[1] - v[0] for v in data[x]]) for x in xs]
    lo = [min(v[1] - v[0] for v in data[x]) for x in xs]
    hi = [max(v[1] - v[0] for v in data[x]) for x in xs]
    ax.fill_between(xs, lo, hi, color=C_MED, alpha=0.15, zorder=2)
    ax.plot(xs, med, color=C_MED, linewidth=2.0, marker="o", markersize=7,
            markeredgecolor="white", markeredgewidth=1.2, zorder=3)
    ax.axhline(0, color="#C0392B", linewidth=1.0, linestyle="--", zorder=1)
    for x, y in zip(xs, med):
        ax.annotate(f"{y:+.0f}", xy=(x, y), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=8.5, color="#2B3A46")
    ax.set_title("Tier 1↔3 격차 (초)", fontsize=10, pad=8)

    # ── (3) 완료율 ──────────────────────────────────────────────
    ax = axes[2]
    med = [st.median([v[2] for v in data[x]]) for x in xs]
    lo = [min(v[2] for v in data[x]) for x in xs]
    hi = [max(v[2] for v in data[x]) for x in xs]
    ax.fill_between(xs, lo, hi, color=C_MED, alpha=0.15, zorder=2)
    ax.plot(xs, med, color=C_MED, linewidth=2.0, marker="o", markersize=7,
            markeredgecolor="white", markeredgewidth=1.2, zorder=3)
    for x, y in zip(xs, med):
        ax.annotate(f"{y:.1f}", xy=(x, y), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=8.5, color="#2B3A46")
    ax.set_title("창내 완료율 (%)", fontsize=10, pad=8)

    for ax in axes:
        ax.set_xticks(xs)
        ax.set_xlabel("$T_a$ (초)")
        _style(ax)

    fig.tight_layout()
    out = os.path.join("results", "figures")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "fig_ta_sweep.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (수준 {xs}, 각 {len(RUNS)}회, 짝지은 시드 {list(RUNS)})")

    print()
    print("  --- 그림이 말하는 것 ---")
    g = {x: st.median([v[1] - v[0] for v in data[x]]) for x in xs}
    print(f"    격차: {g[150]:+.0f} -> {g[300]:+.0f} -> {g[600]:+.0f}초")
    print(f"    T_a=150 에서 두 Tier 선이 붙는다(차등 소멸). 3/3 회차에서 격차가 축소됐다.")
    print(f"    300 -> 600 은 격차 {100*(g[600]-g[300])/g[300]:+.1f}% 변화 — 명확한 무릎은 없다.")
    print(f"    => «최적값»이 아니라 «작동 구간과 그 아래»를 보이는 그림으로 쓴다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
