"""
硬门槛快筛（skill 口径子集）：
  市值 ≥ 1000 亿 且 东财页股息率 ≥ 2 × 中国 30Y 国债收益率

流程：
1) browser 开中美国债页，DOM 解析中国 30Y
2) browser 开分红送配，切报告期，翻页 DOM 解析表格
3) 保留股息率达门槛的行；再用 eastmoney quote 过滤市值
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from config import (
    CACHE_DIR,
    EASTMONEY_BOND_YIELD,
    EASTMONEY_DIVIDEND_BOARD,
    MARKET_CAP_MIN_YI,
    SESSION_NAME,
    TREASURY_YIELD_MULTIPLIER,
)
from screen_eastmoney import (
    _DOM_PARSE_JS,
    _browser,
    _parse_json_stdout,
    browser_close,
    browser_eval_json,
    browser_open,
    browser_select_report_date,
    opencli_quote,
)

OUT_BOND = CACHE_DIR / "cn_bond_30y.csv"
OUT_RESULT = CACHE_DIR / "em_cap_yield_pass.json"
OUT_RAW = CACHE_DIR / "em_cap_yield_raw.json"


def scrape_china_30y(session: str = SESSION_NAME) -> float:
    """Parse latest China 30Y (%) from 中美国债收益率 DOM table."""
    browser_open(EASTMONEY_BOND_YIELD, session=session)
    js = r"""(() => {
      const rows = [...document.querySelectorAll("table tr")].map(tr =>
        [...tr.children].map(td => (td.innerText || "").replace(/\s+/g, " ").trim())
      );
      for (const cells of rows) {
        if (cells.length >= 5 && /^\d{4}-\d{2}-\d{2}$/.test(cells[0])) {
          const y = Number(String(cells[4]).replace(/[^\d.-]/g, ""));
          if (Number.isFinite(y)) return {date: cells[0], yield_30y: y};
        }
      }
      return null;
    })()"""
    data = browser_eval_json(js, session=session)
    if not isinstance(data, dict) or data.get("yield_30y") is None:
        raise RuntimeError(f"failed to parse China 30Y from DOM: {data}")
    y = float(data["yield_30y"])
    import pandas as pd

    today = pd.Timestamp.today().normalize()
    dates = pd.date_range("2015-01-01", today, freq="B")
    pd.DataFrame({"date": dates, "yield_30y": y}).to_csv(OUT_BOND, index=False)
    return y


def try_sort_yield_desc(session: str = SESSION_NAME) -> None:
    """Best-effort: click visible 股息率 header (may be asc/desc toggle)."""
    r = _browser(session, "find", "--css", "th", "--limit", "50")
    data = _parse_json_stdout(r.stdout) or {}
    entries = data.get("entries") if isinstance(data, dict) else None
    ref = None
    if isinstance(entries, list):
        for e in entries:
            if "股息" in str(e.get("text") or "") and e.get("visible"):
                ref = e.get("ref")
        if ref is None:
            for e in entries:
                if "股息" in str(e.get("text") or ""):
                    ref = e.get("ref")
    if ref is None:
        return
    _browser(session, "click", str(ref))
    _browser(session, "wait", "time", "3")
    # second click often flips to descending
    _browser(session, "click", str(ref))
    _browser(session, "wait", "time", "3")


def click_next_page(session: str = SESSION_NAME) -> bool:
    r = _browser(session, "find", "--text", "下一页")
    data = _parse_json_stdout(r.stdout) or {}
    if isinstance(data, dict) and data.get("error"):
        return False
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        return False
    ref = entries[0].get("ref")
    if ref is None:
        return False
    cr = _browser(session, "click", str(ref))
    if cr.returncode != 0:
        return False
    _browser(session, "wait", "time", "3")
    return True


def scrape_dividend_pages(
    session: str = SESSION_NAME,
    report_date: str | None = None,
    min_yield_pct: float = 4.0,
    max_pages: int = 80,
) -> list[dict]:
    browser_open(EASTMONEY_DIVIDEND_BOARD, session=session)
    chosen = browser_select_report_date(session=session, report_date=report_date)
    try_sort_yield_desc(session=session)

    all_rows: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        rows = browser_eval_json(_DOM_PARSE_JS, session=session) or []
        if not isinstance(rows, list) or not rows:
            break
        n_hit = 0
        page_max = None
        for item in rows:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "")
            if not re.fullmatch(r"[036]\d{5}", code) or code in seen:
                continue
            try:
                yp_f = float(item["yield_pct"]) if item.get("yield_pct") is not None else None
            except (TypeError, ValueError):
                yp_f = None
            if yp_f is not None:
                page_max = yp_f if page_max is None else max(page_max, yp_f)
            if yp_f is None or yp_f < min_yield_pct:
                continue
            seen.add(code)
            n_hit += 1
            all_rows.append(
                {
                    **item,
                    "report_date": chosen,
                    "source": "yjfp-dom",
                    "page": page,
                }
            )
        print(
            json.dumps(
                {
                    "page": page,
                    "n_page": len(rows),
                    "n_hit_page": n_hit,
                    "n_kept": len(all_rows),
                    "page_max_yield": page_max,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not click_next_page(session=session):
            break
    return all_rows


def main() -> None:
    p = argparse.ArgumentParser(description="Screen: cap>=1000亿 & yield>=2x 30Y")
    p.add_argument("--report-date", default="2025-12-31")
    p.add_argument("--max-pages", type=int, default=80)
    p.add_argument("--bond-yield", type=float, default=None, help="override China 30Y %")
    p.add_argument("--min-cap-yi", type=float, default=MARKET_CAP_MIN_YI)
    args = p.parse_args()

    try:
        bond = args.bond_yield
        if bond is None:
            bond = scrape_china_30y()
        min_yield = TREASURY_YIELD_MULTIPLIER * float(bond)
        print(
            json.dumps(
                {
                    "bond_30y_pct": bond,
                    "min_div_yield_pct": min_yield,
                    "min_cap_yi": args.min_cap_yi,
                    "report_date": args.report_date,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        rows = scrape_dividend_pages(
            report_date=args.report_date,
            min_yield_pct=min_yield,
            max_pages=args.max_pages,
        )
        OUT_RAW.write_text(
            json.dumps(
                {"bond_30y_pct": bond, "min_div_yield_pct": min_yield, "rows": rows},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        codes = [r["code"] for r in rows]
        quoted_all: list[dict] = []
        for i in range(0, len(codes), 20):
            batch = codes[i : i + 20]
            try:
                quoted_all.extend(opencli_quote(batch))
            except Exception as e:
                print(f"quote batch failed: {e}", file=sys.stderr)

        by_code = {str(q.get("code")): q for q in quoted_all}
        dom_by = {r["code"]: r for r in rows}
        passed = []
        for code in codes:
            q = by_code.get(code) or {}
            mc = q.get("marketCap")
            try:
                mc_yi = float(mc) / 1e8 if mc is not None else None
            except (TypeError, ValueError):
                mc_yi = None
            if mc_yi is None or mc_yi < args.min_cap_yi:
                continue
            dom = dom_by.get(code) or {}
            passed.append(
                {
                    "code": code,
                    "name": q.get("name") or dom.get("name"),
                    "price": q.get("price"),
                    "market_cap_yi": round(mc_yi, 2),
                    "div_yield_pct": dom.get("yield_pct"),
                    "cash_div": dom.get("cash_div"),
                    "bond_30y_pct": bond,
                    "min_div_yield_pct": min_yield,
                    "report_date": dom.get("report_date"),
                    "progress": dom.get("progress"),
                }
            )

        passed.sort(
            key=lambda x: (-(x.get("div_yield_pct") or 0), -(x.get("market_cap_yi") or 0))
        )
        payload = {
            "bond_30y_pct": bond,
            "min_div_yield_pct": min_yield,
            "min_cap_yi": args.min_cap_yi,
            "n_high_yield": len(rows),
            "n_passed": len(passed),
            "passed": passed,
        }
        OUT_RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**payload, "path": str(OUT_RESULT)}, ensure_ascii=False, indent=2))
    finally:
        try:
            browser_close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
