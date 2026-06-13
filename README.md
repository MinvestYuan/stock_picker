# Minvest

Russell 1000 NPORT 月度回测 + 静态 HTML 报告（TradingView 图表 + 交互持仓卡片）。

## 策略 (Strategy)

- **4-1 Momentum**：过去4个月回报 − 过去1个月回报
- **RRG 相对强度**：RS Ratio + RS Momentum（线性）
- **固定硬比例加权**：Momentum 60% + RRG 40%（已移除熵权法）
- **持仓**：等权 Top-5（默认）
- **熊市保护**（backtest 生效）：QQQ 50EMA < 200EMA 时全仓现金；向上穿过时恢复策略
- **Universe**：iShares Russell 1000 ETF (IWB) 每月 NPORT-P 持仓
- **无**个股 50 日均线硬过滤

## 数据获取流程 (Data Acquisition Flow)

1. **NPORT 持仓数据**
   - 自动从 SEC EDGAR 每日轻量检查 iShares Russell 1000 ETF (IWB) 的 NPORT-P filings（XML）
   - 每日最多查询 SEC 一次（同日后续运行直接跳过，使用本地缓存）
   - 解析最新 `reportPeriodDate` ≤ 月末的持仓，构建每月 universe
   - 缓存：`cache/nport.db` (SQLite)
   - Ticker 自动解析：IB Gateway + `cache/manual_ticker_overrides.json` + 失败缓存

2. **价格 & 特征数据**
   - 通过 IB Gateway (默认 Live 4001) 增量获取 OHLC 数据
   - 缓存为 Parquet（`cache/price_cache_*.parquet`，支持 full OHLC 用于 K 线）
   - 同时拉取 benchmarks（SPY / QQQ / SOXX）
   - 特征预计算（data_fetcher）：4-1 momentum、RS ratio/momentum、ema50 等

3. **自动触发**
   - 运行 `python main.py backtest` 时自动同步缺失的 NPORT/价格/ ticker
   - 首次或长时间未更新时可能较慢（IB 连接 + 解析），之后极快

**代理**（国内网络）：` $env:STOCK_PICKER_PROXY = "http://127.0.0.1:10808" `

## 部署流程与命令 (Deployment & Commands)

### 生成报告（必须先有 IB Gateway 登录）
```powershell
python main.py backtest --top 5
```
- 输出：`index.html`（单文件静态报告，内嵌全部数据 + TradingView 轻量图表 + 年份过滤 + 详细月度持仓卡片）
- 报告完全离线可用

### 部署到 GitHub + Cloudflare Pages（推荐私有）
1. 生成 `index.html` 后提交（可单独建 report 仓库或直接本仓库）：
   ```bash
   git add index.html
   git commit -m "Update Minvest report"
   git push
   ```

2. Cloudflare Pages：
   - Pages → Create project → Connect to Git
   - Framework preset: **None**
   - Build command: 留空
   - Output directory: 留空（根目录）

3. Cloudflare Access（仅自己可见）：
   - Zero Trust → Access → Applications → Add self-hosted application
   - 绑定 Pages URL
   - Policy：Allow + Include Email（你的邮箱）
   - 访问时强制 Cloudflare 登录验证

**提示**：
- 推荐绑定自定义域名后再用 Access 保护
- 可配置 GitHub Actions 在 backtest 后自动 push index.html
- 报告使用 Tailwind CDN + lightweight-charts CDN，其余全部内嵌

### 常用命令
```bash
python main.py backtest --top 5                 # 生成 index.html（推荐）
python main.py backtest --top 5 --output report.html
python main.py resolve --missing-only          # 补全缺失 ticker（通常自动）
```

**IB 要求**：必须启动并登录 IB Gateway/TWS（Live 推荐 `--port 4001`；Paper 用 4002）。价格数据增量更新依赖 IB。

**并行加速**：默认使用 4 个 IB 连接（`--num-connections 4`），可显式调小。

---

生成后 `index.html` 即为可直接部署的完整可视化报告。