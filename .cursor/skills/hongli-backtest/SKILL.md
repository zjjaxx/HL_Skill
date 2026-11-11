---
name: hongli-backtest
description: >-
  红利布林策略：先查30Y国债，再用东财条件选股粗筛（市值>1000亿且最新股息率≥2×30Y），
  对每只股票跑策略全流程并落单票结果；今日买点置顶附判断条件。基于 pybroker、
  BaoStock、opencli browser。在用户提到红利回测、今日选股、布林买点、条件选股、
  千亿高股息、多账号接单时使用。
---

# 红利布林回测 / 今日扫描

策略原文：`/Users/zhengjiajun/zjj/learn/blog/src/post/布林线.md`  
细则摘要：[reference.md](reference.md) · 东财抓取：[eastmoney.md](eastmoney.md)

工作区根目录：`/Users/zhengjiajun/zjj/learn/红利`  
量化脚本：`scripts/`（全部可执行）

## 环境

```bash
cd /Users/zhengjiajun/zjj/learn/红利
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 或：pip install "git+https://github.com/edtechre/pybroker.git@v2.0.2" baostock pandas numpy
opencli doctor   # browser 抓东财前必须绿
```

## 今日扫描（默认主流程）

复制并勾选：

```
- [ ] 1. 查 30Y 国债收益率（browser DOM：中美国债页 zmgzsyl.html）
- [ ] 2. 东财条件选股粗筛：总市值>1000亿 且 最新股息率(TTM)≥2×30Y
- [ ] 3. 对每只候选：策略信号 + 多账号回测——粗筛只用东财股息，报告不展示策略股息率
- [ ] 4. 每只股票落 JSON；写详细 REPORT.md（买点置顶 + 条件拆解 + 回测轮次）
```

### 一键

```bash
source .venv/bin/activate
python scripts/run_today_scan.py
# 指定国债 / 只跑前 N 只：
python scripts/run_today_scan.py --bond-yield 2.04 --limit 10
# 无浏览器：
python scripts/run_today_scan.py --no-browser --bond-demo
```

### 输出

| 路径 | 内容 |
|------|------|
| `output/today_scan/REPORT.md` | **详细报告**：买点置顶、条件拆解、汇总表、分票回测轮次 |
| `output/today_scan/summary.json` | 汇总；**`buy_today_first` 为首段** |
| `output/today_scan/{code}.json` | 单票信号拆解 + 多账号回测 |
| `output/features_{code}.csv` | 单票特征/信号全表 |
| `data/cache/cn_bond_30y.csv` | 国债序列 |
| `data/cache/xuangu_universe.json` | 条件选股粗筛池（含 `em_div_yield_pct`） |

粗筛只用东财 **最新股息率(TTM)** ≥ 2×30Y；报告/汇总**不输出策略股息率**。  
股息**历史分位**必须按上市以来序列（`YIELD_SERIES_START`），**不得**用回测 `start` 截断样本，否则分位会被系统性压低。

### 今日买点须同时满足（与 `signals.py` 一致）

0. `hard_ok`（连续分红、市值等硬门槛）  
1. 周收盘(当日) ≤ 上月中轨，且股息历史分位 ≥ 80%  
2. 当周 forming %b ≤ 0.25  
3. 日最低价 > 日中轨，且 `0.5 < 日%b ≤ 0.8`

## 回测流水线（可选）

```bash
python scripts/run_pipeline.py --seed 601318 600900 601088 --start 2018-01-01
python scripts/run_pipeline.py --skip-browser --bond-demo --seed 601318 --mode multi
```

### 分步脚本

| 步骤 | 命令 |
|------|------|
| 今日扫描 | `python scripts/run_today_scan.py` |
| 国债 | `python scripts/fetch_bond_yield.py --force` |
| 条件选股粗筛 | `python scripts/screen_xuangu.py --bond-yield 2.04` |
| 行情/特征（单票） | `python scripts/build_features.py 601318 --start 2018-01-01` |
| 回测 | `python scripts/backtest_pybroker.py 601318 --mode both --start 2018-01-01` |

## Agent 执行约定

1. **先读** `reference.md` 确认硬门槛与买卖口径，再改代码。
2. **可量化逻辑只许改 `scripts/`**，不要把规则只写在对话里。
3. **默认主入口** `run_today_scan.py`：国债 → xuangu(东财股息) → 每票策略 → 买点置顶。
4. 粗筛用东财 TTM；报告只展示 `em_div_yield_pct`，不写策略股息率字段。
5. 每票默认跑多账号回测（`--n 0` 自动最小 N；`--skip-backtest` 可跳过）。
6. browser：session `hongli-em`；`browser` 子命令不要加 `-f json`。
7. 价格口径：技术指标用**前复权**；成交/派息入账用**不复权**。
8. 输出落到 `output/`，缓存落到 `data/cache/`。

## 脚本职责

- `config.py` — 参数常量
- `fetch_bond_yield.py` — browser DOM 抓 30Y 国债（`zmgzsyl.html`）
- `screen_xuangu.py` — 条件选股：市值>1000亿 + 东财最新股息率≥2×30Y
- `run_today_scan.py` — **今日扫描主入口**
- `build_features.py` / `signals.py` / `fundamental.py` / `dividend_yield.py` — 策略复算
- `fetch_baostock.py` — 日线 qfq/raw、分红
- `screen_strategy_hard.py` — 仅调试用（非默认粗筛）
- `run_pipeline.py` — 回测端到端

## 验收

- 粗筛候选均满足：市值>1000亿 且 东财最新股息率≥2×30Y
- 每只候选有 `output/today_scan/{code}.json` + `output/features_{code}.csv`
- `summary.json` 中今日买点在 `buy_today_first`，每条含 `buy_conditions`
- 买入日须同时：硬门槛 + 月中轨下 + 股息分位≥80% + 周%b≤0.25 + 日低>中轨且 0.5<%b≤0.8
