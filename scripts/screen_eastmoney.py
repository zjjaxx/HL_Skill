"""
东方财富红利股筛选：
1) 优先 opencli eastmoney 适配器（quote / rank）
2) 用 opencli browser 打开分红配送/选股页，network 抓 API，落盘候选池
3) 再用 BaoStock 基本面硬门槛过滤（市值/连续分红/现金流）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def opencli_quote(symbols: list[str]) -> list[dict]:
    if not symbols:
        return []
    joined = ",".join(symbols)
    r = _run(["opencli", "eastmoney", "quote", joined, "-f", "json"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    data = json.loads(r.stdout)
    return data if isinstance(data, list) else [data]


def browser_open_dividend_board(session: str = SESSION_NAME) -> dict:
    r = _run(
        [
            "opencli",
            "browser",
            session,
            "open",
            EASTMONEY_DIVIDEND_BOARD,
            "--window",
            "background",
            "-f",
            "json",
        ]
    )
    if r.returncode != 0:
        raise RuntimeError(f"open failed: {r.stderr or r.stdout}")
    _run(["opencli", "browser", session, "wait", "time", "4", "-f", "json"])
    return json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"ok": True}


def browser_capture_network(session: str = SESSION_NAME) -> str:
    r = _run(["opencli", "browser", session, "network", "-f", "json"])
    preview_path = CACHE_DIR / "em_network_preview.json"
    preview_path.write_text(r.stdout or "[]", encoding="utf-8")
    return r.stdout or "[]"


def browser_extract_codes_from_dom(session: str = SESSION_NAME) -> list[str]:
    """Parse visible A-share codes from dividend board DOM."""
    js = r"""(() => {
      const text = document.body ? document.body.innerText : '';
      const codes = new Set();
      const re = /\b([036]\d{5})\b/g;
      let m;
      while ((m = re.exec(text)) !== null) codes.add(m[1]);
      return [...codes].slice(0, 300);
    })()"""
    r = _run(["opencli", "browser", session, "eval", js, "-f", "json"])
    try:
        payload = json.loads(r.stdout)
        data = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(data, list):
            return [str(x) for x in data if re.fullmatch(r"[036]\d{5}", str(x))]
    except json.JSONDecodeError:
        pass
    return []


def browser_try_xuangu(session: str = SESSION_NAME) -> list[str]:
    """Open xuangu page as secondary source; DOM scrape codes."""
    _run(
        [
            "opencli",
            "browser",
            session,
            "open",
            EASTMONEY_XUANGU,
            "--window",
            "background",
            "-f",
            "json",
        ]
    )
    _run(["opencli", "browser", session, "wait", "time", "5", "-f", "json"])
    browser_capture_network(session)
    return browser_extract_codes_from_dom(session)


def filter_by_market_cap_quote(codes: list[str], min_yi: float = MARKET_CAP_MIN_YI) -> list[dict]:
    """Use eastmoney quote marketCap (元) when available."""
    rows = []
    # batch to avoid too-long CLI args
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
) -> dict:
    codes: list[str] = list(seed_codes or [])

    if use_browser:
        try:
            browser_open_dividend_board()
            browser_capture_network()
            codes.extend(browser_extract_codes_from_dom())
        except Exception as e:
            print(f"dividend board scrape warn: {e}", file=sys.stderr)
        try:
            codes.extend(browser_try_xuangu())
        except Exception as e:
            print(f"xuangu scrape warn: {e}", file=sys.stderr)

    # de-dup preserve order
    seen = set()
    uniq = []
    for c in codes:
        c = re.sub(r"\D", "", str(c))[-6:]
        if len(c) == 6 and c not in seen:
            seen.add(c)
            uniq.append(c)

    OUT_CANDIDATES.write_text(
        json.dumps({"codes": uniq}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    quoted = filter_by_market_cap_quote(uniq) if uniq else []
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
    return {"candidates": uniq, "quoted": quoted, "passed": passed}


def main() -> None:
    p = argparse.ArgumentParser(description="Screen Hongli candidates via East Money + BaoStock")
    p.add_argument("--seed", nargs="*", default=[], help="seed codes e.g. 601318 600519")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--no-fundamental", action="store_true")
    p.add_argument("--start", default="2015-01-01")
    args = p.parse_args()

    result = screen(
        seed_codes=args.seed,
        use_browser=not args.no_browser,
        apply_fundamental=not args.no_fundamental,
        start=args.start,
    )
    print(
        json.dumps(
            {
                "n_candidates": len(result["candidates"]),
                "n_quoted_cap": len(result["quoted"]),
                "n_passed": len(result["passed"]),
                "passed_path": str(OUT_PASSED),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
