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
# 只跑前 N 只；国债必须真实抓取（API 优先，失败重试，禁止默认值）
python scripts/run_today_scan.py --limit 10
# 无 browser（国债仍走东财 API；抓不到直接报错）：
python scripts/run_today_scan.py --no-browser --limit 10
```

### 输出

| 路径 | 内容 |
|------|------|
| `output/today_scan/REPORT.md` | **唯一报告**：①今日推荐（无则跳过）②东财粗筛全部个股详情+历史回测表 |
| `output/features_{code}.csv` | 单票特征/信号（中间数据） |
| `data/cache/cn_bond_30y.csv` | 国债序列 |
| `data/cache/xuangu_universe.json` | 条件选股粗筛池 |

报告每只股票含：当前买点/策略股息率/日周月位置/硬门槛；历史轮次表（买点、买入股息率、日周月、硬门槛、本金、收益率、最大亏损本金、回本天数、最大回撤）。  
股息**历史分位**必须按上市以来序列（`YIELD_SERIES_START`），**不得**用回测 `start` 截断样本。

### 今日买点须同时满足（与 `signals.py` 一致）

0. `hard_ok`（股息≥2×30Y + 分位≥80% + 连续分红 + 现金流覆盖 + 市值）  
1. 最新价 ≤ 当月 forming 中轨  
2. 当周 forming %b ≤ 0.25  
3. 日最低价 > 日中轨，且 `0.5 < 日%b ≤ 0.8`  
   （日/周/月均用截至当日 forming；例 10/15 → 用 10/15 的日周月指标）

## 回测流水线（可选）

```bash
python scripts/run_pipeline.py --seed 601318 600900 601088 --start 2018-01-01
python scripts/run_pipeline.py --skip-browser --seed 601318 --mode multi
```

### 分步脚本

| 步骤 | 命令 |
|------|------|
| 今日扫描 | `python scripts/run_today_scan.py` |
| 国债 | `python scripts/fetch_bond_yield.py --force`（API→browser 重试，失败报错） |
| 条件选股粗筛 | `python scripts/screen_xuangu.py --bond-yield <真实30Y>` |
| 行情/特征（单票） | `python scripts/build_features.py 601318 --start 2018-01-01` |
| 回测 | `python scripts/backtest_pybroker.py 601318 --mode both --start 2018-01-01` |

## Agent 执行约定

1. **先读** `reference.md` 确认硬门槛与买卖口径，再改代码。
2. **可量化逻辑只许改 `scripts/`**，不要把规则只写在对话里。
3. **默认主入口** `run_today_scan.py`：国债 → xuangu(东财股息) → 每票策略 → 买点置顶。
4. **国债必须真实抓取**：API 优先、失败继续重试（可再走 browser）；抓不到就报错退出，**禁止** `--bond-demo` / 固定 2.5% / 静默默认值。
5. 粗筛用东财 TTM；报告只展示 `em_div_yield_pct`，不写策略股息率字段。
6. 每票默认跑多账号回测（`--n 0` 按需建户；`--n N` 固定账号数；`--skip-backtest` 可跳过）。
7. browser：session `hongli-em`；`browser` 子命令不要加 `-f json`。
8. 价格口径：技术指标用**前复权**；成交/派息入账用**不复权**。
9. 输出落到 `output/`，缓存落到 `data/cache/`。

## 脚本职责

- `config.py` — 参数常量
- `fetch_bond_yield.py` — 东财 API 抓 30Y（失败再 browser DOM）；**禁止 demo/默认值**
- `screen_xuangu.py` — 条件选股：市值>1000亿 + 东财最新股息率≥2×30Y
- `run_today_scan.py` — **今日扫描主入口**
- `build_features.py` / `signals.py` / `fundamental.py` / `dividend_yield.py` — 策略复算
- `fetch_baostock.py` — 日线 qfq/raw、分红
- `run_pipeline.py` — 回测端到端

## 验收

- 粗筛候选均满足：市值>1000亿 且 东财最新股息率≥2×30Y
- 每只候选有 `output/features_{code}.csv`
- 仅输出 `REPORT.md`：有今日买点才写第1节；第2节覆盖全部粗筛股及历史回测表
- 买入日须同时：硬门槛(含分位≥80%) + 价≤当月forming中轨 + 周%b≤0.25 + 日低>中轨且 0.5<%b≤0.8
