"""测试 stock_selector 的评分逻辑与边界情况。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.stock_selector import min_max_normalize, score_universe


def test_min_max_normalize_normal():
    """正常情况：返回 [0, 1] 范围内的归一化值。"""
    result = min_max_normalize([1.0, 2.0, 5.0, 3.0])
    expected = np.array([0.0, 0.25, 1.0, 0.5])
    assert np.allclose(result, expected), f"Expected {expected}, got {result}"


def test_min_max_normalize_all_same():
    """边界：所有值相同时返回 0.5（中性）。"""
    result = min_max_normalize([2.0, 2.0, 2.0])
    expected = np.array([0.5, 0.5, 0.5])
    assert np.allclose(result, expected), f"Expected {expected}, got {result}"


def test_min_max_normalize_single():
    """边界：单个值时返回 0.5（中性）。"""
    result = min_max_normalize([5.0])
    assert result[0] == 0.5, f"Expected 0.5, got {result[0]}"


def test_score_universe_basic_ranking():
    """基础测试：检查评分排序是否符合预期（动量高 + RRG 强的排前面）。"""
    asof = pd.Timestamp("2026-06-20")
    # 构造 3 只股票：A 动量高、B RRG 强、C 两者均衡
    features = {
        "A": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            "momentum": [0.5],  # 高动量
            "rs_ratio": [100.0],
            "rs_momentum": [100.0],
        }, index=[asof]),
        "B": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            "momentum": [0.1],  # 低动量
            "rs_ratio": [120.0],  # 高 RRG
            "rs_momentum": [110.0],
        }, index=[asof]),
        "C": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            "momentum": [0.3],  # 中等
            "rs_ratio": [110.0],  # 中等
            "rs_momentum": [105.0],
        }, index=[asof]),
    }
    ranked = score_universe(features, asof)
    assert len(ranked) == 3
    # 检查评分字段存在且为浮点数
    for pick in ranked:
        assert hasattr(pick, "total_score")
        assert hasattr(pick, "momentum_score")
        assert hasattr(pick, "rrg_score")
        assert 0 <= pick.total_score <= 1
    # 检查排序：total_score 降序
    assert ranked[0].total_score >= ranked[1].total_score
    assert ranked[1].total_score >= ranked[2].total_score


def test_score_universe_empty_features():
    """边界：空 features 返回空列表。"""
    asof = pd.Timestamp("2026-06-20")
    ranked = score_universe({}, asof)
    assert ranked == []


def test_score_universe_missing_required_columns():
    """边界：缺失必需列（如 momentum）时跳过该股票。"""
    asof = pd.Timestamp("2026-06-20")
    features = {
        "A": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            # 缺 momentum
            "rs_ratio": [100.0],
            "rs_momentum": [100.0],
        }, index=[asof]),
        "B": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            "momentum": [0.3],
            "rs_ratio": [110.0],
            "rs_momentum": [105.0],
        }, index=[asof]),
    }
    ranked = score_universe(features, asof)
    # A 缺列，只有 B 入选
    assert len(ranked) == 1
    assert ranked[0].ticker == "B"


def test_score_universe_all_nan_values():
    """边界：某列全为 NaN 时该股票被过滤。"""
    asof = pd.Timestamp("2026-06-20")
    features = {
        "A": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            "momentum": [np.nan],  # NaN
            "rs_ratio": [100.0],
            "rs_momentum": [100.0],
        }, index=[asof]),
    }
    ranked = score_universe(features, asof)
    assert len(ranked) == 0


def test_score_universe_asof_date_before_data():
    """边界：asof_date 早于数据起始时，该股票被过滤（frame 为空）。"""
    asof = pd.Timestamp("2026-01-01")
    features = {
        "A": pd.DataFrame({
            "close": [100.0],
            "ema50": [95.0],
            "momentum": [0.3],
            "rs_ratio": [100.0],
            "rs_momentum": [100.0],
        }, index=[pd.Timestamp("2026-06-20")]),
    }
    ranked = score_universe(features, asof)
    assert len(ranked) == 0


if __name__ == "__main__":
    test_min_max_normalize_normal()
    test_min_max_normalize_all_same()
    test_min_max_normalize_single()
    test_score_universe_basic_ranking()
    test_score_universe_empty_features()
    test_score_universe_missing_required_columns()
    test_score_universe_all_nan_values()
    test_score_universe_asof_date_before_data()
    print("ok - strategy/stock_selector")
