from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from backtest.tester import backtest_nport_monthly
from config import (
    COST_PER_TRADE,
    DEFAULT_START_MONTH,
    DEFAULT_TOP_N,
    EXTRA_BENCHMARKS,
    IB_CLIENT_ID,
    IB_HOST,
    IB_NUM_CONNECTIONS,
    IB_PAUSE_SECONDS,
    IB_PORT,
)
from data.data_fetcher import (
    DEFAULT_BENCHMARK,
    DEFAULT_DURATION,
    connect_ib,
    fetch_or_update_history,
    get_cache_data_max_age_days,
    get_cache_filename,
    load_price_cache,
    prepare_all_features,
    price_cache_exists,
    save_price_cache,
)
from data.storage import get_symbol_conid_map
from data.nport_data import get_latest_universe, get_monthly_universes, sync_holdings_if_needed
from report.builder import add_benchmark_returns, compute_next_signals, generate_backtest_html
from strategy.risk_overlay import is_qqq_bear_market
from strategy.stock_selector import score_universe
from utils.logconf import get_logger, setup_logging

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stock_picker",
        description="Russell 1000 NPORT 月度回测 + 前向信号 + HTML 报告",
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="等权持仓数量")
    parser.add_argument(
        "--start-month",
        default=DEFAULT_START_MONTH,
        help="回测起始月份（默认 2019-12）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("index.html"),
        help="HTML 报告输出路径（默认 index.html）",
    )
    return parser.parse_args()


def _load_or_update_prices(tickers: list[str], benchmark: str) -> dict:
    """加载 Parquet 缓存，逐只判断是否覆盖最近交易日后再拉 IB。"""
    cache_file = get_cache_filename(benchmark, DEFAULT_DURATION)
    price_map: dict = {}

    if price_cache_exists(benchmark):
        price_map = load_price_cache(cache_file, benchmark) or {}
        if price_map:
            max_age = get_cache_data_max_age_days(price_map)
            if max_age is not None and max_age > 7:
                logger.info("缓存数据已 %d 天未更新，将进行增量刷新", max_age)
    else:
        logger.info("本地尚无价格缓存，将从 IB 全量拉取")

    con_id_map = get_symbol_conid_map()

    ib = None
    if IB_NUM_CONNECTIONS <= 1:
        ib = connect_ib(IB_HOST, IB_PORT, IB_CLIENT_ID)
    try:
        price_map = fetch_or_update_history(
            ib=ib,
            tickers=tickers,
            end_date=None,
            duration=DEFAULT_DURATION,
            pause_seconds=IB_PAUSE_SECONDS,
            existing_price_map=price_map if price_map else None,
            host=IB_HOST,
            port=IB_PORT,
            client_id=IB_CLIENT_ID,
            num_connections=IB_NUM_CONNECTIONS,
            con_id_map=con_id_map,
        )
        save_price_cache(price_map, cache_file, benchmark)
    finally:
        if ib is not None:
            ib.disconnect()
    return price_map


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Russell 1000 NPORT 持仓月度回测 + 前向信号 + HTML 报告。"""
    benchmark = DEFAULT_BENCHMARK
    start_month = args.start_month or DEFAULT_START_MONTH
    top_n = args.top

    logger.info("NPORT 持仓月度回测，起始月: %s", start_month)

    logger.info("正在检查 Russell 1000 ETF 持仓是否有更新...")
    sync_result = sync_holdings_if_needed(
        max_age_hours=0,
        lookback_months=6,
        ib_host=IB_HOST,
        ib_port=IB_PORT,
        ib_client_id=IB_CLIENT_ID,
        ib_num_connections=IB_NUM_CONNECTIONS,
    )
    logger.info("%s", sync_result["message"])

    logger.info("正在构建每月 universe...")
    monthly_universes = get_monthly_universes(start_month=start_month)
    if not monthly_universes:
        logger.error("无法构建每月 universe，请检查 NPORT 缓存")
        return 1
    logger.info("共 %d 个月", len(monthly_universes))

    active_tickers = sorted({t for tickers in monthly_universes.values() for t in tickers})
    logger.info("回测期间共涉及 %d 只不同的股票", len(active_tickers))

    tickers = sorted(set(active_tickers + [benchmark] + EXTRA_BENCHMARKS))
    logger.info("价格目标 %d 只（含 %s、%s）", len(tickers), benchmark, "、".join(EXTRA_BENCHMARKS))

    price_map = _load_or_update_prices(tickers, benchmark)
    logger.info("价格数据就绪，共 %d 只", len(price_map))

    logger.info("正在计算特征...")
    features = prepare_all_features(price_map, benchmark)

    logger.info("开始回测...")
    df_summary, df_detail = backtest_nport_monthly(
        price_map=price_map,
        features=features,
        monthly_universes=monthly_universes,
        benchmark_ticker=benchmark,
        top_n=top_n,
        cost_per_trade=COST_PER_TRADE,
    )

    if df_summary.empty:
        logger.error("回测结果为空")
        return 1

    logger.info("正在计算 benchmark 收益...")
    df_summary = add_benchmark_returns(df_summary, price_map, benchmark, col_prefix=benchmark.lower())
    for bm in EXTRA_BENCHMARKS:
        df_summary = add_benchmark_returns(df_summary, price_map, bm, col_prefix=bm.lower())

    logger.info("正在计算下个开盘日的前向信号...")
    latest_universe = get_latest_universe()
    asof_date, next_trade_date, next_picks = compute_next_signals(
        price_map=price_map,
        features=features,
        universe_tickers=latest_universe,
        top_n=top_n,
    )
    next_date_disp = next_trade_date.date() if next_trade_date else "N/A"
    if next_picks:
        tickers_str = ", ".join(p.ticker for p in next_picks)
        logger.info("下个开盘日 (%s) 信号: %s", next_date_disp, tickers_str)
    else:
        logger.info("下个开盘日 (%s) 信号: 持现金 (QQQ 50MA < 200MA) 或无可选股", next_date_disp)

    current_month_str = datetime.now().strftime("%Y-%m")
    current_month_picks = None
    current_month_buy_date = None
    if current_month_str in monthly_universes:
        bm_dates = price_map[benchmark].index.sort_values()
        month_start = pd.to_datetime(current_month_str + "-01")
        next_month_start = month_start + pd.offsets.MonthBegin(1)
        month_dates = bm_dates[(bm_dates >= month_start) & (bm_dates < next_month_start)]
        if not month_dates.empty:
            buy_date = month_dates[0]
            selection_date = buy_date - pd.Timedelta(days=1)
            avail = bm_dates[bm_dates <= selection_date]
            if not avail.empty:
                asof_cm = avail.max()
                uni = monthly_universes[current_month_str]
                ff = {t: features[t] for t in uni if t in features}
                if ff and not is_qqq_bear_market(price_map, asof_cm):
                    ranked = score_universe(ff, asof_cm, momentum_col="momentum")
                    current_month_picks = ranked[:top_n]
                    current_month_buy_date = buy_date
                    picks_str = ", ".join(p.ticker for p in current_month_picks)
                    logger.info("本月 (%s) 月初选股: %s (基于 %s)", current_month_str, picks_str, asof_cm.date())
                elif ff:
                    logger.info("本月 (%s) 月初 QQQ 熊市保护，持现金", current_month_str)

    output_path = args.output
    output_path = output_path.with_suffix(".html") if output_path.suffix.lower() != ".html" else output_path
    generate_backtest_html(
        df_summary,
        df_detail,
        output_path,
        benchmark=benchmark,
        extra_benchmarks=EXTRA_BENCHMARKS,
        price_map=price_map,
        next_picks=next_picks,
        next_trade_date=next_trade_date,
        asof_date=asof_date,
        current_month_picks=current_month_picks,
        current_month_buy_date=current_month_buy_date,
        cost_per_trade=COST_PER_TRADE,
    )
    logger.info("HTML 已生成 → %s", output_path)
    return 0


def main() -> int:
    setup_logging()
    return cmd_dashboard(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
