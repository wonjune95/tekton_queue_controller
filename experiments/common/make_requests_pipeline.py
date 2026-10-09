#!/usr/bin/env python3
"""실측 피크 메모리 → requests 주석 파이프라인 생성기.

mem_peak_collect.py 가 만든 CSV(단계별 피크)를 읽어
requests=P95, limits=max×계수 를 각 단계에 주입한 파이프라인 YAML 을 만든다.

    python3 common/make_requests_pipeline.py \
        --input results/param_measure/mem_peak.csv \
        --output pipeline/petclinic-build-requests.yaml

용도(HANDOVER §0): A0-R(컨트롤러 off + requests 설정), 요인 실험 2x2,
비교군(tekton-kueue·Volcano) memory requests 통일.

※ 자원 상한 실험의 전제: **L_max=30 이 유일한 구속 조건**이어야 한다.
   requests 가 과하면 K8s 스케줄러가 30개 미만에서 먼저 막아 실험이 오염된다.
   그래서 생성 후 빌드 노드 수용량을 점검하고, 넘치면 경고한다.
"""
import argparse
import csv
import sys

try:
    import yaml
except ImportError:
    sys.exit("pyyaml 이 필요합니다: pip install pyyaml")

# mem_peak_collect.py 의 STAGES ↔ 파이프라인 태스크/스텝 대응
STAGE_TO_STEP = {
    "clone":  ("code-fetch",      "clone"),
    "maven":  ("code-build",      "mvn-package"),
    "kaniko": ("image-build",     "kaniko"),
    "trivy":  ("image-scan",      "trivy-scan"),
}
# 실측 대상이 아닌 경량 단계(고정값)
FIXED = {("write-dockerfile", "write"): 64}


def p95(values):
    """CSV 표본 수가 적으므로 보간 없이 상위 백분위 인덱스를 취한다."""
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="mem_peak_collect.py 산출 CSV")
    ap.add_argument("--base", default="pipeline/petclinic-build-experiment.yaml")
    ap.add_argument("--output", default="pipeline/petclinic-build-requests.yaml")
    ap.add_argument("--name", default="petclinic-build-requests")
    ap.add_argument("--limit-factor", type=float, default=1.2)
    ap.add_argument("--with-limits", action="store_true",
                    help="memory limits 도 주입(기본: requests 만). "
                         "limits 는 JVM 힙 크기를 바꿔 조건 간 워크로드를 달라지게 하므로 기본 비활성.")
    ap.add_argument("--cpu-request", default="200m",
                    help="단계별 cpu requests. 과하면 30 동시 실행 전에 스케줄러가 막는다")
    ap.add_argument("--lmax", type=int, default=30)
    ap.add_argument("--build-nodes", type=int, default=3)
    ap.add_argument("--node-cpu", type=float, default=8.0)
    ap.add_argument("--node-mem-gib", type=float, default=16.0)
    args = ap.parse_args()

    # ── 1. 실측값 집계 ────────────────────────────────────────────
    rows = list(csv.DictReader(open(args.input, encoding="utf-8")))
    if not rows:
        sys.exit(f"입력이 비어 있다: {args.input}")

    stats = {}
    for stage in STAGE_TO_STEP:
        col = f"{stage}_mib"
        vals = []
        for r in rows:
            v = (r.get(col) or "").strip()
            if v and v.upper() != "NA":
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        if not vals:
            print(f"[경고] {stage}: 유효 실측값 없음 → 이 단계는 주입을 건너뛴다")
            continue
        stats[stage] = {"n": len(vals), "p95": p95(vals), "max": max(vals)}

    if not stats:
        sys.exit("유효한 실측값이 하나도 없다. mem_peak_collect.py 출력 확인 필요.")

    # ── 2. 파이프라인에 주입 ──────────────────────────────────────
    with open(args.base, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    doc["metadata"]["name"] = args.name

    injected = {}
    for stage, (task_name, step_name) in STAGE_TO_STEP.items():
        if stage not in stats:
            continue
        req = int(round(stats[stage]["p95"]))
        lim = int(round(stats[stage]["max"] * args.limit_factor))
        injected[(task_name, step_name)] = (req, lim)

    for task in doc["spec"]["tasks"]:
        for step in task.get("taskSpec", {}).get("steps", []):
            key = (task["name"], step.get("name"))
            if key in injected:
                req, lim = injected[key]
            elif key in FIXED:
                req = FIXED[key]
                lim = int(round(req * args.limit_factor))
            else:
                continue
            # ── memory limits 는 주입하지 않는다 (2026-07-29 확정) ──
            # 실험 변수는 **requests 설정 여부**다(A0-NR = requests 미설정). limits 는 변수가 아니다.
            #  · A0-NR 의 인과 사슬(requests 없음 → 과밀 배치 → 노드 OOM)은 스케줄러 배치의 문제라
            #    limits 와 무관하다.
            #  · limits 를 걸면 JVM 이 MaxRAMPercentage 를 limit 기준으로 계산해 힙이 바뀐다
            #    → 조건 간 워크로드가 달라지는 교란 변수가 된다
            #    (실측: limit 4Gi 에서 maven 481MiB vs limit 없음 648MiB).
            #  · 측정값에서 limit 을 도출하는데 그 측정이 limit 에 의존하는 순환도 사라진다.
            # `--with-limits` 를 주면 옛 동작(limits=max×계수)을 쓴다.
            cr = {"requests": {"memory": f"{req}Mi", "cpu": args.cpu_request}}
            if args.with_limits:
                cr["limits"] = {"memory": f"{lim}Mi"}
            step["computeResources"] = cr

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("# 자동 생성 파일 — make_requests_pipeline.py\n")
        f.write(f"# 입력: {args.input} (표본 {len(rows)}건)\n")
        if args.with_limits:
            f.write("# requests=P95, limits=max x %.1f\n" % args.limit_factor)
        else:
            f.write("# requests=P95 (memory limits 미주입 — 실험 변수는 requests 설정 여부)\n")
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=120)

    # ── 3. 요약 + 수용량 점검 ─────────────────────────────────────
    print(f"\n생성: {args.output}  (pipeline name={args.name})")
    print(f"{'단계':<10}{'n':>4}{'P95(req)':>12}{'max':>10}{'limit':>10}")
    for stage, s in stats.items():
        print(f"{stage:<10}{s['n']:>4}{s['p95']:>11.1f}M{s['max']:>9.1f}M"
              f"{s['max']*args.limit_factor:>9.1f}M")

    peak_stage = max(stats, key=lambda s: stats[s]["p95"])
    worst_req_mib = stats[peak_stage]["p95"]
    cap_mem = args.build_nodes * args.node_mem_gib * 1024
    cap_cpu = args.build_nodes * args.node_cpu
    need_mem = args.lmax * worst_req_mib
    cpu_val = (float(args.cpu_request[:-1]) / 1000 if args.cpu_request.endswith("m")
               else float(args.cpu_request))
    need_cpu = args.lmax * cpu_val

    print(f"\n[수용량 점검] L_max={args.lmax} 동시 실행 시 (최악=모두 {peak_stage} 단계 가정)")
    print(f"  메모리 : 필요 {need_mem/1024:.1f} GiB / 가용 {cap_mem/1024:.1f} GiB")
    print(f"  CPU    : 필요 {need_cpu:.1f} vCPU / 가용 {cap_cpu:.1f} vCPU")
    ok = True
    if need_mem > cap_mem * 0.9:
        print("  [경고] 메모리 requests 가 과해 스케줄러가 L_max 전에 막을 수 있다.")
        ok = False
    if need_cpu > cap_cpu * 0.9:
        print(f"  [경고] cpu requests({args.cpu_request})가 과하다. --cpu-request 를 낮출 것.")
        ok = False
    if ok:
        print("  [OK] L_max=30 이 구속 조건으로 유지될 여지가 있다.")
    else:
        print("  → 이 상태로 실험하면 '컨트롤러가 막은 것'과 '스케줄러가 막은 것'이 뒤섞인다.")


if __name__ == "__main__":
    main()
