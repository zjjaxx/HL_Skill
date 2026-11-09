"""Build full feature+signal frame for one symbol."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from config import CACHE_DIR, OUTPUT_DIR
from fetch_baostock import fetch_pair
from fetch_bond_yield import load_or_scrape
from fundamental import attach_fundamental_columns
from indicators import attach_higher_tf_to_daily
from signals import build_signal_frame


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

    qfq["date"] = pd.to_datetime(qfq["date"])
    qfq = qfq.set_index("date").sort_index()
    feat = attach_higher_tf_to_daily(qfq[["open", "high", "low", "close", "volume"]])

    if bond is None:
        try:
            bond = load_or_scrape(force=False)
        except Exception:
            # flat demo so pipeline still runs offline
            bond = pd.DataFrame(
                {
                    "date": pd.date_range(start, end, freq="B"),
                    "yield_30y": 2.5,
                }
            )

    feat = attach_fundamental_columns(
        raw=raw,
        qfq_feat=feat,
        dividend=div,
        bond=bond,
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
