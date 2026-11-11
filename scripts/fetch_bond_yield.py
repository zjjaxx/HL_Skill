"""Fetch 30Y China treasury yield: East Money API first, then browser DOM. No demo fallback."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from config import CACHE_DIR, EASTMONEY_BOND_YIELD, SESSION_NAME

OUT_PATH = CACHE_DIR / "cn_bond_30y.csv"

# 东方财富中美国债收益率（字段 EMM00166469 = 中国 30Y，单位：%）
EASTMONEY_BOND_API = "https://datacenter.eastmoney.com/api/data/get"
EASTMONEY_BOND_API_TOKEN = "894050c76af8597a853f5b408b759f5d"
CN_30Y_FIELD = "EMM00166469"

DEFAULT_RETRIES = 3
DEFAULT_RETRY_SLEEP_SEC = 2.0


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


def _normalize_yield_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["yield_30y"] = pd.to_numeric(df["yield_30y"], errors="coerce")
    df = df.dropna(subset=["date", "yield_30y"]).sort_values("date")
    if df.empty:
        raise RuntimeError("30Y 国债序列为空")
    # 统一存百分数（如 2.04）；若误存成小数则放大
    if float(df["yield_30y"].median()) < 0.2:
        df["yield_30y"] = df["yield_30y"] * 100
    return df.reset_index(drop=True)


def scrape_eastmoney_bond_api(pages: int = 0) -> pd.DataFrame:
    """
    Pull China 30Y via datacenter API (same source as zmgzsyl.html).
    pages=0 means all pages; otherwise first N pages (newest first).
    """
    records: list[dict] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        if pages and page > pages:
            break
        qs = urllib.parse.urlencode(
            {
                "type": "RPTA_WEB_TREASURYYIELD",
                "sty": "ALL",
                "st": "SOLAR_DATE",
                "sr": "-1",
                "token": EASTMONEY_BOND_API_TOKEN,
                "p": str(page),
                "ps": "500",
                "pageNo": str(page),
                "pageNum": str(page),
            }
        )
        url = f"{EASTMONEY_BOND_API}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = payload.get("result") or {}
        if page == 1:
            total_pages = int(result.get("pages") or 1)
        rows = result.get("data") or []
        if not rows:
            break
        for row in rows:
            y = row.get(CN_30Y_FIELD)
            if y is None or y == "":
                continue
            records.append({"date": row.get("SOLAR_DATE"), "yield_30y": y})
        page += 1

    if not records:
        raise RuntimeError("东财国债 API 未返回中国 30Y（EMM00166469）")
    return _normalize_yield_df(pd.DataFrame(records))


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
        # 中美页：中国 30Y 通常是第 5 列（index 4）；不够则取最后一列中国侧
        y30 = nums[3] if len(nums) >= 4 else nums[-1]
        records.append({"date": date_s, "yield_30y": y30})

    try:
        _run(["opencli", "browser", session, "close"])
    except Exception:
        pass

    if not records:
        raise RuntimeError("未能从东财页 DOM 解析 30Y 国债收益率")

    return _normalize_yield_df(pd.DataFrame(records))


def fetch_bond_yield(
    *,
    retries: int = DEFAULT_RETRIES,
    sleep_sec: float = DEFAULT_RETRY_SLEEP_SEC,
    use_browser: bool = True,
) -> pd.DataFrame:
    """
    Keep trying: API (all pages) → browser DOM, with retries.
    Never invents a default yield; raises if all attempts fail.
    """
    errors: list[str] = []
    attempts = max(1, int(retries))

    for i in range(attempts):
        try:
            return scrape_eastmoney_bond_api(pages=0)
        except Exception as e:
            errors.append(f"api[{i + 1}/{attempts}]: {e}")
            time.sleep(sleep_sec)

    if use_browser:
        for i in range(attempts):
            try:
                return scrape_eastmoney_bond_yield()
            except Exception as e:
                errors.append(f"browser[{i + 1}/{attempts}]: {e}")
                time.sleep(sleep_sec)

    raise RuntimeError(
        "无法获取中国 30Y 国债收益率（已重试 API"
        + ("+browser" if use_browser else "")
        + "）。请检查网络/opencli 后重跑，勿用默认值。\n"
        + "\n".join(errors)
    )


def load_or_scrape(
    force: bool = False,
    *,
    retries: int = DEFAULT_RETRIES,
    use_browser: bool = True,
) -> pd.DataFrame:
    if OUT_PATH.exists() and not force:
        df = _normalize_yield_df(pd.read_csv(OUT_PATH, parse_dates=["date"]))
        # 拒绝历史 demo 常数序列（如全表 2.5）
        if df["yield_30y"].nunique() <= 1:
            force = True
        else:
            return df
    df = fetch_bond_yield(retries=retries, use_browser=use_browser)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_PATH, index=False)
    return df


def asof_yield(df: pd.DataFrame, dt) -> float:
    """Nearest available 30Y yield on or before dt, in percent (e.g. 2.85)."""
    dt = pd.Timestamp(dt)
    sub = df[df["date"] <= dt]
    if sub.empty:
        raise RuntimeError(f"国债序列在 {dt.date()} 及之前无可用数据")
    return float(sub["yield_30y"].iloc[-1])


def main() -> None:
    p = argparse.ArgumentParser(
        description="抓取中国 30Y 国债收益率；失败重试，不用默认值"
    )
    p.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--no-browser", action="store_true", help="仅用 API，不走 opencli")
    args = p.parse_args()

    df = load_or_scrape(
        force=args.force,
        retries=args.retries,
        use_browser=not args.no_browser,
    )
    latest = float(df["yield_30y"].iloc[-1])
    print(
        json.dumps(
            {
                "path": str(OUT_PATH),
                "rows": len(df),
                "mode": "scrape",
                "latest_bond_30y_pct": latest,
                "latest_date": str(pd.Timestamp(df["date"].iloc[-1]).date()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
