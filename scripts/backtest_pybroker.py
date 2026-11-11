"""
PyBroker 单账号回测（信号预计算）。
多账号滚动请用 multi_account.py；本脚本用于框架对齐与对照。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import pybroker
from pybroker import Strategy, StrategyConfig

from baostock_datasource import SIGNAL_COLS, BaoStockSignalDataSource, features_to_pybroker_df
from build_features import build_for_symbol
from config import ACCOUNT_CASH, OUTPUT_DIR
from metrics import summarize_rounds
from multi_account import MultiAccountEngine, find_min_accounts


def make_exec_fn():
    """Buy on buy_exec; trim on week/month/yield flags (simplified single account)."""

    state = {
        "week_trimmed": False,
        "month_trimmed": False,
        "ladder_done": set(),
        "ladder_base": None,
        "entry_month": None,
    }

    def _exec(ctx):
        pos = ctx.long_pos()
        buy = bool(ctx.buy_exec[-1]) if hasattr(ctx, "buy_exec") else False
        if not pos and buy:
            ctx.buy_shares = ctx.calc_target_shares(1.0)
            state["week_trimmed"] = False
            state["month_trimmed"] = False
            state["ladder_done"] = set()
            state["ladder_base"] = None
            return

        if not pos:
            return

        shares = float(pos.shares)
        # week trim 20%
        if (not state["week_trimmed"]) and bool(ctx.sell_week_trim[-1]):
            ctx.sell_shares = shares * 0.20
            state["week_trimmed"] = True
            return
        # month trim 30%
        if (not state["month_trimmed"]) and bool(ctx.sell_month_trim[-1]):
            ctx.sell_shares = shares * 0.30
            state["month_trimmed"] = True
            return
        # yield clear
        if bool(getattr(ctx, "yield_clear")[-1]):
            ctx.sell_shares = shares

    return _exec


def run_pybroker(symbols: list[str], start: str, end: str) -> dict:
    pybroker.register_columns(*SIGNAL_COLS)
    ds = BaoStockSignalDataSource(start, end)
    config = StrategyConfig(initial_cash=ACCOUNT_CASH)
    strategy = Strategy(ds, start, end, config)
    strategy.add_execution(make_exec_fn(), symbols)
    result = strategy.backtest()
    metrics = result.metrics_df.to_dict() if hasattr(result, "metrics_df") else {}
    orders = result.orders.to_dict(orient="records") if result.orders is not None else []
    return {
        "framework": "pybroker",
        "symbols": symbols,
        "metrics": metrics,
        "n_orders": len(orders),
        "orders": orders[:50],
    }


def run_multi(symbol: str, start: str, end: str, n: int = 0) -> dict:
    feat = build_for_symbol(symbol, start, end)
    raw = feat["close_raw"]
    # reload dividend from cache via fetch
    from fetch_baostock import fetch_pair

    div = fetch_pair(symbol, start, end)["dividend"]
    if n <= 0:
        auto_n, result = find_min_accounts(feat, raw, div)
        result["auto_n"] = auto_n
    else:
        result = MultiAccountEngine(feat, raw, div, n_accounts=n).run()
        result["auto_n"] = n
    result["summary"] = summarize_rounds(result.get("rounds", []))
    result["framework"] = "multi_account"
    result["symbol"] = symbol
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Hongli bollinger backtest")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--mode", choices=["pybroker", "multi", "both"], default="both")
    p.add_argument("--n", type=int, default=0, help="multi-account N; 0=按需建户")
    p.add_argument("--output", default=str(OUTPUT_DIR / "backtest_result.json"))
    args = p.parse_args()

    out: dict = {"start": args.start, "end": args.end}
    if args.mode in ("pybroker", "both"):
        try:
            out["pybroker"] = run_pybroker(args.symbols, args.start, args.end)
        except Exception as e:
            out["pybroker"] = {"error": str(e)}

    if args.mode in ("multi", "both"):
        out["multi"] = {}
        for sym in args.symbols:
            try:
                out["multi"][sym] = run_multi(sym, args.start, args.end, n=args.n)
            except Exception as e:
                out["multi"][sym] = {"error": str(e)}

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(path), "modes": args.mode, "symbols": args.symbols}, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
