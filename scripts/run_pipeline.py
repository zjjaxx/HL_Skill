"""
端到端流水线：
1) opencli browser 刷选东财候选（可选）
2) 拉 30Y 国债
3) BaoStock 拉行情/分红
4) 构建特征与信号
5) 多账号回测 + PyBroker 对照
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from config import OUTPUT_DIR
from fetch_bond_yield import load_or_scrape
from screen_eastmoney import screen
from backtest_pybroker import run_multi, run_pybroker


def main() -> None:
    p = argparse.ArgumentParser(description="红利布林策略全流程回测")
    p.add_argument(
        "--symbols",
        nargs="*",
        default=[],
        help="直接指定标的；为空则走东财筛选",
    )
    p.add_argument("--seed", nargs="*", default=["601318", "600519", "601088", "600900"])
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--skip-screen", action="store_true")
    p.add_argument("--skip-browser", action="store_true")
    p.add_argument("--bond-demo", action="store_true", help="国债收益率用常量演示序列")
    p.add_argument("--n", type=int, default=0)
    p.add_argument("--mode", choices=["pybroker", "multi", "both"], default="both")
    args = p.parse_args()

    report: dict = {"steps": []}

    # Bond yield
    if args.bond_demo:
        from fetch_bond_yield import OUT_PATH

        dates = pd.date_range("2015-01-01", args.end, freq="B")
        pd.DataFrame({"date": dates, "yield_30y": 2.5}).to_csv(OUT_PATH, index=False)
        report["steps"].append({"bond": "demo", "path": str(OUT_PATH)})
    else:
        try:
            bond = load_or_scrape(force=False)
            report["steps"].append({"bond": "ok", "rows": len(bond)})
        except Exception as e:
            from fetch_bond_yield import OUT_PATH

            dates = pd.date_range("2015-01-01", args.end, freq="B")
            pd.DataFrame({"date": dates, "yield_30y": 2.5}).to_csv(OUT_PATH, index=False)
            report["steps"].append({"bond": f"fallback-demo:{e}", "path": str(OUT_PATH)})

    symbols = list(args.symbols)
    if not symbols and not args.skip_screen:
        scr = screen(
            seed_codes=args.seed,
            use_browser=not args.skip_browser,
            apply_fundamental=True,
            start=args.start,
        )
        symbols = [r["code"] for r in scr["passed"]] or list(args.seed)
        report["steps"].append(
            {
                "screen": {
                    "candidates": len(scr["candidates"]),
                    "passed": len(scr["passed"]),
                    "symbols": symbols,
                }
            }
        )
    elif not symbols:
        symbols = list(args.seed)
        report["steps"].append({"screen": "skipped", "symbols": symbols})

    out: dict = {"symbols": symbols, "start": args.start, "end": args.end}

    if args.mode in ("multi", "both"):
        out["multi"] = {}
        for sym in symbols:
            try:
                out["multi"][sym] = run_multi(sym, args.start, args.end, n=args.n)
            except Exception as e:
                out["multi"][sym] = {"error": str(e)}

    if args.mode in ("pybroker", "both"):
        try:
            out["pybroker"] = run_pybroker(symbols, args.start, args.end)
        except Exception as e:
            out["pybroker"] = {"error": str(e)}

    report["result"] = out
    path = OUTPUT_DIR / "pipeline_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": str(path), "symbols": symbols}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
