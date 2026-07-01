"""QQQ 50/200 EMA 熊市保护（回测、前向信号、MTD 展示共用）。"""

from __future__ import annotations

import weakref
from typing import Dict

import pandas as pd

from config import (
    BEAR_FAST_EMA_SPAN as FAST_EMA_SPAN,
    BEAR_OVERLAY_TICKER,
    BEAR_SLOW_EMA_SPAN as SLOW_EMA_SPAN,
)
from utils.logconf import get_logger

logger = get_logger(__name__)

# 缓存：按 overlay 价格对象的 id + 长度 缓存 (fast_ema, slow_ema)，
# 避免回测中每个月都对整条序列重算 EMA50/200。weakref 守卫确保源对象 GC
# 后条目失效，避免 id 复用读到陈旧 EMA。
_EMA_CACHE: dict[tuple[int, int], tuple[weakref.ref, tuple[pd.Series, pd.Series]]] = {}


def _extract_close(ohlc_or_series: pd.DataFrame | pd.Series) -> pd.Series:
    if isinstance(ohlc_or_series, pd.DataFrame) and "close" in ohlc_or_series.columns:
        return ohlc_or_series["close"]
    return ohlc_or_series


def _get_emas(overlay_obj: pd.DataFrame | pd.Series) -> tuple[pd.Series, pd.Series]:
    """返回 (ema_fast, ema_slow)，对同一个 overlay 价格对象只计算一次。"""
    cache_key = (id(overlay_obj), len(overlay_obj))
    entry = _EMA_CACHE.get(cache_key)
    if entry is not None:
        obj_ref, cached = entry
        if obj_ref() is overlay_obj:
            return cached
        _EMA_CACHE.pop(cache_key, None)
    close = _extract_close(overlay_obj)
    ema_fast = close.ewm(span=FAST_EMA_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA_SPAN, adjust=False).mean()
    result = (ema_fast, ema_slow)
    _EMA_CACHE[cache_key] = (weakref.ref(overlay_obj), result)
    return result


def is_qqq_bear_market(
    price_map: Dict[str, pd.DataFrame],
    asof_date: pd.Timestamp,
    overlay_ticker: str = BEAR_OVERLAY_TICKER,
) -> bool:
    """QQQ 50EMA < 200EMA 时为 True，策略应持现金。"""
    if overlay_ticker not in price_map:
        logger.warning(
            "熊市保护：%s 价格数据缺失，保守按持现金处理",
            overlay_ticker,
        )
        return True
    ema_fast, ema_slow = _get_emas(price_map[overlay_ticker])
    try:
        fast = float(ema_fast.loc[:asof_date].iloc[-1])
        slow = float(ema_slow.loc[:asof_date].iloc[-1])
        return fast < slow
    except Exception as e:
        # 异常时保守持现金（返回 True），而非满仓（False）。
        # 静默关闭熊市保护是最危险的方向：本该避险时却满仓。
        logger.warning(
            "熊市保护判定失败（%s, asof=%s）：%s，保守按持现金处理",
            overlay_ticker, asof_date, e,
        )
        return True
