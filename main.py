from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
import numpy as np
import sys

from data.data_fetcher import (
    build_universe,
    connect_ib,
    prepare_all_features,
    prepare_feature_frame,
    resolve_asof_date,
    get_cache_filename,
    load_price_cache,
    save_price_cache,
    fetch_or_update_history,
    get_cache_data_max_age_days,
    DEFAULT_HISTORY_FILE,
    DEFAULT_BENCHMARK,
    DEFAULT_DURATION,
    DEFAULT_MIN_MARKET_CAP,
)
from strategy.stock_selector import score_universe, pick_rows_to_frame
from backtest.tester import backtest_nport_monthly  # backtest_monthly_returns 为遗留接口，已默认固定2020-01
from data.nport_universe import (
    get_latest_universe,
    get_all_nport_tickers,
    get_monthly_universes,
)
from data.nport_data import sync_holdings_if_needed
from data.ticker_resolver import TickerResolver


RUSSELL_BACKTEST_HTML = Path("russell1000_backtest.html")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stock_picker",
        description="Russell 1000 NPORT 月度回测工具\n\n"
                    "常用命令:\n"
                    "  backtest  基于历史 NPORT 持仓的月度回测 + 单一 HTML 报告（含表格、图表、多 benchmark 对比）\n"
                    "  resolve   补全持仓 ticker（已移除 OpenFIGI，依赖 IB + 手动覆盖 + 失败缓存）"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="backtest",
        choices=["backtest", "resolve", "russell-backtest", "resolve-tickers"],
        help="主要命令（默认 backtest）：backtest=月度NPORT回测, resolve=ticker解析",
    )
    parser.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--universe-source", choices=["nport", "file", "history"], default="nport")
    parser.add_argument("--universe-file", type=Path, default=None)
    parser.add_argument("--min-market-cap", type=float, default=DEFAULT_MIN_MARKET_CAP)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", type=int, default=17)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--duration", default=DEFAULT_DURATION)
    parser.add_argument("--pause-seconds", type=float, default=0.25,
                        help="每请求完一只股票的历史数据后暂停的秒数（限速用）。默认0.25，避免触发IB Gateway/TWS的pacing限制导致请求失败或断连。网络稳定时可设为0.05~0.1加速；想非常稳妥可设0.5+。设为0则不暂停（风险自负）。")
    parser.add_argument("--num-connections", type=int, default=4,
                        help="并行使用的 IB 连接数（多连接同时获取不同股票数据，加速价格更新）。默认4（推荐，显著加快 backtest 的价格数据获取）。如果遇到 pacing 错误或想保守，可设为1。每个连接使用递增的 client-id。")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--start-month", default="2020-01", help="回测起始月份（默认固定为 2020-01，此后所有回测均从2020年开始；可覆盖）")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--use-cache", action=argparse.BooleanOptionalAction, default=True,
                        help="是否使用本地价格缓存（默认启用，使用 --no-use-cache 强制刷新）")

    # 新增：ticker 重解析命令参数
    parser.add_argument("--missing-only", action="store_true", help="仅解析缺失 ticker 的持仓")
    parser.add_argument("--force", action="store_true", help="强制重新解析所有 ticker")
    parser.add_argument("--verbose", action="store_true", help="显示详细解析来源")
    # B 改进：全量回填 + 报告
    parser.add_argument("--full-backfill", action="store_true",
                        help="对所有历史 NPORT 持仓执行完整 ticker 解析（自动去重 + 失败缓存，大幅减少重复 ISIN 请求）")
    parser.add_argument("--report", action="store_true",
                        help="生成详细的 ticker 解析质量报告（按来源、按 filing 统计）")

    return parser.parse_args()


def _load_or_update_prices(tickers, args, cache_file, duration=None):
    """Russell 命令统一价格加载逻辑：加载缓存 + 按需增量更新所有 ticker。
    增量决策直接追平“最近一个交易日”的数据（无冷却、无固定新鲜度天数检查）。"""
    price_map = {}
    if args.use_cache and cache_file.exists():
        price_map = load_price_cache(cache_file) or {}
        if price_map:
            max_age = get_cache_data_max_age_days(price_map)
            if max_age is not None and max_age > 7:
                print(f"[info] 缓存中最新的数据已 {max_age} 天未更新，将进行增量刷新", file=sys.stderr)

    print(f"[info] 正在从 IB Gateway 按需增量更新到最近一个交易日的数据（目标 {len(tickers)} 只股票）...", file=sys.stderr)
    num_conn = getattr(args, "num_connections", 4)
    ib = None
    if num_conn <= 1:
        ib = connect_ib(args.host, args.port, args.client_id)
    try:
        end_date = pd.to_datetime(args.asof) if args.asof else None
        price_map = fetch_or_update_history(
            ib=ib,
            tickers=tickers,
            end_date=end_date,
            duration=duration or args.duration,
            pause_seconds=args.pause_seconds,
            existing_price_map=price_map if price_map else None,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            num_connections=num_conn,
        )
        if cache_file:
            save_price_cache(price_map, cache_file)
    finally:
        if ib is not None:
            ib.disconnect()
    return price_map


def cmd_russell_backtest(args) -> int:
    """Russell 1000 NPORT 持仓月度回测 + 单一 HTML 报告（含汇总表格、明细、图表、多 benchmark 对比）"""
    # 固定从 2020-01 开始回测（用户要求），--start-month 可用于覆盖
    start_month = args.start_month or "2020-01"
    print(f"[info] NPORT 持仓月度回测，起始月: {start_month}（固定从2020年开始）", file=sys.stderr)

    # 自动检查持仓更新（核心需求；今天已查过SEC则直接跳过）
    print("[info] 正在检查 Russell 1000 ETF 持仓是否有更新...", file=sys.stderr)
    # 今天只查询SEC一次（同日后续运行跳过）；只看最近6个月 filing，降序早停（一旦遇到已知 filing 就暂停更早的）
    sync_result = sync_holdings_if_needed(
        max_age_hours=0, lookback_months=6,
        ib_host=args.host, ib_port=args.port, ib_client_id=args.client_id,
    )
    print(f"[info] {sync_result['message']}", file=sys.stderr)

    # 1. 获取每月 universe
    print("[info] 正在构建每月 universe...", file=sys.stderr)
    monthly_universes = get_monthly_universes(start_month=start_month)
    if not monthly_universes:
        print("[error] 无法构建每月 universe，请检查 NPORT 缓存", file=sys.stderr)
        return 1
    print(f"[info] 共 {len(monthly_universes)} 个月", file=sys.stderr)

    # 2. 获取所有需要下载的 ticker
    active_tickers = set()
    for month, tickers in monthly_universes.items():
        active_tickers.update(tickers)
    active_tickers = sorted(active_tickers)
    print(f"[info] 回测期间共涉及 {len(active_tickers)} 只不同的股票", file=sys.stderr)

    # 额外下载 QQQ (Nasdaq-100) 和 SOXX (PHLX Semiconductor Index ETF)
    extra_benchmarks = ["QQQ", "SOXX"]
    tickers = sorted(set(active_tickers + [args.benchmark] + extra_benchmarks))

    # 3. 统一加载缓存 + 增量更新（替换原来的硬编码主缓存逻辑）
    cache_file = get_cache_filename(args.benchmark, args.duration)
    price_map = _load_or_update_prices(tickers, args, cache_file)

    # 4. 预计算所有 features
    print("[info] 正在计算特征...", file=sys.stderr)
    features = prepare_all_features(price_map, args.benchmark)

    # 5. 运行回测
    print("[info] 开始回测...", file=sys.stderr)
    df_summary, df_detail = backtest_nport_monthly(
        price_map=price_map,
        features=features,
        monthly_universes=monthly_universes,
        benchmark_ticker=args.benchmark,
        top_n=args.top_n,
    )

    if df_summary.empty:
        print("[error] 回测结果为空", file=sys.stderr)
        return 1

    # 6. 计算所有 benchmark 同期收益
    print("[info] 正在计算 benchmark 收益...", file=sys.stderr)
    # 关键修复：为主 benchmark 也传入 col_prefix，使其生成 "spy_return" / "spy_cumulative"，与 HTML 期望一致
    df_summary = _add_benchmark_returns(df_summary, price_map, args.benchmark, col_prefix=args.benchmark.lower())
    for bm in extra_benchmarks:
        df_summary = _add_benchmark_returns(df_summary, price_map, bm, col_prefix=bm.lower())

    # 7. 生成 HTML 报告（统一为单一 HTML 文件，不再生成 Excel）
    output_path = args.output or Path("index.html")
    output_path = output_path.with_suffix(".html") if output_path.suffix.lower() != ".html" else output_path
    generate_backtest_html(df_summary, df_detail, output_path, benchmark=args.benchmark, extra_benchmarks=extra_benchmarks, price_map=price_map)
    print(f"[info] HTML 已生成 → {output_path}", file=sys.stderr)
    return 0


def _add_benchmark_returns(df_summary: pd.DataFrame, price_map: dict, benchmark_ticker: str, col_prefix: str | None = None) -> pd.DataFrame:
    """为 summary 添加 benchmark 的月度收益列"""
    prefix = col_prefix or "benchmark"
    if benchmark_ticker not in price_map:
        print(f"[warn] 价格缓存中不存在 benchmark {benchmark_ticker}，将使用 0 填充其收益", file=sys.stderr)
        df_summary[f"{prefix}_return"] = 0.0
        df_summary[f"{prefix}_cumulative"] = 0.0
        return df_summary

    b_df = price_map[benchmark_ticker]
    benchmark_rets = []
    missing_dates = 0
    for _, row in df_summary.iterrows():
        buy = pd.to_datetime(row["buy_date"])
        sell = pd.to_datetime(row["sell_date"])
        try:
            buy_price = float(b_df.loc[buy, "close"])
            sell_price = float(b_df.loc[sell, "close"])
            ret = (sell_price / buy_price) - 1.0
        except KeyError:
            ret = 0.0
            missing_dates += 1
        benchmark_rets.append(ret)

    if missing_dates > 0:
        print(f"[warn] {benchmark_ticker} 有 {missing_dates}/{len(df_summary)} 个月份的价格数据缺失，使用 0 填充", file=sys.stderr)

    df_summary[f"{prefix}_return"] = benchmark_rets
    df_summary[f"{prefix}_cumulative"] = (1 + df_summary[f"{prefix}_return"]).cumprod() - 1
    return df_summary


def generate_backtest_html(df_summary: pd.DataFrame, df_detail: pd.DataFrame, output_path: Path, benchmark: str = "SPY", extra_benchmarks: list[str] | None = None, price_map: Dict[str, pd.DataFrame] | None = None):
    """生成专业回测 HTML 仪表盘。
    包含：KPI卡片、权益曲线、回撤分布、月度回报、持仓表格、详细指标等。
    """
    import json
    from collections import defaultdict

    extra_benchmarks = extra_benchmarks or []
    months = len(df_summary)
    if months == 0:
        return

    # === Prepare monthly data for cards and year filter ===
    monthly_data = []
    for _, row in df_summary.iterrows():
        top_list = [t.strip() for t in str(row.get('top_tickers', '')).split(',') if t.strip()]
        monthly_data.append({
            'month': row['month'],
            'year': row['month'][:4],
            'buy_date': row['buy_date'],
            'sell_date': row['sell_date'],
            'num_stocks': int(row['num_stocks']),
            'monthly_return': float(row['monthly_return']),
            'top_tickers': top_list
        })
    years = sorted(set(m['year'] for m in monthly_data), reverse=True)

    # Compute annual returns for each year (for display when selected)
    annual_returns = {}
    for y in years:
        year_ms = [m for m in monthly_data if m['year'] == y]
        if year_ms:
            ret = 1.0
            for m in year_ms:
                ret *= (1 + m['monthly_return'])
            annual_returns[y] = ret - 1

    # Prepare equity curve data for TradingView lightweight-charts (equity = 1 + cumulative_return, time as YYYY-MM-01 for scale)
    tv_equity_series = {}
    tv_equity_series["策略"] = [
        {"time": f"{row['month']}-01", "value": round(1.0 + float(row["cumulative_return"]), 6)}
        for _, row in df_summary.iterrows()
    ]
    for bm in [benchmark] + extra_benchmarks:
        col = f"{bm.lower()}_cumulative"
        if col in df_summary.columns:
            tv_equity_series[bm] = [
                {"time": f"{row['month']}-01", "value": round(1.0 + float(row[col]), 6)}
                for _, row in df_summary.iterrows()
            ]

    # Prepare per-month detailed picks from df_detail (for rich cards)
    details_by_month = defaultdict(list)
    for _, row in df_detail.iterrows():
        m = row['month']
        details_by_month[m].append({
            'ticker': row['ticker'],
            'buy_price': row.get('buy_price', 0),
            'sell_price': row.get('sell_price', 0),
            'monthly_return': row.get('monthly_return', 0),
            'total_score': row.get('total_score', 0),
            'momentum_score': row.get('momentum_score', 0),
            'rrg_score': row.get('rrg_score', 0),
            'close_over_ema50': row.get('close_over_ema50', 0),
        })

    # === 计算 2026 YTD (今年收益) ===
    y2026_months = [m for m in monthly_data if m['year'] == '2026']
    ytd_2026 = 0.0
    if y2026_months:
        ytd = 1.0
        for m in y2026_months:
            ytd *= (1 + m['monthly_return'])
        ytd_2026 = ytd - 1
    print(f"[info] 2026 YTD 收益: {ytd_2026:.4%} (基于 {len(y2026_months)} 个月数据)")

    def fmt_pct(v): return f"{v:.1%}" if abs(v) < 10 else f"{v:.0%}"
    def fmt_num(v, dec=2): return f"{v:.{dec}f}"

    # === 计算指标 ===
    metrics = _calculate_metrics(df_summary["monthly_return"])
    all_metrics = {"策略": metrics}
    for bm in [benchmark] + extra_benchmarks:
        prefix = bm.lower()
        col = f"{prefix}_return"
        if col in df_summary.columns:
            all_metrics[bm] = _calculate_metrics(df_summary[col])
        else:
            print(f"[warn] {bm} 的收益数据在 df_summary 中缺失（列 {col} 不存在），HTML 中将不显示该 benchmark", file=sys.stderr)

    # 额外计算 Max DD 等用于卡片
    # 正确计算：基于权益曲线 (equity = 1 + cum_return)，DD = (equity - peak_equity) / peak_equity
    # 这样最大回撤始终在 -100% ~ 0% 之间（对于无杠杆多头）
    equity = 1 + df_summary["cumulative_return"]
    peak_equity = equity.cummax()
    proper_dd = (equity - peak_equity) / peak_equity
    max_dd = proper_dd.min()
    strategy_metrics = all_metrics["策略"]
    strategy_metrics["max_drawdown"] = max_dd

    # === 图表准备 ===
    colors_map = {
        "策略": "#22c55e",  # green
        benchmark: "#3b82f6",  # blue
    }
    extra_colors = ["#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6"]
    for i, bm in enumerate(extra_benchmarks):
        colors_map[bm] = extra_colors[i % len(extra_colors)]

    # (Equity curve now rendered via TradingView lightweight-charts below; no plotly fig for it)

    # Prepare TV data for drawdown (reuse equity calc)
    equity = 1 + df_summary["cumulative_return"]
    peak_equity = equity.cummax()
    drawdown = (equity - peak_equity) / peak_equity
    tv_drawdown_series = {}
    tv_drawdown_series["策略"] = [
        {"time": f"{row['month']}-01", "value": round(float(d), 6)}
        for (_, row), d in zip(df_summary.iterrows(), drawdown)
    ]
    for bm in [benchmark] + extra_benchmarks:
        col = f"{bm.lower()}_cumulative"
        if col in df_summary.columns:
            bm_equity = 1 + df_summary[col]
            bm_peak = bm_equity.cummax()
            bm_dd = (bm_equity - bm_peak) / bm_peak
            tv_drawdown_series[bm] = [
                {"time": f"{row['month']}-01", "value": round(float(d), 6)}
                for (_, row), d in zip(df_summary.iterrows(), bm_dd)
            ]

    # Prepare TV data for monthly returns (histogram)
    tv_monthly_data = [
        {"time": f"{row['month']}-01", "value": round(float(row['monthly_return']), 6)}
        for _, row in df_summary.iterrows()
    ]

    # Prepare TV data for drawdown distribution histogram (frequency of DD levels)
    # Only consider periods in drawdown (dd < 0), bin the values
    dd_negative = [float(d) for d in drawdown if d < 0]
    tv_dd_dist_data = []
    if dd_negative:
        min_d = min(dd_negative)
        # bins e.g. from 0 to min_d in steps of 5%
        bin_edges = np.arange(0, min_d - 0.001, -0.05)[::-1]
        if len(bin_edges) < 3:
            bin_edges = np.linspace(0, min_d, 6)
        counts, edges = np.histogram(dd_negative, bins=bin_edges)
        bin_centers = (edges[:-1] + edges[1:]) / 2
        tv_dd_dist_data = [
            {"time": round(c * 10000) + 100000, "value": int(cnt)}
            for c, cnt in zip(bin_centers, counts)
        ]

    # Latest month top 5 stocks MTD yield trend data (for browser live update)
    latest_month = df_summary["month"].iloc[-1]
    latest_details = df_detail[df_detail["month"] == latest_month]
    top5_tickers = latest_details["ticker"].head(5).tolist()
    latest_row = df_summary[df_summary["month"] == latest_month].iloc[0]
    buy_date = pd.to_datetime(latest_row["buy_date"])
    sell_date = pd.to_datetime(latest_row["sell_date"])
    latest_k_metadata = {
        "tickers": top5_tickers,
        "startDate": buy_date.strftime("%Y-%m-%d"),
        "firstCloses": {}
    }
    latest_k_fallback = {}
    mtd_returns = {}
    for t in top5_tickers:
        if price_map and t in price_map:
            ohlc = price_map[t]
            try:
                month_df = ohlc.loc[buy_date.strftime("%Y-%m-%d"): sell_date.strftime("%Y-%m-%d")]
                if len(month_df) > 0:
                    first = float(month_df["close"].iloc[0])
                    latest_k_metadata["firstCloses"][t] = first
                    latest_k_fallback[t] = [
                        {"time": d.strftime("%Y-%m-%d"), "value": float(row["close"] / first - 1)}
                        for d, row in month_df.iterrows()
                    ]
                    last = float(month_df["close"].iloc[-1])
                    mtd = (last / first - 1) if first != 0 else 0
                    mtd_returns[t] = mtd
            except Exception:
                pass

    portfolio_mtd = sum(mtd_returns.values()) / len(mtd_returns) if mtd_returns else 0

    # === KPI 卡片数据 ===
    kpis = [
        ("2026 YTD", fmt_pct(ytd_2026), ""),
        ("累计回报", fmt_pct(strategy_metrics.get('total_return', 0)), ""),
        ("年化收益", fmt_pct(strategy_metrics.get('cagr', 0)), ""),
        ("最大回撤", fmt_pct(strategy_metrics.get('max_drawdown', 0)), ""),
        ("夏普比率", f"{strategy_metrics.get('sharpe', 0):.2f}", ""),
        ("胜率", f"{strategy_metrics.get('win_rate', 0)*100:.0f}%", ""),
    ]

    # Use monthly_data prepared above for cards and filtering

    # 合并写入专业 HTML (Tailwind + lightweight-charts 现代仪表盘)
    tailwind = "https://cdn.tailwindcss.com"
    tv_charts = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Minvest</title>
    <script src="{tailwind}"></script>
    <script src="{tv_charts}"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&amp;display=swap');
        body {{ font-family: 'Inter', ui-sans-serif, system-ui, sans-serif; background: #fafafa; }}
        .font-display {{ font-family: 'Inter', system-ui, sans-serif; font-weight: 600; letter-spacing: -.025em; }}
        .kpi-card {{ transition: all 0.1s ease; }}
        .kpi-card:hover {{ box-shadow: 0 1px 3px rgb(0 0 0 / 0.08); transform: translateY(-1px); }}
        .plot-container {{ border-radius: 8px; border: 1px solid #e5e7eb; background: white; box-sizing: border-box; }}
        .section-title {{ font-size: 0.875rem; font-weight: 600; color: #171717; letter-spacing: -.01em; }}
        table {{ border-collapse: separate; border-spacing: 0; font-size: 0.8125rem; }}
        th, td {{ border-bottom: 1px solid #f4f4f5; padding-top: 0.375rem; padding-bottom: 0.375rem; }}
        th {{ color: #71717a; font-weight: 500; text-transform: uppercase; font-size: 0.6875rem; letter-spacing: .02em; }}
        .month-card {{ font-size: 0.8125rem; line-height: 1.2; border: 1px solid #e5e7eb; background: white; }}
        #monthly-cards .month-card {{ display: none; }}
        .num {{ font-variant-numeric: tabular-nums; }}
        .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 6px; }}
    </style>
</head>
<body class="bg-[#fafafa] text-[#111] antialiased">
    <div class="max-w-[1080px] mx-auto px-8 pt-10 pb-16">
        <!-- Claude-inspired clean header -->
        <div class="mb-6">
            <h1 class="font-display text-[28px] font-semibold tracking-[-0.03em]">Minvest</h1>
            <p class="text-[#666] mt-1 text-[13px]">2020-01 至 2026-06</p>
        </div>

        <!-- KPI Cards -->
        <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-3 gap-4 mb-6">
""")

        # KPI cards
        for label, value, desc in kpis:
            # Positive numbers (returns, sharpe, win rate) → emerald green; negative (e.g. drawdown, losses) → rose red
            text_color = "text-rose-600" if value.startswith("-") else "text-emerald-600"
            f.write(f"""
            <div class="kpi-card bg-white border border-[#e5e7eb] rounded-xl p-4">
                <div class="text-[10px] font-medium text-[#666] tracking-[0.5px]">{label}</div>
                <div class="mt-1.5 text-[28px] font-semibold tabular-nums tracking-tighter {text_color} leading-none">{value}</div>
            </div>
""")

        f.write("""
        </div>

""")

        # MTD section with proper python f-interp for the initial portfolio_mtd value (overridden by live JS)
        mtd_val = portfolio_mtd * 100
        mtd_color = "text-emerald-600" if mtd_val >= 0 else "text-rose-600"
        f.write(f"""
        <!-- 本月收益率 (MTD 走势 + 实时组合值) -->
        <div class="mb-4">
            <div class="flex items-center justify-between mb-3">
                <div class="section-title">本月收益率</div>
                <div id="mtd-live" class="text-sm font-medium text-right tabular-nums">
                    <span class="{mtd_color}">{mtd_val:.1f}%</span> <span class="text-[9px] text-[#888]">(实时)</span>
                </div>
            </div>
            <div class="plot-container p-3">
                <div id="tv-latest-kchart" style="width: 100%; height: 260px;"></div>
            </div>
        </div>

        <!-- Main Equity Curve -->
        <div class="mb-4">
            <div class="flex items-baseline justify-between mb-3">
                <div class="section-title">累计权益曲线</div>
            </div>
            <div class="plot-container p-3" style="height: 380px;">
                <div id="tv-equity-chart" style="width: 100%; height: 100%;"></div>
            </div>
        </div>

        <!-- Risk charts - clean side-by-side like Claude interfaces -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-4">
            <div>
                <div class="section-title mb-2">最大回撤</div>
                <div class="plot-container p-3" style="height: 240px;">
                    <div id="tv-drawdown-chart" style="width: 100%; height: 100%;"></div>
                </div>
            </div>
            <div>
                <div class="section-title mb-2">回撤分布</div>
                <div class="plot-container p-3" style="height: 240px;">
                    <div id="tv-dd-dist-chart" style="width: 100%; height: 100%;"></div>
                </div>
            </div>
        </div>

        <!-- Monthly Returns -->
        <div class="mb-4">
            <div class="section-title mb-3">月度回报分布</div>
            <div class="plot-container p-3" style="height: 240px;">
                <div id="tv-monthly-chart" style="width: 100%; height: 100%;"></div>
            </div>
        </div>

        <!-- Holdings -->
        <div class="pt-2 mb-4">
            <div class="flex items-center justify-between mb-3 px-1">
                <div class="section-title">年度 & 月度持仓 (Minvest)</div>
            </div>

            <!-- Year Dropdown for Annual + Year Return -->
            <div class="mb-5 flex items-center gap-3">
                <label class="text-sm font-medium text-[#555]">年份</label>
                <select id="year-select" class="border border-[#e5e7eb] rounded-md px-3 py-1 text-sm bg-white focus:outline-none focus:border-[#d4d4d8]" onchange="filterByYear()">
""")
        for y in years:
            sel = ' selected' if y == '2026' else ''
            f.write(f'                        <option value="{y}"{sel}>{y}</option>\n')
        f.write("""
                    </select>
                <div id="year-return-display" class="text-sm font-medium text-emerald-600 ml-2 min-w-[100px]"></div>
            </div>

            <div id="monthly-cards" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
""")
        for m in monthly_data:
            ret = m['monthly_return']
            ret_class = "bg-emerald-100 text-emerald-700" if ret > 0 else "bg-rose-100 text-rose-700"
            ret_str = f"{ret*100:+.2f}%"
            month_details = details_by_month.get(m['month'], [])
            # Build table for this month's holdings (one ticker per row, columns: 标的, 买入价, 卖出价, 回报)
            if month_details:
                table_rows = ''
                for d in month_details:
                    t_ret = d['monthly_return']
                    t_ret_class = "text-emerald-600" if t_ret > 0 else "text-rose-600"
                    t_ret_str = f"{t_ret*100:+.2f}%"
                    table_rows += f'''
                        <tr class="border-t border-zinc-200 hover:bg-zinc-50">
                            <td class="px-2 py-1 font-mono font-semibold text-[#111]">{d['ticker']}</td>
                            <td class="px-2 py-1 text-right tabular-nums">{d['buy_price']:.2f}</td>
                            <td class="px-2 py-1 text-right tabular-nums">{d['sell_price']:.2f}</td>
                            <td class="px-2 py-1 text-right tabular-nums {t_ret_class}">{t_ret_str}</td>
                        </tr>
                    '''
                table_html = f'''
                    <table class="w-full text-xs border-collapse mt-1.5">
                        <thead>
                            <tr class="bg-[#f4f4f5] text-[#666]">
                                <th class="text-left px-2 py-1 font-semibold">标的</th>
                                <th class="text-right px-2 py-1 font-semibold tabular-nums">买入价</th>
                                <th class="text-right px-2 py-1 font-semibold tabular-nums">卖出价</th>
                                <th class="text-right px-2 py-1 font-semibold tabular-nums">回报</th>
                            </tr>
                        </thead>
                        <tbody>
                            {table_rows}
                        </tbody>
                    </table>
                '''
            else:
                table_html = '<div class="text-[#888] text-xs mt-1">现金持仓</div>'
            f.write(f"""
                <div class="month-card card rounded-lg p-3" data-year="{m['year']}" data-month="{m['month']}">
                    <div class="flex justify-between items-start mb-1.5">
                        <div>
                            <div class="font-semibold text-[13px]">{m['month']}</div>
                            <div class="text-[10px] text-[#777]">{m['buy_date']} → {m['sell_date']}</div>
                        </div>
                        <div class="px-1.5 py-px rounded text-[10px] font-medium {ret_class}">{ret_str}</div>
                    </div>
                    {table_html}
                </div>
""")
        f.write("""
            </div>
        </div>

        <script>
            const annualReturns = """ + json.dumps(annual_returns) + """;
            const tvEquityData = """ + json.dumps(tv_equity_series) + """;
            const chartColors = """ + json.dumps(colors_map) + """;
            const tvDrawdownSeries = """ + json.dumps(tv_drawdown_series) + """;
            const tvMonthlyData = """ + json.dumps(tv_monthly_data) + """;
            const tvDDDistData = """ + json.dumps(tv_dd_dist_data) + """;
            const latestKMetadata = """ + json.dumps(latest_k_metadata) + """;
            const latestKFallback = """ + json.dumps(latest_k_fallback) + """;
            const mtdReturnsReport = """ + json.dumps(mtd_returns) + """;

            function filterByYear() {
                const select = document.getElementById('year-select');
                const year = select.value;
                const allCards = Array.from(document.querySelectorAll('.month-card'));
                const container = document.getElementById('monthly-cards');

                // hide all
                allCards.forEach(card => card.style.display = 'none');

                let yearCards = [];
                if (year) {
                    yearCards = allCards.filter(card => card.dataset.year === year);
                    // sort reverse chrono (most recent month first)
                    yearCards.sort((a, b) => b.dataset.month.localeCompare(a.dataset.month));
                } else {
                    yearCards = allCards;
                }

                // re-append in sorted order and show
                yearCards.forEach(card => {
                    container.appendChild(card);
                    card.style.display = 'block';
                });

                // update year return display
                const retDisplay = document.getElementById('year-return-display');
                if (retDisplay) {
                    if (year && annualReturns[year] !== undefined) {
                        const ret = annualReturns[year];
                        const retStr = (ret * 100).toFixed(2) + '%';
                        const retClass = ret >= 0 ? 'text-emerald-600' : 'text-rose-600';
                        retDisplay.innerHTML = `<span class="${retClass} font-bold">年收益: ${retStr}</span>`;
                    } else {
                        retDisplay.innerHTML = '';
                    }
                }

            }

            function initTradingViewEquity() {
                const container = document.getElementById('tv-equity-chart');
                if (!container || !tvEquityData || Object.keys(tvEquityData).length === 0) {
                    console.warn('TV chart data missing');
                    return;
                }
                function doCreate() {
                    try {
                        // use client width for responsive
                        const chart = LightweightCharts.createChart(container, {
                            width: container.clientWidth || 800,
                            height: container.clientHeight || 380,
                            layout: {
                                background: { color: '#ffffff' },
                                textColor: '#374151',
                            },
                            grid: {
                                vertLines: { color: '#f3f4f6' },
                                horzLines: { color: '#f3f4f6' },
                            },
                            timeScale: {
                                borderColor: '#e5e7eb',
                                timeVisible: false,
                                secondsVisible: false,
                            },
                            rightPriceScale: {
                                borderColor: '#e5e7eb',
                                scaleMargins: { top: 0.1, bottom: 0.1 },
                            },
                            crosshair: {
                                mode: 0,
                                vertLine: { color: '#d1d5db', width: 1, style: 3 },
                                horzLine: { color: '#d1d5db', width: 1, style: 3 },
                            },
                            legend: {
                                visible: true,
                                position: 'top',
                            },
                        });

                        if (!chart || typeof chart.addLineSeries !== 'function') {
                            console.error('createChart did not return a valid chart with addLineSeries.');
                            container.innerHTML = '<div style="padding:12px;color:#b91c1c;font-size:12px;background:#fef2f2;border-radius:6px;">累计权益曲线初始化失败（图表库加载异常）。请尝试刷新页面或使用其他浏览器。</div>';
                            return;
                        }

                        Object.keys(tvEquityData).forEach(name => {
                            const data = tvEquityData[name];
                            if (!data || data.length === 0) return;
                            const color = chartColors[name] || '#6b7280';
                            const lineWidth = (name === '策略') ? 3 : 2;
                            const series = chart.addLineSeries({
                                color: color,
                                lineWidth: lineWidth,
                                title: name,
                            });
                            series.setData(data);
                        });

                        // handle resize
                        function handleResize() {
                            chart.resize(container.clientWidth || 800, container.clientHeight || 380);
                        }
                        window.addEventListener('resize', handleResize);
                        // initial fit + ensure visible
                        setTimeout(() => {
                            if (container.clientWidth) chart.resize(container.clientWidth, container.clientHeight || 380);
                            try { chart.timeScale().fitContent(); } catch(e) {}
                        }, 80);
                    } catch (err) {
                        console.error('TradingView chart creation error:', err);
                        container.innerHTML = '<div style="padding:12px;color:#b91c1c;font-size:12px;background:#fef2f2;border-radius:6px;">累计权益曲线创建出错，请刷新重试。</div>';
                    }
                }

                if (typeof LightweightCharts !== 'undefined' && typeof LightweightCharts.createChart === 'function') {
                    doCreate();
                } else {
                    // retry a few times in case of CDN timing (rare)
                    let attempts = 0;
                    const iv = setInterval(() => {
                        attempts++;
                        if (typeof LightweightCharts !== 'undefined' && typeof LightweightCharts.createChart === 'function') {
                            clearInterval(iv);
                            doCreate();
                        } else if (attempts > 12) {
                            clearInterval(iv);
                            console.warn('TradingView lightweight-charts failed to load');
                            container.innerHTML = '<div style="padding:12px;color:#666;font-size:12px;">TradingView 图表库加载失败，请检查网络后刷新。</div>';
                        }
                    }, 100);
                }
            }

            function initTradingViewDrawdown() {
                const container = document.getElementById('tv-drawdown-chart');
                if (!container || typeof LightweightCharts === 'undefined' || !tvDrawdownSeries || Object.keys(tvDrawdownSeries).length === 0) {
                    return;
                }
                try {
                    const chart = LightweightCharts.createChart(container, {
                        width: container.clientWidth || 800,
                        height: container.clientHeight || 240,
                        layout: {
                            background: { color: '#ffffff' },
                            textColor: '#374151',
                        },
                        grid: {
                            vertLines: { color: '#f3f4f6' },
                            horzLines: { color: '#f3f4f6' },
                        },
                        timeScale: {
                            borderColor: '#e5e7eb',
                            timeVisible: false,
                            secondsVisible: false,
                        },
                        rightPriceScale: {
                            borderColor: '#e5e7eb',
                        },
                        crosshair: {
                            mode: 0,
                            vertLine: { color: '#d1d5db', width: 1, style: 3 },
                            horzLine: { color: '#d1d5db', width: 1, style: 3 },
                        },
                    });

                    Object.keys(tvDrawdownSeries).forEach(name => {
                        const data = tvDrawdownSeries[name];
                        if (!data || data.length === 0) return;
                        const color = chartColors[name] || '#6b7280';
                        const series = chart.addLineSeries({
                            color: color,
                            lineWidth: name === '策略' ? 2 : 1.5,
                            title: name,
                        });
                        series.setData(data);
                    });

                    function handleResize() {
                        chart.resize(container.clientWidth || 800, container.clientHeight || 240);
                    }
                    window.addEventListener('resize', handleResize);
                    setTimeout(() => {
                        if (container.clientWidth) chart.resize(container.clientWidth, container.clientHeight || 240);
                        try { chart.timeScale().fitContent(); } catch(e) {}
                    }, 80);
                } catch (err) {
                    console.error('TV drawdown error:', err);
                    container.innerHTML = '<div style="padding:12px;color:#b91c1c;font-size:12px;background:#fef2f2;border-radius:6px;">回撤图表创建出错，请刷新。</div>';
                }
            }

            function initTradingViewMonthly() {
                const container = document.getElementById('tv-monthly-chart');
                if (!container || typeof LightweightCharts === 'undefined' || !tvMonthlyData || tvMonthlyData.length === 0) {
                    return;
                }
                try {
                    const chart = LightweightCharts.createChart(container, {
                        width: container.clientWidth || 800,
                        height: container.clientHeight || 240,
                        layout: {
                            background: { color: '#ffffff' },
                            textColor: '#374151',
                        },
                        grid: {
                            vertLines: { color: '#f3f4f6' },
                            horzLines: { color: '#f3f4f6' },
                        },
                        timeScale: {
                            borderColor: '#e5e7eb',
                            timeVisible: false,
                            secondsVisible: false,
                        },
                        rightPriceScale: {
                            borderColor: '#e5e7eb',
                        },
                        crosshair: {
                            mode: 0,
                            vertLine: { color: '#d1d5db', width: 1, style: 3 },
                            horzLine: { color: '#d1d5db', width: 1, style: 3 },
                        },
                    });

                    const series = chart.addHistogramSeries({
                        title: '策略月度回报',
                    });
                    const coloredData = tvMonthlyData.map(d => ({
                        time: d.time,
                        value: d.value,
                        color: d.value >= 0 ? '#22c55e' : '#ef4444'
                    }));
                    series.setData(coloredData);

                    function handleResize() {
                        chart.resize(container.clientWidth || 800, container.clientHeight || 240);
                    }
                    window.addEventListener('resize', handleResize);
                    setTimeout(() => {
                        if (container.clientWidth) chart.resize(container.clientWidth, container.clientHeight || 240);
                        try { chart.timeScale().fitContent(); } catch(e) {}
                    }, 80);
                } catch (err) {
                    console.error('TV monthly error:', err);
                    container.innerHTML = '<div style="padding:12px;color:#b91c1c;font-size:12px;background:#fef2f2;border-radius:6px;">月度回报图表创建出错，请刷新。</div>';
                }
            }

            function initTradingViewDDDist() {
                const container = document.getElementById('tv-dd-dist-chart');
                if (!container || typeof LightweightCharts === 'undefined' || !tvDDDistData || tvDDDistData.length === 0) {
                    return;
                }
                try {
                    const chart = LightweightCharts.createChart(container, {
                        width: container.clientWidth || 800,
                        height: container.clientHeight || 240,
                        layout: {
                            background: { color: '#ffffff' },
                            textColor: '#374151',
                        },
                        grid: {
                            vertLines: { color: '#f3f4f6' },
                            horzLines: { color: '#f3f4f6' },
                        },
                        timeScale: {
                            borderColor: '#e5e7eb',
                            timeVisible: false,
                            secondsVisible: false,
                            tickMarkFormatter: (time, tickMarkType, locale) => {
                                return ((time - 100000) / 100).toFixed(1) + '%';
                            },
                        },
                        rightPriceScale: {
                            borderColor: '#e5e7eb',
                        },
                    });

                    const series = chart.addHistogramSeries({
                        title: '回撤分布',
                        color: '#ef4444',
                    });
                    series.setData(tvDDDistData);

                    function handleResize() {
                        chart.resize(container.clientWidth || 800, container.clientHeight || 240);
                    }
                    window.addEventListener('resize', handleResize);
                    setTimeout(() => {
                        if (container.clientWidth) chart.resize(container.clientWidth, container.clientHeight || 240);
                        try { chart.timeScale().fitContent(); } catch(e) {}
                    }, 80);
                } catch (err) {
                    console.error('TV DD dist error:', err);
                    container.innerHTML = '<div style="padding:4px;color:#666;font-size:10px;">回撤分布图表出错。</div>';
                }
            }

            async function fetchYahooCloses(ticker, startDateStr) {
                const start = Math.floor(new Date(startDateStr).getTime() / 1000);
                const end = Math.floor(Date.now() / 1000);
                const url = `https://query1.finance.yahoo.com/v8/finance/chart/${ticker}?interval=1d&period1=${start}&period2=${end}`;
                const proxy = 'https://api.allorigins.win/raw?url=' + encodeURIComponent(url);
                const resp = await fetch(proxy, { cache: 'no-cache' });
                if (!resp.ok) throw new Error('proxy fail');
                const json = await resp.json();
                const result = json.chart.result[0];
                const timestamps = result.timestamp;
                const closes = result.indicators.quote[0].close;
                const series = [];
                for (let i = 0; i < timestamps.length; i++) {
                    if (closes[i] == null) continue;
                    const dateStr = new Date(timestamps[i] * 1000).toISOString().slice(0, 10);
                    series.push({ time: dateStr, value: closes[i] });
                }
                return series;
            }

            function updateMTDLive(ticker, mtd) {
                if (!window.liveMTDs) window.liveMTDs = { ...mtdReturnsReport };
                window.liveMTDs[ticker] = mtd;
                refreshMTD();
            }

            async function initLatestKChart() {
                const container = document.getElementById('tv-latest-kchart');
                if (!container || typeof LightweightCharts === 'undefined' || !latestKMetadata || !latestKMetadata.tickers || latestKMetadata.tickers.length === 0) {
                    return;
                }
                try {
                    const chart = LightweightCharts.createChart(container, {
                        width: container.clientWidth || 800,
                        height: 260,
                        layout: {
                            background: { color: '#ffffff' },
                            textColor: '#374151',
                        },
                        grid: {
                            vertLines: { color: '#f3f4f6' },
                            horzLines: { color: '#f3f4f6' },
                        },
                        timeScale: {
                            borderColor: '#e5e7eb',
                            timeVisible: false,
                            secondsVisible: false,
                            rightOffset: 3,
                            barSpacing: 18,
                            minBarWidth: 4,
                        },
                        rightPriceScale: {
                            borderColor: '#e5e7eb',
                        },
                    });

                    const colors = ["#22c55e", "#3b82f6", "#f59e0b", "#8b5cf6", "#ec4899"];
                    const seriesMap = {};
                    latestKMetadata.tickers.forEach((t, i) => {
                        const s = chart.addLineSeries({
                            color: colors[i % colors.length],
                            lineWidth: 2,
                            title: t,
                        });
                        seriesMap[t] = s;
                        // initial fallback from report data
                        if (latestKFallback && latestKFallback[t]) {
                            s.setData(latestKFallback[t]);
                        }
                    });

                    // Fit early so fallback data (if no live) spreads across the chart nicely
                    try { chart.timeScale().fitContent(); } catch(e) {}

                    // initial MTD from report (equal weight avg)
                    if (window.liveMTDs === undefined) window.liveMTDs = { ...mtdReturnsReport };
                    function refreshMTD() {
                        const vals = Object.values(window.liveMTDs);
                        const avg = vals.length ? vals.reduce((a,b)=>a+b,0) / vals.length : 0;
                        const el = document.getElementById('mtd-live');
                        if (el) {
                            const pctStr = (avg*100).toFixed(1);
                            const numClass = avg >= 0 ? 'text-emerald-600' : 'text-rose-600';
                            el.innerHTML = `<span class="${numClass}">${pctStr}%</span> <span class="text-[9px] text-[#888]">(实时)</span>`;
                        }
                    }
                    refreshMTD();

                    const fetches = latestKMetadata.tickers.map(async (t) => {
                        try {
                            const firstClose = latestKMetadata.firstCloses[t];
                            const start = latestKMetadata.startDate;
                            const closes = await fetchYahooCloses(t, start);
                            if (closes.length > 0 && firstClose) {
                                const seriesData = closes.map(d => ({
                                    time: d.time,
                                    value: d.value / firstClose - 1
                                }));
                                seriesMap[t].setData(seriesData);
                                const liveMTD = seriesData[seriesData.length - 1].value;
                                updateMTDLive(t, liveMTD);
                            }
                        } catch (e) {
                            console.warn('Live fetch failed for ' + t + ', keeping report MTD', e);
                        }
                    });
                    await Promise.all(fetches);
                    chart.timeScale().fitContent();

                    // Extra delayed resize + fit (consistent with other TV charts) to ensure
                    // layout is settled and the short ~10-day series is spread out, not crammed right.
                    setTimeout(() => {
                        if (container && container.clientWidth) {
                            chart.resize(container.clientWidth, 260);
                        }
                        try { chart.timeScale().fitContent(); } catch(e) {}
                    }, 80);
                } catch (err) {
                    console.error('TV latest K error:', err);
                    container.innerHTML = '<div style="padding:4px;color:#666;font-size:10px;">走势图表出错，使用报告数据。</div>';
                }
            }

            // initial: default to 2026 + filter + TV
            function initializeAll() {
                // set default year to 2026 and filter cards
                const sel = document.getElementById('year-select');
                if (sel) {
                    // prefer 2026 if option exists, else first
                    const opts = Array.from(sel.options).map(o => o.value);
                    if (opts.includes('2026')) {
                        sel.value = '2026';
                    } else if (opts.length > 0) {
                        sel.value = opts[0];
                    }
                    filterByYear();
                }

                // clear or set initial return display (filterByYear handles for default year)
                console.log('%c[Report] Performance report ready (TV charts + default 2026 holdings)', 'color:#64748b');

                // init TV charts a little later for layout
                setTimeout(initTradingViewEquity, 120);
                setTimeout(initTradingViewDrawdown, 150);
                setTimeout(initTradingViewMonthly, 150);
                setTimeout(initTradingViewDDDist, 180);
                setTimeout(initLatestKChart, 150);
            }

            // Run sync immediately (this script executes after #monthly-cards in DOM)
            initializeAll();
        </script>

    </div>
</body>
</html>""")

    print(f"[info] HTML 已生成 → index.html")

def _calculate_metrics(returns: pd.Series) -> dict:
    """计算回测指标（已增强边界情况防护）"""
    rets = returns.dropna()
    if len(rets) == 0:
        return {k: 0.0 for k in ["total_return", "cagr", "volatility", "sharpe", "sortino",
                                  "worst_monthly_return", "calmar", "win_rate", "median_return"]}

    total_return = (1 + rets).prod() - 1
    n_months = len(rets)
    cagr = (1 + total_return) ** (12 / n_months) - 1 if n_months > 0 else 0.0
    volatility = rets.std() * np.sqrt(12) if n_months > 1 else 0.0

    # 夏普比率（无风险利率假设为0）
    sharpe = cagr / volatility if volatility > 1e-12 else 0.0

    # 索提诺比率：仅使用负收益标准差，防护空序列 / 零波动
    negative_rets = rets[rets < 0]
    if len(negative_rets) >= 2:
        downside = negative_rets.std() * np.sqrt(12)
        sortino = cagr / downside if downside > 1e-12 else 0.0
    else:
        sortino = 0.0

    # 最差月度回报
    worst_monthly_return = rets.min()
    # Calmar 比率防护
    if abs(worst_monthly_return) > 1e-12:
        calmar = cagr / abs(worst_monthly_return)
    else:
        calmar = 0.0

    win_rate = (rets > 0).mean()
    median_return = rets.median()

    return {
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "worst_monthly_return": worst_monthly_return,
        "calmar": calmar,
        "win_rate": win_rate,
        "median_return": median_return,
    }


def main() -> int:
    args = parse_args()

    if args.command in ("backtest", "russell-backtest"):
        return cmd_russell_backtest(args)

    if args.command in ("resolve", "resolve-tickers"):
        return cmd_resolve_tickers(args)

    # 仅支持 backtest / resolve 命令（rank 已移除）
    print(f"[error] 未知命令或不支持的命令: {args.command}。当前仅支持: backtest, resolve", file=sys.stderr)
    return 1


def cmd_resolve_tickers(args) -> int:
    """重构后的 Ticker 重解析命令（支持 --full-backfill + 报告）"""
    print("[info] 开始 Ticker 重解析（使用统一元数据缓存 + 外部手动覆盖）...", file=sys.stderr)

    from pathlib import Path
    import json
    from collections import Counter, defaultdict
    from datetime import datetime

    # 优先从 SQLite 加载（最新架构）
    from data.nport_data import _load_holdings_from_db, _save_holdings_cache

    holdings_cache = _load_holdings_from_db()
    if not holdings_cache:
        # 回退旧 JSON
        holdings_path = Path("cache/nport_holdings_cache.json")
        if not holdings_path.exists():
            print("[error] 未找到任何 NPORT holdings 数据", file=sys.stderr)
            return 1
        with open(holdings_path, encoding="utf-8") as f:
            holdings_cache = json.load(f)

    resolver = TickerResolver(
        ib_host=args.host, ib_port=args.port, ib_client_id=args.client_id
    )

    total_resolved = 0
    stats = Counter()
    per_filing = defaultdict(int)

    # B 改进：--full-backfill 模式会扫描所有 filing 的所有持仓（含已解析但质量低的）
    scan_all = args.full_backfill or args.force

    target_holdings = holdings_cache
    if args.report:
        print("[info] 正在生成解析质量报告...")

    for acc, holdings in target_holdings.items():
        if scan_all:
            to_resolve = holdings  # 全量扫描
        else:
            to_resolve = [h for h in holdings if not h.get("ticker") or args.force]
            if args.missing_only and not args.force:
                to_resolve = [h for h in holdings if not h.get("ticker")]

        if not to_resolve:
            continue

        before = sum(1 for h in holdings if h.get("ticker"))
        # 全量回填强制使用去重模式，避免历史数据重复 ISIN 请求
        use_dedup = scan_all or len(to_resolve) > 200
        resolved = resolver.resolve_holdings(to_resolve, force=scan_all or args.force, use_dedup=use_dedup)
        after = sum(1 for h in holdings if h.get("ticker"))
        newly = after - before

        total_resolved += resolved
        per_filing[acc] = resolved

        # 简单来源统计
        for h in holdings:
            src = h.get("_ticker_source") or "missing"
            stats[src] += 1

        if args.verbose and resolved > 0:
            print(f"  {acc}: 新解析 {resolved} 条")

    # 关闭 IB 连接（如果使用了复用）
    resolver.close()

    print(f"\n[info] 本次运行共解析/更新 {total_resolved} 条 ticker")

    if args.full_backfill or args.report:
        print("\n=== Ticker 解析质量报告 ===")
        print(f"总持仓条目数: {sum(len(v) for v in holdings_cache.values())}")
        print("按来源分布:")
        for src, cnt in sorted(stats.items(), key=lambda x: -x[1]):
            pct = cnt / sum(stats.values()) * 100 if stats else 0
            print(f"  {src:18s}: {cnt:6d} ({pct:5.1f}%)")

        still_missing = sum(1 for hs in holdings_cache.values() for h in hs if not h.get("ticker"))
        print(f"\n仍缺失 ticker 的持仓: {still_missing}")

        if args.report:
            print(f"\n报告生成时间: {datetime.now().isoformat()}")
            print("建议：定期运行 python main.py resolve --full-backfill --report")

    print("[info] 结果已保存到 cache/ticker_resolution_cache.json（含 conId / exchange 等丰富信息）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
