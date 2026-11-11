"""
东方财富红利股筛选：
1) opencli browser 打开分红配送/选股页
2) 通过 DOM 表格解析候选（主路径）；network 仅兜底
3) eastmoney quote 补市值后过滤；BaoStock 硬门槛
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from config import (
    CACHE_DIR,
    EASTMONEY_DIVIDEND_BOARD,
    EASTMONEY_XUANGU,
    MARKET_CAP_MIN_YI,
    SESSION_NAME,
)
from fetch_baostock import fetch_pair
from fundamental import hard_gate_snapshot

OUT_CANDIDATES = CACHE_DIR / "em_candidates.json"
OUT_PASSED = CACHE_DIR / "em_passed.json"
OUT_DOM_ROWS = CACHE_DIR / "em_dom_rows.json"

# 分红配送表数据行字段（实测）
_DOM_PARSE_JS = r"""(() => {
  const table = [...document.querySelectorAll("table")].find(t => (t.rows?.length || 0) > 10);
  if (!table) return [];
  return [...table.querySelectorAll("tr")].map(tr => {
    const tds = [...tr.querySelectorAll("td")].map(td =>
      (td.innerText || "").replace(/\s+/g, " ").trim()
    );
    if (tds.length < 8 || !/^[036]\d{5}$/.test(tds[0])) return null;
    const num = (s) => {
      const m = String(s || "").replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
      return m ? Number(m[0]) : null;
    };
    return {
      code: tds[0],
      name: tds[1],
      transfer_ratio: tds[3] || null,
      cash_div: tds[6] || null,
      yield_pct: num(tds[7]),
      eps: num(tds[8]),
      bvps: num(tds[9]),
      total_shares_yi: num(tds[13]),
      plan_notice_date: tds[14] || null,
      record_date: tds[15] || null,
      ex_date: tds[16] || null,
      progress: tds[17] || null,
      notice_date: tds[18] || null,
    };
  }).filter(Boolean);
})()"""

_FILTER_DATES_JS = r"""(() => {
  const sel = document.querySelector("#filter_date");
  if (!sel) return [];
  return [...sel.options].map(o => o.value).filter(Boolean);
})()"""


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _browser(session: str, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return _run(["opencli", "browser", session, *args], timeout=timeout)


def _parse_json_stdout(stdout: str):
    text = (stdout or "").strip()
    if not text:
        return None
    # opencli 可能在 JSON 后附带 Update available 提示
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


def opencli_quote(symbols: list[str]) -> list[dict]:
    if not symbols:
        return []
    joined = ",".join(symbols)
    r = _run(["opencli", "eastmoney", "quote", joined, "-f", "json"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    data = _parse_json_stdout(r.stdout)
    return data if isinstance(data, list) else ([data] if data else [])


def pick_report_date(options: list[str], preferred: str | None = None) -> str | None:
    """Prefer explicit date, else latest Dec-31 on/before today, else first option."""
    if preferred and preferred in options:
        return preferred
    today = date.today().isoformat()
    year_ends = sorted(
        (d for d in options if d.endswith("-12-31") and d <= today),
        reverse=True,
    )
    if year_ends:
        return year_ends[0]
    past = sorted((d for d in options if d <= today), reverse=True)
    return past[0] if past else (options[0] if options else None)


def browser_open(url: str, session: str = SESSION_NAME) -> dict:
    r = _browser(session, "open", url)
    if r.returncode != 0:
        raise RuntimeError(f"open failed: {r.stderr or r.stdout}")
    _browser(session, "wait", "time", "4")
    data = _parse_json_stdout(r.stdout)
    return data if isinstance(data, dict) else {"ok": True, "url": url}


def browser_eval_json(js: str, session: str = SESSION_NAME):
    r = _browser(session, "eval", js)
    if r.returncode != 0:
        raise RuntimeError(f"eval failed: {r.stderr or r.stdout}")
    return _parse_json_stdout(r.stdout)


def browser_select_report_date(
    session: str = SESSION_NAME,
    report_date: str | None = None,
) -> str | None:
    options = browser_eval_json(_FILTER_DATES_JS, session=session) or []
    if not isinstance(options, list):
        options = []
    chosen = pick_report_date([str(x) for x in options], preferred=report_date)
    if not chosen:
        return None
    r = _browser(session, "select", "#filter_date", chosen)
    if r.returncode != 0:
        raise RuntimeError(f"select #filter_date failed: {r.stderr or r.stdout}")
    _browser(session, "wait", "time", "4")
    return chosen


def browser_extract_dividend_rows(session: str = SESSION_NAME) -> list[dict]:
    """Parse dividend-board table rows from DOM."""
    data = browser_eval_json(_DOM_PARSE_JS, session=session)
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if re.fullmatch(r"[036]\d{5}", code):
            rows.append(item)
    return rows


def browser_extract_codes_from_dom(session: str = SESSION_NAME) -> list[str]:
    """Fallback: any visible A-share codes in body text."""
    js = r"""(() => {
      const text = document.body ? document.body.innerText : '';
      const codes = new Set();
      const re = /\b([036]\d{5})\b/g;
      let m;
      while ((m = re.exec(text)) !== null) codes.add(m[1]);
      return [...codes].slice(0, 300);
    })()"""
    data = browser_eval_json(js, session=session)
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if re.fullmatch(r"[036]\d{5}", str(x))]


def browser_scrape_dividend_board(
    session: str = SESSION_NAME,
    report_date: str | None = None,
) -> list[dict]:
    browser_open(EASTMONEY_DIVIDEND_BOARD, session=session)
    chosen = browser_select_report_date(session=session, report_date=report_date)
    rows = browser_extract_dividend_rows(session=session)
    if not rows:
        # DOM 空时再试正文代码兜底
        for code in browser_extract_codes_from_dom(session=session):
            rows.append({"code": code, "name": None, "report_date": chosen})
    for row in rows:
        row.setdefault("report_date", chosen)
        row.setdefault("source", "yjfp-dom")
    OUT_DOM_ROWS.write_text(
        json.dumps({"report_date": chosen, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return rows


def browser_try_xuangu(session: str = SESSION_NAME) -> list[dict]:
    """Secondary page: xuangu result codes via DOM."""
    browser_open(EASTMONEY_XUANGU, session=session)
    codes = browser_extract_codes_from_dom(session=session)
    return [{"code": c, "name": None, "source": "xuangu-dom"} for c in codes]


def browser_close(session: str = SESSION_NAME) -> None:
    _browser(session, "close")


def filter_by_market_cap_quote(codes: list[str], min_yi: float = MARKET_CAP_MIN_YI) -> list[dict]:
    """Use eastmoney quote marketCap (元) when available."""
    rows = []
    for i in range(0, len(codes), 20):
        batch = codes[i : i + 20]
        try:
            quotes = opencli_quote(batch)
        except Exception as e:
            print(f"quote batch failed: {e}", file=sys.stderr)
            continue
        for q in quotes:
            mc = q.get("marketCap")
            try:
                mc_yi = float(mc) / 1e8 if mc is not None else None
            except (TypeError, ValueError):
                mc_yi = None
            rows.append(
                {
                    "code": str(q.get("code") or ""),
                    "name": q.get("name"),
                    "price": q.get("price"),
                    "market_cap_yi": mc_yi,
                    "peDynamic": q.get("peDynamic"),
                }
            )
    return [r for r in rows if r.get("market_cap_yi") and r["market_cap_yi"] >= min_yi]


def screen(
    seed_codes: list[str] | None = None,
    use_browser: bool = True,
    apply_fundamental: bool = True,
    start: str = "2015-01-01",
    report_date: str | None = None,
) -> dict:
    meta_rows: list[dict] = []
    codes: list[str] = list(seed_codes or [])

    if use_browser:
        try:
            meta_rows.extend(browser_scrape_dividend_board(report_date=report_date))
            codes.extend(str(r["code"]) for r in meta_rows if r.get("code"))
        except Exception as e:
            print(f"dividend board scrape warn: {e}", file=sys.stderr)
        try:
            xuangu_rows = browser_try_xuangu()
            meta_rows.extend(xuangu_rows)
            codes.extend(str(r["code"]) for r in xuangu_rows if r.get("code"))
        except Exception as e:
            print(f"xuangu scrape warn: {e}", file=sys.stderr)
        try:
            browser_close()
        except Exception:
            pass

    seen: set[str] = set()
    uniq: list[str] = []
    for c in codes:
        c = re.sub(r"\D", "", str(c))[-6:]
        if len(c) == 6 and c not in seen:
            seen.add(c)
            uniq.append(c)

    by_code = {}
    for row in meta_rows:
        code = re.sub(r"\D", "", str(row.get("code") or ""))[-6:]
        if len(code) == 6 and code not in by_code:
            by_code[code] = row

    OUT_CANDIDATES.write_text(
        json.dumps(
            {
                "codes": uniq,
                "rows": [by_code[c] for c in uniq if c in by_code],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    quoted = filter_by_market_cap_quote(uniq) if uniq else []
    # merge DOM fields onto quote rows
    for q in quoted:
        dom = by_code.get(q["code"]) or {}
        for k in ("cash_div", "yield_pct", "progress", "report_date", "source"):
            if k in dom and dom[k] is not None:
                q[k] = dom[k]

    passed = []
    if apply_fundamental and quoted:
        end = pd.Timestamp.today().strftime("%Y-%m-%d")
        for row in quoted:
            sym = row["code"]
            try:
                pair = fetch_pair(sym, start, end, cache=True)
                gate = hard_gate_snapshot(
                    symbol=sym,
                    raw=pair["raw"],
                    dividend=pair["dividend"],
                    market_cap_yi=row.get("market_cap_yi"),
                )
                row = {**row, **gate}
                if gate.get("pass_hard_ex_bond"):
                    passed.append(row)
            except Exception as e:
                print(f"fundamental skip {sym}: {e}", file=sys.stderr)
    else:
        passed = quoted

    OUT_PASSED.write_text(json.dumps(passed, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"candidates": uniq, "quoted": quoted, "passed": passed, "dom_rows": meta_rows}


def main() -> None:
    p = argparse.ArgumentParser(description="Screen Hongli candidates via East Money DOM + BaoStock")
    p.add_argument("--seed", nargs="*", default=[], help="seed codes e.g. 601318 600519")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--no-fundamental", action="store_true")
    p.add_argument("--start", default="2015-01-01")
    p.add_argument(
        "--report-date",
        default=None,
        help="yjfp #filter_date value, e.g. 2025-12-31",
    )
    args = p.parse_args()

    result = screen(
        seed_codes=args.seed,
        use_browser=not args.no_browser,
        apply_fundamental=not args.no_fundamental,
        start=args.start,
        report_date=args.report_date,
    )
    print(
        json.dumps(
            {
                "n_candidates": len(result["candidates"]),
                "n_dom_rows": len(result["dom_rows"]),
                "n_quoted_cap": len(result["quoted"]),
                "n_passed": len(result["passed"]),
                "candidates_path": str(OUT_CANDIDATES),
                "dom_rows_path": str(OUT_DOM_ROWS),
                "passed_path": str(OUT_PASSED),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
