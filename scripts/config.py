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
MARKET_CAP_MIN_YI = 500.0  # 亿元
DIVIDEND_YEARS = 3
YIELD_PERCENTILE_BUY = 0.80
YIELD_HISTORY_MIN_YEARS = 5
# 股息率历史分位样本起点（策略：上市以来；取足够早覆盖 A 股）
YIELD_SERIES_START = "2000-01-01"
TREASURY_YIELD_MULTIPLIER = 2.0
# 分红稳定：近 N 年每股分红同比跌幅上限（允许多派/利润大增，只挡砍息过狠）
DIVIDEND_STABLE_MAX_YOY_DROP = 0.30

# --- 粗筛行业白名单（东财「所属东财行业是X」；红利核心+次仓+油气卫星）---
SECTOR_FILTER_ENABLED = True
# 主仓：银行 / 保险 / 电力；次仓：证券、电信、铁路公路、燃气；卫星：油气开采
XUANGU_INDUSTRY_WHITELIST = (
    "银行",
    "保险",
    "证券",
    "电力",
    "火力发电",
    "水力发电",
    "核力发电",
    "电信运营",
    "通信服务",
    "高速公路",
    "铁路公路",
    "铁路运输",
    "燃气",
    "油气开采",
    "石油开采",
)
# 命中白名单后仍剔除（避免航运港口等脏分类）
SECTOR_INDUSTRY_BLOCK_KEYWORDS = (
    "航运",
    "白酒",
    "水泥",
    "传媒",
    "航运港口",
)

# --- Entry / exit ---
WEEK_PCTB_BUY_MAX = 0.30
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

SESSION_NAME = "hongli-em"
