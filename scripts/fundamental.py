"""基本面硬门槛：股息率≥2×30Y国债、连续3年分红、现金流覆盖、市值≥门槛."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    CACHE_DIR,
    DIVIDEND_YEARS,
    MARKET_CAP_MIN_YI,
    TREASURY_YIELD_MULTIPLIER,
)
from dividend_yield import normalize_dividends, trailing_year_dividend
from fetch_baostock import (
    baostock_session,
    fetch_annual_cfo_and_shares,
    to_bs_code,
)


def consecutive_dividend_years(divs: pd.DataFrame, asof: pd.Timestamp, n: int = DIVIDEND_YEARS) -> bool:
    if divs.empty:
        return False
    d = normalize_dividends(divs) if "cash_ps" not in divs.columns else divs
    d = d[d["ex_date"] <= asof]
    if d.empty:
        return False
    years = set(d["year"].astype(int))
    y = asof.year
    # 要求最近完整 n 个日历年都有现金分红（y-1..y-n）
    need = {y - i for i in range(1, n + 1)}
    return need.issubset(years)


def _annual_fund_cache_path(symbol: str) -> Path:
    stem = to_bs_code(symbol).replace(".", "_")
    return CACHE_DIR / stem / "annual_cfo_shares.json"


def _load_annual_fund_cache(symbol: str) -> dict[str, dict]:
    path = _annual_fund_cache_path(symbol)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_annual_fund_cache(symbol: str, cache: dict[str, dict]) -> None:
    path = _annual_fund_cache_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_annual_cfo_shares(symbol: str, year: int, *, logged_in: bool = False) -> dict | None:
    """带磁盘缓存的年报 CFO + 总股本。"""
    cache = _load_annual_fund_cache(symbol)
    key = str(year)
    if key in cache:
        val = cache[key]
        return None if val is None else val
    row = fetch_annual_cfo_and_shares(symbol, year, logged_in=logged_in)
    cache[key] = row
    _save_annual_fund_cache(symbol, cache)
    return row


def _dividend_cash_total(divs: pd.DataFrame, year: int, total_share: float) -> float:
    """自然年除权日现金分红总额 ≈ Σ每股派息 × 该年报总股本。"""
    if divs.empty or total_share <= 0:
        return 0.0
    d = divs if "cash_ps" in divs.columns else normalize_dividends(divs)
    ps = float(d.loc[d["year"] == year, "cash_ps"].sum())
    return ps * float(total_share)


def _fiscal_years_for_asof(
    symbol: str,
    asof: pd.Timestamp,
    n: int = DIVIDEND_YEARS,
    *,
    logged_in: bool = False,
) -> list[int] | None:
    """
    取 asof 日已披露年报的最近 n 个完整财政年（通常为 y-1..，年报未出则再往前）。
    """
    years: list[int] = []
    y = asof.year - 1
    guard = asof.year - 15
    while y >= guard and len(years) < n:
        row = get_annual_cfo_shares(symbol, y, logged_in=logged_in)
        if row is None:
            y -= 1
            continue
        pub = row.get("pub_date")
        if pub:
            if pd.Timestamp(pub) > asof:
                y -= 1
                continue
        years.append(y)
        y -= 1
    return years if len(years) == n else None


def cashflow_covers_dividends(
    symbol: str,
    divs: pd.DataFrame,
    asof: pd.Timestamp,
    n: int = DIVIDEND_YEARS,
    *,
    logged_in: bool = False,
) -> bool | None:
    """
    近 n 年经营现金流净额合计 ≥ 近 n 年现金分红合计。

    CFO 由 BaoStock 比率还原：CFOToNP×净利润（或 CFOToOR×营收）。
    分红总额：各年除权派息每股合计 × 该年总股本。
    返回 None 表示年报不足，无法判断。
    """
    asof = pd.Timestamp(asof)
    d = normalize_dividends(divs) if not divs.empty and "cash_ps" not in divs.columns else (
        divs.copy() if not divs.empty else pd.DataFrame(columns=["ex_date", "cash_ps", "year", "month"])
    )
    years = _fiscal_years_for_asof(symbol, asof, n=n, logged_in=logged_in)
    if not years:
        return None

    cfo_sum = 0.0
    div_sum = 0.0
    for y in years:
        row = get_annual_cfo_shares(symbol, y, logged_in=logged_in)
        if row is None:
            return None
        cfo_sum += float(row["cfo"])
        div_sum += _dividend_cash_total(d, y, float(row["total_share"]))

    if div_sum <= 0:
        return False
    return cfo_sum >= div_sum


def hard_gate_snapshot(
    symbol: str,
    raw: pd.DataFrame,
    dividend: pd.DataFrame,
    market_cap_yi: float | None = None,
    bond_yield_pct: float | None = None,
    asof: pd.Timestamp | None = None,
    default_cashflow_pass: bool = False,
) -> dict:
    """
    Evaluate hard gates at asof (default last raw bar).
    pass_hard: 股息率≥2×30Y + 连续分红 + 现金流覆盖 + 市值。
    """
    asof = pd.Timestamp(asof or raw["date"].iloc[-1])
    divs = normalize_dividends(dividend) if not dividend.empty else pd.DataFrame()
    price = float(raw.loc[raw["date"] <= asof, "close"].iloc[-1]) if not raw.empty else np.nan

    consecutive = consecutive_dividend_years(divs, asof) if not divs.empty else False
    dy = trailing_year_dividend(divs, asof) / price if price and price > 0 and not divs.empty else 0.0
    dy_pct = dy * 100

    cf = cashflow_covers_dividends(symbol, divs, asof)
    if cf is None:
        cf_ok = bool(default_cashflow_pass)
    else:
        cf_ok = bool(cf)

    cap_ok = True if market_cap_yi is None else market_cap_yi >= MARKET_CAP_MIN_YI

    bond_ok = True
    if bond_yield_pct is not None:
        bond_ok = dy_pct >= TREASURY_YIELD_MULTIPLIER * float(bond_yield_pct)

    pass_all = bool(consecutive and cf_ok and cap_ok and bond_ok)

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
        "pass_hard": pass_all,
    }


def attach_fundamental_columns(
    raw: pd.DataFrame,
    qfq_feat: pd.DataFrame,
    dividend: pd.DataFrame,
    bond: pd.DataFrame,
    symbol: str | None = None,
    market_cap_yi: float | None = None,
    default_pass_cap_cf: bool = True,
) -> pd.DataFrame:
    """
    Attach daily fundamental flags used by signals.
    hard_ok = 股息×国债 + 分位≥80% + 连续3年分红 + 现金流覆盖分红 + 市值。
    市值未传入且 default_pass_cap_cf=True 时市值默认通过（回测简化）；
    现金流数据不足时不通过（不再默认放过）。
    """
    from config import YIELD_PERCENTILE_BUY
    from dividend_yield import dividend_yield_series, yield_percentile_series
    from fetch_bond_yield import asof_yield

    if not symbol:
        raise ValueError("attach_fundamental_columns 需要 symbol 以校验现金流覆盖分红")

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

    # 现金流按日 asof（年报披露日之前不用未披露年份）；年报数据磁盘缓存
    bond_vals = []
    cons = []
    cf_ok: list[bool] = []
    cf_memo: dict[tuple[int, ...] | None, bool] = {}
    with baostock_session():
        for dt in out.index:
            by = asof_yield(bond, dt) if bond is not None and not bond.empty else np.nan
            bond_vals.append(by)
            cons.append(consecutive_dividend_years(divs, dt) if not divs.empty else False)
            years = _fiscal_years_for_asof(symbol, pd.Timestamp(dt), logged_in=True)
            key: tuple[int, ...] | None = tuple(years) if years else None
            if key not in cf_memo:
                if key is None:
                    cf_memo[key] = False
                else:
                    cf = cashflow_covers_dividends(
                        symbol, divs, pd.Timestamp(dt), logged_in=True
                    )
                    cf_memo[key] = bool(cf) if cf is not None else False
            cf_ok.append(cf_memo[key])

    out["bond_30y_pct"] = bond_vals
    out["consec_div"] = cons
    out["cashflow_ok"] = cf_ok
    out["yield_vs_bond_ok"] = out["div_yield_pct"] >= TREASURY_YIELD_MULTIPLIER * out["bond_30y_pct"]
    out["yield_pctile_ok"] = out["yield_pctile"] >= YIELD_PERCENTILE_BUY
    if market_cap_yi is None and default_pass_cap_cf:
        out["cap_ok"] = True
    elif market_cap_yi is None:
        out["cap_ok"] = False
    else:
        out["cap_ok"] = market_cap_yi >= MARKET_CAP_MIN_YI
    out["hard_ok"] = (
        out["yield_vs_bond_ok"]
        & out["yield_pctile_ok"].fillna(False)
        & out["consec_div"]
        & out["cashflow_ok"]
        & out["cap_ok"]
    )
    return out
