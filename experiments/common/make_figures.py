# -*- coding: utf-8 -*-
"""
논문 그림 생성기 (results/ 의 CSV → PNG).

Grafana 스크린샷이 아니라 **원자료에서 직접 그린다.** 학술 논문의 결과 그림은
웹 UI 화면이 아니라 데이터 그림이어야 하고, 축·단위·글꼴을 통제할 수 있어야 한다.
Grafana 는 "모니터링 스택을 이렇게 구성했다"는 환경 소개용으로만 쓴다.

사용:
  python3 common/make_figures.py --fig concurrency --scenario s1_peak_hour
  python3 common/make_figures.py --fig all
출력: results/figures/*.png
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import statistics as st
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 한글 축·범례가 깨지지 않도록 시스템 글꼴을 지정한다(Windows 맑은 고딕).
for cand in ("C:/Windows/Fonts/malgun.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"):
    if os.path.exists(cand):
        font_manager.fontManager.addfont(cand)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=cand).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False   # 음수 기호가 네모로 깨지는 것 방지

# 반복 회차는 "같은 양의 재현"이므로 색으로 구분하지 않는다.
# 서로 다른 5색을 쓰면 회차 번호가 의미를 가진 것처럼 읽히고 색맹 안전성도 따져야 한다.
# 개별 반복은 한 가지 옅은 색, 중앙값만 진한 색 — 두 계열이라 범례도 2개면 충분하다.
C_RUN = "#9AA4B2"     # 개별 반복(중립 회색)
C_MED = "#1F4E79"     # 중앙값(진한 청색)
C_GRID = "#E3E6EA"
C_EDGE = "#2B3A46"    # 마크 테두리(대비 담당)

# Tier 는 **순서가 있는 값**(1 = 최우선)이므로 서로 다른 색상이 아니라
# 한 색상의 명도 단계(순차 램프)로 표현한다. 색상 구분이 아니므로 색맹 안전성 문제가 없고,
# 우선순위의 크고 작음이 밝기로 읽힌다.
# 밝은 단계는 배경 대비가 낮으므로 **진한 테두리 + 값 직접 표기**로 보완한다
# (x축이 이미 Tier 를 표시하므로 색은 보조 부호이며, 색만으로 식별하지 않는다).
TIER_RAMP = {1: "#1F4E79", 2: "#3D7AB8", 3: "#86B6DE"}
TIER_FALLBACK = "#C8D3DD"


def load_series(path, metric, label=None):
    """(초 단위 상대시각, 값) 목록. label 지정 시 해당 시리즈만."""
    pts = []
    with open(path, encoding="utf-8") as f:
        for m, ts, lb, v in csv.reader(f):
            if m != metric:
                continue
            if label and lb != label:
                continue
            try:
                val = float(v)
            except ValueError:
                continue
            if val != val:      # NaN
                continue
            pts.append((dt.datetime.fromisoformat(ts.replace("Z", "+00:00")), val))
    if not pts:
        return []
    pts.sort()
    t0 = pts[0][0]
    return [((t - t0).total_seconds() / 60.0, v) for t, v in pts]


def fig_concurrency(results, scenario, out):
    """그림: 동시 실행 파드 수 시계열 — 반복 중첩."""
    files = sorted(glob.glob(os.path.join(results, scenario, "*_resource.csv")))
    if not files:
        print(f"[건너뜀] {scenario}: 자료 없음"); return
    series = [load_series(f, "ns_running_pods") for f in files]
    series = [s for s in series if s]
    if not series:
        print(f"[건너뜀] {scenario}: ns_running_pods 없음"); return

    fig, ax = plt.subplots(figsize=(7.0, 3.4), dpi=200)
    for s in series:
        ax.plot([x for x, _ in s], [y for _, y in s],
                color=C_RUN, linewidth=1.0, alpha=0.75, zorder=2)

    # 중앙값: 회차마다 표본 시각이 달라 공통 격자에 맞춰 계산한다.
    tmax = min(max(x for x, _ in s) for s in series)
    grid = [i * 0.5 for i in range(int(tmax / 0.5) + 1)]
    med = []
    for g in grid:
        vals = []
        for s in series:
            best = min(s, key=lambda p: abs(p[0] - g))
            if abs(best[0] - g) <= 0.5:
                vals.append(best[1])
        if vals:
            med.append((g, st.median(vals)))
    ax.plot([x for x, _ in med], [y for _, y in med],
            color=C_MED, linewidth=2.0, zorder=3)

    ax.set_xlabel("측정 창 경과 시간 (분)")
    ax.set_ylabel("동시 실행 파드 수")
    ax.set_ylim(bottom=0)
    ax.grid(True, color=C_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#B8BFC7")

    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], color=C_RUN, lw=1.0, label=f"개별 반복 (N={len(series)})"),
                       Line2D([], [], color=C_MED, lw=2.0, label="중앙값")],
              frameon=False, loc="upper right", fontsize=9)
    fig.tight_layout()
    path = os.path.join(out, f"fig_concurrency_{scenario}.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (반복 {len(series)}회)")


def fig_node_memory(results, scenario, out):
    """그림: build 노드 메모리 사용률 시계열 — 반복 중첩."""
    files = sorted(glob.glob(os.path.join(results, scenario, "*_resource.csv")))
    if not files:
        print(f"[건너뜀] {scenario}: 자료 없음"); return
    # build 노드만: 회차별로 최대 사용률을 낸 노드를 고른다(어느 노드든 동등한 build 노드).
    BUILD = {"10.128.0.17:9100", "10.128.0.18:9100", "10.128.0.19:9100"}
    series = []
    for f in files:
        best = None
        for lb in BUILD:
            s = load_series(f, "node_mem_util_ratio", lb)
            if s and (best is None or max(v for _, v in s) > max(v for _, v in best)):
                best = s
        if best:
            series.append(best)
    if not series:
        print(f"[건너뜀] {scenario}: 노드 메모리 없음"); return

    fig, ax = plt.subplots(figsize=(7.0, 3.4), dpi=200)
    for s in series:
        ax.plot([x for x, _ in s], [y * 100 for _, y in s],
                color=C_RUN, linewidth=1.0, alpha=0.75, zorder=2)
    ax.set_xlabel("측정 창 경과 시간 (분)")
    ax.set_ylabel("노드 메모리 사용률 (%)")
    ax.set_ylim(0, 100)
    ax.grid(True, color=C_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#B8BFC7")
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], color=C_RUN, lw=1.0,
                              label=f"build 노드 최대 (반복 {len(series)}회)")],
              frameon=False, loc="lower right", fontsize=9)
    fig.tight_layout()
    path = os.path.join(out, f"fig_node_memory_{scenario}.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (반복 {len(series)}회)")


def _style(ax):
    ax.grid(True, color=C_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#B8BFC7")


def _load_runs(results, scenario):
    """시나리오의 회차 CSV 를 모두 읽어 행 목록으로 돌려준다(리소스 CSV 제외)."""
    rows = []
    for f in sorted(glob.glob(os.path.join(results, scenario, "*.csv"))):
        if f.endswith("_resource.csv"):
            continue
        with open(f, encoding="utf-8") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


# 비교군(cmp_*) 회차는 **내 컨트롤러가 꺼진 상태**로 수행되므로
# `queue.tekton.dev/tier` 라벨이 붙지 않아 tier 컬럼이 '?' 다.
# 부하 발생기가 심는 env 는 그대로 있으므로, 내 컨트롤러의 tierRules 와 동일한 규칙으로 대응시킨다.
#   tierRules: prod→1, stg→2, 그 외(dev)→3   (Tier 0 = urgent 라벨은 본 캠페인 부하에 없음)
ENV_TO_TIER = {"prod": 1, "stg": 2, "dev": 3}


def _tier_of(row):
    """tier 컬럼을 우선 쓰되, 없거나 '?' 면 env 로 환산한다."""
    raw = (row.get("tier") or "").strip()
    if raw.isdigit():
        return int(raw)
    return ENV_TO_TIER.get((row.get("env") or "").strip())


def fig_tier_wait(results, scenario, out):
    """그림: Tier 별 대기 시간 박스플롯 (반복 통합)."""
    rows = _load_runs(results, scenario)
    by_tier = {}
    for r in rows:
        t = _tier_of(r)
        try:
            w = float(r.get("wait_sec", ""))
        except (TypeError, ValueError):
            continue
        if t is None:
            continue
        by_tier.setdefault(t, []).append(w)
    by_tier = {t: v for t, v in by_tier.items() if v}
    if len(by_tier) < 2:
        print(f"[건너뜀] {scenario}: Tier 가 2종 미만"); return

    tiers = sorted(by_tier)
    data = [by_tier[t] for t in tiers]
    fig, ax = plt.subplots(figsize=(5.6, 3.4), dpi=200)
    bp = ax.boxplot(data, patch_artist=True, widths=0.55, showfliers=False,
                    medianprops=dict(color=C_EDGE, linewidth=1.8),
                    boxprops=dict(edgecolor=C_EDGE, linewidth=1.2),
                    whiskerprops=dict(color=C_EDGE, linewidth=1.0),
                    capprops=dict(color=C_EDGE, linewidth=1.0))
    for patch, t in zip(bp["boxes"], tiers):
        patch.set_facecolor(TIER_RAMP.get(t, TIER_FALLBACK))

    # 중앙값을 직접 표기한다 — 대비가 낮은 밝은 단계도 값으로 읽히게 하는 보완책.
    # ⚠️ 라벨은 상자 **위쪽 여백**에 두고, 상자 안에 걸치는 경우 글자색을 배경 밝기에 맞춘다.
    #   (진한 Tier 1 상자에 진한 글씨를 얹으면 읽히지 않는다 — 실제로 그랬다.)
    def _ink(hex_color):
        r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return "#FFFFFF" if lum < 0.5 else "#2B3A46"

    for i, t in enumerate(tiers, start=1):
        m = st.median(by_tier[t])
        q = sorted(by_tier[t])
        q3 = q[int(len(q) * 0.75)] if len(q) > 3 else max(q)
        inside = m < q3          # 상자 안쪽이면 배경색에 맞춘 글자색
        ax.annotate(f"{m:.0f}s", xy=(i, m), xytext=(0, 7), textcoords="offset points",
                    ha="center", fontsize=9, fontweight="bold",
                    color=_ink(TIER_RAMP.get(t, TIER_FALLBACK)) if inside else "#2B3A46")

    ax.set_xticks(range(1, len(tiers) + 1))
    ax.set_xticklabels([f"Tier {t}\n(n={len(by_tier[t])})" for t in tiers])
    ax.set_ylabel("대기 시간 (초)")
    ax.set_ylim(bottom=0)
    _style(ax)
    fig.tight_layout()
    path = os.path.join(out, f"fig_tier_wait_{scenario}.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (Tier {len(tiers)}종, n={sum(len(v) for v in by_tier.values())})")


def fig_promotion(results, scenario, out, t_a=300):
    """그림: 승격 타임라인 — 도착 시각 대비 대기 시간, 승격 경계선 표시."""
    rows = _load_runs(results, scenario)
    pts = []
    t0 = None
    for r in rows:
        t = _tier_of(r)
        try:
            w = float(r.get("wait_sec", ""))
            c = dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError, KeyError):
            continue
        if t is None:
            continue
        pts.append((c, w, t))
    if not pts:
        print(f"[건너뜀] {scenario}: 승격 자료 없음"); return
    # 회차가 섞이므로 각 회차의 시작을 0 으로 맞춰야 겹쳐 볼 수 있다.
    # created_at 이 회차마다 다른 날/시각이므로, 30분 이상 벌어지면 새 회차로 본다.
    pts.sort()
    rebased, base, prev = [], pts[0][0], pts[0][0]
    for c, w, t in pts:
        if (c - prev).total_seconds() > 1800:
            base = c
        prev = c
        rebased.append(((c - base).total_seconds() / 60.0, w, t))

    fig, ax = plt.subplots(figsize=(7.0, 3.6), dpi=200)
    for t in sorted({p[2] for p in rebased}):
        xs = [x for x, _, tt in rebased if tt == t]
        ys = [y for _, y, tt in rebased if tt == t]
        ax.scatter(xs, ys, s=22, color=TIER_RAMP.get(t, TIER_FALLBACK),
                   edgecolor=C_EDGE, linewidth=0.5, alpha=0.85,
                   label=f"Tier {t}", zorder=3)

    # 승격 경계: 대기가 T_a 를 넘을 때마다 유효 Tier 가 1 단계 올라간다.
    # ⚠️ 단, Tier_eff = max(Tier_asgn − ⌊wait/T_a⌋, 1) 이므로 **Tier 1 에서 멈춘다.**
    #   최하위 Tier 라도 (최하위 − 1) 회까지만 승격되며, 그 이상은 더 오를 곳이 없다.
    #   전 구간에 T_a 간격으로 선을 그으면 **일어나지 않는 승격을 암시**해 오해를 낳는다.
    max_tier = max(p[2] for p in rebased)
    k_max = max(max_tier - 1, 0)          # 승격 가능 단계 수
    for k in range(1, k_max + 1):
        ax.axhline(k * t_a, color="#C0392B", linewidth=1.0, linestyle="--", zorder=2)
        ax.annotate(f"{k}단계 승격 ({k * t_a}s)", xy=(0, k * t_a), xytext=(3, 4),
                    textcoords="offset points", fontsize=8.5, color="#C0392B")
    if k_max:
        # 승격 상한(W_max) 위쪽은 더 이상 우선순위가 바뀌지 않는 구간이다.
        w_max = k_max * t_a
        ax.axhspan(w_max, max(y for _, y, _ in rebased) * 1.05,
                   color="#C0392B", alpha=0.05, zorder=1)
        ax.annotate(f"$W_{{max}}$={w_max}s 초과 — 전 항목 유효 Tier 1, 승격 종료",
                    xy=(0.99, 0.97), xycoords="axes fraction", ha="right", va="top",
                    fontsize=8.5, color="#8E2A21")

    ax.set_xlabel("도착 시각 (측정 창 경과 분)")
    ax.set_ylabel("대기 시간 (초)")
    ax.set_ylim(bottom=0)
    _style(ax)
    ax.legend(frameon=False, fontsize=9, loc="lower right", ncol=3)
    fig.tight_layout()
    path = os.path.join(out, f"fig_promotion_{scenario}.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (n={len(rebased)}, T_a={t_a}s)")


def fig_comparison_tier(results, out, scenario="s1"):
    """그림: 3자 비교 — Tier 별 대기 시간 (제안 컨트롤러 · tekton-kueue · Volcano).

    같은 상한(30) 조건에서 **정렬 정책의 차이**만 드러나도록 Tier 축으로 나란히 놓는다.
    비교군 회차는 tier 컬럼이 '?' 이므로 `_tier_of()` 가 env 로 환산한다.
    """
    src = {
        "제안 컨트롤러": ("s1_peak_hour" if scenario == "s1" else "s2_release_burst", None),
        "tekton-kueue": ("cmp_tekton_kueue", f"{scenario}_run"),
        "Volcano":      ("cmp_volcano",      f"{scenario}_run"),
    }
    data = {}
    for label, (d, prefix) in src.items():
        rows = []
        for f in sorted(glob.glob(os.path.join(results, d, "*.csv"))):
            if f.endswith("_resource.csv"):
                continue
            if prefix and not os.path.basename(f).startswith(prefix):
                continue
            with open(f, encoding="utf-8") as fh:
                rows.extend(csv.DictReader(fh))
        by = {}
        for r in rows:
            t = _tier_of(r)
            try:
                w = float(r.get("wait_sec", ""))
            except (TypeError, ValueError):
                continue
            if t is None:
                continue
            by.setdefault(t, []).append(w)
        if by:
            data[label] = by
    if len(data) < 2:
        print(f"[건너뜀] 3자 비교({scenario}): 자료가 {len(data)}종뿐"); return

    tiers = sorted({t for by in data.values() for t in by})
    labels = list(data)
    n = len(labels)
    width = 0.8 / n
    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=200)

    # 시스템은 **범주**(순서 없음)이므로 명도 대신 위치+해칭으로 구분하고,
    # 색은 Tier 순서 램프를 그대로 쓴다 — 축이 Tier, 묶음이 시스템이다.
    HATCH = ["", "//", ".."]
    for i, label in enumerate(labels):
        by = data[label]
        pos = [t + (i - (n - 1) / 2) * width for t in tiers]
        vals = [by.get(t, [0]) for t in tiers]
        bp = ax.boxplot(vals, positions=pos, widths=width * 0.85, patch_artist=True,
                        showfliers=False, manage_ticks=False,
                        medianprops=dict(color=C_EDGE, linewidth=1.6),
                        boxprops=dict(edgecolor=C_EDGE, linewidth=1.0),
                        whiskerprops=dict(color=C_EDGE, linewidth=0.9),
                        capprops=dict(color=C_EDGE, linewidth=0.9))
        for patch, t in zip(bp["boxes"], tiers):
            patch.set_facecolor(TIER_RAMP.get(t, TIER_FALLBACK))
            patch.set_hatch(HATCH[i % len(HATCH)])

    ax.set_xticks(tiers)
    ax.set_xticklabels([f"Tier {t}" for t in tiers])
    ax.set_ylabel("대기 시간 (초)")
    ax.set_ylim(bottom=0)
    _style(ax)
    # 상자가 축 상단까지 뻗어 범례가 겹치므로 **축 바깥 위쪽**에 가로로 배치한다.
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(facecolor="white", edgecolor=C_EDGE, hatch=HATCH[i % len(HATCH)],
                             label=f"{lab} (n={sum(len(v) for v in data[lab].values())})")
                       for i, lab in enumerate(labels)],
              frameon=False, fontsize=9, ncol=len(labels),
              loc="lower center", bbox_to_anchor=(0.5, 1.01))
    fig.tight_layout()
    path = os.path.join(out, f"fig_comparison_tier_{scenario}.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (시스템 {len(labels)}종, Tier {len(tiers)}종)")


def fig_lmax_sweep(results, out):
    """그림: L_max 스윕 곡선 — lmax_sweep(18·24) + s2_release_burst(30).

    🔴 완료율은 **공통 관측 창으로 보정**해야 한다 (2026-08-05 정정).
       `lmax_sweep/run.sh` 에만 쿨다운 `sleep 300` 이 있고 L_max=30 을 담당하는
       `s2_release_burst/run.sh` 에는 없다. 원자료 관측 폭이 2,450~2,907초로 갈리므로
       보정 없이 세면 스윕 쪽이 300초를 더 소화한 만큼 완료율이 높게 나온다.
       실제로 보정 전에는 79/80/66% 로 «L_max 를 낮추면 완료율이 오른다» 는
       없는 효과가 보였고, 공통 창으로 자르면 67.8/65.6/62.2% 로 차이가 사라진다.
       (4장 표와 이 그림이 어긋나면 안 되므로 같은 보정을 여기에도 적용한다.)
    """
    series = {}   # L_max -> [(대기중앙, 완료율, CPU최대), ...]
    files = []    # (lmax, csv, json)

    def _ts(s):
        try:
            return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except Exception:
            return None

    def _span(rows):
        a = [x for x in (_ts(r.get("start_time")) for r in rows) if x]
        b = [x for x in (_ts(r.get("end_time")) for r in rows) if x]
        return (min(a), (max(b) - min(a)).total_seconds()) if a and b else (None, 0)

    for f in sorted(glob.glob(os.path.join(results, "lmax_sweep", "lmax*_run*.csv"))):
        if f.endswith("_resource.csv"):
            continue
        m = re.search(r"lmax(\d+)_run\d+\.csv$", os.path.basename(f))
        if m:
            files.append((int(m.group(1)), f, f.replace(".csv", "_resource.json")))
    for f in sorted(glob.glob(os.path.join(results, "s2_release_burst", "run*.csv"))):
        if f.endswith("_resource.csv"):
            continue
        files.append((30, f, f.replace(".csv", "_resource.json")))

    # 전 회차가 공통으로 관측한 폭을 먼저 구한다.
    spans = []
    for _, c, _j in files:
        try:
            spans.append(_span(list(csv.DictReader(open(c, encoding="utf-8"))))[1])
        except OSError:
            pass
    T = min(s for s in spans if s) if any(spans) else None

    def add(lmax, csv_path, json_path):
        try:
            rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
            pk = json.load(open(json_path, encoding="utf-8"))["peaks"]
        except (OSError, KeyError, ValueError):
            return
        w = [float(r["wait_sec"]) for r in rows if r.get("wait_sec")]
        if not w:
            return
        t0, _ = _span(rows)
        if T and t0:      # 공통 창 안에 끝난 건만 센다
            ok = sum(1 for r in rows
                     if r.get("result") == "True" and _ts(r.get("end_time"))
                     and (_ts(r.get("end_time")) - t0).total_seconds() <= T)
        else:
            ok = sum(1 for r in rows if r.get("result") == "True")
        done = ok / len(rows) * 100
        cpu = pk.get("node_cpu_util_ratio")
        series.setdefault(lmax, []).append((st.median(w), done, (cpu or 0) * 100))

    for lmax, c, j in files:
        add(lmax, c, j)

    if len(series) < 2:
        print(f"[건너뜀] L_max 스윕: 수준이 {len(series)}종뿐 (18·24 실행 필요)"); return

    xs = sorted(series)
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.1), dpi=200)
    labels = ("대기 시간 중앙값 (초)", "창내 완료율 (%)", "노드 CPU 최대 (%)")
    for idx, (ax, lab) in enumerate(zip(axes, labels)):
        med = [st.median([v[idx] for v in series[x]]) for x in xs]
        lo = [min(v[idx] for v in series[x]) for x in xs]
        hi = [max(v[idx] for v in series[x]) for x in xs]
        ax.fill_between(xs, lo, hi, color=C_MED, alpha=0.15, zorder=2)
        ax.plot(xs, med, color=C_MED, linewidth=2.0, marker="o", markersize=7,
                markeredgecolor="white", markeredgewidth=1.2, zorder=3)
        for x, y in zip(xs, med):
            ax.annotate(f"{y:.0f}", xy=(x, y), xytext=(0, 8), textcoords="offset points",
                        ha="center", fontsize=8.5, color="#2B3A46")
        ax.set_xticks(xs)
        ax.set_xlabel("$L_{max}$")
        ax.set_title(lab, fontsize=10, pad=8)
        _style(ax)
    fig.tight_layout()
    path = os.path.join(out, "fig_lmax_sweep.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {path}  (수준 {xs}, 회차 {sum(len(v) for v in series.values())})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--results", default="results")
    p.add_argument("--out", default="results/figures")
    p.add_argument("--fig", default="all",
                   choices=["all", "concurrency", "memory", "tier", "promotion", "lmax", "compare"])
    p.add_argument("--t-a", type=int, default=300, help="에이징 주기(초), 승격 경계선용")
    p.add_argument("--scenario", default=None, help="미지정 시 유효 시나리오 전부")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    if args.scenario:
        scenarios = [args.scenario]
    else:
        # INVALID·백업 디렉터리는 제외한다.
        scenarios = sorted(
            d for d in os.listdir(args.results)
            if os.path.isdir(os.path.join(args.results, d))
            and "INVALID" not in d and "precampaign" not in d and d != "figures"
        )
    for sc in scenarios:
        print(f"[{sc}]")
        if args.fig in ("all", "concurrency"):
            fig_concurrency(args.results, sc, args.out)
        if args.fig in ("all", "memory"):
            fig_node_memory(args.results, sc, args.out)
        if args.fig in ("all", "tier"):
            fig_tier_wait(args.results, sc, args.out)
        if args.fig in ("all", "promotion"):
            fig_promotion(args.results, sc, args.out, args.t_a)

    # L_max 스윕은 여러 시나리오 자료를 합치므로 루프 밖에서 1회 생성한다.
    if args.fig in ("all", "lmax"):
        print("[lmax_sweep]")
        fig_lmax_sweep(args.results, args.out)

    # 3자 비교는 여러 디렉터리를 합치므로 루프 밖에서 시나리오별로 1회씩 생성한다.
    if args.fig in ("all", "compare"):
        for sc in ("s1", "s2"):
            print(f"[3자 비교 {sc}]")
            fig_comparison_tier(args.results, args.out, sc)


if __name__ == "__main__":
    main()
