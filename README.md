# Stock Picker

Russell 1000 NPORT 月度回测 + 静态 HTML 报告（TradingView 图表 + 交互持仓卡片）。

## 策略 (Strategy)

- **4-1 Momentum**：过去4个月回报 − 过去1个月回报
- **RRG 相对强度**：RS Ratio + RS Momentum（线性）
- **固定硬比例加权**：Momentum 60% + RRG 40%
- **持仓**：等权 Top-5（默认）
- **熊市保护**：QQQ 50EMA < 200EMA 时全仓现金；向上穿过时恢复策略
- **Universe**：iShares Russell 1000 ETF (IWB) 每月 NPORT-P 持仓
- **无**个股 50 日均线硬过滤

## 数据获取流程 (Data Acquisition Flow)

1. **NPORT 持仓数据**
   - 自动从 SEC EDGAR 每日轻量检查 iShares Russell 1000 ETF (IWB) 的 NPORT-P filings（XML）
   - 每日最多查询 SEC 一次（同日后续运行直接跳过，使用本地缓存）
   - 解析最新 `reportPeriodDate` ≤ 月末的持仓，构建每月 universe
   - 缓存：`cache/stock_picker.db` (SQLite)
   - Ticker 自动解析：IB Gateway + SQLite 缓存（`ticker_unified_cache`）

2. **价格 & 特征数据**
   - 通过 IB Gateway (默认 Live 4002) 增量获取 OHLC 数据
   - 缓存为 Parquet（`cache/price_cache_*.parquet`，支持 full OHLC 用于 K 线）
   - 同时拉取 benchmarks（SPY / QQQ / SOXX）
   - 特征预计算（data_fetcher）：4-1 momentum、RS ratio/momentum、ema50 等

3. **自动触发**
   - 运行 `python main.py` 时自动同步缺失的 NPORT/价格/ticker
   - 首次或长时间未更新时可能较慢（IB 连接 + 解析），之后极快

## 命令 (Commands)

必须先启动并登录 IB Gateway/TWS（Live 默认端口 4002）。价格数据增量更新依赖 IB。

```bash
python main.py                    # 生成 index.html（推荐）
python main.py --top 5            # 指定持仓数量
python main.py --output report.html
python main.py --start-month 2020-01
```

输出 `index.html`：单文件静态报告，内嵌全部数据 + TradingView 轻量图表 + 年份过滤 + 详细月度持仓卡片，完全离线可用。

## 测试 (Tests)

```bash
python -m pytest          # 运行全部测试
```

或逐个运行：

```bash
python backtest/test_trade_dates.py     # 换仓日期 + 开盘价
python strategy/test_stock_selector.py  # 评分逻辑 + 边界
python report/test_metrics.py           # 指标计算（夏普/回撤等）
python backtest/test_backtest.py        # 回测引擎端到端
```

报告使用 Tailwind CDN + lightweight-charts CDN，其余全部内嵌，可本地打开或自行部署 `index.html`。
