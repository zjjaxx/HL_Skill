"""BaoStock data fetcher: OHLCV (qfq/raw), dividends, cashflow, basic info."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import baostock as bs
import pandas as pd

from config import CACHE_DIR

FIELDS_DAY = "date,code,open,high,low,close,volume,amount,turn,tradestatus,pctChg"

# 空分红仍写表头，避免 0 字节毒缓存；也表示「已拉过、确无记录」
_EMPTY_DIV_COLS = [
    "code",
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarket",
    "dividCashPsBeforeTax",
    "dividCashPsAfterTax",
    "dividStocksPs",
    "dividCashStock",
    "dividReserveToStockPs",
]

_login_depth = 0


def ensure_login() -> None:
    """引用计数登录：嵌套安全，depth>0 时不重复 login。"""
    global _login_depth
    if _login_depth == 0:
        rs = bs.login()
        if rs.error_code != "0":
            raise RuntimeError(f"baostock login failed: {rs.error_msg}")
    _login_depth += 1


def release_login() -> None:
    """与 ensure_login 成对；仅在最外层归零时 logout。"""
    global _login_depth
    if _login_depth <= 0:
        return
    _login_depth -= 1
    if _login_depth == 0:
        bs.logout()


@contextmanager
def baostock_session():
    """整批任务包一层，避免每票 login/logout。"""
    ensure_login()
    try:
        yield
    finally:
        release_login()


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


def _csv_usable(path: Path, *, require_date: bool = False) -> bool:
    """存在且可读、非空表头；行情文件还要求有 date 列。"""
    if not path.exists() or path.stat().st_size < 8:
        return False
    try:
        df = pd.read_csv(path, nrows=1)
    except Exception:
        return False
    if require_date and "date" not in df.columns:
        return False
    return True


def _invalidate(*paths: Path) -> None:
    for p in paths:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    if df.empty and len(df.columns) == 0:
        pd.DataFrame(columns=_EMPTY_DIV_COLS).to_csv(path, index=False)
    else:
        df.to_csv(path, index=False)


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
        return pd.DataFrame(columns=_EMPTY_DIV_COLS)
    return pd.concat(frames, ignore_index=True)


def fetch_cash_flow(symbol: str, year: int, quarter: int = 4) -> pd.DataFrame:
    code = to_bs_code(symbol)
    rs = bs.query_cash_flow_data(code=code, year=year, quarter=quarter)
    return _rs_to_df(rs)


def fetch_profit(symbol: str, year: int, quarter: int = 4) -> pd.DataFrame:
    code = to_bs_code(symbol)
    rs = bs.query_profit_data(code=code, year=year, quarter=quarter)
    return _rs_to_df(rs)


def _first_float(row: pd.Series, *cols: str) -> float | None:
    for c in cols:
        if c not in row.index:
            continue
        v = pd.to_numeric(row[c], errors="coerce")
        if pd.notna(v):
            return float(v)
    return None


def fetch_annual_cfo_and_shares(
    symbol: str,
    year: int,
    *,
    logged_in: bool = False,
) -> dict | None:
    """
    年报（Q4）经营现金流净额 + 总股本。

    BaoStock 现金流接口只有比率：CFO ≈ CFOToNP × netProfit
    （缺省时退 CFOToOR/CFOToGr × 营收）。
    """
    own_login = not logged_in
    if own_login:
        ensure_login()
    try:
        profit = fetch_profit(symbol, year, quarter=4)
        cf = fetch_cash_flow(symbol, year, quarter=4)
        if profit.empty:
            return None
        prow = profit.iloc[0]
        pub = pd.to_datetime(prow.get("pubDate"), errors="coerce")
        net_profit = _first_float(prow, "netProfit")
        revenue = _first_float(prow, "MBRevenue")
        total_share = _first_float(prow, "totalShare")
        if total_share is not None and 0 < total_share < 1e5:
            # 少数返回「万股」
            total_share *= 10000.0

        cfo = None
        if not cf.empty:
            crow = cf.iloc[0]
            cfo_to_np = _first_float(crow, "CFOToNP")
            cfo_to_or = _first_float(crow, "CFOToOR", "CFOToGr")
            if cfo_to_np is not None and net_profit is not None:
                cfo = cfo_to_np * net_profit
            elif cfo_to_or is not None and revenue is not None:
                cfo = cfo_to_or * revenue
            if pub is None or pd.isna(pub):
                pub = pd.to_datetime(crow.get("pubDate"), errors="coerce")

        if cfo is None or total_share is None or total_share <= 0:
            return None
        pub_s = None if pub is None or pd.isna(pub) else str(pd.Timestamp(pub).date())
        return {
            "year": year,
            "pub_date": pub_s,
            "cfo": float(cfo),
            "net_profit": net_profit,
            "total_share": float(total_share),
            "revenue": revenue,
        }
    finally:
        if own_login:
            release_login()


def _try_load_cached_pair(
    qfq_path: Path,
    raw_path: Path,
    div_path: Path,
    code: str,
) -> dict[str, pd.DataFrame] | None:
    if not (
        _csv_usable(qfq_path, require_date=True)
        and _csv_usable(raw_path, require_date=True)
        and _csv_usable(div_path, require_date=False)
    ):
        return None
    try:
        qfq = pd.read_csv(qfq_path, parse_dates=["date"])
        raw = pd.read_csv(raw_path, parse_dates=["date"])
        div = pd.read_csv(div_path)
    except Exception:
        _invalidate(qfq_path, raw_path, div_path)
        return None
    if qfq.empty or raw.empty or "date" not in qfq.columns or "date" not in raw.columns:
        _invalidate(qfq_path, raw_path, div_path)
        return None
    return {"qfq": qfq, "raw": raw, "dividend": div, "code": code}


def fetch_pair(
    symbol: str,
    start: str,
    end: str,
    cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """Fetch raw + qfq bars and dividends; optionally cache as csv."""
    code = to_bs_code(symbol)
    stem = code.replace(".", "_")
    out_dir = CACHE_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    qfq_path = out_dir / f"qfq_{start}_{end}.csv"
    raw_path = out_dir / f"raw_{start}_{end}.csv"
    div_path = out_dir / "dividend.csv"

    if cache:
        hit = _try_load_cached_pair(qfq_path, raw_path, div_path, code)
        if hit is not None:
            return hit

    ensure_login()
    try:
        qfq = fetch_kline(symbol, start, end, adjustflag="2")
        raw = fetch_kline(symbol, start, end, adjustflag="3")
        div = fetch_dividend(symbol)
        if qfq.empty or raw.empty:
            raise RuntimeError(
                f"baostock empty kline for {code} {start}..{end} "
                f"(qfq={len(qfq)}, raw={len(raw)})"
            )
        if cache:
            _write_csv(qfq, qfq_path)
            _write_csv(raw, raw_path)
            _write_csv(div, div_path)
        return {"qfq": qfq, "raw": raw, "dividend": div, "code": code}
    finally:
        release_login()


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch BaoStock OHLCV + dividends")
    p.add_argument("symbols", nargs="+", help="e.g. 600519 sh.601318 000858")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    summary = []
    with baostock_session():
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
