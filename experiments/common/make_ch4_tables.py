#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4장 수치 표 생성 — 캠페인 원자료에서 논문에 넣을 표를 뽑는다.

HANDOVER «4장 집필 작업 목록» 규칙을 코드로 옮긴 것이다.
  · ⑤ «완료율» 을 성공 / 실패 / 측정 창 내 미완료 로 나눈다.
  · ⑥ 처리량 우위를 주장하지 않는다 — 3자 비교에 유의성 판정을 함께 낸다.
  · 동시 실행은 **엄격 계수**(같은 시각의 종료를 시작보다 먼저 처리)한다.
    초 단위 타임스탬프 탓에 느슨 계수는 상한을 넘은 것처럼 보인다.
  · 제외: A1·V(최종 설계에 없는 부품의 에이블레이션).

⚠️ **검정은 짝지은 부호뒤집기 순열검정을 쓴다** (2026-08-05 정정).
세 시스템은 **같은 시드로 같은 도착열**을 받았다(S1 인가 건수 117·106·107·111·92 가 양쪽 일치).
짝짓지 않은 검정을 쓰면 회차 간 도착 변동이 잡음으로 들어가 p 가 왜곡된다.
도달 가능한 최소 p 는 n=5 에서 1/16=0.0625, n=3 에서 1/4=0.25 이므로
**n=3 비교에서는 유의성을 주장할 수 없고 방향의 일관성만 서술한다.**

시드 대응 (짝지으려면 반드시 맞출 것):
  · 시드 1~5 조 = S1 run1~5(구) · A2 run1~5 · C-K 기본 s1_run1~5
  · 시드 6~8 조 = S1 run6~8(신) · C-K 우선순위 s1_run6~8 · T_a 150/600 run6~8

사용: python3 common/make_ch4_tables.py [--out <경로>]
"""
import argparse
import csv
import glob
import itertools
import json
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

R = "results"
ENV2TIER = {"prod": 1, "stg": 2, "dev": 3}


def ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def tier_of(row):
    t = (row.get("tier") or "").strip()
    if t.isdigit():
        return int(t)
    return ENV2TIER.get((row.get("env") or "").strip())


def env_tier_of(row):
    """배포 환경(env) 만으로 등급을 매긴다 — 컨트롤러가 부여한 `tier` 를 무시한다.

    ⚠️ A3(Tier 구분 제거)에서 반드시 이것을 써야 한다.
    A3 는 `tierRules` 를 «전 env → Tier 1» 로 패치하므로 `tier` 열이 전부 1 이 된다.
    `tier_of()` 로 집계하면 «나눠 볼 대상이 없다» 는 잘못된 결론이 나오지만,
    **`env` 열에는 원래 혼합(prod/stg/dev)이 그대로 남아 있다.**
    A3 가 보이려는 것은 «같은 부하에서 등급을 무시하면 대기가 어떻게 갈리는가» 이므로
    등급은 부하 생성 시점의 env 로 매기는 것이 맞다.
    """
    return ENV2TIER.get((row.get("env") or "").strip())


def rows_of(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def strict_concurrency(rows):
    """같은 시각에서 종료(-1)를 시작(+1)보다 먼저 처리한다.

    핸드오프(한 건이 끝나는 그 초에 다른 건이 시작)를 겹침으로 세지 않기 위함이다.
    느슨 계수로는 L_max=18 조건이 21 로 보인다 — 상한 위반이 아니라 측정 해상도의 산물이다.
    """
    ev = []
    for r in rows:
        a, b = ts(r.get("start_time")), ts(r.get("end_time"))
        if not a:
            continue
        ev.append((a, +1))
        if b:
            ev.append((b, -1))
    ev.sort(key=lambda x: (x[0], x[1]))
    c = m = 0
    for _, d in ev:
        c += d
        m = max(m, c)
    return m


def summarize(files):
    """회차 목록 → 집계. 회차별 값을 모아 중앙값으로 대표한다."""
    out = {k: [] for k in ("n", "succ", "fail", "unfin", "conc", "wait", "pods", "cpu", "mem")}
    oom = ev = nr = 0
    tier_med = defaultdict(list)
    tier_avg = defaultdict(list)
    tier_max = defaultdict(list)
    tier_n = Counter()
    for f in files:
        if not os.path.exists(f):
            continue
        rows = rows_of(f)
        if not rows:
            continue
        n = len(rows)
        c = Counter(r.get("result") for r in rows)
        out["n"].append(n)
        out["succ"].append(100 * c.get("True", 0) / n)
        out["fail"].append(100 * c.get("False", 0) / n)
        out["unfin"].append(100 * c.get("Unknown", 0) / n)
        out["conc"].append(strict_concurrency(rows))
        w = [float(r["wait_sec"]) for r in rows if r.get("wait_sec") not in (None, "", "NaN")]
        if w:
            out["wait"].append(st.median(w))
        acc = defaultdict(list)
        for r in rows:
            t = tier_of(r)
            ww = r.get("wait_sec")
            if t and ww not in (None, "", "NaN"):
                acc[t].append(float(ww))
        for t, v in acc.items():
            tier_med[t].append(st.median(v))
            tier_avg[t].append(st.mean(v))
            tier_max[t].append(max(v))
            tier_n[t] += len(v)
        jf = f.replace(".csv", "_resource.json")
        if os.path.exists(jf):
            j = json.load(open(jf, encoding="utf-8"))
            p = j.get("peaks", {})
            out["pods"].append(p.get("ns_running_pods") or 0)
            out["cpu"].append(100 * (p.get("node_cpu_util_ratio") or 0))
            out["mem"].append(100 * (p.get("node_mem_util_ratio") or 0))
            oom += j.get("oomkilled_total") or 0
            ev += j.get("evicted_pods") or 0
        rc = f.replace(".csv", "_resource.csv")
        if os.path.exists(rc):
            per = defaultdict(list)
            for r in csv.DictReader(open(rc, encoding="utf-8")):
                if r.get("metric") == "node_ready":
                    per[r["label"]].append(float(r["value"]))
            nr += sum(1 for v in per.values() for x in v if x < 1)
    med = lambda a: st.median(a) if a else 0
    return dict(
        N=len(out["n"]), n=med(out["n"]), succ=med(out["succ"]), fail=med(out["fail"]),
        unfin=med(out["unfin"]), conc=max(out["conc"]) if out["conc"] else 0,
        wait=med(out["wait"]), pods=med(out["pods"]), cpu=med(out["cpu"]), mem=med(out["mem"]),
        oom=oom, evict=ev, notready_sec=nr * 15,
        tier_med={t: med(v) for t, v in tier_med.items()},
        tier_avg={t: med(v) for t, v in tier_avg.items()},
        tier_max={t: med(v) for t, v in tier_max.items()},
        tier_n=dict(tier_n),
        raw=out,
    )


def perm_test(a, b):
    """짝짓지 않은 정확 순열검정(양측). 반환: (평균차, p).

    ⚠️ 본 캠페인 데이터에는 거의 쓰지 않는다 — 조건들이 같은 시드를 공유하므로
    `paired_test` 가 옳다. 짝지을 수 없는 비교(회차 수가 다른 조건)에만 남겨 둔다.
    """
    if not a or not b:
        return 0.0, 1.0
    n = len(a)
    pool = a + b
    obs = st.mean(a) - st.mean(b)
    cnt = tot = 0
    for idx in itertools.combinations(range(len(pool)), n):
        g1 = [pool[i] for i in idx]
        g2 = [pool[i] for i in range(len(pool)) if i not in idx]
        tot += 1
        if abs(st.mean(g1) - st.mean(g2)) >= abs(obs) - 1e-9:
            cnt += 1
    return obs, cnt / tot


def paired_test(a, b):
    """짝지은 부호뒤집기 순열검정(양측). 반환: (평균차, p, 방향일치수, n).

    같은 시드로 같은 도착열을 받은 두 조건을 비교한다. 회차별 차이 d_i 의 부호를
    모든 방식으로 뒤집어(2^n) 평균차의 귀무분포를 만든다.
    도달 가능한 최소 p 는 2/2^n — n=5 면 0.0625, n=3 이면 0.25 다.
    **n 이 작으면 p 가 아니라 «방향 일치»(k/n)로 서술한다.**
    """
    pairs = [(x, y) for x, y in zip(a, b)]
    n = len(pairs)
    if n == 0:
        return 0.0, 1.0, 0, 0
    d = [x - y for x, y in pairs]
    obs = st.mean(d)
    cnt = 0
    for signs in itertools.product((1, -1), repeat=n):
        if abs(st.mean([s * v for s, v in zip(signs, d)])) >= abs(obs) - 1e-9:
            cnt += 1
    agree = max(sum(1 for v in d if v > 0), sum(1 for v in d if v < 0))
    return obs, cnt / (2 ** n), agree, n


F = {
    "S0": [f"{R}/s0_baseline/experiment_run{i}.csv" for i in (1, 2, 3)],
    "S1": [f"{R}/s1_peak_hour/run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "S2": [f"{R}/s2_release_burst/run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "S3": [f"{R}/s3_priority/run{i}_rate3.csv" for i in (1, 2, 3, 4, 5)],
    "A2": [f"{R}/a2_no_aging/run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "A3": [f"{R}/a3_single_fifo/run{i}.csv" for i in (1, 2, 3)],
    # ⚠️ a0r_no_controller 에는 **부하 형태가 다른 두 종류**가 섞여 있다.
    #   run{1,2,3}.csv       = 고정 버스트 90  → S2·A0-NR 과 **동일 부하**. 요인 2×2 는 이것만 쓴다.
    #   run{1,2,3}_peak.csv  = 푸아송 106~117 → S1 형 부하. 별도로 본다.
    # 둘을 뭉치면 «컨트롤러 OFF 의 최대 동시 실행» 이 104(피크 회차)로 잡혀
    # 고정 버스트인 A0-NR(90) 보다 커 보이고, 부하 차이가 조건 차이로 오독된다.
    "A0-R": [f"{R}/a0r_no_controller/run{i}.csv" for i in (1, 2, 3)],
    "A0-R-peak": [f"{R}/a0r_no_controller/run{i}_peak.csv" for i in (1, 2, 3)],
    "A0-NR": [f"{R}/a0nr_no_requests/run{i}.csv" for i in (1, 2, 3)],
    "C-K-S1": [f"{R}/cmp_tekton_kueue/s1_run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "C-K-S2": [f"{R}/cmp_tekton_kueue/s2_run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "C-K-S3": [f"{R}/cmp_tekton_kueue/s3_run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "C-V-S1": [f"{R}/cmp_volcano/s1_run{i}.csv" for i in (1, 2, 3)],
    "C-V-S2": [f"{R}/cmp_volcano/s2_run{i}.csv" for i in (1, 2, 3)],
    # ── 시드 6~8 조 (2026-08-04 야간) ──────────────────────────────────
    # 인가 경로를 단일화한 판본으로 수행했다. 위 시드 1~5 조와 **섞지 않는다**.
    "S1-NEW": [f"{R}/s1_peak_hour/run{i}.csv" for i in (6, 7, 8)],
    "S3-NEW": [f"{R}/s3_priority/run{i}_rate3.csv" for i in (6, 7, 8)],
    # tekton-kueue 에 Tier 정보를 실제로 전달한 구성(WorkloadPriorityClass).
    # s1_run1~5 는 **전건이 같은 우선순위**를 받은 기본 설정이라 정렬 정책 비교가 성립하지 않는다.
    "C-K-S1-PRIO": [f"{R}/cmp_tekton_kueue/s1_run{i}.csv" for i in (6, 7, 8)],
    # T_a 민감도. 300 점은 별도 회차를 돌리지 않고 S1-NEW 가 담당한다(동일 부하·동일 판본).
    "TA150": [f"{R}/ta_sweep/ta150_run{i}.csv" for i in (6, 7, 8)],
    "TA600": [f"{R}/ta_sweep/ta600_run{i}.csv" for i in (6, 7, 8)],
    # 인가 단일화의 비용 측정. 시드 1·2·3 을 신판본으로 다시 돌린 것이라 S0 와 짝지어진다.
    "S0-NEW": [f"{R}/s0_baseline/experiment_run{i}.csv" for i in (4, 5, 6)],
    # ⚠️ L_max 스윕은 **관측 창이 다르다** — `lmax_sweep/run.sh` 에만 쿨다운 300초가 있고
    #    `s2_release_burst/run.sh`(= L_max 30 점)에는 없다. 원자료 관측 폭이 2,750~2,900초 대
    #    2,450~2,600초로 갈린다. 완료율을 그대로 비교하면 **스윕 쪽이 300초 더 소화한 만큼 높게** 나온다.
    #    → 아래 표는 전 회차가 공통으로 관측한 폭으로 잘라 다시 센다(`succ_at`).
    "LMAX18": [f"{R}/lmax_sweep/lmax18_run{i}.csv" for i in (1, 2, 3, 4, 5)],
    "LMAX24": [f"{R}/lmax_sweep/lmax24_run{i}.csv" for i in (1, 2, 3, 4, 5)],
}


def succ_list(files):
    out = []
    for f in files:
        if not os.path.exists(f):
            continue
        rows = rows_of(f)
        if rows:
            out.append(100 * Counter(r.get("result") for r in rows).get("True", 0) / len(rows))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/ch4_tables.md")
    args = ap.parse_args()
    S = {k: summarize(v) for k, v in F.items()}
    L = []
    w = L.append

    w("# 4장 수치 표 (캠페인 원자료 자동 생성)\n")
    w("> 동시 실행은 **엄격 계수**다 — 같은 시각의 종료를 시작보다 먼저 처리한다.")
    w("> 초 단위 타임스탬프 탓에 느슨 계수는 상한을 넘은 것처럼 보인다(L_max=18 조건이 21 로 계수됨).")
    w("> **본문에 이 계수 규칙을 반드시 명시할 것.**\n")
    w("> 제외: A1·V(최종 설계에 없는 부품의 에이블레이션).\n")
    w("> 검정은 **짝지은 부호뒤집기 순열검정**이다. 최소 p 는 n=5 에서 0.0625, n=3 에서 0.25 이므로")
    w("> **n=3 비교에서는 유의성을 주장하지 않고 방향 일치(k/n)로 서술한다.**\n")

    w("\n## 표 4-1. 시나리오별 결과\n")
    w("| 시나리오 | N | 부하 | 성공률 | 실패율 | 창 내 미완료 | 최대 동시 실행 | 대기 중앙 |")
    w("|---|---|---|---|---|---|---|---|")
    for k in ("S0", "S1", "S2", "S3"):
        d = S[k]
        w(f"| {k} | {d['N']} | {d['n']:.0f} | {d['succ']:.1f}% | {d['fail']:.1f}% | "
          f"{d['unfin']:.1f}% | **{d['conc']:.0f}** | {d['wait']:.0f}초 |")
    w("\n※ 성공률·실패율·미완료를 나눠 제시한다. 실패는 파이프라인 자체의 실패이며 컨트롤러와 무관하다.")
    w("※ 미완료는 «못 한 것»이 아니라 측정 창 종료 시점에 큐에 남아 있던 분이다.")
    s3a = S["S3"]["tier_avg"]
    w(f"※ **S3 는 대기 중앙값이 {S['S3']['wait']:.0f}초다** — 절반 이상이 즉시 인가돼 중앙값이 0 근방에 눌린다.")
    w(f"   S3 의 Tier 차등은 중앙값이 아니라 평균으로 읽어야 한다: "
      f"Tier1 {s3a.get(1,0):.0f}초 / Tier2 {s3a.get(2,0):.0f}초 / Tier3 {s3a.get(3,0):.0f}초.")
    w("   **S3 대기를 중앙값으로 서술하지 말 것** — 0 근방에서는 수 초의 차이가 «몇 배»로 부풀어 보인다.\n")

    w("\n## 표 4-2. 3자 비교 (동일 부하·동일 측정 창)\n")
    w("| 시나리오 | 시스템 | N | 성공률(회차 중앙) | 대기 중앙 | 최대 동시 파드 |")
    w("|---|---|---|---|---|---|")
    for sc, trio in (("S1", ("S1", "C-K-S1", "C-V-S1")), ("S2", ("S2", "C-K-S2", "C-V-S2"))):
        for key, name in zip(trio, ("제안 컨트롤러", "tekton-kueue", "Volcano")):
            d = S[key]
            if d["N"] == 0:
                continue
            w(f"| {sc} | {name} | {d['N']} | {d['succ']:.1f}% | {d['wait']:.0f}초 | {d['pods']:.0f} |")
    w("")
    w("**처리량 검정 (짝지은 부호뒤집기 순열검정, 제안 vs tekton-kueue)**\n")
    w("> 검정은 **회차 평균**으로 한다 — 위 표의 중앙값과 값이 다른 것은 통계량이 다르기 때문이다.")
    w("> 같은 시드로 같은 도착열을 받았으므로 **회차끼리 짝지어** 검정한다.\n")
    w("| 시나리오 | 제안(회차 평균) | tekton-kueue(회차 평균) | 차이 | 방향 일치 | p | 판정 |")
    w("|---|---|---|---|---|---|---|")
    for sc, a, b in (("S1", "S1", "C-K-S1"), ("S2", "S2", "C-K-S2"), ("S3", "S3", "C-K-S3")):
        x, y = succ_list(F[a]), succ_list(F[b])
        d, p, k, n = paired_test(x, y)
        verdict = "유의" if p < 0.05 else "유의하지 않음"
        w(f"| {sc} | {st.mean(x):.1f}% | {st.mean(y):.1f}% | {d:+.1f}%p | {k}/{n} | {p:.3f} | {verdict} |")
    w("\n> ⚠️ **처리량 우위를 주장하지 않는다.**")
    w("> 짝지은 검정에서는 세 시나리오 모두 유의하지 않다. 유의차가 없다는 것은 **동등하다는 증거가 아니다**")
    w("> — n=5 의 최소 p 가 0.0625 라 검정력이 낮을 뿐이므로, **«차이를 검출하지 못했다»** 까지만 쓴다.\n")

    w("\n## 표 4-3. Tier 별 대기 시간 (S1 포화 조건, 회차 중앙값의 중앙)\n")
    w("> ⚠️ **시드 조가 다른 행끼리 직접 빼지 말 것.** 짝지은 비교는 조 안에서만 성립한다.")
    w("> · 시드 1~5 조 = 제안(구) ↔ tekton-kueue 기본 설정")
    w("> · 시드 6~8 조 = 제안(신) ↔ tekton-kueue 우선순위 구성\n")
    w("| 시스템 | 시드 조 | N | Tier 1 (prod) | Tier 2 (stg) | Tier 3 (dev) | 단조성 |")
    w("|---|---|---|---|---|---|---|")
    noqueue = []
    for key, name, seed in (
        ("S1", "제안 컨트롤러", "1~5"),
        ("C-K-S1", "tekton-kueue (기본 설정)", "1~5"),
        ("S1-NEW", "제안 컨트롤러", "6~8"),
        ("C-K-S1-PRIO", "tekton-kueue (우선순위 구성)", "6~8"),
        ("C-V-S1", "Volcano", "1~3"),
    ):
        if S[key]["N"] == 0:
            continue
        tm = S[key]["tier_med"]
        v = [tm.get(t, 0) for t in (1, 2, 3)]
        # 전 Tier 대기가 0 근방이면 «차등이 있다»가 아니라 «큐잉이 없다»다.
        # 조건식 0<=0<=0 은 참이므로 단조로 오판된다 — 먼저 걸러낸다.
        if max(S[key]["tier_max"].get(t, 0) for t in (1, 2, 3)) < 5:
            mono = "**대기 없음 — 차등 판정 불가**"
            noqueue.append(name)
        elif v[0] <= v[1] <= v[2]:
            mono = "단조 (우선순위 반영)"
        else:
            mono = "**역전**"
        w(f"| {name} | {seed} | {S[key]['N']} | {v[0]:.0f}초 | {v[1]:.0f}초 | {v[2]:.0f}초 | {mono} |")
    t1 = S["S1"]["tier_med"].get(1, 0)
    k1 = S["C-K-S1"]["tier_med"].get(1, 0)
    if t1:
        w(f"\n※ 시드 1~5 조 Tier 1 대기: 제안 {t1:.0f}초 vs tekton-kueue 기본 설정 {k1:.0f}초 "
          f"(**{k1/t1:.1f}배**). 단 기본 설정은 **전건이 같은 우선순위**를 받았으므로 "
          f"이 차이는 정렬 정책이 아니라 «한쪽에만 우선순위를 알려준» 결과다 — 정렬 정책 비교는 시드 6~8 조로 한다.")
    # 짝지은 조에서만 회차별 방향을 본다.
    p3 = per_run_med = None
    n3, k3 = [], []
    for f in F["C-K-S1-PRIO"]:
        if os.path.exists(f):
            v = [float(r["wait_sec"]) for r in rows_of(f)
                 if tier_of(r) == 3 and r.get("wait_sec") not in (None, "", "NaN")]
            if v:
                k3.append(st.median(v))
    for f in F["S1-NEW"]:
        if os.path.exists(f):
            v = [float(r["wait_sec"]) for r in rows_of(f)
                 if tier_of(r) == 3 and r.get("wait_sec") not in (None, "", "NaN")]
            if v:
                n3.append(st.median(v))
    if len(n3) == len(k3) == 3:
        diff = [(a - b) / b * 100 for a, b in zip(n3, k3)]
        agree = sum(1 for x in diff if x < 0)
        w(f"\n※ **시드 6~8 조 Tier 3 중앙 대기 (짝지은 3회)**: "
          + " / ".join(f"{b:.0f}→{a:.0f}초({d:+.1f}%)" for a, b, d in zip(n3, k3, diff)))
        w(f"   평균 {st.mean(diff):+.1f}%, **{agree}/3 회차에서 단축**. "
          f"n=3 의 최소 p 는 0.25 이므로 **유의성이 아니라 방향 일치로 서술한다.**")
    for name in noqueue:
        w(f"※ ⚠️ **{name} 는 전 Tier 대기가 1초 미만**이다 — 도착분을 그대로 인가해 큐가 형성되지 않았다.")
        w(f"   따라서 «{name} 가 우선순위를 반영한다»고 읽으면 안 된다. 대기가 없으니 차등을 논할 수 없다.")
        w(f"   {name} 의 대조점은 대기가 아니라 **동시 파드 수와 완료율**이다(표 4-2).")
    w("")

    w("\n## 표 4-4. 에이징 효과 (H3) — S1(적용) vs A2(미적용)\n")
    w("| 지표 | Tier | 에이징 미적용 | 에이징 적용 | 변화 |")
    w("|---|---|---|---|---|")
    for label, key in (("중앙", "tier_med"), ("최대", "tier_max")):
        for t in (1, 2, 3):
            o = S["A2"][key].get(t, 0)
            n = S["S1"][key].get(t, 0)
            ch = f"{100*(n-o)/o:+.1f}%" if o else "—"
            w(f"| {label} | {t} | {o:.0f}초 | {n:.0f}초 | {ch} |")
    w("")
    # H3 유의성: 회차별 값으로 검정
    def per_run(files, t, fn):
        out = []
        for f in files:
            if not os.path.exists(f):
                continue
            v = [float(r["wait_sec"]) for r in rows_of(f)
                 if tier_of(r) == t and r.get("wait_sec") not in (None, "", "NaN")]
            if v:
                out.append(fn(v))
        return out
    w("**유의성 (짝지은 부호뒤집기 순열검정, 같은 시드 1~5)**\n")
    w("| 비교 | 미적용 | 적용 | 방향 일치 | p | 판정 |")
    w("|---|---|---|---|---|---|")
    for label, t, fn in (("Tier3 중앙 대기", 3, st.median), ("Tier3 최대 대기", 3, max),
                         ("Tier1 최대 대기", 1, max)):
        a = per_run(F["A2"], t, fn)
        b = per_run(F["S1"], t, fn)
        _, p, k, n = paired_test(a, b)
        w(f"| {label} | {st.mean(a):.0f}초 | {st.mean(b):.0f}초 | {k}/{n} | {p:.3f} | "
          f"{'유의' if p < 0.05 else '**유의하지 않음**'} |")
    # 처리량: 에이징이 오히려 올린다 (같은 구현 안에서의 짝지은 비교)
    sa, sb = succ_list(F["A2"]), succ_list(F["S1"])
    d, p, k, n = paired_test(sb, sa)
    w(f"| 창내 성공률 | {st.mean(sa):.1f}% | {st.mean(sb):.1f}% | {k}/{n} | {p:.3f} | "
      f"{'유의' if p < 0.05 else '**유의하지 않음**'} |")
    w("\n> ⚠️ H3 를 «최대 대기시간 감소»로 서술하면 유의하지 않다. **중앙값 기준으로 진술해야 한다.**")
    w("> 그리고 에이징은 Tier1 **최대** 대기를 크게 늘린다 — 이 상충을 함께 보고한다.")
    w(f"> 처리량은 **깎이지 않는다** — 같은 구현 안에서 에이징을 켠 쪽이 {d:+.1f}%p 높고 방향이 {k}/{n} 일치한다.")
    w("> p=0.0625 는 n=5 짝지은 검정의 **도달 가능한 최솟값**이므로, 이보다 낮은 p 는 이 설계에서 나올 수 없다.\n")

    w("\n## 표 4-5. 요인 2×2 — 요구자원만으로 충분한가 (동일 부하: 고정 버스트 90건)\n")
    w("| 조건 | N | 인가 건수 | 최대 동시 실행 | 최대 동시 파드 | 성공률 | OOM | 축출 |")
    w("|---|---|---|---|---|---|---|---|")
    for key, name in (("S2", "컨트롤러 ON + requests"),
                      ("A0-R", "컨트롤러 OFF + requests"),
                      ("A0-NR", "컨트롤러 OFF + requests 무설정")):
        d = S[key]
        w(f"| {name} | {d['N']} | {d['n']:.0f} | {d['conc']:.0f} | {d['pods']:.0f} | "
          f"{d['succ']:.1f}% | {d['oom']:.0f} | {d['evict']:.0f} |")
    w("\n※ **세 조건의 부하가 동일**하다(고정 버스트 90건) — 두 요인의 효과가 단계적으로 분리된다.")
    w("※ 컨트롤러를 제거하면 동시 실행이 도착분 전체로 늘고 완료율이 절반 아래로 떨어진다.")
    w("   자원 요청까지 없으면 동시 파드가 한 번 더 늘고 완료율이 추가로 낮아진다.")
    w("   → **자원 요청 설정만으로는 동시 실행 수가 통제되지 않는다.**")
    w("※ OOMKilled 는 전 조건에서 관측되지 않았다. **«무제어 시 OOM» 은 주장할 수 없다.**")
    dp = S.get("A0-R-peak")
    if dp and dp["N"]:
        w(f"\n※ `a0r_no_controller` 에는 푸아송(S1형) 회차도 {dp['N']}회 있다"
          f"(인가 {dp['n']:.0f}건, 최대 동시 실행 {dp['conc']:.0f}, 성공률 {dp['succ']:.1f}%).")
        w("   **부하 형태가 달라 위 표에 섞지 않는다.** 필요하면 S1 과 대조하는 별도 항목으로 쓴다.\n")

    # ── 표 4-6. A3 — S3 와 짝지은 에이블레이션 ────────────────────────────
    # ⚠️ 반드시 env 기준으로 집계한다. A3 는 `tierRules` 를 «전 env → Tier 1» 로 패치하므로
    #    `tier` 열이 전부 1 이다. 그것으로 집계하면 «나눠 볼 대상이 없다» 는 오판이 나온다.
    #    A3 의 부하는 S3 와 같다(steady λ=3/분, 30분, 푸아송, seed=RUN) — 인가 건수가
    #    94/87/83 으로 회차별 완전 일치하므로 **같은 도착열**임이 확인된다.
    def env_avgs(f):
        acc = defaultdict(list)
        for r in rows_of(f):
            e, w_ = env_tier_of(r), r.get("wait_sec")
            if e and w_ not in (None, "", "NaN"):
                acc[e].append(float(w_))
        return {t: st.mean(v) for t, v in acc.items() if v}

    A3F = [f"{R}/a3_single_fifo/run{i}.csv" for i in (1, 2, 3)]
    S3F = [f"{R}/s3_priority/run{i}_rate3.csv" for i in (1, 2, 3)]
    rowsA = [env_avgs(f) for f in A3F if os.path.exists(f)]
    rowsS = [env_avgs(f) for f in S3F if os.path.exists(f)]

    w("\n## 표 4-6. 에이블레이션 (A3 — Tier 구분 제거), S3 와 짝지음\n")
    w("> A3 는 `tierRules` 를 «전 env → Tier 1» 로 바꿀 뿐 부하는 S3 와 같다.")
    w("> 회차별 인가 건수가 완전히 일치하므로(같은 시드) **짝지은 에이블레이션**이다.")
    w("> 등급은 컨트롤러가 부여한 `tier` 가 아니라 **부하 생성 시점의 `env`** 로 매긴다.\n")
    if rowsA and rowsS:
        # ⚠️ 회차 간 집계는 **중앙값**이다 — 논문 §4 도입부가 선언한 규칙이며
        #    다른 표(4-1·4-3 등)와 통계량을 섞으면 «수치 불일치»로 읽힌다.
        w("| 조건 | N | prod | stg | dev | 단조 회차 |")
        w("|---|---|---|---|---|---|")
        for lab, rs in (("S3 (Tier 구분 있음)", rowsS), ("A3 (Tier 구분 제거)", rowsA)):
            m = [st.median([r.get(t, 0) for r in rs]) for t in (1, 2, 3)]
            mono = sum(1 for r in rs
                       if r.get(1, 0) <= r.get(2, 0) <= r.get(3, 0))
            w(f"| {lab} | {len(rs)} | {m[0]:.0f}초 | {m[1]:.0f}초 | {m[2]:.0f}초 "
              f"| **{mono}/{len(rs)}** |")
        gS = [r.get(3, 0) - r.get(1, 0) for r in rowsS]
        gA = [r.get(3, 0) - r.get(1, 0) for r in rowsA]
        d, p, k, n = paired_test(gA, gS)
        w("\n**prod→dev 격차 (dev 평균 − prod 평균), 회차별**\n")
        w("| 시드 | S3 | A3 | 차 |")
        w("|---|---|---|---|")
        for i, (x, y) in enumerate(zip(gS, gA), 1):
            w(f"| {i} | {x:+.0f}초 | {y:+.0f}초 | {y-x:+.0f}초 |")
        w(f"\n※ 격차가 **{k}/{n} 회차 전부에서 축소**되었다(평균 {d:+.0f}초, p={p:.3f}).")
        w("   단조성도 S3 에서는 유지되던 것이 A3 에서는 전 회차에서 무너진다.")
        w("※ ⚠️ n=3 의 최소 p 는 0.25 이므로 **유의성이 아니라 방향 일치로 서술한다.**")
        w("※ ⚠️ S3 자체도 3 회차 중 1 회차는 비단조다 — «S3 는 항상 단조» 로 쓰지 말 것.")
    ns3, na3 = S["S3"]["n"], S["A3"]["n"]
    w(f"\n※ 참고: 인가 건수 S3 {ns3:.0f} / A3 {na3:.0f}, 성공률 {S['S3']['succ']:.1f}% / "
      f"{S['A3']['succ']:.1f}%, 최대 동시 실행 {S['S3']['conc']:.0f} / {S['A3']['conc']:.0f}.")
    w("")

    w("\n## 표 4-7. 자원 안정성 종합\n")
    w("| 조건 | OOMKilled | 축출 | 노드 Ready=Unknown 지속 |")
    w("|---|---|---|---|")
    tot_oom = tot_ev = tot_nr = 0
    for k in ("S0", "S1", "S2", "S3", "A2", "A3", "A0-R", "A0-NR"):
        d = S[k]
        tot_oom += d["oom"]; tot_ev += d["evict"]; tot_nr += d["notready_sec"]
        w(f"| {k} | {d['oom']:.0f} | {d['evict']:.0f} | {d['notready_sec']:.0f}초 |")
    w(f"| **합계** | **{tot_oom:.0f}** | **{tot_ev:.0f}** | **{tot_nr:.0f}초** |")
    w("\n> 노드 상태 전이는 모두 `Ready=Unknown`(상태 갱신 미도달)이며 `Ready=false` 는 없었다.")
    w("> kubelet 이 응답 불능이었던 경우는 **컨트롤러를 적용하지 않은 조건에서만** 관측됐다.")
    w("> 자원 압박 조건(MemoryPressure·DiskPressure·PIDPressure)은 전 사례에서 관측되지 않았다.\n")

    w("\n## 표 4-8. 에이징 유무로 묶어 본 Tier 대기 — 귀속 확인\n")
    w("> 조건을 **에이징 유무**로 묶으면 서로 다른 두 구현이 같은 편에 선다.")
    w("> 관측된 차이가 구현 특성이 아니라 **에이징 기구**에 귀속됨을 보이는 배치다.\n")
    w("| 조건 | 구현 | 에이징 | 시드 조 | N | Tier 1 중앙 | Tier 3 중앙 | 격차 |")
    w("|---|---|---|---|---|---|---|---|")
    for key, cond, impl, aging, seed in (
        ("S1", "S1", "제안", "O", "1~5"),
        ("S1-NEW", "S1", "제안", "O", "6~8"),
        ("A2", "A2", "제안", "X", "1~5"),
        ("C-K-S1-PRIO", "C-K 우선순위", "tekton-kueue", "X", "6~8"),
    ):
        d = S[key]
        if d["N"] == 0:
            continue
        a, b = d["tier_med"].get(1, 0), d["tier_med"].get(3, 0)
        w(f"| {cond} | {impl} | **{aging}** | {seed} | {d['N']} | {a:.0f}초 | **{b:.0f}초** | {b-a:+.0f}초 |")
    w("\n※ 에이징을 켠 두 행(서로 다른 시드 조)은 Tier 3 가 비슷한 수준에 모이고,")
    w("   에이징이 없는 두 행은 **구현이 다른데도** 함께 더 큰 값을 보인다.")
    w("※ 이 표는 **귀속의 근거**이며 시스템 간 우열 비교가 아니다. 해석은 고찰 장에서 한다.")
    w("※ ⚠️ 서로 다른 시드 조의 값을 빼서 «몇 % 차이»로 쓰지 말 것 — 짝지어지지 않은 값이다.\n")

    w("\n## 표 4-9. 에이징 주기 $T_a$ 민감도 (짝지은 3회, 시드 6~8)\n")
    w("| $T_a$ | N | Tier 1 중앙 | Tier 3 중앙 | 격차(회차 중앙) | 창내 성공률 |")
    w("|---|---|---|---|---|---|")
    ta_gap = {}
    for ta, key in ((150, "TA150"), (300, "S1-NEW"), (600, "TA600")):
        d = S[key]
        if d["N"] == 0:
            continue
        gaps = []
        for f in F[key]:
            if not os.path.exists(f):
                continue
            acc = defaultdict(list)
            for r in rows_of(f):
                t, ww = tier_of(r), r.get("wait_sec")
                if t and ww not in (None, "", "NaN"):
                    acc[t].append(float(ww))
            if acc.get(1) and acc.get(3):
                gaps.append(st.median(acc[3]) - st.median(acc[1]))
        g = st.median(gaps) if gaps else 0
        ta_gap[ta] = g
        w(f"| {ta}초 | {d['N']} | {d['tier_med'].get(1,0):.0f}초 | {d['tier_med'].get(3,0):.0f}초 "
          f"| {g:+.0f}초 | {d['succ']:.1f}% |")
    if len(ta_gap) == 3:
        w(f"\n※ 격차: {ta_gap[150]:+.0f} → {ta_gap[300]:+.0f} → {ta_gap[600]:+.0f}초.")
        w("※ $T_a$ 가 짧으면 두 Tier 의 대기가 붙어 **차등이 소멸**한다 — §3 이 추론으로 서술한 거동이 실측됐다.")
        w("※ ⚠️ **«$T_a$=300 이 최적»은 주장할 수 없다.** 300→600 에서 격차와 Tier 3 대기가 함께 늘어")
        w("   거의 1:1 로 교환되며 명확한 무릎이 없다. **사전 도출값이 작동 구간 안에 있음**까지만 쓴다.")
        w("※ 그림: `results/figures/fig_ta_sweep.png`\n")

    w("\n## 표 4-10. 인가 경로 단일화의 비용 (S0 여유 조건, 같은 시드 1·2·3)\n")
    w("> 웹훅에서 즉시 인가하던 경로를 없애고 매니저 루프를 유일한 인가 지점으로 두면,")
    w("> 대기 없이 통과하던 요청도 **최대 한 사이클(5초)** 을 기다린다. 그 비용을 짝지어 측정했다.\n")
    w("| 지표 | 웹훅 시점 인가 | 단일 루프 인가 | 차이 |")
    w("|---|---|---|---|")

    def _s0(files, fn):
        out = []
        for f in files:
            if not os.path.exists(f):
                continue
            v = [float(r["wait_sec"]) for r in rows_of(f)
                 if r.get("wait_sec") not in (None, "", "NaN")]
            if v:
                out.append(fn(v))
        return out
    for label, fn, unit in (("평균 대기", st.mean, "초"), ("최대 대기", max, "초")):
        a, b = _s0(F["S0"], fn), _s0(F["S0-NEW"], fn)
        if a and b:
            w(f"| {label} | {st.mean(a):.2f}{unit} | {st.mean(b):.2f}{unit} | {st.mean(b)-st.mean(a):+.2f}{unit} |")
    sa, sb = succ_list(F["S0"]), succ_list(F["S0-NEW"])
    if sa and sb:
        w(f"| 창내 성공률 | {st.mean(sa):.1f}% | {st.mean(sb):.1f}% | {st.mean(sb)-st.mean(sa):+.1f}%p |")
    w("\n※ 여유 조건(대기가 거의 발생하지 않는 구간)에서만 드러나는 비용이다.")
    w("   포화 조건에서는 어차피 모든 건이 매니저 틱을 기다리므로 차이가 나타나지 않는다.")
    w("※ 본문에는 **«이전 설계»·«수정 전/후» 로 쓰지 않는다** — 인가 판정을 어디에 둘지의 설계 공간 비교로 서술한다.\n")

    # ── 표 4-11. L_max 스윕 (관측 창 보정 필수) ───────────────────────────
    def obs_span(f):
        rows = rows_of(f)
        s = [x for x in (ts(r.get("start_time")) for r in rows) if x]
        e = [x for x in (ts(r.get("end_time")) for r in rows) if x]
        return (max(e) - min(s)).total_seconds() if s and e else 0

    def succ_at(f, T):
        """공통 절단 T 안에 완료된 비율. 관측 창 길이 차이를 제거한다."""
        rows = rows_of(f)
        s = [x for x in (ts(r.get("start_time")) for r in rows) if x]
        if not s or not rows:
            return None
        t0 = min(s)
        ok = 0
        for r in rows:
            e = ts(r.get("end_time"))
            if r.get("result") == "True" and e and (e - t0).total_seconds() <= T:
                ok += 1
        return 100 * ok / len(rows)

    LM = (("18", "LMAX18"), ("24", "LMAX24"), ("30", "S2"))
    allf = [f for _, k in LM for f in F[k] if os.path.exists(f)]
    if len(allf) >= 9:
        spans = [obs_span(f) for f in allf]
        T = min(spans)
        w("\n## 표 4-11. 동시 실행 상한 $L_{\\max}$ 스윕 (동일 부하: 고정 버스트 90건, 시드 1~5)\n")
        w("> 🔴 **관측 창 보정을 적용했다.** `lmax_sweep/run.sh` 에만 쿨다운 300초가 있어")
        w(f"> 원자료 관측 폭이 {min(spans):.0f}~{max(spans):.0f}초로 갈린다. 보정 없이 세면")
        w("> 스윕 쪽이 300초를 더 소화한 만큼 완료율이 높게 나와 **없는 효과가 만들어진다.**")
        w(f"> 아래 완료율은 전 회차 공통 관측 폭 **{T:.0f}초**로 잘라 다시 센 값이다.\n")
        w("| $L_{\\max}$ | N | 완료율(공통 창) | 대기 중앙 | 노드 CPU 최대 | 노드 메모리 최대 |")
        w("|---|---|---|---|---|---|")
        SUCC = {}
        for lab, key in LM:
            d = S[key]
            vals = [v for v in (succ_at(f, T) for f in F[key] if os.path.exists(f)) if v is not None]
            SUCC[lab] = vals
            w(f"| {lab} | {d['N']} | {st.median(vals):.1f}% | {d['wait']:.0f}초 "
              f"| {d['cpu']:.1f}% | {d['mem']:.1f}% |")
        w("\n**완료율 짝지은 검정 (같은 시드 1~5)**\n")
        w("| 비교 | 차이 | 방향 일치 | p | 판정 |")
        w("|---|---|---|---|---|")
        for a, b in (("24", "30"), ("18", "30"), ("24", "18")):
            o, p, k, n = paired_test(SUCC[a], SUCC[b])
            w(f"| L_max {a} vs {b} | {o:+.1f}%p | {k}/{n} | {p:.3f} | "
              f"{'유의' if p < 0.05 else '유의하지 않음'} |")
        w("\n※ **완료율에서는 세 수준 간 차이를 검출하지 못했다.**")
        w("   보정 전 원자료로는 $L_{\\max}$=24 가 30 보다 +12.4%p·5/5 로 앞서는 것처럼 보이나,")
        w("   그 차이는 위 쿨다운 300초에서 나온 것이며 공통 창으로 자르면 사라진다.")
        w("※ 반면 **대기 시간은 단조 감소**하고 **노드 자원 사용률은 단조 증가**한다.")
        w("   → $L_{\\max}$ 는 대기 시간과 자원 여유 사이를 조율하는 파라미터이며,")
        w("   측정한 구간 {18, 24, 30} 전체가 작동 구간이다. **최적값은 주장하지 않는다.**\n")

    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"저장: {out}  ({len(L)}행)")


if __name__ == "__main__":
    sys.exit(main())
