"""本金浮亏 / 回本天数 / 价 MAE 等回测重点指标."""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


def round_metrics(
    cost: float,
    path: Sequence[tuple[pd.Timestamp, float]],
    entry_date: pd.Timestamp | None,
    exit_date: pd.Timestamp | None,
) -> dict:
    """
    path: (date, equity) where equity = 已实现(卖出回笼+股息) + 余仓×不复权收盘
    本金最大浮亏 = min(equity - cost)
    """
    if cost <= 0 or not path:
        return {
            "cost": cost,
            "max_capital_dd_amt": 0.0,
            "max_capital_dd_pct": 0.0,
            "recovered": False,
            "days_entry_to_recover": None,
            "days_trough_to_recover": None,
            "exit_date": str(exit_date.date()) if exit_date is not None else None,
            "entry_date": str(entry_date.date()) if entry_date is not None else None,
            "pnl": 0.0,
        }

    dates = [p[0] for p in path]
    eqs = np.array([p[1] for p in path], dtype=float)
    dd = eqs - cost
    min_i = int(np.nanargmin(dd))
    max_dd_amt = float(dd[min_i])
    max_dd_pct = max_dd_amt / cost

    recovered = False
    days_entry = None
    days_trough = None
    # 曾跌破成本后再次 >= 成本
    underwater = False
    trough_date = None
    for i, (dt, eq) in enumerate(path):
        if eq < cost:
            underwater = True
            if trough_date is None or eq <= eqs[min_i] + 1e-9:
                trough_date = dates[min_i]
        if underwater and eq >= cost:
            recovered = True
            if entry_date is not None:
                days_entry = (dt - entry_date).days
            if trough_date is not None:
                days_trough = (dt - trough_date).days
            break

    pnl = float(eqs[-1] - cost)
    return {
        "cost": cost,
        "max_capital_dd_amt": max_dd_amt,
        "max_capital_dd_pct": max_dd_pct,
        "recovered": recovered,
        "days_entry_to_recover": days_entry,
        "days_trough_to_recover": days_trough,
        "exit_date": str(exit_date.date()) if exit_date is not None else None,
        "entry_date": str(entry_date.date()) if entry_date is not None else None,
        "pnl": pnl,
        "pnl_pct": pnl / cost,
    }


def price_mae(entry_price: float, low_path: Sequence[float]) -> float:
    """价 MAE：买入价到路径最低价跌幅（不含股息）."""
    if not low_path or entry_price <= 0:
        return 0.0
    mn = min(low_path)
    return (mn - entry_price) / entry_price


def summarize_rounds(rounds: list[dict]) -> dict:
    if not rounds:
        return {"n": 0}
    dd_pct = [r["max_capital_dd_pct"] for r in rounds]
    pnl = [r["pnl"] for r in rounds]
    recovered = [r["recovered"] for r in rounds]
    return {
        "n": len(rounds),
        "win_rate": float(np.mean([p > 0 for p in pnl])),
        "avg_pnl_pct": float(np.mean([r.get("pnl_pct", 0) for r in rounds])),
        "worst_capital_dd_pct": float(np.min(dd_pct)),
        "avg_capital_dd_pct": float(np.mean(dd_pct)),
        "recover_rate": float(np.mean(recovered)),
    }
