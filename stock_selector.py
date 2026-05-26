from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Sequence

import pandas as pd

# ==================== 用户修改版评分规则常量 ====================
MOMENTUM_INTERCEPT = 1.5883847005580216
MOMENTUM_EMA50 = 0.14489851
RRG_INTERCEPT = 1.8602
RRG_RS_RATIO = 0.0393
RRG_RS_MOMENTUM = -0.0382

MOMENTUM_MIN = 0.0
MOMENTUM_MAX = 2.0
RRG_MIN = 0.0
RRG_MAX = 3.0


@dataclass
class PickRow:
    ticker: str
    close: float
    momentum_score: float
    rrg_score: float
    total_score: float
    ret126: float
    ret252: float
    close_over_ema50: float
    rel_ret63: float
    rel_ret4w: float
    rs_ratio: float
    rs_momentum: float


def clip(value: float, low: float, high: float) -> float:
    return float(min(max(value, low), high))


def compute_momentum_score(row: pd.Series) -> float:
    """【修改后】仅使用 50 日均线 + 原拟合截距，归一化到 [0, 1]"""
    raw = (
        MOMENTUM_INTERCEPT
        + MOMENTUM_EMA50 * (row["close"] / row["ema50"] - 1.0)
    )
    clipped = clip(raw, MOMENTUM_MIN, MOMENTUM_MAX)
    return clipped / MOMENTUM_MAX


def compute_rrg_score(row: pd.Series) -> float:
    """【修改后】仅使用 RRG_RS_RATIO + RRG_RS_MOMENTUM + 原拟合截距，归一化到 [0, 1]"""
    raw = (
        RRG_INTERCEPT
        + RRG_RS_RATIO * (row["rs_ratio"] - 100.0)
        + RRG_RS_MOMENTUM * (row["rs_momentum"] - 100.0)
    )
    clipped = clip(raw, RRG_MIN, RRG_MAX)
    return clipped / RRG_MAX


def score_universe(features: Dict[str, pd.DataFrame], asof_date: pd.Timestamp) -> List[PickRow]:
    picks: List[PickRow] = []
    for ticker, frame in features.items():
        frame = frame.loc[:asof_date]
        if frame.empty:
            continue
        row = frame.iloc[-1]
        required = [
            "close", "ema50", "ret126", "ret252",
            "rel_ret63", "rel_ret4w", "rs_ratio", "rs_momentum"
        ]
        if any(pd.isna(row[col]) for col in required):
            continue

        momentum_score = compute_momentum_score(row)
        rrg_score = compute_rrg_score(row)
        total_score = momentum_score * 2 + rrg_score * 3

        picks.append(
            PickRow(
                ticker=ticker,
                close=float(row["close"]),
                momentum_score=momentum_score,
                rrg_score=rrg_score,
                total_score=total_score,
                ret126=float(row["ret126"]),
                ret252=float(row["ret252"]),
                close_over_ema50=float(row["close"] / row["ema50"]),
                rel_ret63=float(row["rel_ret63"]),
                rel_ret4w=float(row["rel_ret4w"]),
                rs_ratio=float(row["rs_ratio"]),
                rs_momentum=float(row["rs_momentum"]),
            )
        )
    return sorted(
        picks,
        key=lambda x: (x.total_score, x.rrg_score, x.momentum_score, x.ticker),
        reverse=True,
    )


def pick_rows_to_frame(picks: Sequence[PickRow]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": p.ticker,
                "close": p.close,
                "momentum_score": p.momentum_score,
                "rrg_score": p.rrg_score,
                "total_score": p.total_score,
                "ret126": p.ret126,
                "ret252": p.ret252,
                "close_over_ema50": p.close_over_ema50,
                "rel_ret63": p.rel_ret63,
                "rel_ret4w": p.rel_ret4w,
                "rs_ratio": p.rs_ratio,
                "rs_momentum": p.rs_momentum,
            }
            for p in picks
        ]
    )