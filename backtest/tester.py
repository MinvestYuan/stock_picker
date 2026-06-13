from __future__ import annotations
from typing import Dict, Tuple

import numpy as np
import pandas as pd
import sys

from data.data_fetcher import DEFAULT_BENCHMARK
from strategy.stock_selector import score_universe


def backtest_nport_monthly(
    price_map: Dict[str, pd.DataFrame],
    features: Dict[str, pd.DataFrame],
    monthly_universes: Dict[str, List[str]],
    benchmark_ticker: str = DEFAULT_BENCHMARK,
    top_n: int = 5,
    momentum_col: str = "momentum",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    NPORT 持仓月度回测：每个月的 universe 来自 NPORT 持仓
    monthly_universes: {month_str (YYYY-MM): [ticker_list]}

    策略更新（按用户要求）：
    - 去掉 EMA50 硬性过滤
    - 动量从 6-1 改为 4-1
    - QQQ 50/200 MA 熊市保护：50MA < 200MA 时持现金，向上穿过时重启
    """
    if benchmark_ticker not in price_map:
        raise ValueError(f"Benchmark {benchmark_ticker} not in price_map")

    dates: pd.DatetimeIndex = price_map[benchmark_ticker].index.sort_values()

    summary_rows: List[dict] = []
    detail_rows: List[dict] = []

    for month_str, universe in sorted(monthly_universes.items()):
        month_start = pd.to_datetime(month_str + "-01")
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

        # 过滤出该月 universe 中可用的 features
        filtered_features = {t: features[t] for t in universe if t in features}
        if not filtered_features:
            continue

        # Risk overlay: QQQ 50/200 MA 熊市保护（按用户要求）
        # 当 QQQ 50MA < 200MA 时，全仓现金；反之重启策略
        # 注意：price_map 现为 OHLC DataFrame（为支持浏览器 K 线 MTD），需提取 close 序列
        qqq_bear = False
        if "QQQ" in price_map:
            qqq_raw = price_map["QQQ"]
            qqq_close = qqq_raw["close"] if isinstance(qqq_raw, pd.DataFrame) and "close" in qqq_raw.columns else qqq_raw
            qqq_ema50 = qqq_close.ewm(span=50, adjust=False).mean()
            qqq_ema200 = qqq_close.ewm(span=200, adjust=False).mean()
            try:
                q50 = float(qqq_ema50.loc[:asof_date].iloc[-1])
                q200 = float(qqq_ema200.loc[:asof_date].iloc[-1])
                qqq_bear = q50 < q200
            except Exception:
                qqq_bear = False

        if qqq_bear:
            # 熊市，持现金
            summary_rows.append({
                "month": month_str,
                "asof_date": asof_date.date().isoformat(),
                "buy_date": buy_date.date().isoformat(),
                "sell_date": sell_date.date().isoformat(),
                "num_stocks": 0,
                "monthly_return": 0.0,
                "top_tickers": "CASH (QQQ 50MA < 200MA)",
            })
            continue

        ranked = score_universe(filtered_features, asof_date, momentum_col=momentum_col)
        top_picks = ranked[:top_n]
        if not top_picks:
            continue

        rets: List[float] = []
        selected: List[str] = []

        for p in top_picks:
            ticker = p.ticker
            if ticker not in price_map:
                continue
            pseries = price_map[ticker]
            try:
                buy_price = float(pseries.loc[buy_date, "close"])
                sell_price = float(pseries.loc[sell_date, "close"])
                ret = (sell_price / buy_price) - 1.0
                rets.append(ret)
                selected.append(ticker)

                detail_rows.append({
                    "month": month_str,
                    "ticker": ticker,
                    "buy_date": buy_date.date().isoformat(),
                    "buy_price": round(buy_price, 4),
                    "sell_date": sell_date.date().isoformat(),
                    "sell_price": round(sell_price, 4),
                    "monthly_return": round(ret, 6),
                    "momentum_score": round(getattr(p, 'momentum_score', 0), 4),
                    "rrg_score": round(getattr(p, 'rrg_score', 0), 4),
                    "total_score": round(getattr(p, 'total_score', 0), 4),
                    "close_over_ema50": round(getattr(p, 'close_over_ema50', 0), 4),
                })
            except KeyError:
                continue

        if not rets:
            continue

        monthly_ret = float(np.mean(rets))
        summary_rows.append({
            "month": month_str,
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

    worst_monthly_return = df_summary["monthly_return"].min()

    months = len(df_summary)
    print(f"\n=== NPORT 持仓月度回测完成（{df_summary['month'].iloc[0]} ~ {df_summary['month'].iloc[-1]}）===")
    print(f"测试月份数量 : {months}")
    print(f"平均月度回报 : {df_summary['monthly_return'].mean():.4%}")
    print(f"累计回报     : {df_summary['cumulative_return'].iloc[-1]:.2%}")
    if months > 1:
        ann_ret = (1 + df_summary['cumulative_return'].iloc[-1]) ** (12 / months) - 1
        print(f"年化回报（近似）: {ann_ret:.4%}")
    print(f"胜率         : {(df_summary['monthly_return'] > 0).mean():.1%}")
    print(f"最差月度回报 : {worst_monthly_return:.2%}")

    # 计算今年 (2026) YTD 收益
    y2026 = df_summary[df_summary['month'].str.startswith('2026')]
    if not y2026.empty:
        ytd = (1 + y2026['monthly_return']).prod() - 1
        print(f"2026 YTD 今年收益 : {ytd:.4%} (截至 {y2026['month'].iloc[-1]})")
    else:
        print("2026 YTD 今年收益 : 无数据")

    return df_summary, df_detail


def backtest_monthly_returns(
    price_map: Dict[str, pd.Series],
    features: Dict[str, pd.DataFrame],
    benchmark_ticker: str = DEFAULT_BENCHMARK,
    start_month: str | None = "2020-01",
    top_n: int = 5,
    years: int | None = None,     # 已废弃；回测默认固定从 2020-01 开始
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """返回月度汇总 + 个股明细，回测默认固定从2020-01开始"""
    if benchmark_ticker not in price_map:
        raise ValueError(f"Benchmark {benchmark_ticker} not in price_map")

    dates: pd.DatetimeIndex = price_map[benchmark_ticker].index.sort_values()

    if start_month is None:
        start_month = "2020-01"
    if years is not None:
        print(f"[warn] years 参数已废弃，回测固定从2020-01开始（忽略 --years）", file=sys.stderr)

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
                buy_price = float(pseries.loc[buy_date, "close"])
                sell_price = float(pseries.loc[sell_date, "close"])
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

    worst_monthly_return = df_summary["monthly_return"].min()

    # 打印摘要
    print(f"\n=== 策略回测完成（{start_month} 至今：{df_summary['month'].iloc[0]} ~ {df_summary['month'].iloc[-1]}）===")
    print(f"测试月份数量 : {len(df_summary)}")
    print(f"平均月度回报 : {df_summary['monthly_return'].mean():.4%}")
    print(f"累计回报     : {df_summary['cumulative_return'].iloc[-1]:.2%}")
    print(f"年化回报（近似）: {(1 + df_summary['cumulative_return'].iloc[-1]) ** (12 / len(df_summary)) - 1:.4%}")
    print(f"胜率         : {(df_summary['monthly_return'] > 0).mean():.1%}")
    print(f"最差月度回报 : {worst_monthly_return:.2%}")

    return df_summary, df_detail