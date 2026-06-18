from __future__ import annotations
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import sys

from data.data_fetcher import DEFAULT_BENCHMARK
from strategy.stock_selector import score_universe


def resolve_month_trade_dates(dates: pd.DatetimeIndex, month_str: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """买入日 = 当月首个交易日；卖出日 = 下月首个交易日。"""
    month_start = pd.to_datetime(month_str + "-01")
    next_month_start = month_start + pd.offsets.MonthBegin(1)
    month_dates = dates[(dates >= month_start) & (dates < next_month_start)]
    if month_dates.empty:
        return None
    future_dates = dates[dates >= next_month_start]
    if future_dates.empty:
        return None
    return month_dates[0], future_dates[0]


def _normalize_ohlc(pseries: pd.DataFrame) -> pd.DataFrame:
    out = pseries.copy()
    out.index = pd.to_datetime(out.index).normalize()
    return out.sort_index()


def open_at(pseries: pd.DataFrame, dt: pd.Timestamp) -> float:
    """取指定日期的开盘价；缺失时回退到收盘价。"""
    s = _normalize_ohlc(pseries)
    dt = pd.to_datetime(dt).normalize()
    if dt not in s.index:
        raise KeyError(dt)
    row = s.loc[dt]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    if "open" in s.columns and pd.notna(row.get("open")):
        return float(row["open"])
    return float(row["close"])


def backtest_nport_monthly(
    price_map: Dict[str, pd.DataFrame],
    features: Dict[str, pd.DataFrame],
    monthly_universes: Dict[str, List[str]],
    benchmark_ticker: str = DEFAULT_BENCHMARK,
    top_n: int = 5,
    momentum_col: str = "momentum",
    cost_per_trade: float = 0.001,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    NPORT 持仓月度回测：每个月的 universe 来自 NPORT 持仓
    monthly_universes: {month_str (YYYY-MM): [ticker_list]}

    交易规则（月度换仓）：
    - 买入日 = 当月首个交易日，买入价 = 当日开盘价
    - 卖出日 = 下月首个交易日，卖出价 = 当日开盘价
    - 月度收益 = 持仓股票 (卖出价 / 买入价 - 1) 的等权平均
    - 交易成本：每笔单边 cost_per_trade（默认 0.1%），买卖各扣一次

    选股与风控：
    - 去掉 EMA50 硬性过滤
    - 动量从 6-1 改为 4-1
    - QQQ 50/200 MA 熊市保护：50MA < 200MA 时持现金，向上穿过时重启
    """
    if benchmark_ticker not in price_map:
        raise ValueError(f"Benchmark {benchmark_ticker} not in price_map")

    dates: pd.DatetimeIndex = price_map[benchmark_ticker].index.sort_values()

    summary_rows: List[dict] = []
    detail_rows: List[dict] = []

    # 幸存者偏差诊断：统计因无可交易价格而按现金计入的仓位占比
    total_pick_slots = 0
    total_missing_slots = 0

    for month_str, universe in sorted(monthly_universes.items()):
        trade_dates = resolve_month_trade_dates(dates, month_str)
        if trade_dates is None:
            continue
        buy_date, sell_date = trade_dates
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
        if not ranked:
            continue

        # 修复幸存者偏差：取实际排名的 top_n 只股票，不再因缺价而向下替补。
        # 若某只入选股票无可交易价格（多为退市/停牌/被并购，且常为亏损股），
        # 该仓位按“持现金”计入（收益 0），而不是用排名更靠后的股票顶替。
        # 顶替会系统性剔除亏损退市股，导致回测收益虚高。
        rets: List[float] = []
        selected: List[str] = []
        top_picks_used = []
        month_missing = 0

        for p in ranked[:top_n]:
            ticker = p.ticker
            buy_price = None
            sell_price = None
            if ticker in price_map:
                pseries = price_map[ticker]
                try:
                    buy_price = open_at(pseries, buy_date)
                    sell_price = open_at(pseries, sell_date)
                except KeyError:
                    buy_price = None
                    sell_price = None

            if buy_price is None or sell_price is None:
                # 无可交易价格：该仓位计为现金（收益 0），并计数用于诊断
                month_missing += 1
                total_missing_slots += 1
                rets.append(0.0)
                selected.append(f"{ticker}(无价/现金)")
                top_picks_used.append(p)
                detail_rows.append({
                    "month": month_str,
                    "ticker": ticker,
                    "buy_date": buy_date.date().isoformat(),
                    "buy_price": 0.0,
                    "sell_date": sell_date.date().isoformat(),
                    "sell_price": 0.0,
                    "monthly_return": 0.0,
                    "gross_return": 0.0,
                    "momentum_score": round(getattr(p, 'momentum_score', 0), 4),
                    "rrg_score": round(getattr(p, 'rrg_score', 0), 4),
                    "total_score": round(getattr(p, 'total_score', 0), 4),
                    "close_over_ema50": round(getattr(p, 'close_over_ema50', 0), 4),
                })
                continue

            # 扣除双边交易成本（买入滑点+佣金 + 卖出滑点+佣金）
            gross_ret = (sell_price / buy_price) - 1.0
            ret = gross_ret - 2 * cost_per_trade
            rets.append(ret)
            selected.append(ticker)
            top_picks_used.append(p)

            detail_rows.append({
                "month": month_str,
                "ticker": ticker,
                "buy_date": buy_date.date().isoformat(),
                "buy_price": round(buy_price, 4),
                "sell_date": sell_date.date().isoformat(),
                "sell_price": round(sell_price, 4),
                "monthly_return": round(ret, 6),
                "gross_return": round(gross_ret, 6),
                "momentum_score": round(getattr(p, 'momentum_score', 0), 4),
                "rrg_score": round(getattr(p, 'rrg_score', 0), 4),
                "total_score": round(getattr(p, 'total_score', 0), 4),
                "close_over_ema50": round(getattr(p, 'close_over_ema50', 0), 4),
            })

        if not rets:
            continue

        total_pick_slots += len(rets)
        if month_missing > 0:
            print(
                f"[warn] {month_str}: top{top_n} 中有 {month_missing} 只无可交易价格，"
                f"按现金(0)计入（避免向下替补造成幸存者偏差）",
                file=sys.stderr,
            )

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

    # 幸存者偏差诊断：无价仓位占比越高，回测收益越不可信
    if total_pick_slots > 0:
        miss_pct = total_missing_slots / total_pick_slots
        print(
            f"无价仓位(按现金计) : {total_missing_slots}/{total_pick_slots} ({miss_pct:.2%})"
        )
        if miss_pct > 0.02:
            print(
                "[warn] 无价仓位占比偏高：退市/停牌股缺失会影响回测可信度，"
                "建议补充含退市历史的价格源",
                file=sys.stderr,
            )

    # 计算今年 (2026) YTD 收益
    y2026 = df_summary[df_summary['month'].str.startswith('2026')]
    if not y2026.empty:
        ytd = (1 + y2026['monthly_return']).prod() - 1
        print(f"2026 YTD 今年收益 : {ytd:.4%} (截至 {y2026['month'].iloc[-1]})")
    else:
        print("2026 YTD 今年收益 : 无数据")

    # 将幸存者偏差诊断统计附加到 df_summary 元数据，供 HTML 报告使用
    df_summary.attrs["survivorship_diagnostic"] = {
        "total_pick_slots": total_pick_slots,
        "missing_slots": total_missing_slots,
        "missing_pct": total_missing_slots / total_pick_slots if total_pick_slots > 0 else 0.0,
    }

    return df_summary, df_detail
