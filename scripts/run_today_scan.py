"""
今日扫描主流程（skill 约定）：
1) 查 30Y 国债
2) 东财条件选股粗筛：总市值>1000亿 且 最新股息率(TTM)≥2×30Y
3) 对每只候选：特征/硬门槛/布林信号 + 多账号策略回测
4) 每只股票落一份结果；今日买点置顶并附判断条件；写详细 REPORT.md

输出：
  output/today_scan/summary.json
  output/today_scan/REPORT.md
  output/today_scan/{code}.json
  output/features_{code}.csv
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

from config import (
    CACHE_DIR,
    DAY_PCTB_BUY_HIGH,
    DAY_PCTB_BUY_LOW,
    MARKET_CAP_MIN_YI,
    OUTPUT_DIR,
    TREASURY_YIELD_MULTIPLIER,
    WEEK_PCTB_BUY_MAX,
    YIELD_PERCENTILE_BUY,
)
from build_features import build_for_symbol
from fetch_baostock import fetch_pair
from fetch_bond_yield import OUT_PATH as BOND_OUT, load_or_scrape, scrape_eastmoney_bond_yield
from metrics import summarize_rounds
from multi_account import MultiAccountEngine, find_min_accounts
from screen_xuangu import screen_xuangu

SCAN_DIR = OUTPUT_DIR / "today_scan"


def _safe_float(x):
    try:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def ensure_bond(force: bool = False, demo: bool = False) -> dict:
    if demo:
        dates = pd.date_range("2015-01-01", pd.Timestamp.today(), freq="B")
        pd.DataFrame({"date": dates, "yield_30y": 2.5}).to_csv(BOND_OUT, index=False)
        return {"mode": "demo", "bond_30y_pct": 2.5, "path": str(BOND_OUT)}
    if force or not BOND_OUT.exists():
        try:
            df = scrape_eastmoney_bond_yield()
            df.to_csv(BOND_OUT, index=False)
            y = float(df["yield_30y"].iloc[-1])
            return {"mode": "scrape", "bond_30y_pct": y, "path": str(BOND_OUT)}
        except Exception as e:
            if not BOND_OUT.exists():
                raise
            df = load_or_scrape(force=False)
            return {
                "mode": f"cache-after-fail:{e}",
                "bond_30y_pct": float(df["yield_30y"].iloc[-1]),
                "path": str(BOND_OUT),
            }
    df = load_or_scrape(force=False)
    return {"mode": "cache", "bond_30y_pct": float(df["yield_30y"].iloc[-1]), "path": str(BOND_OUT)}


def evaluate_buy_conditions(row: pd.Series) -> dict:
    """对齐 signals.add_signals 的买入拆解（便于报告）。"""
    week_close = _safe_float(row.get("w_close", row.get("close")))
    m_bb_mid = _safe_float(row.get("m_bb_mid"))
    yield_pctile = _safe_float(row.get("yield_pctile"))
    w_pct_b = _safe_float(row.get("w_pct_b"))
    low = _safe_float(row.get("low"))
    bb_mid = _safe_float(row.get("bb_mid"))
    pct_b = _safe_float(row.get("pct_b"))
    hard_ok = bool(row.get("hard_ok")) if not pd.isna(row.get("hard_ok")) else False

    cond = {
        "hard_ok": hard_ok,
        "month_pos_ok": (
            week_close is not None and m_bb_mid is not None and week_close <= m_bb_mid
        ),
        "yield_pctile_ok": (
            yield_pctile is not None and yield_pctile >= YIELD_PERCENTILE_BUY
        ),
        "week_pctb_ok": w_pct_b is not None and w_pct_b <= WEEK_PCTB_BUY_MAX,
        "day_low_gt_mid": low is not None and bb_mid is not None and low > bb_mid,
        "day_pctb_ok": (
            pct_b is not None and pct_b > DAY_PCTB_BUY_LOW and pct_b <= DAY_PCTB_BUY_HIGH
        ),
    }
    cond["buy_signal"] = all(cond.values())
    return {
        "conditions": cond,
        "values": {
            "hard_ok": hard_ok,
            "w_close": week_close,
            "m_bb_mid": m_bb_mid,
            "yield_pctile": yield_pctile,
            "bond_30y_pct": _safe_float(row.get("bond_30y_pct")),
            "w_pct_b": w_pct_b,
            "low": low,
            "bb_mid": bb_mid,
            "pct_b": pct_b,
            "close": _safe_float(row.get("close")),
            "close_raw": _safe_float(row.get("close_raw")),
            "bb_upper": _safe_float(row.get("bb_upper")),
            "bb_lower": _safe_float(row.get("bb_lower")),
            "w_bb_mid": _safe_float(row.get("w_bb_mid")),
            "w_bb_upper": _safe_float(row.get("w_bb_upper")),
            "m_bb_upper": _safe_float(row.get("m_bb_upper")),
        },
        "thresholds": {
            "yield_pctile_min": YIELD_PERCENTILE_BUY,
            "week_pctb_max": WEEK_PCTB_BUY_MAX,
            "day_pctb_low": DAY_PCTB_BUY_LOW,
            "day_pctb_high": DAY_PCTB_BUY_HIGH,
        },
    }


def _fmt(v, digits: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _pct(v, digits: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _cond_mark(ok) -> str:
    return "✓" if ok else "✗"


def _compact_round(r: dict) -> dict:
    return {
        "account": r.get("account"),
        "entry_date": r.get("entry_date"),
        "exit_date": r.get("exit_date"),
        "cost": _safe_float(r.get("cost")),
        "pnl": _safe_float(r.get("pnl")),
        "pnl_pct": _safe_float(r.get("pnl_pct")),
        "max_capital_dd_pct": _safe_float(r.get("max_capital_dd_pct")),
        "recovered": bool(r.get("recovered")),
        "days_entry_to_recover": r.get("days_entry_to_recover"),
        "n_trades": len(r.get("trades") or []),
    }


def run_backtest(feat: pd.DataFrame, code: str, start: str, end: str, n_accounts: int) -> dict:
    """多账号策略回测；n_accounts<=0 时自动找最小 N。"""
    pair = fetch_pair(code, start, end, cache=True)
    raw = feat["close_raw"]
    div = pair["dividend"]
    if n_accounts <= 0:
        auto_n, result = find_min_accounts(feat, raw, div)
    else:
        result = MultiAccountEngine(feat, raw, div, n_accounts=n_accounts).run()
        auto_n = n_accounts
    summary = summarize_rounds(result.get("rounds", []))
    rounds = [_compact_round(r) for r in (result.get("rounds") or [])]
    return {
        "framework": "multi_account",
        "auto_n": int(auto_n),
        "n_accounts": int(result.get("n_accounts") or auto_n),
        "pool_return": _safe_float(result.get("pool_return")),
        "end_equity": _safe_float(result.get("end_equity")),
        "miss_busy": int(result.get("miss_busy") or 0),
        "miss_month": int(result.get("miss_month") or 0),
        "n_rounds": int(result.get("n_rounds") or 0),
        "summary": summary,
        "rounds": rounds,
    }


def buy_history(feat: pd.DataFrame, limit: int = 20) -> list[dict]:
    """历史买点（buy_exec）摘要。"""
    if "buy_exec" not in feat.columns:
        return []
    hits = feat[feat["buy_exec"].fillna(False)]
    out = []
    for dt, row in hits.tail(limit).iterrows():
        out.append(
            {
                "date": str(pd.Timestamp(dt).date()),
                "close": _safe_float(row.get("close")),
                "pct_b": _safe_float(row.get("pct_b")),
                "w_pct_b": _safe_float(row.get("w_pct_b")),
                "yield_pctile": _safe_float(row.get("yield_pctile")),
            }
        )
    return out


def _write_condition_block(lines: list[str], r: dict) -> None:
    bc = r.get("buy_conditions") or {}
    cond = bc.get("conditions") or {}
    vals = bc.get("values") or {}
    lines.append(f"- 收盘 {_fmt(vals.get('close'))}（不复权 {_fmt(vals.get('close_raw'))}）")
    lines.append(
        f"- 市值 {_fmt(r.get('market_cap_yi'))} 亿"
        f" · 东财最新股息 {_fmt(r.get('em_div_yield_pct'))}%"
        f" · 30Y {_fmt(r.get('bond_30y_pct'))}% / 门槛 {_fmt(r.get('min_div_yield_pct'))}%"
    )
    lines.append("")
    lines.append("| # | 条件 | 结果 | 当日值 | 阈值 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| 0 | hard_ok | {_cond_mark(cond.get('hard_ok'))} | - | 连续分红/市值等硬门槛 |"
    )
    lines.append(
        f"| 1a | 周收 ≤ 上月中轨 | {_cond_mark(cond.get('month_pos_ok'))} |"
        f" 周收 {_fmt(vals.get('w_close'))} / 月中 {_fmt(vals.get('m_bb_mid'))} | ≤ |"
    )
    lines.append(
        f"| 1b | 股息分位 ≥ 80% | {_cond_mark(cond.get('yield_pctile_ok'))} |"
        f" {_fmt(vals.get('yield_pctile'), 3)} | ≥ 0.80 |"
    )
    lines.append(
        f"| 2 | 周 %b ≤ 0.25 | {_cond_mark(cond.get('week_pctb_ok'))} |"
        f" {_fmt(vals.get('w_pct_b'), 3)} | ≤ 0.25 |"
    )
    lines.append(
        f"| 3a | 日最低 > 日中轨 | {_cond_mark(cond.get('day_low_gt_mid'))} |"
        f" low {_fmt(vals.get('low'))} / mid {_fmt(vals.get('bb_mid'))} | > |"
    )
    lines.append(
        f"| 3b | 0.5 < 日%b ≤ 0.8 | {_cond_mark(cond.get('day_pctb_ok'))} |"
        f" {_fmt(vals.get('pct_b'), 3)} | (0.5, 0.8] |"
    )
    lines.append("")
    lines.append(
        f"布林日线：下轨 {_fmt(vals.get('bb_lower'))} / 中轨 {_fmt(vals.get('bb_mid'))}"
        f" / 上轨 {_fmt(vals.get('bb_upper'))}；"
        f"周中轨 {_fmt(vals.get('w_bb_mid'))} / 周上轨 {_fmt(vals.get('w_bb_upper'))}；"
        f"月上轨 {_fmt(vals.get('m_bb_upper'))}"
    )


def _write_backtest_block(lines: list[str], bt: dict | None) -> None:
    if not bt:
        lines.append("_无回测结果。_")
        return
    if bt.get("error"):
        lines.append(f"_回测失败：{bt['error']}_")
        return
    sm = bt.get("summary") or {}
    lines.append(
        f"- 框架：多账号滚动 · 账号数 **{bt.get('auto_n')}**"
        f"（miss_busy={bt.get('miss_busy')} / miss_month={bt.get('miss_month')}）"
    )
    lines.append(
        f"- 资金池收益：**{_pct(bt.get('pool_return'))}**"
        f" · 期末权益 {_fmt(bt.get('end_equity'), 0)}"
        f" · 完成轮次 **{bt.get('n_rounds')}**"
    )
    lines.append(
        f"- 轮次胜率 {_pct(sm.get('win_rate'))}"
        f" · 平均单轮收益 {_pct(sm.get('avg_pnl_pct'))}"
        f" · 平均本金浮亏 {_pct(sm.get('avg_capital_dd_pct'))}"
        f" · 最差本金浮亏 **{_pct(sm.get('worst_capital_dd_pct'))}**"
        f" · 回本率 {_pct(sm.get('recover_rate'))}"
    )
    rounds = bt.get("rounds") or []
    if rounds:
        lines.append("")
        lines.append("| 账号 | 入场 | 出场 | 成本 | 盈亏 | 收益率 | 最大本金浮亏 | 回本 |")
        lines.append("|---:|---|---|---:|---:|---:|---:|---|")
        for rr in rounds:
            lines.append(
                f"| {rr.get('account')} | {rr.get('entry_date') or '-'} |"
                f" {rr.get('exit_date') or '-'} |"
                f" {_fmt(rr.get('cost'), 0)} |"
                f" {_fmt(rr.get('pnl'), 0)} |"
                f" {_pct(rr.get('pnl_pct'))} |"
                f" {_pct(rr.get('max_capital_dd_pct'))} |"
                f" {_cond_mark(rr.get('recovered'))} |"
            )


def write_report_md(summary: dict, path: Path) -> None:
    """详细 Markdown：买点置顶 + 汇总表 + 分票信号/回测。"""
    buy = summary.get("buy_today_first") or []
    all_rows = summary.get("all_results") or []
    rules = summary.get("buy_rule") or {}
    lines: list[str] = []

    lines.append(f"# 红利今日扫描 {summary.get('asof', '')}")
    lines.append("")
    lines.append(
        f"- 30Y 国债：**{_fmt(summary.get('bond_30y_pct'))}%**"
        f" → 东财股息门槛 **{_fmt(summary.get('min_div_yield_pct'))}%**（×2）"
    )
    lines.append(f"- 市值门槛：>{_fmt(summary.get('min_cap_yi'), 0)} 亿")
    lines.append(
        f"- 粗筛（东财最新股息率）：**{summary.get('n_xuangu', 0)}** 只"
        f" → 策略扫描+回测 **{summary.get('n_scanned', 0)}** 只"
        f" → 今日买点 **{summary.get('n_buy_today', 0)}** 只"
    )
    lines.append(f"- 说明：{summary.get('screen_note', '')}")
    lines.append("")

    lines.append("## 今日买点（置顶）")
    lines.append("")
    if not buy:
        lines.append("_今日无满足全部买点条件的股票。_")
        lines.append("")
    else:
        for r in buy:
            lines.append(f"### {r.get('code')} {r.get('name', '')} · asof {r.get('asof', '-')}")
            lines.append("")
            _write_condition_block(lines, r)
            lines.append("")
            lines.append("#### 回测")
            lines.append("")
            _write_backtest_block(lines, r.get("backtest"))
            lines.append("")

    lines.append("## 买点规则")
    lines.append("")
    for k in sorted(rules.keys()):
        lines.append(f"- {rules[k]}")
    lines.append("")

    lines.append("## 汇总表")
    lines.append("")
    lines.append(
        "| 买点 | 代码 | 名称 | 市值亿 | 东财股息% | hard | 月位 | 分位 | 周%b |"
        " 日低>中 | 日%b | 账号N | 池收益 | 轮次 | 胜率 | 最差浮亏 |"
    )
    lines.append(
        "|---|---|---|---:|---:|---|---|---|---|---|---|---:|---:|---:|---:|---:|"
    )
    for r in all_rows:
        if r.get("error"):
            lines.append(
                f"| - | {r.get('code')} | {r.get('name', '')} |"
                f" {_fmt(r.get('market_cap_yi'))} |"
                f" {_fmt(r.get('em_div_yield_pct'))} |"
                f" ERR | | | | | | | | | | |"
            )
            continue
        bc = (r.get("buy_conditions") or {}).get("conditions") or {}
        bt = r.get("backtest") or {}
        sm = bt.get("summary") or {}
        lines.append(
            f"| {_cond_mark(r.get('buy_today'))} | {r.get('code')} | {r.get('name', '')} |"
            f" {_fmt(r.get('market_cap_yi'))} |"
            f" {_fmt(r.get('em_div_yield_pct'))} |"
            f" {_cond_mark(bc.get('hard_ok'))} |"
            f" {_cond_mark(bc.get('month_pos_ok'))} |"
            f" {_cond_mark(bc.get('yield_pctile_ok'))} |"
            f" {_cond_mark(bc.get('week_pctb_ok'))} |"
            f" {_cond_mark(bc.get('day_low_gt_mid'))} |"
            f" {_cond_mark(bc.get('day_pctb_ok'))} |"
            f" {bt.get('auto_n', '-')} |"
            f" {_pct(bt.get('pool_return'))} |"
            f" {bt.get('n_rounds', '-')} |"
            f" {_pct(sm.get('win_rate'))} |"
            f" {_pct(sm.get('worst_capital_dd_pct'))} |"
        )
    lines.append("")

    lines.append("## 分票明细")
    lines.append("")
    for r in all_rows:
        lines.append(f"### {r.get('code')} {r.get('name', '')}")
        lines.append("")
        if r.get("error"):
            lines.append(f"_失败：{r['error']}_")
            lines.append("")
            continue
        lines.append(f"asof **{r.get('asof', '-')}** · bars {r.get('n_bars', '-')}"
                     f" · 历史买点次数 {r.get('n_buy_exec_hist', 0)}")
        lines.append("")
        lines.append("#### 今日信号拆解")
        lines.append("")
        _write_condition_block(lines, r)
        lines.append("")
        lines.append("#### 多账号回测")
        lines.append("")
        _write_backtest_block(lines, r.get("backtest"))
        lines.append("")
        hist = r.get("buy_history") or []
        if hist:
            lines.append("#### 历史买点（最近）")
            lines.append("")
            lines.append("| 日期 | 收盘 | 日%b | 周%b | 股息分位 |")
            lines.append("|---|---:|---:|---:|---:|")
            for h in hist:
                lines.append(
                    f"| {h.get('date')} | {_fmt(h.get('close'))} |"
                    f" {_fmt(h.get('pct_b'), 3)} |"
                    f" {_fmt(h.get('w_pct_b'), 3)} |"
                    f" {_fmt(h.get('yield_pctile'), 3)} |"
                )
            lines.append("")
        lines.append(f"明细 JSON：`{r.get('result_path', '')}`")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def scan_symbol(
    code: str,
    name: str,
    market_cap_yi: float | None,
    em_div_yield_pct: float | None,
    bond_df: pd.DataFrame,
    bond_pct: float,
    start: str,
    end: str,
    n_accounts: int = 0,
    skip_backtest: bool = False,
) -> dict:
    min_yield = TREASURY_YIELD_MULTIPLIER * bond_pct
    feat = build_for_symbol(
        code,
        start,
        end,
        bond=bond_df,
        market_cap_yi=market_cap_yi,
    )
    feat_path = OUTPUT_DIR / f"features_{code}.csv"
    feat.reset_index().rename(columns={"index": "date"}).to_csv(feat_path, index=False)

    last = feat.iloc[-1]
    asof = str(pd.Timestamp(feat.index[-1]).date())
    buy = evaluate_buy_conditions(last)
    hist = buy_history(feat)

    backtest = None
    if not skip_backtest:
        try:
            backtest = run_backtest(feat, code, start, end, n_accounts=n_accounts)
        except Exception as e:
            backtest = {"error": str(e)}

    result = {
        "code": code,
        "name": name,
        "asof": asof,
        "market_cap_yi": market_cap_yi,
        "em_div_yield_pct": em_div_yield_pct,
        "bond_30y_pct": bond_pct,
        "min_div_yield_pct": min_yield,
        "hard_ok": bool(last.get("hard_ok")) if not pd.isna(last.get("hard_ok")) else False,
        "buy_today": bool(buy["conditions"]["buy_signal"]),
        "buy_conditions": buy,
        "buy_history": hist,
        "backtest": backtest,
        "features_path": str(feat_path),
        "n_bars": int(len(feat)),
        "n_buy_exec_hist": int(feat["buy_exec"].fillna(False).sum()),
    }
    stock_path = SCAN_DIR / f"{code}.json"
    stock_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    result["result_path"] = str(stock_path)
    return result


def _sort_key(x: dict):
    return (-(x.get("em_div_yield_pct") or 0), -(x.get("market_cap_yi") or 0))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Today scan: bond → xuangu → strategy+backtest per stock → report"
    )
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--min-cap-yi", type=float, default=MARKET_CAP_MIN_YI)
    p.add_argument("--bond-yield", type=float, default=None)
    p.add_argument("--force-bond", action="store_true")
    p.add_argument("--bond-demo", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="debug limit on xuangu universe")
    p.add_argument("--n", type=int, default=0, help="multi-account N; 0=auto min N")
    p.add_argument("--skip-backtest", action="store_true")
    args = p.parse_args()

    SCAN_DIR.mkdir(parents=True, exist_ok=True)

    # 1) bond
    if args.bond_yield is not None:
        bond_meta = {"mode": "override", "bond_30y_pct": float(args.bond_yield), "path": None}
        if not BOND_OUT.exists() or args.force_bond:
            dates = pd.date_range("2015-01-01", args.end, freq="B")
            pd.DataFrame({"date": dates, "yield_30y": float(args.bond_yield)}).to_csv(
                BOND_OUT, index=False
            )
            bond_meta["path"] = str(BOND_OUT)
        elif BOND_OUT.exists():
            bond_meta["path"] = str(BOND_OUT)
    else:
        bond_meta = ensure_bond(force=args.force_bond, demo=args.bond_demo)
    bond_pct = float(bond_meta["bond_30y_pct"])
    min_yield = TREASURY_YIELD_MULTIPLIER * bond_pct
    if not BOND_OUT.exists():
        dates = pd.date_range("2015-01-01", args.end, freq="B")
        pd.DataFrame({"date": dates, "yield_30y": bond_pct}).to_csv(BOND_OUT, index=False)
    bond_df = pd.read_csv(BOND_OUT, parse_dates=["date"])
    print(
        json.dumps(
            {"step": 1, "bond": bond_meta, "min_div_yield_pct": min_yield},
            ensure_ascii=False,
        ),
        flush=True,
    )

    # 2) xuangu coarse
    uni = screen_xuangu(
        min_cap_yi=args.min_cap_yi,
        bond_30y_pct=bond_pct,
        open_browser=not args.no_browser,
        close_browser=True,
    )
    universe = uni["rows"]
    if args.limit > 0:
        universe = universe[: args.limit]
    print(
        json.dumps(
            {
                "step": 2,
                "n_xuangu": len(universe),
                "min_em_div_yield_pct": uni.get("min_em_div_yield_pct"),
                "note": uni.get("note"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    # 3) strategy + backtest per symbol
    results = []
    for i, row in enumerate(universe, 1):
        code = row["code"]
        print(
            json.dumps(
                {
                    "step": 3,
                    "i": i,
                    "n": len(universe),
                    "code": code,
                    "name": row.get("name"),
                    "em_dy": row.get("em_div_yield_pct"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            res = scan_symbol(
                code=code,
                name=row.get("name") or code,
                market_cap_yi=row.get("market_cap_yi"),
                em_div_yield_pct=row.get("em_div_yield_pct"),
                bond_df=bond_df,
                bond_pct=bond_pct,
                start=args.start,
                end=args.end,
                n_accounts=args.n,
                skip_backtest=args.skip_backtest,
            )
            bt = res.get("backtest") or {}
            print(
                json.dumps(
                    {
                        "step": 3,
                        "done": code,
                        "buy_today": res.get("buy_today"),
                        "auto_n": bt.get("auto_n"),
                        "pool_return": bt.get("pool_return"),
                        "n_rounds": bt.get("n_rounds"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as e:
            res = {
                "code": code,
                "name": row.get("name"),
                "market_cap_yi": row.get("market_cap_yi"),
                "em_div_yield_pct": row.get("em_div_yield_pct"),
                "error": str(e),
                "buy_today": False,
            }
            (SCAN_DIR / f"{code}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        results.append(res)

    buy_today = sorted([r for r in results if r.get("buy_today")], key=_sort_key)
    others = sorted([r for r in results if not r.get("buy_today")], key=_sort_key)

    summary = {
        "asof": args.end,
        "bond_30y_pct": bond_pct,
        "min_div_yield_pct": min_yield,
        "min_cap_yi": args.min_cap_yi,
        "n_xuangu": len(universe),
        "n_scanned": len(results),
        "n_buy_today": len(buy_today),
        "buy_today_first": buy_today,
        "all_results": buy_today + others,
        "buy_rule": {
            "0_hard": "硬门槛 hard_ok（连续分红、市值等）",
            "1_month": "周收盘(当日) ≤ 上月中轨 且 股息分位≥80%",
            "2_week": "当周 forming %b ≤ 0.25",
            "3_day": "日最低价 > 日中轨 且 0.5 < 日%b ≤ 0.8",
        },
        "screen_note": "粗筛用东财最新股息率(TTM)；每票跑多账号策略回测并输出信号拆解与轮次明细",
        "paths": {
            "summary": str(SCAN_DIR / "summary.json"),
            "report": str(SCAN_DIR / "REPORT.md"),
            "xuangu_universe": str(CACHE_DIR / "xuangu_universe.json"),
            "bond": str(BOND_OUT),
            "scan_dir": str(SCAN_DIR),
        },
    }
    report_path = SCAN_DIR / "REPORT.md"
    write_report_md(summary, report_path)
    (SCAN_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "n_buy_today": len(buy_today),
                "buy_today_first": [
                    {
                        "code": x["code"],
                        "name": x.get("name"),
                        "em_div_yield_pct": x.get("em_div_yield_pct"),
                        "buy_conditions": x.get("buy_conditions"),
                        "backtest": {
                            "auto_n": (x.get("backtest") or {}).get("auto_n"),
                            "pool_return": (x.get("backtest") or {}).get("pool_return"),
                            "n_rounds": (x.get("backtest") or {}).get("n_rounds"),
                        },
                        "result_path": x.get("result_path"),
                    }
                    for x in buy_today
                ],
                "n_scanned": len(results),
                "summary": str(SCAN_DIR / "summary.json"),
                "report": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
