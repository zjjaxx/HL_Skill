"""
今日扫描主流程（skill 约定）：
1) 查 30Y 国债
2) 东财条件选股粗筛：总市值>1000亿 且 最新股息率(TTM)≥2×30Y
3) 对每只候选：特征/硬门槛/布林信号 + 多账号策略回测
4) 只写 Markdown 报告 REPORT.md（今日推荐 + 全部粗筛股历史回测）
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
from fetch_baostock import baostock_session, fetch_pair
from fetch_bond_yield import OUT_PATH as BOND_OUT, load_or_scrape
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


def ensure_bond(force: bool = False, use_browser: bool = True) -> dict:
    """抓 30Y；失败重试，不用 demo/默认值。"""
    df = load_or_scrape(force=force, use_browser=use_browser)
    return {
        "mode": "ok",
        "bond_30y_pct": float(df["yield_30y"].iloc[-1]),
        "path": str(BOND_OUT),
    }


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


def _feat_row_on(feat: pd.DataFrame, dt) -> pd.Series | None:
    if dt is None:
        return None
    ts = pd.Timestamp(dt)
    if ts in feat.index:
        return feat.loc[ts]
    sub = feat.index[feat.index <= ts]
    if len(sub) == 0:
        return None
    return feat.loc[sub[-1]]


def _snapshot_from_row(row: pd.Series | None) -> dict:
    if row is None:
        return {}
    bc = evaluate_buy_conditions(row)
    cond = bc.get("conditions") or {}
    vals = bc.get("values") or {}
    return {
        "div_yield_pct": _safe_float(row.get("div_yield_pct")),
        "yield_pctile": _safe_float(row.get("yield_pctile")),
        "pct_b": _safe_float(row.get("pct_b")),
        "w_pct_b": _safe_float(row.get("w_pct_b")),
        "m_pct_b": _safe_float(row.get("m_pct_b")),
        "w_close": _safe_float(row.get("w_close", row.get("close"))),
        "m_bb_mid": _safe_float(row.get("m_bb_mid")),
        "close": _safe_float(row.get("close")),
        "low": _safe_float(row.get("low")),
        "bb_mid": _safe_float(row.get("bb_mid")),
        "hard_ok": bool(cond.get("hard_ok")),
        "yield_vs_bond_ok": bool(row.get("yield_vs_bond_ok"))
        if not pd.isna(row.get("yield_vs_bond_ok"))
        else False,
        "yield_pctile_ok": bool(cond.get("yield_pctile_ok")),
        "consec_div": bool(row.get("consec_div")) if not pd.isna(row.get("consec_div")) else False,
        "cashflow_ok": bool(row.get("cashflow_ok")) if not pd.isna(row.get("cashflow_ok")) else False,
        "cap_ok": bool(row.get("cap_ok")) if not pd.isna(row.get("cap_ok")) else False,
        "month_pos_ok": bool(cond.get("month_pos_ok")),
        "week_pctb_ok": bool(cond.get("week_pctb_ok")),
        "day_low_gt_mid": bool(cond.get("day_low_gt_mid")),
        "day_pctb_ok": bool(cond.get("day_pctb_ok")),
        "bond_30y_pct": _safe_float(row.get("bond_30y_pct")),
        "buy_conditions": bc,
        "values": vals,
        "conditions": cond,
    }


def _enrich_round(r: dict, feat: pd.DataFrame) -> dict:
    snap = _snapshot_from_row(_feat_row_on(feat, r.get("entry_date")))
    return {
        "account": r.get("account"),
        "entry_date": r.get("entry_date"),
        "exit_date": r.get("exit_date"),
        "cost": _safe_float(r.get("cost")),
        "pnl": _safe_float(r.get("pnl")),
        "pnl_pct": _safe_float(r.get("pnl_pct")),
        "max_capital_dd_amt": _safe_float(r.get("max_capital_dd_amt")),
        "max_capital_dd_pct": _safe_float(r.get("max_capital_dd_pct")),
        "max_drawdown_pct": _safe_float(r.get("max_drawdown_pct")),
        "recovered": bool(r.get("recovered")),
        "days_entry_to_recover": r.get("days_entry_to_recover"),
        "div_yield_pct": snap.get("div_yield_pct"),
        "pct_b": snap.get("pct_b"),
        "w_pct_b": snap.get("w_pct_b"),
        "w_close": snap.get("w_close"),
        "m_bb_mid": snap.get("m_bb_mid"),
        "hard_ok": snap.get("hard_ok"),
        "yield_vs_bond_ok": snap.get("yield_vs_bond_ok"),
        "yield_pctile_ok": snap.get("yield_pctile_ok"),
        "consec_div": snap.get("consec_div"),
        "cashflow_ok": snap.get("cashflow_ok"),
        "cap_ok": snap.get("cap_ok"),
        "snap": snap,
    }


def run_backtest(feat: pd.DataFrame, code: str, start: str, end: str, n_accounts: int) -> dict:
    """多账号策略回测；n_accounts<=0 时按需建户。"""
    pair = fetch_pair(code, start, end, cache=True)
    raw = feat["close_raw"]
    div = pair["dividend"]
    if n_accounts <= 0:
        auto_n, result = find_min_accounts(feat, raw, div)
    else:
        result = MultiAccountEngine(feat, raw, div, n_accounts=n_accounts).run()
        auto_n = n_accounts
    summary = summarize_rounds(result.get("rounds", []))
    rounds = [_enrich_round(r, feat) for r in (result.get("rounds") or [])]
    return {
        "framework": "multi_account",
        "auto_n": int(auto_n),
        "n_accounts": int(result.get("n_accounts") or auto_n),
        "on_demand": bool(result.get("on_demand")),
        "peak_busy": int(result.get("peak_busy") or 0),
        "pool_return": _safe_float(result.get("pool_return")),
        "end_equity": _safe_float(result.get("end_equity")),
        "miss_busy": int(result.get("miss_busy") or 0),
        "miss_month": int(result.get("miss_month") or 0),
        "n_rounds": int(result.get("n_rounds") or 0),
        "summary": summary,
        "rounds": rounds,
    }


def _write_detail_block(lines: list[str], r: dict) -> None:
    """当前时点概览 + 买点条件表（全部表格）。"""
    bc = r.get("buy_conditions") or {}
    cond = bc.get("conditions") or {}
    vals = bc.get("values") or {}

    lines.append("#### 当前概览")
    lines.append("")
    lines.append("| 项目 | 结果 | 明细 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| 买点 | {_cond_mark(r.get('buy_today'))} |"
        f" asof `{r.get('asof', '-')}` ·"
        f" 收盘 {_fmt(vals.get('close'))}（不复权 {_fmt(vals.get('close_raw'))}） |"
    )
    lines.append(
        f"| 策略股息率 | {_fmt(r.get('div_yield_pct'))}% |"
        f" 分位 {_fmt(vals.get('yield_pctile'), 3)} ·"
        f" 东财TTM {_fmt(r.get('em_div_yield_pct'))}% ·"
        f" 30Y {_fmt(r.get('bond_30y_pct'))}% / 门槛 {_fmt(r.get('min_div_yield_pct'))}% |"
    )
    lines.append(
        f"| 日线位置 | 日%b {_fmt(vals.get('pct_b'), 3)} |"
        f" 低 {_fmt(vals.get('low'))} / 中轨 {_fmt(vals.get('bb_mid'))} ·"
        f" 站上中轨 {_cond_mark(cond.get('day_low_gt_mid'))} ·"
        f" 买点带 {_cond_mark(cond.get('day_pctb_ok'))} |"
    )
    lines.append(
        f"| 周线位置 | 周%b {_fmt(vals.get('w_pct_b'), 3)} |"
        f" ≤0.25 {_cond_mark(cond.get('week_pctb_ok'))} |"
    )
    lines.append(
        f"| 月线位置 | 价 {_fmt(vals.get('w_close'))} vs 月中 {_fmt(vals.get('m_bb_mid'))} |"
        f" 价≤月中 {_cond_mark(cond.get('month_pos_ok'))} |"
    )
    lines.append(
        f"| 硬门槛合计 | {_cond_mark(cond.get('hard_ok'))} |"
        f" 市值 {_fmt(r.get('market_cap_yi'))} 亿 |"
    )
    lines.append("")
    lines.append("#### 硬门槛拆解")
    lines.append("")
    lines.append("| 子项 | 结果 |")
    lines.append("|---|---|")
    lines.append(f"| 股息 ≥ 2×30Y | {_cond_mark(r.get('yield_vs_bond_ok'))} |")
    lines.append(f"| 分位 ≥ 80% | {_cond_mark(cond.get('yield_pctile_ok'))} |")
    lines.append(f"| 连续 3 年分红 | {_cond_mark(r.get('consec_div'))} |")
    lines.append(f"| 现金流覆盖分红 | {_cond_mark(r.get('cashflow_ok'))} |")
    lines.append(f"| 市值 ≥ 1000 亿 | {_cond_mark(r.get('cap_ok'))} |")
    lines.append("")
    lines.append("#### 买入条件检查")
    lines.append("")
    lines.append("| # | 条件 | 结果 | 当日值 | 阈值 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| 0 | hard_ok | {_cond_mark(cond.get('hard_ok'))} | - |"
        f" 股息×国债/分位/连分/现金流/市值 |"
    )
    lines.append(
        f"| 1 | 价 ≤ 当月 forming 中轨 | {_cond_mark(cond.get('month_pos_ok'))} |"
        f" {_fmt(vals.get('w_close'))} / {_fmt(vals.get('m_bb_mid'))} | ≤ |"
    )
    lines.append(
        f"| 2 | 周 %b ≤ 0.25 | {_cond_mark(cond.get('week_pctb_ok'))} |"
        f" {_fmt(vals.get('w_pct_b'), 3)} | ≤ 0.25 |"
    )
    lines.append(
        f"| 3a | 日最低 > 日中轨 | {_cond_mark(cond.get('day_low_gt_mid'))} |"
        f" {_fmt(vals.get('low'))} / {_fmt(vals.get('bb_mid'))} | > |"
    )
    lines.append(
        f"| 3b | 0.5 < 日%b ≤ 0.8 | {_cond_mark(cond.get('day_pctb_ok'))} |"
        f" {_fmt(vals.get('pct_b'), 3)} | (0.5, 0.8] |"
    )


def _write_history_table(lines: list[str], bt: dict | None) -> None:
    lines.append("#### 历史回测轮次")
    lines.append("")
    if not bt:
        lines.append("_无回测结果。_")
        return
    if bt.get("error"):
        lines.append(f"_回测失败：{bt['error']}_")
        return
    rounds = bt.get("rounds") or []
    sm = bt.get("summary") or {}
    lines.append(
        f"按需建户 N=**{bt.get('auto_n')}** · 轮次 **{bt.get('n_rounds')}**"
        f" · 胜率 {_pct(sm.get('win_rate'))}"
        f" · 平均收益 {_pct(sm.get('avg_pnl_pct'))}"
        f" · miss_month={bt.get('miss_month')}"
    )
    lines.append("")
    if not rounds:
        lines.append("_无完成轮次。_")
        return
    lines.append(
        "| 买点 | 买入股息率 | 日%b | 周%b | 价/月中 | 硬 | ≥2×30Y | 分位 |"
        " 连分 | 现金流 | 市值 | 本金 | 收益率 | 最大亏损本金 | 回本天数 | 最大回撤 |"
    )
    lines.append(
        "|---|---:|---:|---:|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|"
    )
    for rr in rounds:
        recover = rr.get("days_entry_to_recover")
        recover_s = "-" if recover is None else str(recover)
        lines.append(
            f"| {rr.get('entry_date') or '-'} |"
            f" {_fmt(rr.get('div_yield_pct'))}% |"
            f" {_fmt(rr.get('pct_b'), 3)} |"
            f" {_fmt(rr.get('w_pct_b'), 3)} |"
            f" {_fmt(rr.get('w_close'))}/{_fmt(rr.get('m_bb_mid'))} |"
            f" {_cond_mark(rr.get('hard_ok'))} |"
            f" {_cond_mark(rr.get('yield_vs_bond_ok'))} |"
            f" {_cond_mark(rr.get('yield_pctile_ok'))} |"
            f" {_cond_mark(rr.get('consec_div'))} |"
            f" {_cond_mark(rr.get('cashflow_ok'))} |"
            f" {_cond_mark(rr.get('cap_ok'))} |"
            f" {_fmt(rr.get('cost'), 0)} |"
            f" {_pct(rr.get('pnl_pct'))} |"
            f" {_fmt(rr.get('max_capital_dd_amt'), 0)}"
            f" ({_pct(rr.get('max_capital_dd_pct'))}) |"
            f" {recover_s} |"
            f" {_pct(rr.get('max_drawdown_pct'))} |"
        )


def _write_stock_section(lines: list[str], r: dict, *, heading: str) -> None:
    lines.append(f"### {heading}")
    lines.append("")
    if r.get("error"):
        lines.append(f"_失败：{r['error']}_")
        lines.append("")
        return
    _write_detail_block(lines, r)
    lines.append("")
    _write_history_table(lines, r.get("backtest"))
    lines.append("")


def write_report_md(summary: dict, path: Path) -> None:
    """只输出 Markdown：①今日推荐（无则跳过）②全部东财粗筛股历史回测。"""
    buy = summary.get("buy_today_first") or []
    all_rows = summary.get("all_results") or []
    lines: list[str] = []

    lines.append(f"# 红利今日扫描 {summary.get('asof', '')}")
    lines.append("")
    lines.append(
        f"- 30Y 国债：**{_fmt(summary.get('bond_30y_pct'))}%**"
        f" → 东财股息门槛 **{_fmt(summary.get('min_div_yield_pct'))}%**（×2）"
    )
    lines.append(f"- 市值门槛：>{_fmt(summary.get('min_cap_yi'), 0)} 亿")
    lines.append(
        f"- 东财条件选股：**{summary.get('n_xuangu', 0)}** 只"
        f" · 今日推荐 **{summary.get('n_buy_today', 0)}** 只"
    )
    lines.append("")

    if buy:
        lines.append("## 1. 今日推荐")
        lines.append("")
        for r in buy:
            title = f"{r.get('code')} {r.get('name', '')}"
            _write_stock_section(lines, r, heading=title)

    lines.append("## 2. 东财条件选股全部标的")
    lines.append("")
    for r in all_rows:
        tag = " · 今日推荐" if r.get("buy_today") else ""
        title = f"{r.get('code')} {r.get('name', '')}{tag}"
        _write_stock_section(lines, r, heading=title)

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

    backtest = None
    if not skip_backtest:
        try:
            backtest = run_backtest(feat, code, start, end, n_accounts=n_accounts)
        except Exception as e:
            backtest = {"error": str(e)}

    def _flag(col: str) -> bool | None:
        v = last.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return bool(v)

    return {
        "code": code,
        "name": name,
        "asof": asof,
        "market_cap_yi": market_cap_yi,
        "em_div_yield_pct": em_div_yield_pct,
        "div_yield_pct": _safe_float(last.get("div_yield_pct")),
        "bond_30y_pct": bond_pct,
        "min_div_yield_pct": min_yield,
        "hard_ok": _flag("hard_ok"),
        "yield_vs_bond_ok": _flag("yield_vs_bond_ok"),
        "consec_div": _flag("consec_div"),
        "cashflow_ok": _flag("cashflow_ok"),
        "cap_ok": _flag("cap_ok"),
        "buy_today": bool(buy["conditions"]["buy_signal"]),
        "buy_conditions": buy,
        "backtest": backtest,
        "features_path": str(feat_path),
        "n_bars": int(len(feat)),
        "n_buy_exec_hist": int(feat["buy_exec"].fillna(False).sum())
        if "buy_exec" in feat.columns
        else 0,
    }


def _sort_key(x: dict):
    return (-(x.get("em_div_yield_pct") or 0), -(x.get("market_cap_yi") or 0))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Today scan: bond → xuangu → strategy+backtest per stock → report"
    )
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--min-cap-yi", type=float, default=MARKET_CAP_MIN_YI)
    p.add_argument(
        "--bond-yield",
        type=float,
        default=None,
        help="仅当你已从东财查到真实 30Y 时手工覆盖；禁止用假默认值",
    )
    p.add_argument("--force-bond", action="store_true")
    p.add_argument("--no-browser", action="store_true", help="国债仅用 API；选股也不开 browser")
    p.add_argument("--limit", type=int, default=0, help="debug limit on xuangu universe")
    p.add_argument("--n", type=int, default=0, help="multi-account N; 0=按需建户")
    p.add_argument("--skip-backtest", action="store_true")
    args = p.parse_args()

    SCAN_DIR.mkdir(parents=True, exist_ok=True)

    # 1) bond — 必须拿到真实序列；失败重试后仍失败则报错退出
    if args.bond_yield is not None:
        bond_meta = {
            "mode": "override",
            "bond_30y_pct": float(args.bond_yield),
            "path": str(BOND_OUT) if BOND_OUT.exists() else None,
        }
        # 覆盖只改门槛用最新值；历史序列仍须真实抓取（供特征/分位）
        bond_df = load_or_scrape(force=args.force_bond, use_browser=not args.no_browser)
        bond_meta["path"] = str(BOND_OUT)
    else:
        bond_meta = ensure_bond(force=args.force_bond, use_browser=not args.no_browser)
        bond_df = pd.read_csv(BOND_OUT, parse_dates=["date"])
    bond_pct = float(bond_meta["bond_30y_pct"])
    min_yield = TREASURY_YIELD_MULTIPLIER * bond_pct
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

    # 3) strategy + backtest per symbol（整批共用一次 baostock 会话）
    results = []
    with baostock_session():
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
    }
    report_path = SCAN_DIR / "REPORT.md"
    write_report_md(summary, report_path)
    print(
        json.dumps(
            {
                "n_buy_today": len(buy_today),
                "buy_today": [
                    {"code": x["code"], "name": x.get("name"), "em_dy": x.get("em_div_yield_pct")}
                    for x in buy_today
                ],
                "n_scanned": len(results),
                "report": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
