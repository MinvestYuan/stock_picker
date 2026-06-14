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

### 部署（唯一方式：Git push 自动部署）

**从现在开始，唯一部署方式是 git push。**  
其他所有本地部署方式（直接运行 npm run deploy 等）已全部取消。推送代码到主分支后，Cloudflare 会自动完成构建和部署。

1. 生成 `index.html` 后提交并推送（可单独建 report 仓库或直接本仓库）：
   ```bash
   git add index.html
   git commit -m "Update Minvest report"
   git push
   ```

2. Cloudflare Git 自动部署配置（只需配置一次）：
   - Cloudflare dashboard 进入 **Workers & Pages** → 找到你的 `minvest` Worker → **Settings** → **Builds**
   - 配置 Build settings：
     - Build command: `npm ci`
     - Deploy command: `npm run deploy`
     - Root directory: 留空
     - 生产分支: main（或你的主分支）
   - **关键：添加 Build secret（让所有 build 都使用 .env 里的 token）**
     - 在 **Build variables and secrets** 区域添加 Secret：
       - Name: `CLOUDFLARE_API_TOKEN`
       - Value: 打开你本地的 `.env` 文件，复制完整的 token 值（`cfut_...` 那整行）
     - （可选）再添加 Secret：
       - Name: `CLOUDFLARE_ACCOUNT_ID`
       - Value: 从 .env 复制对应的值
   - **注意**：Worker 名称必须与 `wrangler.jsonc` 里的 `"name": "minvest"` 一致（可在 dashboard 修改或同步 config）。
   - 保存后，**每次 push 到主分支都会自动触发 Cloning → Installing → Deploying**，最终部署到 Worker。

3. Cloudflare Access（仅自己可见，强烈推荐）：
   - Zero Trust → Access → Applications → Add an application（选 Self-hosted 或 Worker 相关）
   - 绑定 Worker URL（`minvest.*.workers.dev` 或自定义域名）
   - Policy：Allow + Include Email（你的邮箱）
   - 访问时强制 Cloudflare 登录验证

**提示**：
- 项目中已添加 `.assetsignore`，部署时只会包含 `index.html`（干净的报告站点，不暴露 Python 源码等文件）。
- `.env` 仅本地使用（已加入 .gitignore，绝不会提交）。CI 构建通过 Dashboard 的 Build secrets 注入 `CLOUDFLARE_API_TOKEN`，所有 build 都使用你 .env 里的这个 token。
- 推荐绑定自定义域名后再用 Access 保护。
- 报告使用 Tailwind CDN + lightweight-charts CDN，其余全部内嵌。
- 本地仅可预览（不部署）：先加载 .env token 后运行 `npm run dev`
- 非主分支 push 时，默认会用 preview 版本（可获得预览 URL，不影响生产）。

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