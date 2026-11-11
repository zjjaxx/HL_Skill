"""
多账号滚动回测引擎（策略文档「多账号滚动」完整实现）。

默认按需建户：有空闲账号则复用；全忙则新建一个 100 万账号接单。
也可 --n N 固定账号数（全忙记 miss_busy，不再新建）。
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
        n_accounts: int = 0,
        lot: int = 100,
    ):
        """
        n_accounts <= 0：按需建户（推荐）。
        n_accounts > 0：固定 N 个账号；全忙只记 miss_busy。
        """
        self.df = signal_df.sort_index()
        self.raw_close = raw_close.reindex(self.df.index).ffill()
        self.divs = normalize_dividends(dividends) if not dividends.empty else pd.DataFrame(
            columns=["ex_date", "cash_ps", "year", "month"]
        )
        self.on_demand = n_accounts <= 0
        self.lot = lot
        if self.on_demand:
            self.accounts: list[Account] = []
            self.n = 0
        else:
            self.accounts = [Account(aid=i) for i in range(n_accounts)]
            self.n = n_accounts
        self.next_ptr = 0
        self.miss_busy = 0
        self.miss_month = 0
        self.buy_months: set[str] = set()
        self.rounds: list[dict] = []
        self.peak_busy = 0
        self.n_created = 0

    def _month_key(self, dt: pd.Timestamp) -> str:
        return f"{dt.year:04d}-{dt.month:02d}"

    def _new_account(self) -> Account:
        a = Account(aid=len(self.accounts), cash=ACCOUNT_CASH)
        self.accounts.append(a)
        self.n = len(self.accounts)
        self.n_created += 1
        return a

    def _acquire_account(self) -> tuple[Account | None, int | None]:
        """优先复用空闲账号；按需模式下全忙则新建。"""
        if self.accounts:
            n = len(self.accounts)
            for offset in range(n):
                idx = (self.next_ptr + offset) % n
                if not self.accounts[idx].busy:
                    return self.accounts[idx], idx
        if self.on_demand:
            a = self._new_account()
            return a, len(self.accounts) - 1
        return None, None

    def _mark_equity(self, dt: pd.Timestamp) -> None:
        px = float(self.raw_close.loc[dt])
        busy = 0
        for a in self.accounts:
            if a.busy:
                busy += 1
            if a.busy or a.cost > 0:
                # 本金浮亏路径 = 已实现(卖出回笼+股息) + 余仓×不复权收盘
                a.equity_path.append((dt, a.realized + a.shares * px))
        self.peak_busy = max(self.peak_busy, busy)

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
        shares = int(shares // self.lot) * self.lot
        if shares <= 0 and reason == "clear":
            shares = a.shares
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
            self._sell(a, dt, px, a.shares, "clear")

    def run(self) -> dict:
        for dt, row in self.df.iterrows():
            self._credit_dividend(dt)
            px = float(self.raw_close.loc[dt])
            if np.isnan(px) or px <= 0:
                continue

            for a in self.accounts:
                self._trim_logic(a, row, dt, px)

            if bool(row.get("buy_signal")):
                mk = self._month_key(dt)
                if mk in self.buy_months:
                    self.miss_month += 1
                else:
                    a, idx = self._acquire_account()
                    if a is None or idx is None:
                        self.miss_busy += 1
                    else:
                        self._buy(a, dt, px)
                        if a.busy:
                            self.buy_months.add(mk)
                            self.next_ptr = (idx + 1) % max(len(self.accounts), 1)

            self._mark_equity(dt)

        if len(self.df.index) > 0:
            last = self.df.index[-1]
            px = float(self.raw_close.loc[last])
            for a in self.accounts:
                if a.busy:
                    self._sell(a, last, px, a.shares, "force_eod")

        end_equity = sum(a.cash for a in self.accounts)
        denom = self.n * ACCOUNT_CASH if self.n > 0 else ACCOUNT_CASH
        pool_ret = (end_equity / denom - 1) if self.n > 0 else 0.0
        return {
            "n_accounts": self.n,
            "on_demand": self.on_demand,
            "n_created": self.n_created if self.on_demand else self.n,
            "peak_busy": self.peak_busy,
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
) -> tuple[int, dict]:
    """按需建户跑一遍，返回实际用到的账号数（= 峰值并发所需的最小 N）。"""
    result = MultiAccountEngine(signal_df, raw_close, dividends, n_accounts=0).run()
    n = int(result.get("n_accounts") or 0)
    return n, result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--signals", required=True)
    p.add_argument("--raw", required=True, help="raw daily csv with date,close")
    p.add_argument("--dividend", required=True)
    p.add_argument(
        "--n",
        type=int,
        default=0,
        help="0=按需建户（默认）；>0 固定账号数（全忙记 miss_busy）",
    )
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
                "on_demand": result.get("on_demand"),
                "peak_busy": result.get("peak_busy"),
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
