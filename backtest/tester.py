from __future__ import annotations
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from config import COST_PER_TRADE, DEFAULT_BENCHMARK, DEFAULT_TOP_N
from strategy.risk_overlay import is_qqq_bear_market
from strategy.stock_selector import score_universe
from utils.logconf import get_logger

logger = get_logger(__name__)


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


# 缓存规整后的 OHLC（按对象 id + 长度），避免 open_at 每次调用都
# copy + to_datetime + sort_index。对于已规整且升序的索引（缓存加载的
# 价格即如此），直接复用原对象，不额外占内存。
_NORM_CACHE: dict[tuple[int, int], pd.DataFrame] = {}


def _normalize_ohlc(pseries: pd.DataFrame) -> pd.DataFrame:
    cache_key = (id(pseries), len(pseries))
    cached = _NORM_CACHE.get(cache_key)
    if cached is not None:
        return cached
    idx = pseries.index
    if isinstance(idx, pd.DatetimeIndex) and idx.is_monotonic_increasing and idx.equals(idx.normalize()):
        out = pseries
    else:
        out = pseries.copy()
        out.index = pd.to_datetime(out.index).normalize()
        out = out.sort_index()
    _NORM_CACHE[cache_key] = out
    return out


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
    top_n: int = DEFAULT_TOP_N,
    momentum_col: str = "momentum",
    cost_per_trade: float = COST_PER_TRADE,
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

        if is_qqq_bear_market(price_map, asof_date):
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
            logger.warning(
                "%s: top%d 中有 %d 只无可交易价格，按现金(0)计入（避免向下替补造成幸存者偏差）",
                month_str, top_n, month_missing,
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
        logger.warning("没有找到可回测的月份")
        return pd.DataFrame(), pd.DataFrame()

    df_summary = pd.DataFrame(summary_rows).sort_values("month").reset_index(drop=True)
    df_detail = pd.DataFrame(detail_rows)

    df_summary["cumulative_return"] = (1 + df_summary["monthly_return"]).cumprod() - 1

    worst_monthly_return = df_summary["monthly_return"].min()

    months = len(df_summary)
    logger.info("=== NPORT 持仓月度回测完成（%s ~ %s）===", df_summary['month'].iloc[0], df_summary['month'].iloc[-1])
    logger.info("测试月份数量 : %d", months)
    logger.info("平均月度回报 : %.4f%%", df_summary['monthly_return'].mean() * 100)
    logger.info("累计回报     : %.2f%%", df_summary['cumulative_return'].iloc[-1] * 100)
    if months > 1:
        ann_ret = (1 + df_summary['cumulative_return'].iloc[-1]) ** (12 / months) - 1
        logger.info("年化回报（近似）: %.4f%%", ann_ret * 100)
    logger.info("胜率         : %.1f%%", (df_summary['monthly_return'] > 0).mean() * 100)
    logger.info("最差月度回报 : %.2f%%", worst_monthly_return * 100)

    # 幸存者偏差诊断：无价仓位占比越高，回测收益越不可信
    if total_pick_slots > 0:
        miss_pct = total_missing_slots / total_pick_slots
        logger.info("无价仓位(按现金计) : %d/%d (%.2f%%)", total_missing_slots, total_pick_slots, miss_pct * 100)
        if miss_pct > 0.02:
            logger.warning("无价仓位占比偏高：退市/停牌股缺失会影响回测可信度，建议补充含退市历史的价格源")

    # 计算最新年份的 YTD 收益（动态取数据中最新月份的年份，避免写死年份）
    latest_year = df_summary['month'].iloc[-1][:4]
    ytd_df = df_summary[df_summary['month'].str.startswith(latest_year)]
    if not ytd_df.empty:
        ytd = (1 + ytd_df['monthly_return']).prod() - 1
        logger.info("%s YTD 今年收益 : %.4f%% (截至 %s)", latest_year, ytd * 100, ytd_df['month'].iloc[-1])
    else:
        logger.info("%s YTD 今年收益 : 无数据", latest_year)

    # 将幸存者偏差诊断统计附加到 df_summary 元数据，供 HTML 报告使用
    df_summary.attrs["survivorship_diagnostic"] = {
        "total_pick_slots": total_pick_slots,
        "missing_slots": total_missing_slots,
        "missing_pct": total_missing_slots / total_pick_slots if total_pick_slots > 0 else 0.0,
    }

    return df_summary, df_detail
