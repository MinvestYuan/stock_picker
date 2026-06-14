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

### 部署（手动使用 Cloudflare / Wrangler）

GitHub 现在**仅用于存放代码**，不再用于自动部署。  
所有部署都通过 Cloudflare Workers + wrangler CLI 手动完成（使用你本地的 `.env` 中的 token）。

1. 生成报告：
   ```powershell
   python main.py backtest --top 5
   ```

2. 部署到 Cloudflare Worker：
   - 确保 `.env` 中有你的最新 `CLOUDFLARE_API_TOKEN`（和可选的 `CLOUDFLARE_ACCOUNT_ID`）。
   - 运行：
     ```powershell
     # 加载 token 并部署（仅上传 index.html，因为有 .assetsignore）
     Get-Content .env | ForEach-Object { if ($_ -match '^(CLOUDFLARE_[^=]+)=(.*)$') { $env:($matches[1]) = $matches[2] } }; npm run deploy
     ```
   - 或者直接：
     ```powershell
     npx wrangler deploy
     ```
   - 部署后访问：`https://minvest.<你的子域>.workers.dev`

3. **重要：取消 GitHub 自动部署集成（避免 build token 问题）**
   - 登录 Cloudflare Dashboard → Workers & Pages → 找到 `minvest` Worker。
   - Settings → Builds。
   - 如果有 Git 仓库连接，点击 **Disconnect** 或 **Manage** → 断开 Git 集成。
   - 或者在 Builds 设置中清空 Build command / Deploy command（改成不自动部署）。
   - 以后 push 到 GitHub 不会再触发构建/部署。

4. Cloudflare Access（仅自己可见，强烈推荐）：
   - Zero Trust → Access → Applications → Add an application（选 Self-hosted 或 Worker 相关）。
   - 绑定 Worker URL（`minvest.*.workers.dev` 或自定义域名）。
   - Policy：Allow + Include Email（你的邮箱）。
   - 访问时强制 Cloudflare 登录验证。

**提示**：
- 项目中已添加 `.assetsignore`，部署时只会包含 `index.html`（干净的报告站点，不暴露 Python 源码等文件）。
- `.env` 仅本地使用（已加入 .gitignore，绝不会提交到 GitHub）。部署时通过加载 .env 使用 token。
- 推荐绑定自定义域名后再用 Access 保护。
- 报告使用 Tailwind CDN + lightweight-charts CDN，其余全部内嵌。
- 本地预览（模拟 Workers 静态服务）：`npm run dev`（先加载 .env token）。
- 你可以随时在 Cloudflare Dashboard 手动上传或管理 Worker。

### 常用命令
```bash
python main.py backtest --top 5                 # 生成 index.html（推荐）
python main.py backtest --top 5 --output report.html
python main.py resolve --missing-only          # 补全缺失 ticker（通常自动）
```

**Workers 部署相关**：
```powershell
# 加载 .env 中的 token 并部署
Get-Content .env | ForEach-Object { if ($_ -match '^(CLOUDFLARE_[^=]+)=(.*)$') { $env:($matches[1]) = $matches[2] } }; npm run deploy

# 本地预览
npm run dev
```

**IB 要求**：必须启动并登录 IB Gateway/TWS（Live 推荐 `--port 4001`；Paper 用 4002）。价格数据增量更新依赖 IB。

**并行加速**：默认使用 4 个 IB 连接（`--num-connections 4`），可显式调小。

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