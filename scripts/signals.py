"""Buy/sell signal generation per 布林线.md steps 0–5."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    DAY_PCTB_BUY_HIGH,
    DAY_PCTB_BUY_LOW,
    MONTH_PCTB_TRIM,
    OUTPUT_DIR,
    WEEK_PCTB_BUY_MAX,
    WEEK_PCTB_TRIM,
    YIELD_LADDER_END,
    YIELD_LADDER_START,
    YIELD_LADDER_STEP,
    YIELD_PERCENTILE_BUY,
)


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Expect columns from attach_higher_tf + fundamental:
      hard_ok, m_bb_mid, yield_pctile, w_pct_b, pct_b, low, bb_mid, close
    """
    out = df.copy()

    # Step1: 周收盘(当日WTD=日收) ≤ 上一根已收完月中轨；股息分位 ≥ 80%
    week_close = out["w_close"] if "w_close" in out.columns else out["close"]
    step1 = (week_close <= out["m_bb_mid"]) & (out["yield_pctile"] >= YIELD_PERCENTILE_BUY)

    # Step2: 当周 forming %b ≤ 0.25（买入日当天仍满足，非上一根已收完周线）
    step2 = out["w_pct_b"] <= WEEK_PCTB_BUY_MAX

    # Step3 day: low > day mid, 0.5 < pct_b <= 0.8
    step3 = (out["low"] > out["bb_mid"]) & (out["pct_b"] > DAY_PCTB_BUY_LOW) & (
        out["pct_b"] <= DAY_PCTB_BUY_HIGH
    )

    out["buy_signal"] = out["hard_ok"].fillna(False) & step1 & step2 & step3

    # Same-month buy limit handled in engine (global), not here.
    out["sell_week_trim"] = out["w_pct_b"] >= WEEK_PCTB_TRIM
    out["sell_month_trim"] = out["m_pct_b"] >= MONTH_PCTB_TRIM

    # Yield ladder thresholds: 78,76,...,70 as percentile * 100
    for level in range(YIELD_LADDER_START, YIELD_LADDER_END - 1, -YIELD_LADDER_STEP):
        out[f"yield_le_{level}"] = out["yield_pctile"] * 100 <= level

    out["yield_clear"] = out["yield_pctile"] * 100 <= YIELD_LADDER_END
    return out


def same_month_mask(buy_flags: pd.Series) -> pd.Series:
    """Keep only first True per calendar month."""
    s = buy_flags.fillna(False).astype(bool)
    month = s.index.to_period("M")
    first = s.groupby(month).cumsum() == 1
    return s & first


def build_signal_frame(feat: pd.DataFrame) -> pd.DataFrame:
    sig = add_signals(feat)
    sig["buy_exec"] = same_month_mask(sig["buy_signal"])
    return sig


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="feature parquet/csv with indicators+fundamental")
    p.add_argument("--output", default=str(OUTPUT_DIR / "signals.csv"))
    args = p.parse_args()

    path = Path(args.input)
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path, parse_dates=["date"])
        df = df.set_index("date")

    out = build_signal_frame(df)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.reset_index().to_csv(out_path, index=False)
    print(
        json.dumps(
            {
                "rows": len(out),
                "buy_signal": int(out["buy_signal"].sum()),
                "buy_exec": int(out["buy_exec"].sum()),
                "output": str(out_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
