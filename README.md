# Minvest



Russell 1000 NPORT 月度回测 + 静态 HTML 报告（TradingView 图表 + 交互持仓卡片）。



## 策略 (Strategy)



- **4-1 Momentum**：过去4个月回报 − 过去1个月回报

- **RRG 相对强度**：RS Ratio + RS Momentum（线性）

- **固定硬比例加权**：Momentum 60% + RRG 40%

- **持仓**：等权 Top-5

- **熊市保护**：QQQ 50EMA < 200EMA 时全仓现金；向上穿过时恢复策略

- **Universe**：iShares Russell 1000 ETF (IWB) 每月 NPORT-P 持仓



## 数据获取流程 (Data Acquisition Flow)



1. **NPORT 持仓数据**

   - 自动从 SEC EDGAR 每日轻量检查 iShares Russell 1000 ETF (IWB) 的 NPORT-P filings（XML）

   - 每日最多查询 SEC 一次（同日后续运行直接跳过，使用本地缓存）

   - 解析最新 `reportPeriodDate` ≤ 月末的持仓，构建每月 universe

   - 缓存：`cache/stock_picker.db` (SQLite)

   - Ticker 自动解析：IB Gateway + SQLite 缓存（`ticker_unified_cache`）



2. **价格 & 特征数据**

   - 通过 IB Gateway (默认 Live 4001) 增量获取 OHLC 数据

   - 缓存为 Parquet（`cache/price_cache_*.parquet`，支持 full OHLC 用于 K 线）

   - 同时拉取 benchmarks（SPY / QQQ / SOXX）

   - 特征预计算（data_fetcher）：4-1 momentum、RS ratio/momentum、ema50 等



3. **自动触发**

   - 运行 `python main.py` 时自动同步缺失的 NPORT/价格/ticker

   - 首次或长时间未更新时可能较慢（IB 连接 + 解析），之后极快



## 部署流程与命令 (Deployment & Commands)



### 生成报告（必须先有 IB Gateway 登录）

```powershell

python main.py

python main.py --top 5

python main.py --output report.html

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

   - 公开部署仓库是 **Minvest** (https://github.com/MinvestYuan/Minvest) ，只包含 index.html。

   - 在本仓库（stock_picker）中添加 Minvest 作为第二个 remote（只需一次）：

     ```powershell

     git remote add minvest git@github.com:MinvestYuan/Minvest.git

     ```

   - **自动推送**（推荐）：安装 post-commit hook 后，每次你 `git commit` 包含 index.html 时，会**自动**只推送 index.html 到 Minvest 仓库。

     - 最简单方式：运行仓库里提供的安装脚本（推荐）：

       ```powershell

       pwsh scripts/setup-minvest-hook.ps1

       ```

     - 这会自动创建 .git/hooks/post-commit ，以后每次 commit 包含 index.html 时自动推送。

     - 然后 `chmod +x .git/hooks/post-commit` （在 Git Bash）或在 Windows 上确保 Git 可以运行它。

   - 在 Minvest 仓库 Settings → Pages:

     - Source: Deploy from a branch

     - Branch: **main**

     - Folder: **/ (root)**

   - 报告将部署到 `https://minvestyuan.github.io/Minvest/`



**提示**：整个过程都在 stock_picker 文件夹内完成，不需要创建第二个本地文件夹。Minvest 远程仓库只保留 index.html 的历史。Hook 会在你提交包含 index.html 的 commit 时自动触发。



**提示**：

- 报告完全静态，离线可用，支持自定义域名（在 Pages 设置中添加）。

- 每次更新报告后 push，GitHub Pages 会自动重新部署。

- 如果想用 gh-pages 分支或其他方式，可自行配置。

- 报告使用 Tailwind CDN + lightweight-charts CDN，其余全部内嵌。



### 常用命令

```bash

python main.py                    # 生成 index.html（推荐）

python main.py --top 5            # 指定持仓数量

python main.py --output report.html

python main.py --start-month 2020-01

```



**IB 要求**：必须启动并登录 IB Gateway/TWS（Live 默认端口 4002）。价格数据增量更新依赖 IB。



---



生成后 `index.html` 即为可直接部署的完整可视化报告。

