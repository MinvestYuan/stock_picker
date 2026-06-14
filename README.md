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

### 部署到 GitHub Pages（静态托管）

GitHub 仅用于存放代码和部署静态报告。

1. 生成 `index.html` 后提交到仓库（推荐直接用本仓库或单独的 report 仓库）：
   ```bash
   git add index.html
   git commit -m "Update Minvest report"
   git push
   ```

2. GitHub Pages 设置（只需设置一次）：
   - 这个仓库（stock_picker）是**隐私项目**，包含完整源码。
   - 公开部署仓库是 **Minvest** (https://github.com/HaominYuan/Minvest) ，只包含 index.html。
   - 在本仓库（stock_picker）中添加 Minvest 作为第二个 remote（只需一次）：
     ```powershell
     git remote add minvest git@github.com:HaominYuan/Minvest.git
     ```
   - **自动推送**（推荐）：安装 post-commit hook 后，每次你 `git commit` 包含 index.html 时，会**自动**只推送 index.html 到 Minvest 仓库。
     - 安装 hook（在 stock_picker 目录执行一次）：
       ```powershell
       # 创建 hooks 目录（如果没有）
       mkdir -p .git/hooks
       # 下载或创建 post-commit hook（内容见下面）
       # 或者复制下面的脚本内容到 .git/hooks/post-commit
       ```
     - post-commit hook 内容（保存为 .git/hooks/post-commit ，并确保可执行）：
       ```sh
       #!/bin/sh
       if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q index.html; then
         echo "index.html changed, auto-pushing to Minvest..."
         CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
         git checkout --orphan minvest-deploy
         git rm -rf . > /dev/null 2>&1 || true
         git checkout "$CURRENT_BRANCH" -- index.html
         git add index.html
         git commit -m "Update Minvest report (auto from stock_picker)"
         git push minvest minvest-deploy:main --force
         git checkout "$CURRENT_BRANCH"
         git branch -D minvest-deploy
         echo "✅ Pushed index.html to Minvest"
       fi
       ```
     - 然后 `chmod +x .git/hooks/post-commit` （在 Git Bash）或在 Windows 上确保 Git 可以运行它。
   - 在 Minvest 仓库 Settings → Pages:
     - Source: Deploy from a branch
     - Branch: **main**
     - Folder: **/ (root)**
   - 报告将部署到 `https://haominyuan.github.io/Minvest/`

**提示**：整个过程都在 stock_picker 文件夹内完成，不需要创建第二个本地文件夹。Minvest 远程仓库只保留 index.html 的历史。Hook 会在你提交包含 index.html 的 commit 时自动触发。

**提示**：
- 报告完全静态，离线可用，支持自定义域名（在 Pages 设置中添加）。
- 每次更新报告后 push，GitHub Pages 会自动重新部署。
- 如果想用 gh-pages 分支或其他方式，可自行配置。
- 报告使用 Tailwind CDN + lightweight-charts CDN，其余全部内嵌。

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