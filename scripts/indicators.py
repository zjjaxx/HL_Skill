"""Technical indicators: Bollinger, %b, bandwidth, RSI, MFI, volume MA."""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import (
    BB_PERIOD,
    BB_STD,
    MFI_PERIOD,
    RSI_PERIOD,
    VOL_EXPAND,
    VOL_MA_PERIOD,
    VOL_PANIC,
    VOL_SHRINK,
)


def bollinger(
    close: pd.Series,
    period: int = BB_PERIOD,
    n_std: float = BB_STD,
) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width,
            "pct_b": pct_b,
        }
    )


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = MFI_PERIOD,
) -> pd.Series:
    tp = (high + low + close) / 3.0
    mf = tp * volume
    direction = tp.diff()
    pos = mf.where(direction > 0, 0.0)
    neg = mf.where(direction < 0, 0.0)
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum().replace(0, np.nan)
    ratio = pos_sum / neg_sum
    return 100 - (100 / (1 + ratio))


def volume_flags(volume: pd.Series, period: int = VOL_MA_PERIOD) -> pd.DataFrame:
    vol_ma = volume.rolling(period).mean()
    return pd.DataFrame(
        {
            "vol_ma": vol_ma,
            "vol_expand": volume >= VOL_EXPAND * vol_ma,
            "vol_panic": volume >= VOL_PANIC * vol_ma,
            "vol_shrink": volume <= VOL_SHRINK * vol_ma,
        }
    )


def resample_ohlcv(daily: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate daily OHLCV to weekly (W-FRI) or monthly (ME)."""
    need = {"open", "high", "low", "close", "volume"}
    missing = need - set(daily.columns)
    if missing:
        raise ValueError(f"missing columns: {missing}")
    df = daily.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    agg = (
        df.resample(rule)
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["close"])
    )
    return agg


def attach_daily_indicators(qfq: pd.DataFrame) -> pd.DataFrame:
    """Compute day BB/%b/RSI/MFI on forward-adjusted daily bars."""
    out = qfq.copy()
    bb = bollinger(out["close"])
    out = pd.concat([out, bb], axis=1)
    out["rsi"] = rsi(out["close"])
    out["mfi"] = mfi(out["high"], out["low"], out["close"], out["volume"])
    out = pd.concat([out, volume_flags(out["volume"])], axis=1)
    return out


def _forming_tf_bollinger(
    daily: pd.DataFrame,
    period_rule: str,
) -> pd.DataFrame:
    """
    As-of-each-day higher-TF Bollinger using the *forming* bar (WTD / MTD).

    策略要求买入日当天周 %b 仍 ≤0.25，禁止「上周曾 ≤0.25、本周已修复仍可买」。
    因此周线不能只用上一根已收完 K 的 shift(1)，而要用「截至当日的当周/当月」收盘重算 %b。
    """
    cols = ["bb_mid", "bb_upper", "bb_lower", "bb_width", "pct_b", "tf_close", "tf_volume"]
    out = pd.DataFrame(index=daily.index, columns=cols, dtype=float)
    groups = daily.groupby(daily.index.to_period(period_rule))
    completed_closes: list[float] = []

    for _, group in groups:
        hist_close = pd.Series(completed_closes, dtype=float)
        for dt, row in group.iterrows():
            closes = pd.concat(
                [hist_close, pd.Series([float(row["close"])])],
                ignore_index=True,
            )
            bb = bollinger(closes)
            out.loc[dt, "bb_mid"] = bb["bb_mid"].iloc[-1]
            out.loc[dt, "bb_upper"] = bb["bb_upper"].iloc[-1]
            out.loc[dt, "bb_lower"] = bb["bb_lower"].iloc[-1]
            out.loc[dt, "bb_width"] = bb["bb_width"].iloc[-1]
            out.loc[dt, "pct_b"] = bb["pct_b"].iloc[-1]
            out.loc[dt, "tf_close"] = float(row["close"])

        completed_closes.append(float(group["close"].iloc[-1]))
        out.loc[group.index, "tf_volume"] = group["volume"].cumsum()

    # 量能：用已完成期 volume 序列 + 当日 forming volume 做均量判断
    vol_expand = pd.Series(index=daily.index, dtype=bool)
    vol_panic = pd.Series(index=daily.index, dtype=bool)
    vol_shrink = pd.Series(index=daily.index, dtype=bool)
    vol_ma = pd.Series(index=daily.index, dtype=float)

    completed_volumes = []
    for _, group in groups:
        hist_vol = pd.Series(completed_volumes, dtype=float)
        cum_vol = group["volume"].cumsum()
        for dt, v in cum_vol.items():
            vols = pd.concat([hist_vol, pd.Series([float(v)])], ignore_index=True)
            ma = vols.rolling(VOL_MA_PERIOD).mean().iloc[-1]
            vol_ma.loc[dt] = ma
            if pd.isna(ma) or ma <= 0:
                vol_expand.loc[dt] = False
                vol_panic.loc[dt] = False
                vol_shrink.loc[dt] = False
            else:
                vol_expand.loc[dt] = v >= VOL_EXPAND * ma
                vol_panic.loc[dt] = v >= VOL_PANIC * ma
                vol_shrink.loc[dt] = v <= VOL_SHRINK * ma
        completed_volumes.append(float(group["volume"].sum()))

    out["vol_ma"] = vol_ma
    out["vol_expand"] = vol_expand
    out["vol_panic"] = vol_panic
    out["vol_shrink"] = vol_shrink
    return out


def attach_higher_tf_to_daily(qfq: pd.DataFrame) -> pd.DataFrame:
    """
    周线：当日 forming（WTD）布林/%b —— 满足「买入日当天仍 ≤0.25」。
    月中轨（step1）：上一根*已收完*月线中轨。
    月 %b（减仓）：当日 forming（MTD）%b。
    """
    daily = qfq.copy()
    if not isinstance(daily.index, pd.DatetimeIndex):
        daily.index = pd.to_datetime(daily.index)
    daily = daily.sort_index()

    w = _forming_tf_bollinger(daily, "W-FRI")
    m_form = _forming_tf_bollinger(daily, "M")

    # 已收完月线中轨：用完整月 resample 后 shift(1) asof
    monthly = resample_ohlcv(daily, "ME")
    m_bb = bollinger(monthly["close"])
    m_completed = m_bb.shift(1)
    m_completed.index.name = "date"
    left = pd.DataFrame({"date": daily.index}).sort_values("date")
    m_mid_asof = pd.merge_asof(
        left,
        m_completed.reset_index().rename(columns={"bb_mid": "m_bb_mid_completed"}),
        on="date",
        direction="backward",
    ).set_index("date")

    out = attach_daily_indicators(daily)
    out["w_bb_mid"] = w["bb_mid"]
    out["w_bb_upper"] = w["bb_upper"]
    out["w_bb_lower"] = w["bb_lower"]
    out["w_bb_width"] = w["bb_width"]
    out["w_pct_b"] = w["pct_b"]
    out["w_vol_ma"] = w["vol_ma"]
    out["w_vol_expand"] = w["vol_expand"]
    out["w_vol_panic"] = w["vol_panic"]
    out["w_vol_shrink"] = w["vol_shrink"]
    out["w_close"] = w["tf_close"]

    # step1 用已收完月中轨；减仓用 MTD %b
    out["m_bb_mid"] = m_mid_asof["m_bb_mid_completed"]
    out["m_bb_upper"] = m_form["bb_upper"]
    out["m_bb_lower"] = m_form["bb_lower"]
    out["m_bb_width"] = m_form["bb_width"]
    out["m_pct_b"] = m_form["pct_b"]
    return out


def percentile_rank(series: pd.Series) -> pd.Series:
    """Expanding historical percentile of current value in [0, 1]."""
    def _rank(window: np.ndarray) -> float:
        if len(window) < 2 or np.isnan(window[-1]):
            return np.nan
        x = window[-1]
        hist = window[~np.isnan(window)]
        if len(hist) < 2:
            return np.nan
        return float((hist <= x).sum() / len(hist))

    return series.expanding().apply(_rank, raw=True)
