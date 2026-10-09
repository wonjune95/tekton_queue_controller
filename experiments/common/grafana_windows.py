# -*- coding: utf-8 -*-
"""
회차별 Grafana URL 생성기.

각 회차의 측정 창(`run<N>_resource.json` 의 start/end)을 읽어 Grafana 대시보드 URL 을 만든다.
캡처할 때 창을 손으로 맞추면 틀리기 쉽고, 어떤 회차를 찍었는지도 남지 않는다.

사용:
  python3 common/grafana_windows.py                      # 전 회차
  python3 common/grafana_windows.py --scenario s2_release_burst
  python3 common/grafana_windows.py --base http://localhost:3000 --pad 120
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--results", default="results")
    p.add_argument("--base", default="http://localhost:3000")
    p.add_argument("--uid", default="thesis-campaign")
    p.add_argument("--scenario", help="특정 시나리오만(디렉터리명)")
    # 창 앞뒤로 여유를 준다. 경계에 딱 맞추면 상승·하강 구간이 잘려 그림이 읽기 어렵다.
    p.add_argument("--pad", type=int, default=120, help="앞뒤 여유(초, 기본 120)")
    args = p.parse_args()

    pattern = os.path.join(args.results, args.scenario or "*", "*_resource.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"[중단] 대상 없음: {pattern}")

    print(f"{'회차':38s} {'창':>6s}  URL")
    print("-" * 110)
    for f in files:
        # INVALID·백업 디렉터리는 캡처 대상이 아니다.
        if "INVALID" in f or "precampaign" in f or "archive" in f:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"  [경고] 읽기 실패 {f}: {e}")
            continue
        start = dt.datetime.fromisoformat(d["start"])
        end = dt.datetime.fromisoformat(d["end"])
        frm = int((start.timestamp() - args.pad) * 1000)
        to = int((end.timestamp() + args.pad) * 1000)
        name = os.path.relpath(f, args.results).replace("_resource.json", "")
        url = f"{args.base}/d/{args.uid}/?from={frm}&to={to}&kiosk"
        print(f"{name:38s} {d.get('window',''):>6s}  {url}")


if __name__ == "__main__":
    main()
