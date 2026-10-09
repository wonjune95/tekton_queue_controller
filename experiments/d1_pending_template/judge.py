#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D1 판정 — 계획서 §4 «사전 등록» 표를 그대로 코드화한 것.

    사람이 눈으로 판정하지 않는다. 결과를 본 뒤에 기준을 세우면 그것이 사후 합리화다.
    이 파일의 기준은 실험 «착수 전» 에 확정된 것이며, 결과를 보고 고치지 않는다.

    회차 하나:  python3 judge.py --state ../results/d1_pending_template/k_run1_state.json
    전체 종합:  python3 judge.py --summary ../results/d1_pending_template
"""
import argparse
import glob
import json
import os
import sys

# 출력 인코딩 방어 — 배스천은 UTF-8 이지만 Windows 콘솔(cp949)에서 시험할 때
# 「—」·이모지에서 UnicodeEncodeError 로 죽는다. 검증을 막지 않도록 한다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# ── 조건별 예상 (착수 전 확정) ───────────────────────────────────
#   None = 예상을 세우지 않는다. 그것이 이 실험이 «확인하려는» 것이다.
#   pre 템플릿은 웹훅을 거치지 않으므로 큐잉이 붙인 라벨이 없다.
#   그런 오브젝트까지 컨트롤러가 잡는지는 소스만 봐서는 단정할 수 없다.
EXPECT = {
    "k": {"pre": None,  "other-sa": True,  "same-sa": True},
    "p": {"pre": False, "other-sa": False, "same-sa": True},
    "v": {"pre": False, "other-sa": False, "same-sa": False},
}
# 주 판정에서 제외하는 역할.
#   same-sa 는 «실행본과 같은 계정으로 만든 템플릿» 이다. 제안 컨트롤러의 구분 축이 요청 출처이므로
#   계정이 같으면 원리상 구분할 수 없고, 제외 설정을 넣으면 실행본까지 함께 빠진다.
#   실무에서는 템플릿과 실행본의 생성 경로가 다르므로(other-sa) 이 칸은 실무 결과가 아니다.
#   숨기지는 않되 «설계 축의 경계» 로 따로 보고한다.
SIDE_ROLES = ("same-sa",)
COND_NAME = {"k": "tekton-kueue", "p": "제안 컨트롤러", "v": "Volcano"}
SURVIVAL_RATIO = 0.8      # 새 파이프라인런의 몇 할이 실행돼야 «계층이 살아 있다» 로 보는가


def survival(state):
    """② 생존 확인 — 이것이 서야 ① 을 해석할 수 있다."""
    load = state.get("load") or {}
    total, started = load.get("total", 0), load.get("started", 0)
    if total == 0:
        return False, "새 파이프라인런이 하나도 관측되지 않았다"
    if started < total * SURVIVAL_RATIO:
        return False, f"새 {total}건 중 {started}건만 실행 — Pending 잔류 {load.get('pending', 0)}건"
    return True, f"새 {total}건 중 {started}건 실행"


def judge_run(state):
    """회차 하나를 판정한다. (생존여부, 역할별 실행여부, 예상과의 불일치 목록)"""
    cond = state["condition"]
    alive, why = survival(state)
    got = {v["role"]: v["started"] for v in state["templates"].values()}
    mismatch = []
    for role, exp in EXPECT[cond].items():
        if exp is None or role in SIDE_ROLES:
            continue
        if got.get(role) != exp:
            mismatch.append((role, exp, got.get(role)))
    return alive, why, got, mismatch


def show_run(path):
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    cond = st["condition"]
    alive, why, got, mismatch = judge_run(st)

    print(f"조건 {cond} ({COND_NAME[cond]}) · run {st['run']}")
    print(f"  ② 생존 확인: {'통과' if alive else '실패'} — {why}")
    if not alive:
        print("  ⇒ 판정 불가. 구성 실패이지 상충의 증거가 아니다. 이 회차는 폐기·재수행한다.")
        return

    print("  ① 상주 템플릿")
    for name, v in st["templates"].items():
        role, exp = v["role"], EXPECT[cond][v["role"]]
        mark = "🔴 실행됨" if v["started"] else "미실행"
        when = f" ({v['started_at_sec']}초)" if v["started_at_sec"] is not None else ""
        expect_s = "확인 대상" if exp is None else ("실행 예상" if exp else "미실행 예상")
        agree = "" if exp is None else ("  ← 예상과 일치" if v["started"] == exp else "  ⚠️ 예상과 다름")
        print(f"    · {role:14s} {mark}{when}  [{expect_s}]{agree}")
        print(f"        spec.status={v['spec_status_last'] or '(지워짐)'} "
              f"managed={v['managed_label'] or '없음'} workload={v['workload']}")

    if mismatch:
        print("  ⚠️ 예상과 어긋난 항목:")
        for role, exp, act in mismatch:
            print(f"    · {role}: 예상 {exp} / 실제 {act}")


def show_summary(d):
    """계획서 §4 표대로 전체를 판정한다."""
    runs = {}
    for path in sorted(glob.glob(os.path.join(d, "*_state.json"))):
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        alive, _, got, _ = judge_run(st)
        if alive:
            runs.setdefault(st["condition"], []).append(got)

    print("=== D1 전체 판정 (계획서 §4 사전 등록 기준) ===\n")
    for c in ("k", "p", "v"):
        rs = runs.get(c, [])
        print(f"조건 {c} ({COND_NAME[c]}) — 유효 {len(rs)}회")
        for role in ("pre", "other-sa", "same-sa"):
            n = sum(1 for r in rs if r.get(role))
            print(f"  · {role:14s} 실행 {n}/{len(rs)}")
        print()

    k, p, v = runs.get("k", []), runs.get("p", []), runs.get("v", [])
    # 주 판정은 실무 조건(other-sa)으로 닫는다. same-sa 는 경계 관측이므로 판정에 넣지 않는다.
    k_ok = sum(1 for r in k if r.get("other-sa"))
    p_split = sum(1 for r in p if not r.get("other-sa"))
    p_leak = sum(1 for r in p if r.get("other-sa"))
    v_none = sum(1 for r in v if not r.get("pre") and not r.get("other-sa"))
    p_edge = sum(1 for r in p if r.get("same-sa"))

    print("판정:")
    if p_leak:
        print(f"  🔴 기존 서술이 틀렸다 — P 의 other-sa(실무 조건)가 {p_leak}회 실행됐다.")
        print("     논문 6.8절 ①의 「요청 출처 확인으로 회피」를 철회·수정한다.")
        print("     실험을 버리는 것이 아니라 논문을 고친다.")
    elif k and k_ok == 0:
        print("  재현 실패 — tekton-kueue 조건에서 템플릿이 한 번도 실행되지 않았다.")
        print("     실험을 버린다. 6.8절 ①은 현행(논증) 유지.")
    elif len(k) >= 3 and k_ok == len(k) and p_split == len(p) and v_none == len(v):
        print("  완전 정합 — 6.8절 ①을 실측으로 승격한다. 5장에 관측 항목 신설.")
    elif k_ok >= 2 and p_split >= 2:
        print("  부분 정합 — 승격하되 회차별 값을 그대로 병기한다. 조건부 서술을 강화한다.")
    else:
        print("  판정 보류 — 유효 회차가 부족하거나 결과가 갈린다. 원자료를 직접 본다.")

    if p:
        print(f"\n[경계 관측] P 의 same-sa 실행 {p_edge}/{len(p)}회 — 실행본과 같은 계정으로 만든 템플릿.")
        print("   구분 축이 요청 출처이므로 계정이 같으면 원리상 구분할 수 없다.")
        print("   제외 설정을 넣으면 실행본까지 함께 빠지므로 «설정으로 해결» 되지 않는다.")
        print("   실무에서는 템플릿과 실행본의 생성 경로가 다르다 — 이 칸은 실무 결과가 아니다.")
        print("   숨기지 말고 «설계 축의 경계» 로 따로 적는다.")

    print("\n※ 서술 강도는 올리지 않는다. 결과가 선명해도 「같은 조건에서 세 설계의 거동이")
    print("   갈린다」에서 멈춘다. V 의 미실행은 «회피» 가 아니라 «무관심» 이며,")
    print("   파드 계층의 대가(부분 실행·파드 2배)를 반드시 병기한다.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", help="회차 하나의 state.json")
    ap.add_argument("--summary", help="결과 디렉터리 — 전체 종합")
    a = ap.parse_args()
    if a.summary:
        show_summary(a.summary)
    elif a.state:
        show_run(a.state)
    else:
        ap.error("--state 또는 --summary 가 필요하다")
