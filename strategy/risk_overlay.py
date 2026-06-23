"""QQQ 50/200 EMA 熊市保护（回测、前向信号、MTD 展示共用）。"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from config import (
    BEAR_FAST_EMA_SPAN as FAST_EMA_SPAN,
    BEAR_OVERLAY_TICKER,
    BEAR_SLOW_EMA_SPAN as SLOW_EMA_SPAN,
)

# 缓存：按 overlay 价格对象的 id + 长度 缓存 (fast_ema, slow_ema)，
# 避免回测中每个月都对整条序列重算 EMA50/200。键用稳定的 price_map[ticker]
# 对象（同一次运行内 id 不变），而非每次访问都新建的 df["close"]。
_EMA_CACHE: dict[tuple[int, int], tuple[pd.Series, pd.Series]] = {}


def _extract_close(ohlc_or_series: pd.DataFrame | pd.Series) -> pd.Series:
    if isinstance(ohlc_or_series, pd.DataFrame) and "close" in ohlc_or_series.columns:
        return ohlc_or_series["close"]
    return ohlc_or_series


def _get_emas(overlay_obj: pd.DataFrame | pd.Series) -> tuple[pd.Series, pd.Series]:
    """返回 (ema_fast, ema_slow)，对同一个 overlay 价格对象只计算一次。"""
    cache_key = (id(overlay_obj), len(overlay_obj))
    cached = _EMA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    close = _extract_close(overlay_obj)
    ema_fast = close.ewm(span=FAST_EMA_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA_SPAN, adjust=False).mean()
    _EMA_CACHE[cache_key] = (ema_fast, ema_slow)
    return ema_fast, ema_slow


def is_qqq_bear_market(
    price_map: Dict[str, pd.DataFrame],
    asof_date: pd.Timestamp,
    overlay_ticker: str = BEAR_OVERLAY_TICKER,
) -> bool:
    """QQQ 50EMA < 200EMA 时为 True，策略应持现金。"""
    if overlay_ticker not in price_map:
        return False
    ema_fast, ema_slow = _get_emas(price_map[overlay_ticker])
    try:
        fast = float(ema_fast.loc[:asof_date].iloc[-1])
        slow = float(ema_slow.loc[:asof_date].iloc[-1])
        return fast < slow
    except Exception:
        return False
