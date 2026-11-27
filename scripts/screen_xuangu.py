"""
东方财富条件选股粗筛：
  总市值>门槛 + 最新股息率(TTM)≥2×30Y + 红利行业白名单。

行业口径（配置 XUANGU_INDUSTRY_WHITELIST）：
  主仓 银行/保险/电力；次仓 证券/电信/铁路公路/燃气；卫星 油气开采。
  明确不碰：航运、白酒、水泥、传媒等（SECTOR_INDUSTRY_BLOCK_KEYWORDS）。

流程：
1) opencli browser 打开 https://xuangu.eastmoney.com/（session=hongli-em）
2) 按白名单行业分别调东财 search-code，合并去重
3) 落盘 data/cache/xuangu_universe.json

注意：此处股息率是东财「最新股息率(TTM)」，不是策略完整一年股息。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from config import (
    CACHE_DIR,
    EASTMONEY_XUANGU,
    MARKET_CAP_MIN_YI,
    SECTOR_FILTER_ENABLED,
    SECTOR_INDUSTRY_BLOCK_KEYWORDS,
    SESSION_NAME,
    TREASURY_YIELD_MULTIPLIER,
    XUANGU_INDUSTRY_WHITELIST,
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


def _industry_text(row: dict) -> str:
    parts = [
        row.get("INDUSTRY_ALL"),
        row.get("SW_INDUSTRY"),
        row.get("INDUSTRY"),
        row.get("INDUSTRY_LV1"),
    ]
    return " ".join(str(p) for p in parts if p)


def _blocked_industry(industry: str) -> bool:
    text = industry or ""
    return any(k in text for k in SECTOR_INDUSTRY_BLOCK_KEYWORDS)


def _parse_row(r: dict, key: str, sector_tag: str | None) -> dict | None:
    code = str(r.get("SECURITY_CODE") or "")
    name = str(r.get("SECURITY_SHORT_NAME") or "")
    if not code or "ST" in name.upper():
        return None
    industry = _industry_text(r)
    if _blocked_industry(industry):
        return None
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
        return None
    return {
        "code": code,
        "name": name,
        "em_price": price_f,
        "market_cap_yi": round(cap_yi, 2),
        "em_div_yield_pct": round(dy, 4),
        "industry": industry or None,
        "sector_tag": sector_tag,
        "source": "xuangu-search-code",
        "query": key,
    }


def _search_pages(key: str, page_size: int = 100) -> list[dict]:
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
        code = str(body.get("code") or "")
        msg = str(body.get("msg") or "")
        # 某行业当前无命中时东财返回非 100，视为空结果而非失败
        if code not in ("100", "0"):
            if "未能找到" in msg or "符合条件" in msg:
                break
            raise RuntimeError(f"xuangu search-code failed: {msg or body}")
        result = ((body.get("data") or {}).get("result") or {})
        data_list = result.get("dataList") or []
        if total is None:
            total = result.get("total")
        rows.extend(data_list)
        if not data_list:
            break
        if total is not None and page * page_size >= int(total):
            break
        if len(data_list) < page_size:
            break
        page += 1
    return rows


def fetch_xuangu_cap_yield(
    min_cap_yi: float,
    min_yield_pct: float,
    page_size: int = 100,
    *,
    sector_filter: bool | None = None,
    industries: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    """
    条件选股：
      总市值>N亿;最新股息率大于等于X%小于等于100%[;所属东财行业是Y]
    sector_filter 默认跟 SECTOR_FILTER_ENABLED；开启则按行业白名单多次查询合并。
    """
    use_sector = SECTOR_FILTER_ENABLED if sector_filter is None else bool(sector_filter)
    industry_list = list(industries or XUANGU_INDUSTRY_WHITELIST)

    base = f"总市值>{min_cap_yi:g}亿;最新股息率大于等于{min_yield_pct:g}%小于等于100%"
    queries: list[tuple[str, str | None]]
    if use_sector:
        queries = [
            (f"{base};所属东财行业是{ind}", ind) for ind in industry_list
        ]
    else:
        queries = [(base, None)]

    rows: list[dict] = []
    for key, tag in queries:
        for raw in _search_pages(key, page_size=page_size):
            parsed = _parse_row(raw, key, tag)
            if parsed is None:
                continue
            if parsed["market_cap_yi"] < min_cap_yi or parsed["em_div_yield_pct"] < min_yield_pct:
                continue
            rows.append(parsed)

    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r["code"] in seen:
            # 保留已有；若新的 industry 更完整可补
            continue
        seen.add(r["code"])
        out.append(r)
    out.sort(
        key=lambda x: (
            -(x.get("em_div_yield_pct") or 0),
            -(x.get("market_cap_yi") or 0),
        )
    )
    return out


def screen_xuangu(
    min_cap_yi: float = MARKET_CAP_MIN_YI,
    min_yield_pct: float | None = None,
    bond_30y_pct: float | None = None,
    open_browser: bool = True,
    close_browser: bool = False,
    sector_filter: bool | None = None,
) -> dict:
    if min_yield_pct is None:
        if bond_30y_pct is None:
            raise ValueError("need min_yield_pct or bond_30y_pct")
        min_yield_pct = TREASURY_YIELD_MULTIPLIER * float(bond_30y_pct)

    use_sector = SECTOR_FILTER_ENABLED if sector_filter is None else bool(sector_filter)

    browser_meta = None
    if open_browser:
        try:
            browser_meta = browser_open_xuangu()
        except Exception as e:
            browser_meta = {"ok": False, "error": str(e)}

    universe = fetch_xuangu_cap_yield(
        min_cap_yi, float(min_yield_pct), sector_filter=use_sector
    )
    payload = {
        "min_cap_yi": min_cap_yi,
        "min_em_div_yield_pct": float(min_yield_pct),
        "bond_30y_pct": bond_30y_pct,
        "sector_filter": use_sector,
        "industries": list(XUANGU_INDUSTRY_WHITELIST) if use_sector else None,
        "industry_block_keywords": list(SECTOR_INDUSTRY_BLOCK_KEYWORDS)
        if use_sector
        else None,
        "xuangu_url": EASTMONEY_XUANGU,
        "browser": browser_meta,
        "n": len(universe),
        "rows": universe,
        "note": (
            "粗筛=东财条件选股：总市值>门槛 且 最新股息率(TTM)≥2×30Y"
            + (
                " 且行业白名单(银行/保险/证券/电力/电信/铁路公路/燃气/油气)；"
                "剔除航运/白酒/水泥/传媒。"
                if use_sector
                else "；未启用行业过滤。"
            )
            + "不做策略完整一年股息过滤。"
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
    p = argparse.ArgumentParser(
        description="Xuangu: cap + EM股息 + 红利行业白名单"
    )
    p.add_argument("--min-cap-yi", type=float, default=MARKET_CAP_MIN_YI)
    p.add_argument("--bond-yield", type=float, required=True, help="China 30Y %%")
    p.add_argument("--min-yield", type=float, default=None, help="override EM yield floor %%")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--close-browser", action="store_true")
    p.add_argument(
        "--no-sector-filter",
        action="store_true",
        help="关闭行业白名单，仅市值+股息",
    )
    args = p.parse_args()
    result = screen_xuangu(
        min_cap_yi=args.min_cap_yi,
        min_yield_pct=args.min_yield,
        bond_30y_pct=args.bond_yield,
        open_browser=not args.no_browser,
        close_browser=args.close_browser,
        sector_filter=False if args.no_sector_filter else None,
    )
    by_tag: dict[str, int] = {}
    for r in result["rows"]:
        tag = r.get("sector_tag") or "未分类"
        by_tag[tag] = by_tag.get(tag, 0) + 1
    print(
        json.dumps(
            {
                "n": result["n"],
                "min_em_div_yield_pct": result["min_em_div_yield_pct"],
                "sector_filter": result.get("sector_filter"),
                "by_sector_tag": by_tag,
                "path": str(OUT_PATH),
                "browser_ok": (result.get("browser") or {}).get("ok"),
                "rows": [
                    {
                        "code": r["code"],
                        "name": r["name"],
                        "cap": r["market_cap_yi"],
                        "em_dy": r["em_div_yield_pct"],
                        "tag": r.get("sector_tag"),
                        "industry": r.get("industry"),
                    }
                    for r in result["rows"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
