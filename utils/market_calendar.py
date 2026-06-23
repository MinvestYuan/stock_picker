"""美股交易日历工具（默认 NYSE）。

用 ``pandas_market_calendars`` 计算真实交易日，避免把节假日当成交易日；
若该库不可用或出错，则回退到 ``BDay``（仅跳过周末）。
"""

from __future__ import annotations

import pandas as pd

from config import MARKET_CALENDAR
from utils.logconf import get_logger

logger = get_logger(__name__)

# None=未尝试加载；False=加载失败（用回退）；否则为日历对象
_CAL = None


def _get_calendar():
    global _CAL
    if _CAL is None:
        try:
            import pandas_market_calendars as mcal

            _CAL = mcal.get_calendar(MARKET_CALENDAR)
        except Exception as e:
            logger.warning("加载市场日历 %s 失败，回退到工作日: %s", MARKET_CALENDAR, e)
            _CAL = False
    return _CAL


def next_trading_day(asof: pd.Timestamp) -> pd.Timestamp:
    """返回 asof 之后的下一个交易日（不含 asof，tz-naive、已 normalize）。"""
    asof = pd.Timestamp(asof)
    if asof.tz is not None:
        asof = asof.tz_localize(None)
    asof = asof.normalize()

    cal = _get_calendar()
    if cal:
        try:
            start = asof + pd.Timedelta(days=1)
            end = asof + pd.Timedelta(days=15)  # 足以跨过最长的连假
            valid = cal.valid_days(start_date=start, end_date=end)
            if len(valid) > 0:
                nxt = pd.Timestamp(valid[0])
                if nxt.tz is not None:
                    nxt = nxt.tz_convert(None)
                return nxt.normalize()
        except Exception as e:
            logger.warning("市场日历计算下一个交易日失败，回退到工作日: %s", e)

    return (asof + pd.offsets.BDay(1)).normalize()
