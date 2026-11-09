"""完整一年每股股息拼接 + 当前股息率 + 历史分位."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from config import YIELD_HISTORY_MIN_YEARS
from indicators import percentile_rank


@dataclass
class DividendEvent:
    ex_date: pd.Timestamp
    cash_ps: float  # 元/股（税前现金派息）


def normalize_dividends(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize BaoStock dividend table to columns: ex_date, cash_ps, year, month.
    Expects columns containing operate/ex-date and cash per share.
    """
    df = raw.copy()
    # BaoStock: dividOperateDate, dividCashPsBeforeTax
    date_col = next(
        (
            c
            for c in (
                "dividOperateDate",
                "ex_date",
                "divideDate",
                "dividPayDate",
            )
            if c in df.columns
        ),
        None,
    )
    cash_col = next(
        (
            c
            for c in (
                "dividCashPsBeforeTax",
                "cash_ps",
                "dividCashPsAfterTax",
            )
            if c in df.columns
        ),
        None,
    )
    if date_col is None or cash_col is None:
        raise ValueError(f"cannot find dividend columns in {list(df.columns)}")

    out = pd.DataFrame(
        {
            "ex_date": pd.to_datetime(df[date_col], errors="coerce"),
            "cash_ps": pd.to_numeric(df[cash_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["ex_date"])
    out["cash_ps"] = pd.to_numeric(out["cash_ps"], errors="coerce").fillna(0.0)
    out = out[out["cash_ps"] > 0].sort_values("ex_date").reset_index(drop=True)
    # BaoStock 偶发同日重复行（同金额）→ 按除权日去重，避免翻倍
    out = out.drop_duplicates(subset=["ex_date"], keep="first").reset_index(drop=True)
    out["year"] = out["ex_date"].dt.year
    out["month"] = out["ex_date"].dt.month
    return out


def _year_periods(year_df: pd.DataFrame) -> list[float]:
    """同一自然年内按除权日排序的各期现金派息（元/股）。"""
    if year_df.empty:
        return []
    g = (
        year_df.sort_values("ex_date")
        .groupby("ex_date", as_index=False)["cash_ps"]
        .sum()
    )
    return [float(x) for x in g["cash_ps"].tolist()]


def trailing_year_dividend(divs: pd.DataFrame, asof: pd.Timestamp | date) -> float:
    """
    完整一年每股股息（自然年期次拼接口径）。

    按「期次」对齐，不用死板同月份——避免年报/中报除权月漂移时
    把去年年报与今年年报叠成两份（如平安 2021-04 → 2022-06）。

    规则对齐策略文案第 3 点：
    - 以上一日历年派息期数/顺序为模板（一年两派 = 2 期）
    - 第 i 期：今年已派第 i 次 → 用今年；否则用去年第 i 次
    - 今年期数多于去年 → 多出的期次额外计入
    """
    asof = pd.Timestamp(asof)
    if divs.empty:
        return 0.0

    d = divs[divs["ex_date"] <= asof].copy()
    if d.empty:
        return 0.0

    this_year = asof.year
    last_year = this_year - 1
    last = d[d["year"] == last_year]
    curr = d[d["year"] == this_year]

    if last.empty:
        years = sorted(d["year"].unique())
        complete = [y for y in years if y < this_year]
        if not complete:
            return float(d["cash_ps"].sum())
        y = complete[-1]
        return float(sum(_year_periods(d.loc[d["year"] == y])))

    last_ps = _year_periods(last)
    curr_ps = _year_periods(curr)

    total = 0.0
    for i, last_amt in enumerate(last_ps):
        if i < len(curr_ps):
            total += curr_ps[i]
        else:
            total += last_amt
    if len(curr_ps) > len(last_ps):
        total += sum(curr_ps[len(last_ps) :])

    return float(total)


def dividend_yield_series(
    close_unadj: pd.Series,
    divs: pd.DataFrame,
) -> pd.Series:
    """股息率 = 完整一年每股股息 / 当日不复权收盘价."""
    close_unadj = close_unadj.copy()
    close_unadj.index = pd.to_datetime(close_unadj.index)
    yields = []
    for dt, px in close_unadj.items():
        if px is None or px <= 0 or np.isnan(px):
            yields.append(np.nan)
            continue
        dy = trailing_year_dividend(divs, dt)
        yields.append(dy / float(px))
    return pd.Series(yields, index=close_unadj.index, name="div_yield")


def yield_percentile_series(div_yield: pd.Series) -> pd.Series:
    """上市以来股息率历史分位；样本不足约 5 年的早期返回 NaN."""
    pct = percentile_rank(div_yield)
    # 约 5 年交易日
    min_bars = int(YIELD_HISTORY_MIN_YEARS * 242)
    valid_count = div_yield.expanding().count()
    pct = pct.where(valid_count >= min_bars)
    return pct.rename("yield_pct")


def cash_dividends_on_date(divs: pd.DataFrame, dt: pd.Timestamp) -> float:
    """每股现金派息（除权日入账）."""
    if divs.empty:
        return 0.0
    hit = divs[divs["ex_date"] == pd.Timestamp(dt).normalize()]
    if hit.empty:
        # tolerate date-only compare
        hit = divs[divs["ex_date"].dt.normalize() == pd.Timestamp(dt).normalize()]
    return float(hit["cash_ps"].sum()) if not hit.empty else 0.0
