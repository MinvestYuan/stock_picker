from __future__ import annotations
from typing import Dict, Tuple

import numpy as np
import pandas as pd
import sys
from pathlib import Path

from data_fetcher import load_history_rows, DEFAULT_BENCHMARK
from stock_selector import score_universe


def evaluate_against_history(features: Dict[str, pd.DataFrame], history_file: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    # （保持不变）
    history = load_history_rows(history_file)
    rows: List[dict] = []
    for entry_ts, group in history.groupby("entry_ts"):
        selection_date = pd.to_datetime(entry_ts) - pd.Timedelta(days=1)
        ranked = score_universe(features, selection_date)
        predicted = [p.ticker for p in ranked[:5]]
        actual = sorted(group["ticker"].tolist())
        overlap = len(set(predicted) & set(actual))
        rows.append({
            "entry_ts": pd.to_datetime(entry_ts).date().isoformat(),
            "month": group["month"].iloc[0],
            "actual": ",".join(actual),
            "predicted": ",".join(predicted),
            "overlap_count": overlap,
            "overlap_ratio": overlap / 5.0,
        })
    detail = pd.DataFrame(rows).sort_values("entry_ts").reset_index(drop=True)
    summary = pd.DataFrame([{
        "months": len(detail),
        "avg_overlap_count": detail["overlap_count"].mean(),
        "avg_overlap_ratio": detail["overlap_ratio"].mean(),
        "perfect_5_of_5_months": int((detail["overlap_count"] == 5).sum()),
        "at_least_3_of_5_months": int((detail["overlap_count"] >= 3).sum()),
    }])
    return summary, detail


def backtest_monthly_returns(
    price_map: Dict[str, pd.Series],
    features: Dict[str, pd.DataFrame],
    benchmark_ticker: str = DEFAULT_BENCHMARK,
    start_month: str | None = None,
    top_n: int = 10,
    years: int = 1,               # ← 新增：默认 1 年，可指定 6
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """返回月度汇总 + 个股明细，支持过去任意年数回测"""
    if benchmark_ticker not in price_map:
        raise ValueError(f"Benchmark {benchmark_ticker} not in price_map")

    dates: pd.DatetimeIndex = price_map[benchmark_ticker].index.sort_values()

    if start_month is None:
        latest = dates.max()
        start_date = latest - pd.DateOffset(years=years)
        start_month = start_date.strftime("%Y-%m")
        print(f"[info] 自动回测过去 {years} 年 → 从 {start_month} 开始", file=sys.stderr)

    test_start = pd.to_datetime(start_month + "-01")
    month_starts = pd.date_range(start=test_start, end=dates.max(), freq="MS")

    summary_rows: List[dict] = []
    detail_rows: List[dict] = []

    for month_start in month_starts:
        next_month_start = month_start + pd.offsets.MonthBegin(1)
        month_mask = (dates >= month_start) & (dates < next_month_start)
        month_dates = dates[month_mask]
        if len(month_dates) < 2:
            continue
        buy_date = month_dates[0]
        sell_date = month_dates[-1]
        selection_date = buy_date - pd.Timedelta(days=1)
        available_dates = dates[dates <= selection_date]
        if available_dates.empty:
            continue
        asof_date = available_dates.max()

        ranked = score_universe(features, asof_date)
        top_picks = ranked[:top_n]
        if not top_picks:
            continue

        tickers = [p.ticker for p in top_picks]
        rets: List[float] = []
        selected: List[str] = []

        for ticker in tickers:
            if ticker not in price_map:
                continue
            pseries = price_map[ticker]
            try:
                buy_price = float(pseries.loc[buy_date])
                sell_price = float(pseries.loc[sell_date])
                ret = (sell_price / buy_price) - 1.0
                rets.append(ret)
                selected.append(ticker)

                detail_rows.append({
                    "month": month_start.strftime("%Y-%m"),
                    "ticker": ticker,
                    "buy_date": buy_date.date().isoformat(),
                    "buy_price": round(buy_price, 4),
                    "sell_date": sell_date.date().isoformat(),
                    "sell_price": round(sell_price, 4),
                    "monthly_return": round(ret, 6),
                })
            except KeyError:
                continue

        if not rets:
            continue

        monthly_ret = float(np.mean(rets))
        summary_rows.append({
            "month": month_start.strftime("%Y-%m"),
            "asof_date": asof_date.date().isoformat(),
            "buy_date": buy_date.date().isoformat(),
            "sell_date": sell_date.date().isoformat(),
            "num_stocks": len(rets),
            "monthly_return": monthly_ret,
            "top_tickers": ",".join(selected),
        })

    if not summary_rows:
        print("[warn] 没有找到可回测的月份")
        return pd.DataFrame(), pd.DataFrame()

    df_summary = pd.DataFrame(summary_rows).sort_values("month").reset_index(drop=True)
    df_detail = pd.DataFrame(detail_rows)

    df_summary["cumulative_return"] = (1 + df_summary["monthly_return"]).cumprod() - 1

    # 计算最大回撤
    cum_max = df_summary["cumulative_return"].cummax()
    drawdown = df_summary["cumulative_return"] - cum_max
    max_drawdown = drawdown.min()

    # 打印摘要（新增「过去 X 年」提示）
    print(f"\n=== 策略回测完成（过去 {years} 年：{df_summary['month'].iloc[0]} ~ {df_summary['month'].iloc[-1]}）===")
    print(f"测试月份数量 : {len(df_summary)}")
    print(f"平均月度回报 : {df_summary['monthly_return'].mean():.4%}")
    print(f"累计回报     : {df_summary['cumulative_return'].iloc[-1]:.2%}")
    print(f"年化回报（近似）: {(1 + df_summary['cumulative_return'].iloc[-1]) ** (12 / len(df_summary)) - 1:.4%}")
    print(f"胜率         : {(df_summary['monthly_return'] > 0).mean():.1%}")
    print(f"最大回撤     : {max_drawdown:.2%}")

    return df_summary, df_detail