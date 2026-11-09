---
name: hongli-backtest
description: >-
  红利布林策略回测：基本面硬门槛 + 月/周/日布林买卖 + 多账号滚动，基于 pybroker、
  BaoStock 与 opencli browser 爬取东方财富筛选。在用户提到红利回测、布林线策略回测、
  股息率筛选、多账号接单、pybroker+baostock、东财选股时使用。
---

# 红利布林回测

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
# 包名 lib-pybroker，import 仍为 pybroker
opencli doctor   # browser 抓东财前必须绿
```

## 标准流水线（按序执行）

复制并勾选：

```
- [ ] 1. 东财筛选候选
- [ ] 2. 30Y 国债收益率
- [ ] 3. BaoStock 行情/分红
- [ ] 4. 特征 + 信号
- [ ] 5. 多账号回测（主）
- [ ] 6. pybroker 单账号对照
```

### 一键

```bash
source .venv/bin/activate
python scripts/run_pipeline.py --seed 601318 600900 601088 --start 2018-01-01
# 无浏览器时：
python scripts/run_pipeline.py --skip-browser --bond-demo --seed 601318 --mode multi
```

### 分步脚本

| 步骤 | 命令 |
|------|------|
| 东财筛选 | `python scripts/screen_eastmoney.py --seed 601318 600900` |
| 国债 | `python scripts/fetch_bond_yield.py` 或 `--seed-demo` |
| 行情 | `python scripts/fetch_baostock.py 601318 --start 2015-01-01` |
| 特征信号 | `python scripts/build_features.py 601318 --start 2018-01-01` |
| 多账号 | `python scripts/multi_account.py --signals output/features_601318.csv --raw data/cache/sh_601318/raw_*.csv --dividend data/cache/sh_601318/dividend.csv` |
| 回测 | `python scripts/backtest_pybroker.py 601318 --mode both --start 2018-01-01` |

## Agent 执行约定

1. **先读** `reference.md` 确认第 0～5 步与回测口径，再改代码。
2. **可量化逻辑只许改 `scripts/`**，不要把规则只写在对话里。
3. 东财筛选：**优先** `opencli eastmoney quote/rank`；缺口用 `opencli browser`（见 eastmoney.md）。禁止手写无验证的 selector 硬编码跨会话复用。
4. 回测主结果看 **multi_account**（资金池收益、miss_busy、本金最大浮亏）；pybroker 仅单账号对照。
5. 价格口径：技术指标用**前复权**；成交/股息率分母/派息入账用**不复权**。
6. 输出落到 `output/`，缓存落到 `data/cache/`。

## 脚本职责

- `config.py` — 参数常量
- `fetch_baostock.py` — 日线 qfq/raw、分红
- `fetch_bond_yield.py` — opencli browser 抓 30Y 国债
- `screen_eastmoney.py` — 东财候选 + 硬门槛初筛
- `indicators.py` — BB/%b/RSI/MFI/量能、周月对齐
- `dividend_yield.py` — 完整一年股息拼接与分位
- `fundamental.py` — 四条硬门槛
- `signals.py` — 买卖信号与同月首信号
- `multi_account.py` — N 账号滚动引擎 + 最小 N
- `metrics.py` — 本金浮亏/回本
- `baostock_datasource.py` — pybroker DataSource
- `backtest_pybroker.py` — pybroker + multi 入口
- `build_features.py` — 单标的特征表
- `run_pipeline.py` — 端到端

## 验收

- 买入日须同时：硬门槛 + 月中轨下 + 股息分位≥80% + 周%b≤0.25 + 日低>中轨且 0.5<%b≤0.8
- 同月全局限购 1 次；账号忙与同月限购分别计数
- 减仓：周上轨 20%、月上轨 30%、股息分位 78→70 阶梯后清仓
