from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Sequence
import pandas as pd
import numpy as np

from config import MOMENTUM_WEIGHT, RRG_WEIGHT


@dataclass
class PickRow:
    ticker: str
    close: float
    momentum_score: float
    rrg_score: float
    total_score: float
    close_over_ema50: float
    rs_ratio: float
    rs_momentum: float


def min_max_normalize(values: list[float]) -> np.ndarray:
    """单个指标 min-max 标准化到 [0, 1]"""
    arr = np.array(values, dtype=float)
    min_val = np.min(arr)
    max_val = np.max(arr)
    if max_val - min_val == 0:
        return np.full_like(arr, 0.5)
    return (arr - min_val) / (max_val - min_val)


def score_universe(
        features: Dict[str, pd.DataFrame],
        asof_date: pd.Timestamp,
        min_close_over_ema50: float = 0.0,
        momentum_col: str = "momentum",
) -> List[PickRow]:
    """4-1 动量 + 相对强度（RS） + 固定比例加权（momentum 60% + RRG 40%），无 EMA50 硬过滤"""
    valid_data = []
    for ticker, frame in features.items():
        frame = frame.loc[:asof_date]
        if frame.empty:
            continue
        row = frame.iloc[-1]

        required = ["close", "ema50", "rs_ratio", "rs_momentum", momentum_col]
        # 缺列（KeyError）或值为 NaN 都视为数据不全，跳过该股票而非崩溃
        if any(col not in row.index or pd.isna(row[col]) for col in required):
            continue

        # 移除了 50 日均线硬性过滤（按用户要求）
        close_over_ema50 = float(row["close"] / row["ema50"])

        momentum_ratio = float(row[momentum_col])
        rrg_combined = float(row["rs_ratio"]) + float(row["rs_momentum"])

        valid_data.append({
            'ticker': ticker,
            'close': float(row["close"]),
            'momentum_ratio': momentum_ratio,
            'rs_ratio': float(row["rs_ratio"]),
            'rs_momentum': float(row["rs_momentum"]),
            'rrg_combined': rrg_combined,
            'close_over_ema50': close_over_ema50,  # 存下来供 PickRow 使用
        })

    if not valid_data:
        return []

    momentum_ratios = [item['momentum_ratio'] for item in valid_data]
    rrg_combined = [item['rrg_combined'] for item in valid_data]

    # 固定比例：4-1 momentum 与 RRG (rs_ratio + rs_momentum) 按 config 权重加权
    mom_norm = min_max_normalize(momentum_ratios)
    rrg_norm = min_max_normalize(rrg_combined)
    total_scores = MOMENTUM_WEIGHT * mom_norm + RRG_WEIGHT * rrg_norm

    picks: List[PickRow] = []
    for i, item in enumerate(valid_data):
        picks.append(
            PickRow(
                ticker=item['ticker'],
                close=item['close'],
                momentum_score=float(mom_norm[i]),
                rrg_score=float(rrg_norm[i]),
                total_score=float(total_scores[i]),
                close_over_ema50=item['close_over_ema50'],
                rs_ratio=item['rs_ratio'],
                rs_momentum=item['rs_momentum'],
            )
        )

    return sorted(
        picks,
        key=lambda x: (x.total_score, x.rrg_score, x.momentum_score, x.ticker),
        reverse=True,
    )


def pick_rows_to_frame(picks: Sequence[PickRow]) -> pd.DataFrame:
    """保持原来的 DataFrame 输出格式"""
    return pd.DataFrame(
        [
            {
                "ticker": p.ticker,
                "close": p.close,
                "momentum_score": p.momentum_score,
                "rrg_score": p.rrg_score,
                "total_score": p.total_score,
                "close_over_ema50": p.close_over_ema50,
                "rs_ratio": p.rs_ratio,
                "rs_momentum": p.rs_momentum,
            }
            for p in picks
        ]
    )
