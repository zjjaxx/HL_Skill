"""BaoStock data fetcher: OHLCV (qfq/raw), dividends, cashflow, basic info."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import baostock as bs
import pandas as pd

from config import CACHE_DIR

FIELDS_DAY = "date,code,open,high,low,close,volume,amount,turn,tradestatus,pctChg"


def _login() -> None:
    rs = bs.login()
    if rs.error_code != "0":
        raise RuntimeError(f"baostock login failed: {rs.error_msg}")


def _logout() -> None:
    bs.logout()


def to_bs_code(symbol: str) -> str:
    s = symbol.strip().lower().replace("sh.", "").replace("sz.", "")
    s = s.replace("sh", "").replace("sz", "")
    if "." in symbol.lower():
        # already sh.600519
        parts = symbol.lower().split(".")
        if parts[0] in ("sh", "sz") and parts[1].isdigit():
            return f"{parts[0]}.{parts[1]}"
    if s.startswith("6") or s.startswith("9"):
        return f"sh.{s}"
    return f"sz.{s}"


def _rs_to_df(rs) -> pd.DataFrame:
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=rs.fields)
    return pd.DataFrame(rows, columns=rs.fields)


def fetch_kline(
    symbol: str,
    start: str,
    end: str,
    adjustflag: str = "2",
) -> pd.DataFrame:
    """
    adjustflag: 1=后复权 2=前复权 3=不复权
    """
    code = to_bs_code(symbol)
    rs = bs.query_history_k_data_plus(
        code,
        FIELDS_DAY,
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag=adjustflag,
    )
    df = _rs_to_df(rs)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["tradestatus"] == "1"] if "tradestatus" in df.columns else df
    return df.sort_values("date").reset_index(drop=True)


def fetch_dividend(symbol: str, year_start: int = 1990, year_end: int | None = None) -> pd.DataFrame:
    code = to_bs_code(symbol)
    year_end = year_end or pd.Timestamp.today().year
    frames = []
    for y in range(year_start, year_end + 1):
        rs = bs.query_dividend_data(code=code, year=str(y), yearType="report")
        df = _rs_to_df(rs)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_cash_flow(symbol: str, year: int, quarter: int = 4) -> pd.DataFrame:
    code = to_bs_code(symbol)
    rs = bs.query_cash_flow_data(code=code, year=year, quarter=quarter)
    return _rs_to_df(rs)


def fetch_profit(symbol: str, year: int, quarter: int = 4) -> pd.DataFrame:
    code = to_bs_code(symbol)
    rs = bs.query_profit_data(code=code, year=year, quarter=quarter)
    return _rs_to_df(rs)


def fetch_stock_basic(symbol: str | None = None) -> pd.DataFrame:
    if symbol:
        code = to_bs_code(symbol)
        rs = bs.query_stock_basic(code=code)
    else:
        rs = bs.query_stock_basic()
    return _rs_to_df(rs)


def fetch_pair(
    symbol: str,
    start: str,
    end: str,
    cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """Fetch raw + qfq bars and dividends; optionally cache as parquet/csv."""
    code = to_bs_code(symbol)
    stem = code.replace(".", "_")
    out_dir = CACHE_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    qfq_path = out_dir / f"qfq_{start}_{end}.csv"
    raw_path = out_dir / f"raw_{start}_{end}.csv"
    div_path = out_dir / "dividend.csv"

    _login()
    try:
        if cache and qfq_path.exists() and raw_path.exists() and div_path.exists():
            qfq = pd.read_csv(qfq_path, parse_dates=["date"])
            raw = pd.read_csv(raw_path, parse_dates=["date"])
            div = pd.read_csv(div_path)
            return {"qfq": qfq, "raw": raw, "dividend": div, "code": code}

        qfq = fetch_kline(symbol, start, end, adjustflag="2")
        raw = fetch_kline(symbol, start, end, adjustflag="3")
        div = fetch_dividend(symbol)
        if cache:
            qfq.to_csv(qfq_path, index=False)
            raw.to_csv(raw_path, index=False)
            div.to_csv(div_path, index=False)
        return {"qfq": qfq, "raw": raw, "dividend": div, "code": code}
    finally:
        _logout()


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch BaoStock OHLCV + dividends")
    p.add_argument("symbols", nargs="+", help="e.g. 600519 sh.601318 000858")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    summary = []
    for sym in args.symbols:
        data = fetch_pair(sym, args.start, args.end, cache=not args.no_cache)
        summary.append(
            {
                "code": data["code"],
                "qfq_rows": len(data["qfq"]),
                "raw_rows": len(data["raw"]),
                "div_rows": len(data["dividend"]),
            }
        )
        print(json.dumps(summary[-1], ensure_ascii=False))
    (CACHE_DIR / "fetch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    # Allow `python scripts/fetch_baostock.py` from repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
