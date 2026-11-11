# 东方财富筛选（opencli browser + 条件选股）

## 原则

1. **今日粗筛主路径**：东财条件选股 API（`search-code`），等价于页面「总市值>800亿;最新股息率…」。
2. browser：先 `opencli doctor`，session 名固定 `hongli-em`；打开 xuangu 页作对齐，**筛选结果以 API 为准**。
3. 粗筛股息率 = 东财 **最新股息率(TTM)**，**不是**策略完整一年股息。
4. 适配器 `opencli eastmoney quote/rank/...` 仅补字段；分红页 DOM 仅调试/回测用。
5. 不要跨会话复用 numeric ref；`browser` 子命令不要加 `-f json`。

## 主流程页面 / API

| 用途 | URL / Endpoint | 要点 |
|------|----------------|------|
| 条件选股 | https://xuangu.eastmoney.com/ | browser 打开；粗筛用 API |
| search-code | `POST https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code` | body 必须含 `fingerprint:"web"` |
| 国债收益率 | API `RPTA_WEB_TREASURYYIELD` / 页 https://data.eastmoney.com/cjsj/zmgzsyl.html | 字段 `EMM00166469`=中国30Y；抓不到重试，禁止默认值 |

### 条件选股查询串

```
总市值>{N}亿;最新股息率大于等于{2×30Y}%小于等于100%
```

字段：`SECURITY_CODE` / `SECURITY_SHORT_NAME` / `DIVIDEND_NEWRATIO` / `TOAL_MARKET_VALUE`（东财拼写）。

脚本：先 `python scripts/fetch_bond_yield.py --force` 拿真实 30Y，再  
`python scripts/screen_xuangu.py --bond-yield <最新30Y>` → `data/cache/xuangu_universe.json`

## Browser（国债 / 可选对齐）

```bash
opencli doctor
opencli browser hongli-em open https://data.eastmoney.com/cjsj/zmgzsyl.html
opencli browser hongli-em wait time 3
# DOM 取 30Y；或直接 python scripts/fetch_bond_yield.py --force
opencli browser hongli-em open https://xuangu.eastmoney.com/
opencli browser hongli-em close
```

## 分红配送 DOM（调试 / 非默认粗筛）

默认报告期常是未来期末。必须先切到有数据的期（如最近 `*-12-31`）。

```bash
opencli browser hongli-em open https://data.eastmoney.com/yjfp/
opencli browser hongli-em wait time 4
opencli browser hongli-em select "#filter_date" "2025-12-31"
opencli browser hongli-em wait time 4
opencli browser hongli-em eval '(() => {
  const table = [...document.querySelectorAll("table")].find(t => (t.rows?.length||0) > 10);
  if (!table) return [];
  return [...table.querySelectorAll("tr")].map(tr => {
    const tds = [...tr.querySelectorAll("td")].map(td => td.innerText.replace(/\s+/g," ").trim());
    if (tds.length < 8 || !/^[036]\d{5}$/.test(tds[0])) return null;
    return { code: tds[0], name: tds[1], cash_div: tds[6], yield_pct: tds[7] };
  }).filter(Boolean);
})()'
```

## 适配器（补字段）

```bash
opencli eastmoney quote 601318,600519 -f json   # marketCap 元 → /1e8 亿元
opencli eastmoney rank --market hs-a --sort turnover --limit 50 -f json
```

## 脚本封装

| 脚本 | 用途 |
|------|------|
| `fetch_bond_yield.py` | 东财 API→browser 抓 30Y；失败重试后报错，不用默认值 |
| `screen_xuangu.py` | **默认粗筛**：市值>800亿 + 东财最新股息率≥2×30Y |
| `run_today_scan.py` | 国债 → xuangu → 每票策略 → 买点置顶 |
| `screen_eastmoney.py` | 旧 DOM 路径（仅 `run_pipeline` 可选使用） |

## 筛选落地（今日）

1. 国债 → `data/cache/cn_bond_30y.csv`
2. xuangu 粗筛 → `data/cache/xuangu_universe.json`（含 `em_div_yield_pct`）
3. 每票策略 → `output/today_scan/{code}.json` + `output/features_{code}.csv`
4. 汇总 → `output/today_scan/summary.json` + **`REPORT.md`**（买点置顶、含判断条件）
