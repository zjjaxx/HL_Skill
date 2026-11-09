"""基本面硬门槛：股息率≥2×30Y国债、连续3年分红、现金流覆盖、市值≥1000亿."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from config import (
    DIVIDEND_YEARS,
    MARKET_CAP_MIN_YI,
    TREASURY_YIELD_MULTIPLIER,
)
from dividend_yield import normalize_dividends, trailing_year_dividend
from fetch_baostock import fetch_cash_flow, to_bs_code


def consecutive_dividend_years(divs: pd.DataFrame, asof: pd.Timestamp, n: int = DIVIDEND_YEARS) -> bool:
    if divs.empty:
        return False
    d = normalize_dividends(divs) if "cash_ps" not in divs.columns else divs
    d = d[d["ex_date"] <= asof]
    if d.empty:
        return False
    years = set(d["year"].astype(int))
    y = asof.year
    # 要求最近完整 n 个日历年都有现金分红（不含当年未完结也可要求 y-1..y-n）
    need = {y - i for i in range(1, n + 1)}
    return need.issubset(years)


def cashflow_covers_dividends(
    symbol: str,
    divs: pd.DataFrame,
    asof: pd.Timestamp,
    n: int = DIVIDEND_YEARS,
) -> bool | None:
    """
    近 n 年经营现金流净额合计 ≥ 近 n 年现金分红合计。
    返回 None 表示数据不足（回测文档允许样本股默认通过时由调用方处理）。
    """
    d = normalize_dividends(divs) if "cash_ps" not in divs.columns else divs.copy()
    years = [asof.year - i for i in range(1, n + 1)]
    # 分红合计用每股×无法直接得总额；BaoStock 分红表无股本。
    # 近似：用 query_cash_flow 的 CFO，与分红派息总额字段（若有）比较；
    # 若只有每股，则退化为「每股维度」：要求每年 CFO 相关字段存在且分红连续。
    from fetch_baostock import _login, _logout

    cfo_sum = 0.0
    have = 0
    _login()
    try:
        for y in years:
            try:
                cf = fetch_cash_flow(symbol, y, quarter=4)
            except Exception:
                continue
            if cf.empty:
                continue
            col = None
            for cand in ("netCashOperAct", "CFOToNP", "CAOToAsset"):
                if cand in cf.columns:
                    col = cand
                    break
            if col is None:
                continue
            val = pd.to_numeric(cf.iloc[0][col], errors="coerce")
            if pd.isna(val):
                continue
            cfo_sum += float(val)
            have += 1
    finally:
        _logout()

    if have < n:
        return None

    div_cash_ps = float(d[d["year"].isin(years)]["cash_ps"].sum()) if not d.empty else 0.0
    if div_cash_ps <= 0:
        return False
    # BaoStock 无分红总额时：正经营现金流 + 有现金派息 → 通过
    # 严谨场景用东财 F10 分红总额对齐。
    return cfo_sum > 0


def hard_gate_snapshot(
    symbol: str,
    raw: pd.DataFrame,
    dividend: pd.DataFrame,
    market_cap_yi: float | None = None,
    bond_yield_pct: float | None = None,
    asof: pd.Timestamp | None = None,
    default_cashflow_pass: bool = True,
) -> dict:
    """
    Evaluate hard gates at asof (default last raw bar).
    pass_hard_ex_bond: 连续分红 + 市值 + 现金流（不含国债倍数，便于筛股阶段）
    pass_hard: 含股息率 ≥ 2×30Y
    """
    asof = pd.Timestamp(asof or raw["date"].iloc[-1])
    divs = normalize_dividends(dividend) if not dividend.empty else pd.DataFrame()
    price = float(raw.loc[raw["date"] <= asof, "close"].iloc[-1]) if not raw.empty else np.nan

    consecutive = consecutive_dividend_years(divs, asof) if not divs.empty else False
    dy = trailing_year_dividend(divs, asof) / price if price and price > 0 and not divs.empty else 0.0
    # dy 是小数（元/股价）；国债 yield 用百分数 → 统一成百分数比较
    dy_pct = dy * 100

    cf = cashflow_covers_dividends(symbol, divs, asof)
    if cf is None:
        cf_ok = default_cashflow_pass
    else:
        cf_ok = bool(cf)

    cap_ok = True if market_cap_yi is None else market_cap_yi >= MARKET_CAP_MIN_YI

    bond_ok = True
    if bond_yield_pct is not None:
        bond_ok = dy_pct >= TREASURY_YIELD_MULTIPLIER * float(bond_yield_pct)

    pass_ex = bool(consecutive and cf_ok and cap_ok)
    pass_all = bool(pass_ex and bond_ok)

    return {
        "symbol": to_bs_code(symbol),
        "asof": str(asof.date()),
        "price": price,
        "div_yield_pct": dy_pct,
        "bond_yield_pct": bond_yield_pct,
        "consecutive_3y": consecutive,
        "cashflow_ok": cf_ok,
        "market_cap_ok": cap_ok,
        "market_cap_yi": market_cap_yi,
        "pass_hard_ex_bond": pass_ex,
        "pass_hard": pass_all,
    }


def attach_fundamental_columns(
    raw: pd.DataFrame,
    qfq_feat: pd.DataFrame,
    dividend: pd.DataFrame,
    bond: pd.DataFrame,
    market_cap_yi: float | None = None,
    default_pass_cap_cf: bool = True,
) -> pd.DataFrame:
    """
    Attach daily fundamental flags used by signals.
    Backtest doc: 市值/现金流在样本股上可默认通过；硬过滤股息率×国债 + 连续3年分红。
    """
    from dividend_yield import dividend_yield_series, yield_percentile_series
    from fetch_bond_yield import asof_yield

    divs = normalize_dividends(dividend) if not dividend.empty else pd.DataFrame(
        columns=["ex_date", "cash_ps", "year", "month"]
    )
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])
    raw = raw.set_index("date").sort_index()

    dy = dividend_yield_series(raw["close"], divs)
    ypct = yield_percentile_series(dy)

    out = qfq_feat.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        if "date" in out.columns:
            out = out.set_index("date")
        out.index = pd.to_datetime(out.index)

    out["div_yield"] = dy.reindex(out.index)
    out["div_yield_pct"] = out["div_yield"] * 100
    out["yield_pctile"] = ypct.reindex(out.index)

    bond_vals = []
    cons = []
    for dt in out.index:
        by = asof_yield(bond, dt) if bond is not None and not bond.empty else np.nan
        bond_vals.append(by)
        cons.append(consecutive_dividend_years(divs, dt) if not divs.empty else False)
    out["bond_30y_pct"] = bond_vals
    out["consec_div"] = cons
    out["yield_vs_bond_ok"] = out["div_yield_pct"] >= TREASURY_YIELD_MULTIPLIER * out["bond_30y_pct"]
    out["cap_ok"] = True if (market_cap_yi is None and default_pass_cap_cf) else (
        True if market_cap_yi is None else market_cap_yi >= MARKET_CAP_MIN_YI
    )
    out["hard_ok"] = out["yield_vs_bond_ok"] & out["consec_div"] & out["cap_ok"]
    return out
