"""Shared paths and strategy constants for 红利布林回测."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = DATA_DIR / "cache"

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- Indicator params ---
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
MFI_PERIOD = 10
VOL_MA_PERIOD = 20
VOL_EXPAND = 1.5
VOL_PANIC = 2.0
VOL_SHRINK = 0.8

# --- Fundamental hard gates ---
MARKET_CAP_MIN_YI = 1000.0  # 亿元
DIVIDEND_YEARS = 3
YIELD_PERCENTILE_BUY = 0.80
YIELD_HISTORY_MIN_YEARS = 5
# 股息率历史分位样本起点（策略：上市以来；取足够早覆盖 A 股）
YIELD_SERIES_START = "2000-01-01"
TREASURY_YIELD_MULTIPLIER = 2.0

# --- Entry / exit ---
WEEK_PCTB_BUY_MAX = 0.25
DAY_PCTB_BUY_LOW = 0.50
DAY_PCTB_BUY_HIGH = 0.80
WEEK_PCTB_TRIM = 1.0
MONTH_PCTB_TRIM = 1.0
WEEK_TRIM_RATIO = 0.20
MONTH_TRIM_RATIO = 0.30
YIELD_LADDER_START = 78
YIELD_LADDER_END = 70
YIELD_LADDER_STEP = 2
YIELD_LADDER_TRIM = 0.10

# --- Multi-account ---
ACCOUNT_CASH = 1_000_000.0
DEFAULT_N_ACCOUNTS = 3

# --- East Money pages used by opencli browser ---
EASTMONEY_DIVIDEND_BOARD = (
    "https://data.eastmoney.com/yjfp/"
)
EASTMONEY_XUANGU = (
    "https://xuangu.eastmoney.com/"
)
EASTMONEY_BOND_YIELD = (
    "https://data.eastmoney.com/cjsj/zmgzsyl.html"  # 中美国债；中国30年在第4列
)
EASTMONEY_GUXILV_API_HINT = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
)

SESSION_NAME = "hongli-em"