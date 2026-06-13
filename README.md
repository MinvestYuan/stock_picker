# QuantGT 月度选股策略 — Russell 1000 扩展版

基于 iShares Russell 1000 ETF 的 SEC NPORT-P 持仓数据，对策略进行月度选股排行和月度回测。

## 环境要求

- Python 3.11+（推荐 3.11 或 3.12）
- Interactive Brokers (IB) Gateway 或 TWS 已启动并登录（用于获取价格数据）
- IB API 端口默认 `4001`（Gateway Live 真实账户）或 `7496`（TWS Live）；Paper 模拟交易使用 `4002` / `7497`

## 快速开始（推荐步骤）

```powershell
# 1. 克隆仓库
cd C:\Users\yourname\Desktop
git clone <your-repo-url> stock_picker
cd stock_picker

# 2. 创建虚拟环境
python -m venv .venv

# 3. 激活虚拟环境（Windows PowerShell）
.\.venv\Scripts\Activate.ps1

# 如果 PowerShell 报错执行策略，请先运行：
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# macOS/Linux:
# source .venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt
```

> **注意**：`.gitignore` 已配置，会自动忽略价格缓存（`price_cache_*.parquet`）、原始 XML、输出文件等大文件。

## 当前策略说明

本项目**仅使用 4-1 Momentum** 作为绝对动量策略（已按要求更新）：

- **绝对动量**：过去4个月回报 − 过去1个月回报（4-1 Momentum）
- **相对强度**：RS Ratio + RS Momentum（RRG 风格）
- **赋权方式**：固定比例（Momentum 占 60%，RRG 占 40%）
- **选股池**：iShares Russell 1000 ETF（IWB）最新 NPORT 持仓
- **熊市保护**（backtest）：当 QQQ 50日均线向下穿过 200日均线时，策略转持现金；向上穿过时重启策略。

历史实验版本（50日均线动能、极简排名版等）已全部移除，代码库仅保留当前这套逻辑。

## 代理配置（重要）

本项目默认**不使用代理**直连 SEC EDGAR 和 OpenFIGI。

如果你需要通过代理访问（例如国内网络环境），请设置环境变量：

```powershell
$env:STOCK_PICKER_PROXY = "http://127.0.0.1:10808"
```

或在运行前导出（bash）：
```bash
export STOCK_PICKER_PROXY=http://127.0.0.1:10808
```

设置后重新运行 `python main.py resolve` 即可（NPORT 同步和 ticker 解析已集成在 main 命令中）。

## 数据准备（重大改进）

**好消息**：从现在开始，**你几乎不需要手动维护持仓数据了**。

当你运行以下任意命令时，程序会**自动轻量检查**是否有新的 NPORT-P filing（新增：**今天只查询SEC一次**，同一天后续运行直接跳过SEC网络请求，完全使用缓存）：

- 今天第一次运行 backtest / 相关命令时才真正访问SEC（日历日级别），之后同日直接返回“今天已查询过SEC，直接跳过”
- 每次只拉取最近 6 个月的 filing
- 按 filingDate 降序处理，一旦发现一个之前已读过的 filing，立即暂停读取更早的（高效早停，控制台会打印提示）
- 默认不节流（max_age_hours=0），但每天只做一次完整SEC检查（除非 force=True）

```powershell
python main.py                    # 默认 backtest：月度回测 + HTML 报告
python main.py backtest --top-n 15
```

- 如果今天已查过或没有变化 → 几乎瞬间返回，使用本地缓存（同日第二次运行 backtest 极快）。
- 如果 SEC 有新季报（且今天首次检查） → 自动下载 XML、解析、补全 ticker，并更新本地缓存。

你仍然可以手动触发完整检查（或自定义 lookback_months）：

```powershell
python -c "
from data.nport_data import sync_holdings_if_needed
print(sync_holdings_if_needed(force=True, lookback_months=6))
"
```

**注意**：
- 首次运行或长时间未更新时，自动补全 ticker 可能需要 IB Gateway + 缓存（会花一些时间）。默认使用 Live 真实账户（port 4001），Paper 模拟请传 ib_port=4002。
- 中国大陆网络请先设置代理：`$env:STOCK_PICKER_PROXY = "http://127.0.0.1:10808"`
- 原始 XML 和大文件仍被 `.gitignore` 正确忽略。

---

## 注意：`rank` 命令已移除

`rank` / `russell-rank` 等命令已完全删除（被 `backtest` 全面取代）。请直接使用 `backtest` 命令获取回测报告（包含最新持仓的月度表现、累计曲线、详细持仓表格等）。

旧的 `russell1000_rank.html` 不会再生成。

---

## 功能：NPORT 持仓月度回测（`backtest`，默认命令）

**固定从 2020-01 开始**，使用每个月的 Russell 1000 持仓作为当月 universe，进行月度选股回测（可通过 --start-month 覆盖）。

### 回测规则

- **Universe 来源**：每月使用最近一个 `reportPeriodDate` ≤ 该月末的 NPORT filing 持仓
- **选股时点**：每月第一个交易日开盘前（基于上月最后一个交易日的数据）
- **调仓频率**：月度
- **持仓数量**：默认 `top-n=5` 只（等权）
- **卖出时点**：每月最后一个交易日收盘
- **Benchmarks**：SPY（标普500）、QQQ（纳斯达克100）、SOXX（费城半导体指数）

### 基础用法

```bash
python main.py backtest --top-n 5
```

### 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--top-n` | 每月持仓数量（等权） | `5` |
| `--start-month` | 回测起始月份（覆盖默认的2020-01） | `2020-01` |
| `--output` | 输出 HTML 报告路径 | `russell1000_backtest.html` |
| `--client-id` | IB client ID | `17` |
| `--host` | IB 主机 | `127.0.0.1` |
| `--port` | IB 端口 | `4001` |
| `--pause-seconds` | 每只股票请求后暂停秒数（限速） | `0.25` |
| `--num-connections` | 并行 IB 连接数（多连接加速数据获取） | `4` |

### 示例

```bash
# 完整回测（2020-01 至今，固定起始），每月持仓 5 只
python main.py backtest --top-n 5

# 如需从其他月份开始（覆盖默认）
python main.py backtest --top-n 5 --start-month 2022-01

# 指定输出路径
python main.py backtest --top-n 5 --output my_backtest.html
```

### 输出文件

#### `russell1000_backtest.html`（唯一输出文件）

这是一个完整的可视化 HTML 报告，包含：

- **回测摘要统计**：月份数、平均/累计/年化回报、胜率、最差月份等
- **累计收益对比图**：策略 vs SPY vs QQQ (Nasdaq-100) vs SOXX (费城半导体指数)
- **月度回报分布图**
- **月度汇总表格**（可交互）
- **个股明细表格**（前 N 名持仓细节）
- **基准对比表格**

所有数据和图表都内嵌在单个 HTML 文件中，便于分享和查看，无需 Excel。
- **月度收益对比**：柱状图 + SPY / QQQ / SOXX 曲线
- **回撤对比**：策略 vs SPY / QQQ / SOXX
- **指标对比表**：累计收益、CAGR、年化波动率、夏普比率、索提诺比率、最差月度回报、Calmar 比率、胜率、月度收益中位数（四列对比：策略、SPY、QQQ、SOXX）

---

## 其他命令

### resolve —— Ticker 重解析

```bash
python main.py resolve --missing-only
python main.py resolve --force
```

用于补全 NPORT 持仓中的 ticker（一般不需要手动调用，backtest 会自动触发）。

> 旧的 rank 相关命令已移除；`resolve-tickers` 等仍兼容。

---

## 策略核心逻辑（`stock_selector.py`）

1. **4-1 动量**：过去4个月回报 − 过去1个月回报
2. **RRG 综合指标**：`rs_ratio + rs_momentum`
3. **固定比例加权**：Momentum 标准化得分 × 0.6 + RRG 标准化得分 × 0.4
（已移除 EMA50 硬性过滤）
5. **排序**：按总分降序排列

---

## 常见问题

### 安装后提示缺少模块（ModuleNotFoundError）

请确保已激活虚拟环境并执行：
```powershell
pip install -r requirements.txt
```

### SEC / OpenFIGI 请求失败（连接超时或 403）

设置代理环境变量后重试：
```powershell
$env:STOCK_PICKER_PROXY = "http://127.0.0.1:10808"
# 已集成到 `python main.py backtest` / `python main.py resolve` 中，无需单独运行
```

### IB 连接失败 / clientId 被占用

更换 client ID：
```powershell
python main.py backtest --client-id 22
```

### IB 连接要求

backtest 命令**强制**连接 IB Gateway/TWS 获取最新价格数据并增量缓存（不再支持跳过）。请确保 IB 已启动并登录，否则命令会失败。

**注意**：默认 `--port 4001` 连接 Gateway **Live（真实账户）**。如使用 Paper 模拟账户，需显式 `--port 4002`（Gateway）或 `--port 7497`（TWS）。启动 IB Gateway/TWS 时请选择对应登录模式。

**加速价格数据获取**（默认已启用）：程序默认使用 4 个并行 IB 连接同时请求不同股票的价格数据，大幅缩短 backtest 的更新时间（比原来单连接快 3~5 倍）。如果你遇到 pacing 错误或想用单连接保守模式，可显式 `--num-connections 1`。配合 `--pause-seconds 0.05` 可进一步提速。示例：
```powershell
python main.py backtest --pause-seconds 0.05
# 如需保守单连接：
# python main.py backtest --num-connections 1
```

### 想清理已生成的大文件（推荐）

执行以下命令可从 Git 跟踪中移除大文件（本地文件仍保留）：
```powershell
git rm --cached price_cache_*.parquet
git rm --cached -r nport_xmls/
git add .gitignore
git commit -m "chore: ignore large caches and outputs"
```

### 缓存文件说明（已重构）

所有缓存文件已统一迁移到 `cache/` 文件夹下，项目结构更清晰：

| 文件 | 说明 | 是否建议提交 |
|------|------|--------------|
| `cache/price_cache_*.parquet` | 历史价格缓存（增量更新） | **忽略** |
| `cache/nport.db` | 核心 NPORT 持仓 + 元数据（SQLite） | 可提交（强烈建议保留） |
| `cache/ticker_resolution_cache.json` + `manual_ticker_overrides.json` | ticker 解析 + 手动覆盖 | **忽略**（manual 可手动维护） |
| `cache/failed_*.json` | 失败记录缓存 | **忽略** |

所有代码已自动适配 `cache/` 目录。运行后新缓存会自动生成在该文件夹内。

---

## 项目组织（三大核心部分）

本项目严格按照以下三个逻辑部分组织代码：

1. **获取数据 (Data Acquisition & Caching)**
   - `data/nport_data.py`：核心 NPORT 持仓管理（自动同步 SEC、SQLite 缓存、轻量检查）
   - `data/ticker_resolver.py`：ticker 解析（IB + 手动覆盖 + 缓存）
   - `data/data_fetcher.py`：IB 价格数据获取、Parquet 增量缓存、特征预计算（4-1 动量 + RS）
   - `data/nport_universe.py`：从持仓数据构建最新/每月 universe（兼容层）

2. **实现选股策略 (Strategy)**
   - `strategy/stock_selector.py`：4-1 Momentum + RRG 相对强度 + 固定 6:4 加权（Momentum 60% + RRG 40%），无 EMA50 硬过滤；backtest 含 QQQ 50/200 MA 熊市保护

3. **选股 + 回测 (Selection & Backtesting)**
   - `backtest/tester.py`：`backtest_nport_monthly` —— 使用历史每月 NPORT universe 进行月度再平衡回测
   - `main.py`：CLI 统一入口（backtest 回测 / resolve 数据维护；rank 已移除）

```
stock_picker/
├── main.py                     # CLI 入口，编排三大模块
├── data/                       # 1. 数据获取与缓存
│   ├── nport_data.py
│   ├── data_fetcher.py
│   ├── ticker_resolver.py
│   └── nport_universe.py
├── strategy/                   # 2. 选股策略
│   └── stock_selector.py
├── backtest/                   # 3. 选股执行与回测
│   └── tester.py
├── cache/                      # 各类缓存（价格 Parquet、NPORT DB 等）
└── russell1000_backtest.html   # 示例：回测仪表盘（唯一输出，含 TV 权益曲线 + 详细月度卡片表格）
```

> 历史冗余已清理（rank 命令、旧 RRG 线性/熵权、legacy momentum 字段、旧 JSON 缓存、fetch_iwb_nport 独立脚本等）。
```
