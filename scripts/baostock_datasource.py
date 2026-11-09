"""PyBroker DataSource backed by BaoStock (前复权日线 + 预计算信号列)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pybroker
from pybroker.data import DataSource

from build_features import build_for_symbol
from fetch_baostock import to_bs_code

SIGNAL_COLS = [
    "buy_signal",
    "buy_exec",
    "sell_week_trim",
    "sell_month_trim",
    "yield_clear",
    "pct_b",
    "w_pct_b",
    "m_pct_b",
    "div_yield_pct",
    "yield_pctile",
    "hard_ok",
    "close_raw",
    "bb_mid",
    "m_bb_mid",
]


class BaoStockSignalDataSource(DataSource):
    """
    Loads BaoStock bars, builds strategy features, exposes signal columns to PyBroker.
    """

    def __init__(self, start: str, end: str, market_cap_yi: float | None = None):
        super().__init__()
        self.start = start
        self.end = end
        self.market_cap_yi = market_cap_yi
        pybroker.register_columns(*SIGNAL_COLS)

    def _fetch_data(self, symbols, start_date, end_date, _timeframe, _adjust):
        frames = []
        start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        for sym in symbols:
            feat = build_for_symbol(sym, self.start, self.end, market_cap_yi=self.market_cap_yi)
            df = feat.reset_index().rename(columns={"index": "date"})
            if "date" not in df.columns:
                df = feat.copy()
                df["date"] = feat.index
            df["symbol"] = to_bs_code(str(sym)).upper().replace("SH.", "SH").replace("SZ.", "SZ")
            # PyBroker prefers plain symbols; keep original request symbol
            df["symbol"] = str(sym)
            keep = ["date", "symbol", "open", "high", "low", "close", "volume"] + [
                c for c in SIGNAL_COLS if c in df.columns
            ]
            frames.append(df[keep])
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out["date"] = pd.to_datetime(out["date"])
        return out[(out["date"] >= pd.Timestamp(start_date)) & (out["date"] <= pd.Timestamp(end_date))]


def features_to_pybroker_df(feat: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = feat.reset_index().rename(columns={"index": "date"})
    if "date" not in df.columns:
        df["date"] = feat.index
    df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"])
    cols = ["date", "symbol", "open", "high", "low", "close", "volume"] + [
        c for c in SIGNAL_COLS if c in df.columns
    ]
    return df[cols]
