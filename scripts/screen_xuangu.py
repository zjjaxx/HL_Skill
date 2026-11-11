"""
东方财富条件选股粗筛：总市值>1000亿 + 最新股息率(TTM)≥2×30Y。

流程：
1) opencli browser 打开 https://xuangu.eastmoney.com/（session=hongli-em）
2) 调用东财条件选股 search-code API（与页面「总市值>1000亿;最新股息率…」等价）
3) 落盘 data/cache/xuangu_universe.json

注意：此处股息率是东财「最新股息率(TTM)」，不是策略完整一年股息。
策略复算在 run_today_scan 对每只股票单独执行，粗筛不再用策略股息率过滤。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from config import (
    CACHE_DIR,
    EASTMONEY_XUANGU,
    MARKET_CAP_MIN_YI,
    SESSION_NAME,
    TREASURY_YIELD_MULTIPLIER,
)

OUT_PATH = CACHE_DIR / "xuangu_universe.json"
SEARCH_URL = "https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code"


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def browser_open_xuangu(session: str = SESSION_NAME) -> dict:
    r = _run(["opencli", "browser", session, "open", EASTMONEY_XUANGU])
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    _run(["opencli", "browser", session, "wait", "time", "3"])
    return {"ok": True, "url": EASTMONEY_XUANGU}


def _parse_yi(text) -> float | None:
    """Parse '2190.15亿' / '2.20万亿' / bare number(元) → 亿元."""
    if text is None:
        return None
    s = str(text).strip().replace(",", "")
    try:
        if s.endswith("万亿"):
            return float(s[:-2]) * 10000.0
        if s.endswith("亿"):
            return float(s[:-1])
        v = float(s)
        # raw yuan
        if v > 1e6:
            return v / 1e8
        return v
    except (TypeError, ValueError):
        return None


def _pick_field(row: dict, *needles: str):
    for k, v in row.items():
        ku = str(k).upper()
        if any(n.upper() in ku for n in needles):
            return k, v
    return None, None


def fetch_xuangu_cap_yield(
    min_cap_yi: float,
    min_yield_pct: float,
    page_size: int = 100,
) -> list[dict]:
    """
    条件选股自然语言查询（与页面一致）：
      总市值>N亿;最新股息率大于等于X%小于等于100%
    """
    key = f"总市值>{min_cap_yi:g}亿;最新股息率大于等于{min_yield_pct:g}%小于等于100%"
    rows: list[dict] = []
    page = 1
    total = None
    while True:
        payload = {
            "keyWord": key,
            "pageSize": page_size,
            "pageNo": page,
            "fingerprint": "web",
        }
        req = urllib.request.Request(
            SEARCH_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if str(body.get("code")) not in ("100", "0"):
            raise RuntimeError(f"xuangu search-code failed: {body.get('msg') or body}")
        result = ((body.get("data") or {}).get("result") or {})
        data_list = result.get("dataList") or []
        if total is None:
            total = result.get("total")
        for r in data_list:
            code = str(r.get("SECURITY_CODE") or "")
            name = str(r.get("SECURITY_SHORT_NAME") or "")
            if not code or "ST" in name.upper():
                continue
            _, dy_raw = _pick_field(r, "DIVIDEND_NEWRATIO", "DIVIDEND")
            _, cap_raw = _pick_field(r, "TOAL_MARKET_VALUE", "TOTAL_MARKET_VALUE")
            try:
                dy = float(dy_raw) if dy_raw is not None else None
            except (TypeError, ValueError):
                dy = None
            cap_yi = _parse_yi(cap_raw)
            price = r.get("NEWEST_PRICE")
            try:
                price_f = float(price) if price is not None else None
            except (TypeError, ValueError):
                price_f = None
            if dy is None or cap_yi is None:
                continue
            if cap_yi < min_cap_yi or dy < min_yield_pct:
                continue
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "em_price": price_f,
                    "market_cap_yi": round(cap_yi, 2),
                    "em_div_yield_pct": round(dy, 4),
                    "source": "xuangu-search-code",
                    "query": key,
                }
            )
        if not data_list:
            break
        if total is not None and page * page_size >= int(total):
            break
        if len(data_list) < page_size:
            break
        page += 1

    seen: set[str] = set()
    out = []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)
    out.sort(key=lambda x: (-(x.get("em_div_yield_pct") or 0), -(x.get("market_cap_yi") or 0)))
    return out


def screen_xuangu(
    min_cap_yi: float = MARKET_CAP_MIN_YI,
    min_yield_pct: float | None = None,
    bond_30y_pct: float | None = None,
    open_browser: bool = True,
    close_browser: bool = False,
) -> dict:
    if min_yield_pct is None:
        if bond_30y_pct is None:
            raise ValueError("need min_yield_pct or bond_30y_pct")
        min_yield_pct = TREASURY_YIELD_MULTIPLIER * float(bond_30y_pct)

    browser_meta = None
    if open_browser:
        try:
            browser_meta = browser_open_xuangu()
        except Exception as e:
            browser_meta = {"ok": False, "error": str(e)}

    universe = fetch_xuangu_cap_yield(min_cap_yi, float(min_yield_pct))
    payload = {
        "min_cap_yi": min_cap_yi,
        "min_em_div_yield_pct": float(min_yield_pct),
        "bond_30y_pct": bond_30y_pct,
        "xuangu_url": EASTMONEY_XUANGU,
        "browser": browser_meta,
        "n": len(universe),
        "rows": universe,
        "note": (
            "粗筛=东财条件选股：总市值>门槛 且 最新股息率(TTM)≥2×30Y；"
            "不做策略完整一年股息过滤。"
        ),
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if close_browser:
        try:
            _run(["opencli", "browser", SESSION_NAME, "close"])
        except Exception:
            pass
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Xuangu: cap>1000亿 + EM最新股息率≥2×30Y")
    p.add_argument("--min-cap-yi", type=float, default=MARKET_CAP_MIN_YI)
    p.add_argument("--bond-yield", type=float, required=True, help="China 30Y %%")
    p.add_argument("--min-yield", type=float, default=None, help="override EM yield floor %%")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--close-browser", action="store_true")
    args = p.parse_args()
    result = screen_xuangu(
        min_cap_yi=args.min_cap_yi,
        min_yield_pct=args.min_yield,
        bond_30y_pct=args.bond_yield,
        open_browser=not args.no_browser,
        close_browser=args.close_browser,
    )
    print(
        json.dumps(
            {
                "n": result["n"],
                "min_em_div_yield_pct": result["min_em_div_yield_pct"],
                "path": str(OUT_PATH),
                "browser_ok": (result.get("browser") or {}).get("ok"),
                "top": [
                    {
                        "code": r["code"],
                        "name": r["name"],
                        "cap": r["market_cap_yi"],
                        "em_dy": r["em_div_yield_pct"],
                    }
                    for r in result["rows"][:10]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
