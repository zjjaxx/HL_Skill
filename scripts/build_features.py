"""Build full feature+signal frame for one symbol."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from config import CACHE_DIR, OUTPUT_DIR, YIELD_SERIES_START
from fetch_baostock import fetch_pair, to_bs_code
from fetch_bond_yield import load_or_scrape
from fundamental import attach_fundamental_columns
from indicators import attach_higher_tf_to_daily
from signals import build_signal_frame


def _longest_cached_raw(symbol: str, end: str) -> pd.DataFrame | None:
    """Prefer longest local raw_*.csv covering end (offline fallback)."""
    stem = to_bs_code(symbol).replace(".", "_")
    out_dir = CACHE_DIR / stem
    if not out_dir.exists():
        return None
    best_start = None
    best_df = None
    end_ts = pd.Timestamp(end)
    for p in out_dir.glob("raw_*.csv"):
        if not p.exists() or p.stat().st_size < 8:
            continue
        try:
            # stem like raw_2007-03-01_2026-11-10
            _, s, e = p.stem.split("_", 2)
            s_ts, e_ts = pd.Timestamp(s), pd.Timestamp(e)
        except Exception:
            continue
        if e_ts < end_ts:
            continue
        try:
            df = pd.read_csv(p, parse_dates=["date"])
        except Exception:
            continue
        if df.empty or "date" not in df.columns:
            continue
        if best_start is None or s_ts < best_start:
            best_start = s_ts
            best_df = df
    return best_df


def _raw_for_yield_percentile(
    symbol: str,
    start: str,
    end: str,
    raw_window: pd.DataFrame,
    div: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load listing-scale raw for yield percentile; fall back to longest cache / window."""
    yield_start = min(pd.Timestamp(start), pd.Timestamp(YIELD_SERIES_START)).strftime(
        "%Y-%m-%d"
    )
    if pd.Timestamp(yield_start) >= pd.Timestamp(start):
        return raw_window, div
    try:
        pair_long = fetch_pair(symbol, yield_start, end, cache=True)
        return pair_long["raw"], pair_long["dividend"]
    except Exception:
        cached = _longest_cached_raw(symbol, end)
        if cached is not None and len(cached) > len(raw_window):
            return cached, div
        return raw_window, div


def build_for_symbol(
    symbol: str,
    start: str,
    end: str,
    bond: pd.DataFrame | None = None,
    market_cap_yi: float | None = None,
) -> pd.DataFrame:
    pair = fetch_pair(symbol, start, end, cache=True)
    qfq = pair["qfq"].copy()
    raw = pair["raw"].copy()
    div = pair["dividend"]

    # 股息分位按「上市以来」：拉长历史算 dy/pctile，再对齐到回测窗口（避免 start=2018 截断压低分位）
    raw_for_yield, div = _raw_for_yield_percentile(symbol, start, end, raw, div)

    qfq["date"] = pd.to_datetime(qfq["date"])
    qfq = qfq.set_index("date").sort_index()
    feat = attach_higher_tf_to_daily(qfq[["open", "high", "low", "close", "volume"]])

    if bond is None:
        bond = load_or_scrape(force=False)

    feat = attach_fundamental_columns(
        raw=raw_for_yield,
        qfq_feat=feat,
        dividend=div,
        bond=bond,
        symbol=symbol,
        market_cap_yi=market_cap_yi,
        default_pass_cap_cf=True,
    )
    # merge raw close for engine
    raw_i = raw.copy()
    raw_i["date"] = pd.to_datetime(raw_i["date"])
    raw_i = raw_i.set_index("date").sort_index()
    feat["close_raw"] = raw_i["close"].reindex(feat.index).ffill()
    feat["symbol"] = pair["code"]

    sig = build_signal_frame(feat)
    return sig


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("symbol")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--market-cap-yi", type=float, default=None)
    p.add_argument("--output", default="")
    args = p.parse_args()

    df = build_for_symbol(args.symbol, args.start, args.end, market_cap_yi=args.market_cap_yi)
    out = Path(args.output) if args.output else OUTPUT_DIR / f"features_{args.symbol}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.reset_index().rename(columns={"index": "date"}).to_csv(out, index=False)
    # also dump raw/div paths hint
    print(
        json.dumps(
            {
                "symbol": args.symbol,
                "rows": len(df),
                "buy_exec": int(df["buy_exec"].sum()),
                "output": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
