"""Fetch 30Y China treasury yield via opencli browser (East Money) + CSV fallback."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

from config import CACHE_DIR, EASTMONEY_BOND_YIELD, SESSION_NAME

OUT_PATH = CACHE_DIR / "cn_bond_30y.csv"


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _parse_json_stdout(stdout: str):
    text = (stdout or "").strip()
    if not text:
        return None
    start = text.find("{")
    start_arr = text.find("[")
    if start < 0 and start_arr < 0:
        return None
    if start < 0:
        start = start_arr
    elif start_arr >= 0:
        start = min(start, start_arr)
    chunk = text[start:]
    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(chunk)
    return data


def scrape_eastmoney_bond_yield(session: str = SESSION_NAME) -> pd.DataFrame:
    """
    Use opencli browser against 东方财富国债收益率页.
    Prefer DOM table parse; network is not the primary path.
    """
    r = _run(["opencli", "browser", session, "open", EASTMONEY_BOND_YIELD])
    if r.returncode != 0:
        raise RuntimeError(f"browser open failed: {r.stderr or r.stdout}")

    _run(["opencli", "browser", session, "wait", "time", "3"])

    eval_js = r"""(() => {
      const rows = [];
      document.querySelectorAll('table tr').forEach(tr => {
        const tds = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
        if (tds.length >= 2 && /\d{4}/.test(tds[0])) rows.push(tds);
      });
      return rows.slice(0, 500);
    })()"""
    ev = _run(["opencli", "browser", session, "eval", eval_js])
    rows = []
    data = _parse_json_stdout(ev.stdout)
    if isinstance(data, list):
        rows = data

    records = []
    for rrow in rows:
        if not isinstance(rrow, list) or len(rrow) < 2:
            continue
        date_s = rrow[0]
        nums = []
        for cell in rrow[1:]:
            try:
                nums.append(float(str(cell).replace("%", "").replace(",", "")))
            except ValueError:
                continue
        if not nums:
            continue
        y30 = nums[-1]
        records.append({"date": date_s, "yield_30y": y30 / (100 if y30 > 1 else 1)})

    try:
        _run(["opencli", "browser", session, "close"])
    except Exception:
        pass

    if not records:
        raise RuntimeError(
            "未能从东财页 DOM 解析 30Y 国债收益率。可手工补 data/cache/cn_bond_30y.csv"
        )

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    # If values look like percent (e.g. 2.5), keep as decimal fraction? Strategy compares
    # 股息率(小数或百分数) vs 2×国债。统一存「百分数」如 2.85 表示 2.85%。
    # 上面若 y30>1 已 /100 成小数；再转回百分数便于与股息率对齐。
    if df["yield_30y"].median() < 0.2:
        df["yield_30y"] = df["yield_30y"] * 100
    return df


def load_or_scrape(force: bool = False) -> pd.DataFrame:
    if OUT_PATH.exists() and not force:
        return pd.read_csv(OUT_PATH, parse_dates=["date"])
    df = scrape_eastmoney_bond_yield()
    df.to_csv(OUT_PATH, index=False)
    return df


def asof_yield(df: pd.DataFrame, dt) -> float:
    """Nearest available 30Y yield on or before dt, in percent (e.g. 2.85)."""
    dt = pd.Timestamp(dt)
    sub = df[df["date"] <= dt]
    if sub.empty:
        return float(df["yield_30y"].iloc[0])
    return float(sub["yield_30y"].iloc[-1])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="re-scrape via opencli browser")
    p.add_argument(
        "--seed-demo",
        action="store_true",
        help="write a flat demo series if scrape unavailable",
    )
    p.add_argument("--demo-yield", type=float, default=2.5)
    args = p.parse_args()

    if args.seed_demo and (args.force or not OUT_PATH.exists()):
        dates = pd.date_range("2015-01-01", pd.Timestamp.today(), freq="B")
        df = pd.DataFrame({"date": dates, "yield_30y": args.demo_yield})
        df.to_csv(OUT_PATH, index=False)
        print(json.dumps({"path": str(OUT_PATH), "rows": len(df), "mode": "demo"}))
        return

    try:
        df = load_or_scrape(force=args.force)
        print(json.dumps({"path": str(OUT_PATH), "rows": len(df), "mode": "scrape"}))
    except Exception as e:
        print(f"scrape failed: {e}", file=sys.stderr)
        if args.seed_demo:
            dates = pd.date_range("2015-01-01", pd.Timestamp.today(), freq="B")
            df = pd.DataFrame({"date": dates, "yield_30y": args.demo_yield})
            df.to_csv(OUT_PATH, index=False)
            print(json.dumps({"path": str(OUT_PATH), "rows": len(df), "mode": "demo-fallback"}))
        else:
            raise


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
