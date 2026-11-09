"""
多账号滚动回测引擎（策略文档「多账号滚动」完整实现）。
每个账号独立 100 万、独立减仓档；同月限购全局；接单指针环形找空仓账号。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    ACCOUNT_CASH,
    DEFAULT_N_ACCOUNTS,
    MONTH_TRIM_RATIO,
    OUTPUT_DIR,
    WEEK_TRIM_RATIO,
    YIELD_LADDER_END,
    YIELD_LADDER_START,
    YIELD_LADDER_STEP,
    YIELD_LADDER_TRIM,
)
from dividend_yield import cash_dividends_on_date, normalize_dividends
from metrics import round_metrics


@dataclass
class Account:
    aid: int
    cash: float = ACCOUNT_CASH
    shares: float = 0.0
    cost: float = 0.0  # 本轮买入金额
    entry_date: pd.Timestamp | None = None
    week_trimmed: bool = False
    month_trimmed: bool = False
    ladder_base_shares: float | None = None
    ladder_done: set[int] = field(default_factory=set)
    realized: float = 0.0  # 已实现卖出回笼 + 股息
    equity_path: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return self.shares > 0


class MultiAccountEngine:
    def __init__(
        self,
        signal_df: pd.DataFrame,
        raw_close: pd.Series,
        dividends: pd.DataFrame,
        n_accounts: int = DEFAULT_N_ACCOUNTS,
        lot: int = 100,
    ):
        self.df = signal_df.sort_index()
        self.raw_close = raw_close.reindex(self.df.index).ffill()
        self.divs = normalize_dividends(dividends) if not dividends.empty else pd.DataFrame(
            columns=["ex_date", "cash_ps", "year", "month"]
        )
        self.n = n_accounts
        self.lot = lot
        self.accounts = [Account(aid=i) for i in range(n_accounts)]
        self.next_ptr = 0
        self.miss_busy = 0
        self.miss_month = 0
        self.buy_months: set[str] = set()
        self.rounds: list[dict] = []

    def _month_key(self, dt: pd.Timestamp) -> str:
        return f"{dt.year:04d}-{dt.month:02d}"

    def _mark_equity(self, dt: pd.Timestamp) -> None:
        px = float(self.raw_close.loc[dt])
        for a in self.accounts:
            eq = a.cash + a.shares * px
            # For open round path: realized already in cash; track vs cost
            if a.busy or a.cost > 0:
                path_eq = a.realized + a.shares * px
                # realized tracks sell proceeds + dividends; cash includes idle.
                # 本金浮亏路径 = 已实现(卖出回笼+股息) + 余仓×不复权收盘
                # 开仓后 realized 从 0 起，买入时 cost 记账，现金减少不进 realized。
                a.equity_path.append((dt, a.realized + a.shares * px))

    def _credit_dividend(self, dt: pd.Timestamp) -> None:
        cps = cash_dividends_on_date(self.divs, dt)
        if cps <= 0:
            return
        for a in self.accounts:
            if a.shares <= 0:
                continue
            cash = a.shares * cps
            a.cash += cash
            a.realized += cash
            a.trades.append(
                {
                    "date": str(dt.date()),
                    "account": a.aid,
                    "side": "dividend",
                    "shares": a.shares,
                    "price": cps,
                    "amount": cash,
                }
            )

    def _buy(self, a: Account, dt: pd.Timestamp, px: float) -> None:
        # 全仓买入，按手取整
        shares = int(a.cash // (px * self.lot)) * self.lot
        if shares <= 0:
            return
        amount = shares * px
        a.cash -= amount
        a.shares = float(shares)
        a.cost = amount
        a.entry_date = dt
        a.week_trimmed = False
        a.month_trimmed = False
        a.ladder_base_shares = None
        a.ladder_done = set()
        a.realized = 0.0
        a.equity_path = []
        a.trades.append(
            {
                "date": str(dt.date()),
                "account": a.aid,
                "side": "buy",
                "shares": shares,
                "price": px,
                "amount": amount,
            }
        )

    def _sell(self, a: Account, dt: pd.Timestamp, px: float, shares: float, reason: str) -> None:
        shares = min(shares, a.shares)
        if shares <= 0:
            return
        # 按手向下
        shares = int(shares // self.lot) * self.lot
        if shares <= 0 and reason == "clear":
            shares = a.shares  # allow odd lot on clear
        if shares <= 0:
            return
        amount = shares * px
        a.cash += amount
        a.shares -= shares
        a.realized += amount
        a.trades.append(
            {
                "date": str(dt.date()),
                "account": a.aid,
                "side": "sell",
                "reason": reason,
                "shares": shares,
                "price": px,
                "amount": amount,
            }
        )
        if a.shares <= 0:
            a.shares = 0.0
            self._close_round(a, dt)

    def _close_round(self, a: Account, dt: pd.Timestamp) -> None:
        path = a.equity_path + [(dt, a.realized)]
        m = round_metrics(cost=a.cost, path=path, entry_date=a.entry_date, exit_date=dt)
        m.update({"account": a.aid, "trades": list(a.trades)})
        self.rounds.append(m)
        # reset round state but keep cash
        a.cost = 0.0
        a.entry_date = None
        a.week_trimmed = False
        a.month_trimmed = False
        a.ladder_base_shares = None
        a.ladder_done = set()
        a.realized = 0.0
        a.equity_path = []
        a.trades = []

    def _trim_logic(self, a: Account, row: pd.Series, dt: pd.Timestamp, px: float) -> None:
        if not a.busy:
            return
        if (not a.week_trimmed) and bool(row.get("sell_week_trim")):
            self._sell(a, dt, px, a.shares * WEEK_TRIM_RATIO, "week_upper")
            a.week_trimmed = True
        if not a.busy:
            return
        if (not a.month_trimmed) and bool(row.get("sell_month_trim")):
            self._sell(a, dt, px, a.shares * MONTH_TRIM_RATIO, "month_upper")
            a.month_trimmed = True
        if not a.busy:
            return

        # yield ladder
        y100 = float(row.get("yield_pctile", np.nan)) * 100 if pd.notna(row.get("yield_pctile")) else np.nan
        if np.isnan(y100):
            return
        for level in range(YIELD_LADDER_START, YIELD_LADDER_END - 1, -YIELD_LADDER_STEP):
            if level in a.ladder_done:
                continue
            if y100 <= level:
                if a.ladder_base_shares is None:
                    a.ladder_base_shares = a.shares
                qty = a.ladder_base_shares * YIELD_LADDER_TRIM
                self._sell(a, dt, px, qty, f"yield_le_{level}")
                a.ladder_done.add(level)
                if not a.busy:
                    return
        if y100 <= YIELD_LADDER_END and a.busy:
            # 最后一档后剩余清仓
            self._sell(a, dt, px, a.shares, "clear")

    def run(self) -> dict:
        for dt, row in self.df.iterrows():
            self._credit_dividend(dt)
            px = float(self.raw_close.loc[dt])
            if np.isnan(px) or px <= 0:
                continue

            # sells first on each account
            for a in self.accounts:
                self._trim_logic(a, row, dt, px)

            # buy
            if bool(row.get("buy_signal")):
                mk = self._month_key(dt)
                if mk in self.buy_months:
                    self.miss_month += 1
                else:
                    bought = False
                    for offset in range(self.n):
                        idx = (self.next_ptr + offset) % self.n
                        a = self.accounts[idx]
                        if not a.busy:
                            self._buy(a, dt, px)
                            if a.busy:
                                self.buy_months.add(mk)
                                self.next_ptr = (idx + 1) % self.n
                                bought = True
                                break
                    if not bought:
                        self.miss_busy += 1

            self._mark_equity(dt)

        # liquidate leftover for metrics
        last = self.df.index[-1]
        px = float(self.raw_close.loc[last])
        for a in self.accounts:
            if a.busy:
                self._sell(a, last, px, a.shares, "force_eod")

        end_equity = sum(a.cash for a in self.accounts)
        pool_ret = end_equity / (self.n * ACCOUNT_CASH) - 1
        return {
            "n_accounts": self.n,
            "end_equity": end_equity,
            "pool_return": pool_ret,
            "miss_busy": self.miss_busy,
            "miss_month": self.miss_month,
            "n_rounds": len(self.rounds),
            "rounds": self.rounds,
            "accounts_cash": [a.cash for a in self.accounts],
        }


def find_min_accounts(
    signal_df: pd.DataFrame,
    raw_close: pd.Series,
    dividends: pd.DataFrame,
    max_n: int = 20,
) -> tuple[int, dict]:
    """找使 miss_busy==0 的最小 N。"""
    last = None
    for n in range(1, max_n + 1):
        eng = MultiAccountEngine(signal_df, raw_close, dividends, n_accounts=n)
        last = eng.run()
        if last["miss_busy"] == 0:
            return n, last
    return max_n, last  # type: ignore


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--signals", required=True)
    p.add_argument("--raw", required=True, help="raw daily csv with date,close")
    p.add_argument("--dividend", required=True)
    p.add_argument("--n", type=int, default=0, help="0 = auto min N")
    p.add_argument("--output", default=str(OUTPUT_DIR / "multi_account_result.json"))
    args = p.parse_args()

    sig = pd.read_csv(args.signals, parse_dates=["date"]).set_index("date")
    raw = pd.read_csv(args.raw, parse_dates=["date"]).set_index("date")["close"]
    div = pd.read_csv(args.dividend)

    if args.n <= 0:
        n, result = find_min_accounts(sig, raw, div)
        result["auto_n"] = n
    else:
        result = MultiAccountEngine(sig, raw, div, n_accounts=args.n).run()
        result["auto_n"] = args.n

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "auto_n": result.get("auto_n"),
                "pool_return": result["pool_return"],
                "miss_busy": result["miss_busy"],
                "miss_month": result["miss_month"],
                "n_rounds": result["n_rounds"],
                "output": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
