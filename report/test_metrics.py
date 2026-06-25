"""测试 report.builder 的指标计算与边界情况。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report.builder import _calculate_metrics, _drawdown_from_cumulative


def test_calculate_metrics_normal():
    """正常情况：12 个月正负混合收益，检查各指标合理。"""
    returns = pd.Series([0.05, -0.02, 0.03, 0.01, -0.04, 0.06,
                         0.02, -0.01, 0.04, 0.03, -0.03, 0.05])
    m = _calculate_metrics(returns)
    # total_return = prod(1+r) - 1
    expected_total = (1 + returns).prod() - 1
    assert abs(m["total_return"] - expected_total) < 1e-9
    # 12 个月 → CAGR == total_return
    assert abs(m["cagr"] - expected_total) < 1e-6
    # 波动率、夏普为正
    assert m["volatility"] > 0
    assert m["sharpe"] > 0
    # 胜率 = 正收益占比
    assert abs(m["win_rate"] - (returns > 0).mean()) < 1e-9
    # 最大回撤应 <= 0
    assert m["max_drawdown"] <= 0


def test_calculate_metrics_empty():
    """边界：空序列返回全 0，不崩溃。"""
    m = _calculate_metrics(pd.Series([], dtype=float))
    assert m["total_return"] == 0.0
    assert m["sharpe"] == 0.0
    assert m["max_drawdown"] == 0.0


def test_calculate_metrics_all_nan():
    """边界：全 NaN（dropna 后为空）返回全 0。"""
    m = _calculate_metrics(pd.Series([np.nan, np.nan, np.nan]))
    assert m["total_return"] == 0.0
    assert m["volatility"] == 0.0


def test_calculate_metrics_single_month():
    """边界：单月收益，波动率为 0（n<=1），夏普应为 0 而非崩溃/inf。"""
    m = _calculate_metrics(pd.Series([0.05]))
    assert m["volatility"] == 0.0
    assert m["sharpe"] == 0.0  # 波动率 0 时夏普取 0
    assert abs(m["total_return"] - 0.05) < 1e-9


def test_calculate_metrics_zero_volatility():
    """边界：所有月收益相同（零波动），夏普不应为 inf。"""
    m = _calculate_metrics(pd.Series([0.02, 0.02, 0.02, 0.02]))
    assert m["volatility"] == 0.0
    assert m["sharpe"] == 0.0
    assert np.isfinite(m["sharpe"])


def test_calculate_metrics_all_negative():
    """边界：全负收益，profit_factor 应为 0（无盈利），sortino/calmar 有限。"""
    m = _calculate_metrics(pd.Series([-0.05, -0.03, -0.02, -0.04]))
    assert m["total_return"] < 0
    assert m["profit_factor"] == 0.0  # 无盈利
    assert np.isfinite(m["sortino"])
    assert np.isfinite(m["calmar"])
    assert m["max_drawdown"] < 0


def test_calculate_metrics_all_positive():
    """边界：全正收益，无回撤，calmar 应为 0（max_drawdown≈0 时保护除零）。"""
    m = _calculate_metrics(pd.Series([0.01, 0.02, 0.03, 0.01]))
    assert m["total_return"] > 0
    # 全正收益无回撤 → max_drawdown == 0 → calmar 走除零保护返回 0
    assert m["calmar"] == 0.0
    # 无亏损 → profit_factor 走 99.99 上限分支
    assert m["profit_factor"] == 99.99


def test_calculate_metrics_one_negative_no_sortino():
    """边界：只有 1 个负收益（<2），下行偏差不足，sortino 应为 0。"""
    m = _calculate_metrics(pd.Series([0.05, 0.03, -0.02, 0.04]))
    assert m["sortino"] == 0.0  # 负收益样本 < 2


def test_drawdown_from_cumulative():
    """回撤计算：累计收益序列 → 回撤序列。"""
    # 累计收益：0% → 10% → 5%（回撤）→ 20%
    cum = pd.Series([0.0, 0.10, 0.05, 0.20])
    dd = _drawdown_from_cumulative(cum)
    # equity = [1.0, 1.1, 1.05, 1.2]，peak = [1.0, 1.1, 1.1, 1.2]
    # dd = [0, 0, (1.05-1.1)/1.1, 0]
    assert dd.iloc[0] == 0.0
    assert dd.iloc[1] == 0.0
    assert abs(dd.iloc[2] - (1.05 - 1.1) / 1.1) < 1e-9
    assert dd.iloc[3] == 0.0
    # 回撤序列恒 <= 0
    assert (dd <= 1e-12).all()


if __name__ == "__main__":
    test_calculate_metrics_normal()
    test_calculate_metrics_empty()
    test_calculate_metrics_all_nan()
    test_calculate_metrics_single_month()
    test_calculate_metrics_zero_volatility()
    test_calculate_metrics_all_negative()
    test_calculate_metrics_all_positive()
    test_calculate_metrics_one_negative_no_sortino()
    test_drawdown_from_cumulative()
    print("ok - report/builder metrics")
