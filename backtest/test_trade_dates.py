"""验证月度换仓日期与开盘价逻辑。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.tester import open_at, resolve_month_trade_dates


def _spy_like_calendar() -> pd.DatetimeIndex:
    """2026-01 ~ 2026-03 美股交易日（含周末/节假日缺口）。"""
    return pd.DatetimeIndex([
        "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
        "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15",
        "2026-01-16", "2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23",
        "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
        "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
        "2026-02-09", "2026-02-10", "2026-02-11", "2026-02-12", "2026-02-13",
        "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
        "2026-02-24", "2026-02-25", "2026-02-26", "2026-02-27",
        "2026-03-02", "2026-03-03", "2026-03-04",
    ])


def test_resolve_month_trade_dates_2026_02():
    dates = _spy_like_calendar()
    trade = resolve_month_trade_dates(dates, "2026-02")
    assert trade is not None
    buy, sell = trade
    assert buy == pd.Timestamp("2026-02-02")
    assert sell == pd.Timestamp("2026-03-02")


def test_open_at_uses_open_not_close():
    ohlc = pd.DataFrame({
        "open": [405.49],
        "high": [430.0],
        "low": [400.0],
        "close": [423.42],
    }, index=pd.DatetimeIndex(["2026-02-02"]))
    assert open_at(ohlc, pd.Timestamp("2026-02-02")) == 405.49


def test_lite_example_return():
    ohlc = pd.DataFrame({
        "open": [405.49, 742.895],
        "high": [430.0, 750.0],
        "low": [400.0, 735.0],
        "close": [423.42, 740.0],
    }, index=pd.DatetimeIndex(["2026-02-02", "2026-03-02"]))
    buy = open_at(ohlc, pd.Timestamp("2026-02-02"))
    sell = open_at(ohlc, pd.Timestamp("2026-03-02"))
    ret = sell / buy - 1.0
    assert abs(buy - 405.49) < 0.01
    assert abs(sell - 742.895) < 0.01
    assert abs(ret - 0.8321) < 0.01


if __name__ == "__main__":
    test_resolve_month_trade_dates_2026_02()
    test_open_at_uses_open_not_close()
    test_lite_example_return()
    print("ok")