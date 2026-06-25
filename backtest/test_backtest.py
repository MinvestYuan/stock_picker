"""测试 backtest_nport_monthly 的回测核心逻辑：换仓、成本、熊市保护、幸存者偏差。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.tester import backtest_nport_monthly


def _make_ohlc(dates: pd.DatetimeIndex, opens: list[float], closes: list[float] | None = None) -> pd.DataFrame:
    """构造 OHLC 帧；close 默认等于 open。"""
    closes = closes if closes is not None else opens
    return pd.DataFrame({
        "open": opens,
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
    }, index=dates)


def _trading_calendar() -> pd.DatetimeIndex:
    """2025-12 ~ 2026-03 简化交易日历（每月首日明确）。

    benchmark 与个股共用此日历，使买卖日（取自 benchmark）落在个股有价的日期上。
    含 2025-12 作为前置，保证 2026-01 的 selection_date 之前有可用数据。
    """
    return pd.DatetimeIndex([
        "2025-12-01", "2025-12-15", "2025-12-31",
        "2026-01-02", "2026-01-15", "2026-01-30",
        "2026-02-02", "2026-02-13", "2026-02-27",
        "2026-03-02", "2026-03-13", "2026-03-31",
    ])


def _bullish_features(dates: pd.DatetimeIndex, tickers: list[str]) -> dict:
    """构造可通过 score_universe 的 features（全有效）。"""
    features = {}
    for i, t in enumerate(tickers):
        features[t] = pd.DataFrame({
            "close": np.linspace(100, 110, len(dates)),
            "ema50": np.linspace(95, 100, len(dates)),
            "momentum": [0.1 + 0.05 * i] * len(dates),
            "rs_ratio": [100.0 + i] * len(dates),
            "rs_momentum": [100.0 + i] * len(dates),
        }, index=dates)
    return features


def _qqq_bull(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """QQQ 持续上涨 → 50EMA > 200EMA → 非熊市。"""
    return _make_ohlc(dates, list(np.linspace(100, 200, len(dates))))


def _qqq_bear(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """QQQ 持续下跌 → 50EMA < 200EMA → 熊市。"""
    return _make_ohlc(dates, list(np.linspace(200, 100, len(dates))))


def test_backtest_basic_return_and_cost():
    """基础：单股每月 +10%，验证收益扣除双边交易成本。"""
    dates = _trading_calendar()
    price_map = {
        "AAA": _make_ohlc(dates, [100, 100, 100, 100, 100, 100, 110, 110, 110, 121, 121, 121]),
        "QQQ": _qqq_bull(dates),
    }
    features = _bullish_features(dates, ["AAA"])
    monthly_universes = {"2026-01": ["AAA"], "2026-02": ["AAA"]}

    cost = 0.001
    df_summary, df_detail = backtest_nport_monthly(
        price_map=price_map,
        features=features,
        monthly_universes=monthly_universes,
        benchmark_ticker="QQQ",
        top_n=1,
        cost_per_trade=cost,
    )
    assert not df_summary.empty
    # 2026-01：买入日 01-02 开盘 100，卖出日 02-02 开盘 110 → 毛收益 0.10
    jan = df_summary[df_summary["month"] == "2026-01"].iloc[0]
    expected_net = 0.10 - 2 * cost
    assert abs(jan["monthly_return"] - expected_net) < 1e-9, jan["monthly_return"]


def test_backtest_bear_market_holds_cash():
    """熊市保护：QQQ 50EMA<200EMA 时持现金，月收益为 0。"""
    dates = _trading_calendar()
    price_map = {
        "AAA": _make_ohlc(dates, [100, 100, 100, 100, 100, 100, 110, 110, 110, 121, 121, 121]),
        "QQQ": _qqq_bear(dates),
    }
    features = _bullish_features(dates, ["AAA"])
    monthly_universes = {"2026-01": ["AAA"], "2026-02": ["AAA"]}

    df_summary, df_detail = backtest_nport_monthly(
        price_map=price_map,
        features=features,
        monthly_universes=monthly_universes,
        benchmark_ticker="QQQ",
        top_n=1,
    )
    assert not df_summary.empty
    # 熊市所有月份持现金
    assert (df_summary["monthly_return"] == 0.0).all()
    assert df_summary["top_tickers"].str.contains("CASH").all()


def test_backtest_survivorship_missing_price_as_cash():
    """幸存者偏差：入选股无可交易价格时按现金(0)计入，不向下替补。"""
    dates = _trading_calendar()
    # BBB 入选但 price_map 里完全没有它的价格 → 应按现金计入
    price_map = {
        "QQQ": _qqq_bull(dates),
        # 注意：没有 BBB 的价格
    }
    features = _bullish_features(dates, ["BBB"])
    monthly_universes = {"2026-01": ["BBB"]}

    df_summary, df_detail = backtest_nport_monthly(
        price_map=price_map,
        features=features,
        monthly_universes=monthly_universes,
        benchmark_ticker="QQQ",
        top_n=1,
    )
    assert not df_summary.empty
    jan = df_summary[df_summary["month"] == "2026-01"].iloc[0]
    # 无价仓位按现金 0 计入
    assert jan["monthly_return"] == 0.0
    assert "无价" in jan["top_tickers"] or "现金" in jan["top_tickers"]
    # 诊断元数据记录了无价仓位
    diag = df_summary.attrs.get("survivorship_diagnostic", {})
    assert diag.get("missing_slots", 0) >= 1


def test_backtest_empty_universe():
    """边界：空 universe 返回空结果，不崩溃。"""
    dates = _trading_calendar()
    price_map = {"QQQ": _qqq_bull(dates)}
    df_summary, df_detail = backtest_nport_monthly(
        price_map=price_map,
        features={},
        monthly_universes={"2026-01": []},
        benchmark_ticker="QQQ",
        top_n=1,
    )
    assert df_summary.empty


def test_backtest_missing_benchmark_raises():
    """边界：benchmark 不在 price_map 时抛 ValueError。"""
    try:
        backtest_nport_monthly(
            price_map={"AAA": _make_ohlc(_trading_calendar(), [100] * 12)},
            features={},
            monthly_universes={"2026-01": ["AAA"]},
            benchmark_ticker="QQQ",
            top_n=1,
        )
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_backtest_cumulative_return_compounding():
    """验证累计收益为月度收益的复利。"""
    dates = _trading_calendar()
    price_map = {
        "AAA": _make_ohlc(dates, [100, 100, 100, 100, 100, 100, 110, 110, 110, 121, 121, 121]),
        "QQQ": _qqq_bull(dates),
    }
    features = _bullish_features(dates, ["AAA"])
    monthly_universes = {"2026-01": ["AAA"], "2026-02": ["AAA"]}

    df_summary, _ = backtest_nport_monthly(
        price_map=price_map,
        features=features,
        monthly_universes=monthly_universes,
        benchmark_ticker="QQQ",
        top_n=1,
        cost_per_trade=0.0,
    )
    # 累计收益 == prod(1+月收益) - 1
    expected_cum = (1 + df_summary["monthly_return"]).prod() - 1
    assert abs(df_summary["cumulative_return"].iloc[-1] - expected_cum) < 1e-9


if __name__ == "__main__":
    test_backtest_basic_return_and_cost()
    test_backtest_bear_market_holds_cash()
    test_backtest_survivorship_missing_price_as_cash()
    test_backtest_empty_universe()
    test_backtest_missing_benchmark_raises()
    test_backtest_cumulative_return_compounding()
    print("ok - backtest/tester")
