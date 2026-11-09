# 东方财富筛选（opencli）

## 原则

1. 先用现成适配器，再用 `opencli browser`。
2. browser：先 `opencli doctor`，session 名固定 `hongli-em`。
3. 优先 `network` 抓 JSON API，其次 `eval`/`extract` DOM。
4. 不要跨会话复用 numeric ref；每次 `state`/`find` 再操作。

## 适配器（无需 browser）

```bash
opencli eastmoney quote 601318,600519 -f json
opencli eastmoney rank --market hs-a --sort turnover --limit 50 -f json
opencli eastmoney holders 601318 -f json
opencli eastmoney kline 601318 --help
```

`quote` 字段含 `marketCap`（元）→ 脚本里 /1e8 得亿元，过滤 ≥1000 亿。

## Browser 页面

| 用途 | URL |
|------|-----|
| 分红配送 | https://data.eastmoney.com/yjfp/ |
| 条件选股 | https://xuangu.eastmoney.com/ |
| 国债收益率 | https://data.eastmoney.com/cjsj/zfzqyl.html |

```bash
opencli browser hongli-em open https://data.eastmoney.com/yjfp/ --window background -f json
opencli browser hongli-em wait time 4 -f json
opencli browser hongli-em network -f json          # 存 shape；再 --detail <key> 拉 body
opencli browser hongli-em eval '(() => [...document.body.innerText.matchAll(/\b([036]\d{5})\b/g)].map(m=>m[1]))()' -f json
opencli browser hongli-em close
```

脚本封装：`python scripts/screen_eastmoney.py`、`python scripts/fetch_bond_yield.py`。

## 筛选落地

1. browser/种子 → `data/cache/em_candidates.json`
2. quote 市值过滤
3. BaoStock 连续分红等 → `data/cache/em_passed.json`
4. 通过列表交给 `run_pipeline` / `backtest_pybroker`
