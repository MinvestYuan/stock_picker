"""集中配置：IB 连接、交易成本、策略权重、技术指标周期、SEC/NPORT 等。

所有可调参数集中在此，便于复现实验与调参。其他模块统一从这里导入，
避免魔法数字散落各处。
"""

from __future__ import annotations

# ==================== IB Gateway 连接 ====================
IB_HOST = "127.0.0.1"
IB_PORT = 4002
IB_CLIENT_ID = 17
IB_NUM_CONNECTIONS = 4
IB_PAUSE_SECONDS = 0.25
# 并行拉取 clientId 偏移，避免 TickerResolver 与价格拉取互相冲突
PRICE_CLIENT_ID_OFFSET = 10
TICKER_CLIENT_ID_OFFSET = 1

# ==================== 基准 & 数据范围 ====================
DEFAULT_BENCHMARK = "SPY"
EXTRA_BENCHMARKS = ["QQQ", "SOXX"]
DEFAULT_DURATION = "11 Y"
DEFAULT_START_MONTH = "2019-12"

# ==================== 交易成本 ====================
COST_PER_TRADE = 0.001  # 单边（买/卖各扣一次）

# ==================== 策略：持仓数量 ====================
DEFAULT_TOP_N = 5

# ==================== 策略：评分权重（固定比例） ====================
MOMENTUM_WEIGHT = 0.6
RRG_WEIGHT = 0.4

# ==================== 策略：4-1 动量回看（交易日） ====================
MOMENTUM_LONG_LOOKBACK = 84   # ~4 个月
MOMENTUM_SHORT_LOOKBACK = 21  # ~1 个月

# ==================== 技术指标周期 ====================
FEATURE_EMA_SPAN = 50
RS_RATIO_WINDOW = 26
RS_MOMENTUM_WINDOW = 13

# ==================== 熊市保护（QQQ 50/200 EMA） ====================
BEAR_OVERLAY_TICKER = "QQQ"
BEAR_FAST_EMA_SPAN = 50
BEAR_SLOW_EMA_SPAN = 200

# ==================== SEC / NPORT ====================
SEC_CIK = "0001100663"
SEC_SERIES_ID = "S000004347"
NPORT_BOOTSTRAP_START_DATE = "2019-01-01"
SEC_REQUEST_DELAY = 0.2

# ==================== 市场日历 ====================
MARKET_CALENDAR = "NYSE"
