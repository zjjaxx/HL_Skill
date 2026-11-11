"""
按策略硬门槛子集筛选（非东财页股息率）：
  市值 ≥ 1000 亿
  策略股息率 = 完整一年每股股息 / 不复权收盘 ≥ 2 × 中国 30Y 国债

股票池：东财 clist 按总市值降序，截到市值门槛。
股息：BaoStock 已除权税前派息 + trailing_year_dividend。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from config import CACHE_DIR, MARKET_CAP_MIN_YI, TREASURY_YIELD_MULTIPLIER
from dividend_yield import normalize_dividends, trailing_year_dividend
from fetch_baostock import _login, _logout, fetch_dividend, fetch_kline

OUT_PATH = CACHE_DIR / "strategy_cap_yield_pass.json"
OUT_UNIVERSE = CACHE_DIR / "strategy_cap_universe.json"
OUT_BOND = CACHE_DIR / "cn_bond_30y.csv"

CLIST_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # 深沪主板/创业/科创


def fetch_large_caps(min_cap_yi: float = MARKET_CAP_MIN_YI) -> list[dict]:
    rows: list[dict] = []
    pn = 1
    pz = 100
    while True:
        qs = urllib.parse.urlencode(
            {
                "pn": pn,
                "pz": pz,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f20",
                "fs": CLIST_FS,
                "fields": "f12,f14,f2,f20",
            }
        )
        url = f"https://push2.eastmoney.com/api/qt/clist/get?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = (payload or {}).get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        stop = False
        for r in diff:
            code = str(r.get("f12") or "")
            name = str(r.get("f14") or "")
            price = r.get("f2")
            cap = r.get("f20")
            try:
                cap_yi = float(cap) / 1e8 if cap not in (None, "-", "") else None
            except (TypeError, ValueError):
                cap_yi = None
            if cap_yi is None:
                continue
            if cap_yi < min_cap_yi:
                stop = True
                break
            if "ST" in name.upper():
                continue
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "em_price": price,
                    "market_cap_yi": round(cap_yi, 2),
                }
            )
        if stop or len(diff) < pz:
            break
        pn += 1
        time.sleep(0.12)
    seen: set[str] = set()
    uniq = []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        uniq.append(r)
    return uniq


def strategy_yield_asof(symbol: str, asof: pd.Timestamp, year_start: int = 2020) -> dict:
    """Requires active baostock login."""
    # 足够覆盖「上一日历年模板」；K 线只需近两年即可取最新不复权收盘
    k_start = (asof - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    end = asof.strftime("%Y-%m-%d")
    raw = fetch_kline(symbol, k_start, end, adjustflag="3")
    div = fetch_dividend(symbol, year_start=year_start, year_end=asof.year)
    if raw.empty:
        return {"ok": False, "reason": "no_raw"}
    raw = raw[raw["date"] <= asof]
    if raw.empty:
        return {"ok": False, "reason": "no_raw_asof"}
    px = float(raw["close"].iloc[-1])
    if px <= 0:
        return {"ok": False, "reason": "bad_price"}
    if div.empty:
        return {
            "ok": True,
            "close": px,
            "trailing_div_ps": 0.0,
            "div_yield_pct": 0.0,
            "asof_bar": str(pd.Timestamp(raw["date"].iloc[-1]).date()),
        }
    try:
        d = normalize_dividends(div)
    except ValueError as e:
        return {"ok": False, "reason": f"div_norm:{e}"}
    trailing = float(trailing_year_dividend(d, asof))
    return {
        "ok": True,
        "close": px,
        "trailing_div_ps": trailing,
        "div_yield_pct": trailing / px * 100.0,
        "asof_bar": str(pd.Timestamp(raw["date"].iloc[-1]).date()),
    }


def load_bond_pct(override: float | None) -> float:
    if override is not None:
        return float(override)
    if OUT_BOND.exists():
        df = pd.read_csv(OUT_BOND, parse_dates=["date"])
        if not df.empty:
            return float(df["yield_30y"].iloc[-1])
    return 2.04


def main() -> None:
    p = argparse.ArgumentParser(description="Strategy hard screen: cap + yield vs 2x 30Y")
    p.add_argument("--min-cap-yi", type=float, default=MARKET_CAP_MIN_YI)
    p.add_argument("--bond-yield", type=float, default=None)
    p.add_argument("--asof", default=None, help="YYYY-MM-DD, default today")
    p.add_argument("--limit", type=int, default=0, help="debug: only first N large caps")
    args = p.parse_args()

    bond = load_bond_pct(args.bond_yield)
    min_yield = TREASURY_YIELD_MULTIPLIER * bond
    asof = pd.Timestamp(args.asof or pd.Timestamp.today().normalize())

    print(
        json.dumps(
            {
                "asof": str(asof.date()),
                "bond_30y_pct": bond,
                "min_div_yield_pct": min_yield,
                "min_cap_yi": args.min_cap_yi,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    universe = fetch_large_caps(args.min_cap_yi)
    if args.limit > 0:
        universe = universe[: args.limit]
    OUT_UNIVERSE.write_text(
        json.dumps({"n": len(universe), "rows": universe}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"n_universe_cap": len(universe)}, ensure_ascii=False), flush=True)

    passed: list[dict] = []
    _login()
    try:
        for i, row in enumerate(universe, 1):
            code = row["code"]
            try:
                y = strategy_yield_asof(code, asof)
            except Exception as e:
                y = {"ok": False, "reason": str(e)}
            dy = float(y["div_yield_pct"]) if y.get("ok") else None
            ok_yield = dy is not None and dy >= min_yield
            item = {
                "code": code,
                "name": row["name"],
                "market_cap_yi": row["market_cap_yi"],
                "close": y.get("close"),
                "trailing_div_ps": y.get("trailing_div_ps"),
                "div_yield_pct": None if dy is None else round(dy, 4),
                "asof_bar": y.get("asof_bar"),
                "bond_30y_pct": bond,
                "min_div_yield_pct": min_yield,
                "reason": y.get("reason"),
            }
            if ok_yield:
                passed.append(item)
            print(
                json.dumps(
                    {
                        "i": i,
                        "n": len(universe),
                        "code": code,
                        "name": row["name"],
                        "cap": row["market_cap_yi"],
                        "dy": item["div_yield_pct"],
                        "flag": "PASS" if ok_yield else "skip",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        _logout()

    passed.sort(key=lambda x: (-(x.get("div_yield_pct") or 0), -(x.get("market_cap_yi") or 0)))
    payload = {
        "asof": str(asof.date()),
        "bond_30y_pct": bond,
        "min_div_yield_pct": min_yield,
        "min_cap_yi": args.min_cap_yi,
        "n_universe_cap": len(universe),
        "n_passed": len(passed),
        "passed": passed,
        "note": "div_yield = trailing_year_dividend / baostock raw close; NOT eastmoney TTM",
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"n_passed": len(passed), "path": str(OUT_PATH), "passed": passed},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
