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
    price_map_needs_ohlc_refresh,
    DEFAULT_HISTORY_FILE,
    DEFAULT_BENCHMARK,
    DEFAULT_DURATION,
    DEFAULT_MIN_MARKET_CAP,
)
from strategy.stock_selector import score_universe, pick_rows_to_frame
from backtest.tester import backtest_nport_monthly, open_at  # backtest_monthly_returns 为遗留接口，已默认固定2020-01
from data.nport_universe import (
    get_latest_universe,
    get_all_nport_tickers,
    get_monthly_universes,
)
from data.nport_data import sync_holdings_if_needed
from data.ticker_resolver import TickerResolver
from datetime import datetime


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

    if price_map and price_map_needs_ohlc_refresh(price_map):
        print(
            "[warn] 检测到旧版收盘价缓存（open=close），将从 IB 全量刷新真实 OHLC 数据",
            file=sys.stderr,
        )
    # 同一天重复运行保护（以本地仓库 mtime 为准）：如果价格缓存文件今天已更新过，直接跳过 IB 增量部分
    # 避免用户多次运行 backtest 时反复看到“需要从 IB 获取/更新数据：N 只”
    # 第一次运行当天会正常做 trading-day 追平，之后同日运行使用缓存。
    elif price_map and cache_file.exists():
        try:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            age_days = (datetime.now() - mtime).days
            if age_days == 0:
                print(f"[info] 价格缓存今天已更新（文件 {age_days} 天前），跳过重复从 IB Gateway 增量更新（使用缓存数据）", file=sys.stderr)
                return price_map
        except Exception:
            pass

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
            buy_price = open_at(b_df, buy)
            sell_price = open_at(b_df, sell)
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


def _drawdown_from_cumulative(cum_returns: pd.Series) -> pd.Series:
    """基于累计回报序列计算回撤（-100% ~ 0%）。"""
    equity = 1 + cum_returns
    peak = equity.cummax()
    return (equity - peak) / peak


def _kpi_text_color(label: str, value: str) -> str:
    if label in ("最大回撤", "最差月份") or value.startswith("-"):
        return "val-neg"
    if label in ("夏普比率", "索提诺比率", "Calmar 比率", "盈亏比"):
        try:
            return "val-pos" if float(value.replace("%", "")) >= 1 else "val-neg"
        except ValueError:
            pass
    return "val-pos"


def _write_metric_card(f, label: str, value: str, text_color: str) -> None:
    f.write(f"""
                    <div class="metric-card">
                        <div class="metric-label">{label}</div>
                        <div class="metric-value {text_color}">{value}</div>
                    </div>
""")


def generate_backtest_html(
    df_summary: pd.DataFrame,
    df_detail: pd.DataFrame,
    output_path: Path,
    benchmark: str = "SPY",
    extra_benchmarks: list[str] | None = None,
    price_map: dict[str, pd.DataFrame] | None = None,
    mtd_snapshot: dict | None = None,
):
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

    # === 计算当年 YTD ===
    current_year = str(datetime.now().year)
    ytd_months = [m for m in monthly_data if m["year"] == current_year]
    ytd_return = 0.0
    if ytd_months:
        ytd = 1.0
        for m in ytd_months:
            ytd *= (1 + m["monthly_return"])
        ytd_return = ytd - 1
    print(f"[info] {current_year} YTD 收益: {ytd_return:.4%} (基于 {len(ytd_months)} 个月数据)")

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

    strategy_metrics = all_metrics["策略"]
    drawdown = _drawdown_from_cumulative(df_summary["cumulative_return"])

    # === 图表准备 ===
    colors_map = {
        "策略": "#059669",
        benchmark: "#3b82f6",
    }
    extra_colors = ["#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6"]
    for i, bm in enumerate(extra_benchmarks):
        colors_map[bm] = extra_colors[i % len(extra_colors)]

    tv_drawdown_series = {}
    tv_drawdown_series["策略"] = [
        {"time": f"{row['month']}-01", "value": round(float(d), 6)}
        for (_, row), d in zip(df_summary.iterrows(), drawdown)
    ]
    for bm in [benchmark] + extra_benchmarks:
        col = f"{bm.lower()}_cumulative"
        if col in df_summary.columns:
            bm_dd = _drawdown_from_cumulative(df_summary[col])
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
    if df_detail.empty or "month" not in df_detail.columns:
        top5_tickers = []
    else:
        latest_details = df_detail[df_detail["month"] == latest_month]
        top5_tickers = latest_details["ticker"].head(5).tolist()
    latest_row = df_summary[df_summary["month"] == latest_month].iloc[0]
    buy_date = pd.to_datetime(latest_row["buy_date"])
    sell_date = pd.to_datetime(latest_row["sell_date"])
    latest_k_metadata = {
        "tickers": top5_tickers,
        "startDate": buy_date.strftime("%Y-%m-%d"),
        "firstOpens": {}
    }
    latest_k_fallback = {}
    mtd_returns = {}
    for t in top5_tickers:
        if price_map and t in price_map:
            ohlc = price_map[t]
            try:
                end_date = min(sell_date, ohlc.index.max())
                month_df = ohlc.loc[buy_date.strftime("%Y-%m-%d"): end_date.strftime("%Y-%m-%d")]
                if len(month_df) > 0:
                    first = open_at(ohlc, buy_date)
                    latest_k_metadata["firstOpens"][t] = first
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

    if mtd_snapshot:
        latest_k_metadata = mtd_snapshot.get("metadata", latest_k_metadata)
        latest_k_fallback = mtd_snapshot.get("fallback", latest_k_fallback)
        mtd_returns = mtd_snapshot.get("returns", mtd_returns)
        portfolio_mtd = mtd_snapshot.get("portfolio_mtd", portfolio_mtd)

    date_range = f"{df_summary['month'].iloc[0]} 至 {df_summary['month'].iloc[-1]}"
    default_year = current_year if current_year in years else years[0]

    performance_kpis = [
        (f"{current_year} YTD", fmt_pct(ytd_return)),
        ("累计回报", fmt_pct(strategy_metrics.get("total_return", 0))),
        ("年化收益", fmt_pct(strategy_metrics.get("cagr", 0))),
        ("最大回撤", fmt_pct(strategy_metrics.get("max_drawdown", 0))),
        ("夏普比率", f"{strategy_metrics.get('sharpe', 0):.2f}"),
        ("胜率", f"{strategy_metrics.get('win_rate', 0) * 100:.0f}%"),
        ("索提诺比率", f"{strategy_metrics.get('sortino', 0):.2f}"),
        ("Calmar 比率", f"{strategy_metrics.get('calmar', 0):.2f}"),
        ("年化波动率", fmt_pct(strategy_metrics.get("volatility", 0))),
        ("盈亏比", f"{strategy_metrics.get('profit_factor', 0):.2f}"),
        ("最差月份", fmt_pct(strategy_metrics.get("worst_monthly_return", 0))),
        ("中位月回报", fmt_pct(strategy_metrics.get("median_return", 0))),
    ]
    benchmark_names = ["策略"] + [benchmark] + extra_benchmarks

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
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&amp;display=swap');
        :root {{
            --bg: #f8fafc;
            --surface: #ffffff;
            --surface-muted: #ffffff;
            --border: rgba(148, 163, 184, 0.22);
            --border-strong: rgba(148, 163, 184, 0.35);
            --text: #334155;
            --text-muted: #64748b;
            --text-subtle: #94a3b8;
            --pos: #059669;
            --neg: #e11d48;
            --accent: #3b82f6;
            --shadow-sm: 0 1px 3px rgba(15, 23, 42, 0.04);
            --shadow-md: 0 8px 30px rgba(15, 23, 42, 0.05);
            --radius: 16px;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: 'Inter', ui-sans-serif, system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
        }}
        .font-display {{ font-weight: 700; letter-spacing: -0.03em; }}
        .page {{ max-width: 1120px; margin: 0 auto; padding: 2.5rem 2rem 4rem; }}
        .panel {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow-sm);
        }}
        .panel-padded {{ padding: 1.5rem; }}
        .section-block {{ margin-bottom: 1.75rem; }}
        .section-head {{
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 1rem; gap: 1rem;
        }}
        .section-title {{
            font-size: 0.8125rem; font-weight: 600; color: var(--text);
            letter-spacing: -0.01em;
        }}
        .section-title::before {{
            content: ''; display: inline-block; width: 3px; height: 14px;
            background: var(--accent); border-radius: 2px;
            margin-right: 0.5rem; vertical-align: -2px;
        }}
        .section-subtitle {{
            font-size: 0.6875rem; font-weight: 600; color: var(--text-subtle);
            text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.75rem;
        }}
        .date-badge {{
            font-size: 0.75rem; color: var(--text-muted); font-weight: 500;
            background: var(--surface-muted); border: 1px solid var(--border);
            padding: 0.375rem 0.75rem; border-radius: 999px; white-space: nowrap;
        }}
        .metric-grid {{
            display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem;
        }}
        @media (min-width: 768px) {{ .metric-grid {{ grid-template-columns: repeat(3, 1fr); }} }}
        @media (min-width: 1024px) {{ .metric-grid.performance-grid {{ grid-template-columns: repeat(4, 1fr); }} }}
        .metric-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1rem 1.125rem;
            transition: border-color 0.15s ease, box-shadow 0.15s ease;
        }}
        .metric-card:hover {{
            border-color: var(--border-strong);
            box-shadow: var(--shadow-sm);
        }}
        .metric-label {{
            font-size: 0.6875rem; font-weight: 600; color: var(--text-subtle);
            text-transform: uppercase; letter-spacing: 0.06em;
        }}
        .metric-value {{
            margin-top: 0.5rem;
            font-size: 1.625rem; font-weight: 700;
            font-variant-numeric: tabular-nums;
            letter-spacing: -0.03em; line-height: 1;
        }}
        .val-pos {{ color: var(--pos); }}
        .val-neg {{ color: var(--neg); }}
        .val-neutral {{ color: var(--text); }}
        .data-table {{ width: 100%; border-collapse: collapse; font-size: 0.8125rem; }}
        .data-table th {{
            text-align: left; padding: 0.625rem 1rem;
            font-size: 0.625rem; font-weight: 600; color: var(--text-subtle);
            text-transform: uppercase; letter-spacing: 0.06em;
            border-bottom: 1px solid var(--border);
            background: var(--surface-muted);
        }}
        .data-table th:not(:first-child) {{ text-align: right; }}
        .data-table td {{
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--border);
            font-variant-numeric: tabular-nums;
        }}
        .data-table td:not(:first-child) {{ text-align: right; }}
        .data-table tr:last-child td {{ border-bottom: none; }}
        .data-table tbody tr:hover {{ background: #fafafa; }}
        .data-table .row-highlight {{ background: rgba(59, 130, 246, 0.05); }}
        .data-table .row-highlight:hover {{ background: rgba(59, 130, 246, 0.08); }}
        .legend-dot {{
            display: inline-block; width: 8px; height: 8px;
            border-radius: 50%; margin-right: 0.5rem; vertical-align: middle;
        }}
        .chart-panel {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow-sm);
            padding: 1rem;
        }}
        .mtd-badge {{
            font-size: 0.875rem; font-weight: 600;
            font-variant-numeric: tabular-nums;
        }}
        .mtd-live-tag {{
            font-size: 0.625rem; color: var(--text-subtle);
            font-weight: 500; margin-left: 0.25rem;
        }}
        .year-select {{
            appearance: none;
            background: var(--surface) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2371717a' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E") no-repeat right 0.75rem center;
            border: 1px solid var(--border-strong);
            border-radius: 10px;
            padding: 0.5rem 2rem 0.5rem 0.875rem;
            font-size: 0.8125rem; font-weight: 500; color: var(--text);
            cursor: pointer; transition: border-color 0.15s ease;
        }}
        .year-select:focus {{ outline: none; border-color: var(--accent); }}
        .month-card {{
            font-size: 0.8125rem; line-height: 1.3;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 0.875rem;
            transition: border-color 0.15s ease, box-shadow 0.15s ease;
        }}
        .month-card:hover {{ border-color: var(--border-strong); box-shadow: var(--shadow-sm); }}
        #monthly-cards .month-card {{ display: none; }}
        .month-card table {{ width: 100%; border-collapse: collapse; font-size: 0.75rem; margin-top: 0.5rem; }}
        .month-card th {{
            text-align: left; padding: 0.375rem 0.5rem;
            font-size: 0.625rem; font-weight: 600; color: var(--text-subtle);
            text-transform: uppercase; letter-spacing: 0.04em;
            background: var(--surface-muted); border-radius: 4px;
        }}
        .month-card th:not(:first-child) {{ text-align: right; }}
        .month-card td {{ padding: 0.375rem 0.5rem; border-top: 1px solid var(--border); }}
        .month-card td:not(:first-child) {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .ret-badge {{
            font-size: 0.625rem; font-weight: 600;
            padding: 0.125rem 0.5rem; border-radius: 999px;
        }}
        .ret-badge-pos {{ background: rgba(5, 150, 105, 0.1); color: var(--pos); }}
        .ret-badge-neg {{ background: rgba(225, 29, 72, 0.08); color: var(--neg); }}
    </style>
</head>
<body class="antialiased">
    <div class="page">
        <header class="flex items-end justify-between gap-4 mb-8">
            <div>
                <p class="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-400 mb-1.5">Portfolio Report</p>
                <h1 class="font-display text-[2rem] text-slate-700 leading-none">Minvest</h1>
            </div>
            <span class="date-badge">{date_range}</span>
        </header>

        <section class="panel panel-padded section-block">
            <div class="section-subtitle">Performance</div>
            <div class="metric-grid performance-grid">
""")

        for label, value in performance_kpis:
            _write_metric_card(f, label, value, _kpi_text_color(label, value))

        f.write("""
            </div>
        </section>

        <section class="section-block">
            <div class="section-head">
                <div class="section-title">策略 vs 基准对比</div>
            </div>
            <div class="panel overflow-hidden">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>名称</th>
                            <th>累计回报</th>
                            <th>年化收益</th>
                            <th>最大回撤</th>
                            <th>夏普</th>
                            <th>胜率</th>
                        </tr>
                    </thead>
                    <tbody>
""")
        for name in benchmark_names:
            if name not in all_metrics:
                continue
            m = all_metrics[name]
            row_cls = "row-highlight" if name == "策略" else ""
            dot_color = colors_map.get(name, "#6b7280")
            ret_cls = "val-pos" if m["total_return"] >= 0 else "val-neg"
            cagr_cls = "val-pos" if m["cagr"] >= 0 else "val-neg"
            f.write(f"""
                        <tr class="{row_cls}">
                            <td class="font-medium">
                                <span class="legend-dot" style="background:{dot_color}"></span>{name}
                            </td>
                            <td class="{ret_cls}">{fmt_pct(m["total_return"])}</td>
                            <td class="{cagr_cls}">{fmt_pct(m["cagr"])}</td>
                            <td class="val-neg">{fmt_pct(m["max_drawdown"])}</td>
                            <td class="val-neutral">{m["sharpe"]:.2f}</td>
                            <td class="val-neutral">{m["win_rate"] * 100:.0f}%</td>
                        </tr>
""")
        f.write("""
                    </tbody>
                </table>
            </div>
        </section>
""")

        mtd_val = portfolio_mtd * 100
        mtd_color = "val-pos" if mtd_val >= 0 else "val-neg"
        f.write(f"""
        <section class="section-block">
            <div class="section-head">
                <div class="section-title">本月收益率</div>
                <div id="mtd-live" class="mtd-badge">
                    <span class="{mtd_color}">{mtd_val:.1f}%</span><span class="mtd-live-tag">实时</span>
                </div>
            </div>
            <div class="chart-panel">
                <div id="tv-latest-kchart" style="width: 100%; height: 260px;"></div>
            </div>
        </section>

        <section class="section-block">
            <div class="section-head">
                <div class="section-title">累计权益曲线</div>
            </div>
            <div class="chart-panel" style="height: 400px;">
                <div id="tv-equity-chart" style="width: 100%; height: 100%;"></div>
            </div>
        </section>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-5 section-block">
            <div>
                <div class="section-title mb-3">最大回撤</div>
                <div class="chart-panel" style="height: 260px;">
                    <div id="tv-drawdown-chart" style="width: 100%; height: 100%;"></div>
                </div>
            </div>
            <div>
                <div class="section-title mb-3">回撤分布</div>
                <div class="chart-panel" style="height: 260px;">
                    <div id="tv-dd-dist-chart" style="width: 100%; height: 100%;"></div>
                </div>
            </div>
        </div>

        <section class="section-block">
            <div class="section-head">
                <div class="section-title">月度回报分布</div>
            </div>
            <div class="chart-panel" style="height: 260px;">
                <div id="tv-monthly-chart" style="width: 100%; height: 100%;"></div>
            </div>
        </section>

        <section class="section-block">
            <div class="section-head">
                <div class="section-title">年度 & 月度持仓</div>
            </div>
            <div class="flex items-center gap-3 mb-5">
                <label class="text-xs font-semibold uppercase tracking-wider text-zinc-400">年份</label>
                <select id="year-select" class="year-select" onchange="filterByYear()">
""")
        for y in years:
            sel = ' selected' if y == default_year else ''
            f.write(f'                        <option value="{y}"{sel}>{y}</option>\n')
        f.write("""
                    </select>
                <div id="year-return-display" class="text-sm font-semibold min-w-[100px]"></div>
            </div>
            <div id="monthly-cards" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
""")
        for m in monthly_data:
            ret = m['monthly_return']
            ret_class = "ret-badge-pos" if ret > 0 else "ret-badge-neg"
            ret_str = f"{ret*100:+.2f}%"
            month_details = details_by_month.get(m['month'], [])
            if month_details:
                table_rows = ''
                for d in month_details:
                    t_ret = d['monthly_return']
                    t_ret_class = "val-pos" if t_ret > 0 else "val-neg"
                    t_ret_str = f"{t_ret*100:+.2f}%"
                    table_rows += f'''
                        <tr>
                            <td class="font-mono font-semibold">{d['ticker']}</td>
                            <td>{d['buy_price']:.2f}</td>
                            <td>{d['sell_price']:.2f}</td>
                            <td class="{t_ret_class}">{t_ret_str}</td>
                        </tr>
                    '''
                table_html = f'''
                    <table>
                        <thead>
                            <tr>
                                <th>标的</th>
                                <th>买入价格</th>
                                <th>卖出价</th>
                                <th>回报</th>
                            </tr>
                        </thead>
                        <tbody>{table_rows}</tbody>
                    </table>
                '''
            else:
                table_html = '<div class="text-xs text-zinc-400 mt-1">现金持仓</div>'
            f.write(f"""
                <div class="month-card" data-year="{m['year']}" data-month="{m['month']}">
                    <div class="flex justify-between items-start mb-1">
                        <div>
                            <div class="font-semibold text-[13px] tracking-tight">{m['month']}</div>
                            <div class="text-[10px] text-zinc-400 mt-0.5">{m['buy_date']} → {m['sell_date']}</div>
                        </div>
                        <div class="ret-badge {ret_class}">{ret_str}</div>
                    </div>
                    {table_html}
                </div>
""")
        f.write("""
            </div>
        </section>
""")

        f.write("""
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
            const defaultYear = """ + json.dumps(default_year) + """;

            const TV_BASE = {
                layout: { background: { color: 'transparent' }, textColor: '#71717a', fontSize: 11 },
                grid: { vertLines: { color: 'rgba(24,24,27,0.04)' }, horzLines: { color: 'rgba(24,24,27,0.04)' } },
                timeScale: { borderColor: 'rgba(24,24,27,0.08)', timeVisible: false, secondsVisible: false },
                crosshair: { mode: 0, vertLine: { color: 'rgba(24,24,27,0.15)', width: 1, style: 3 }, horzLine: { color: 'rgba(24,24,27,0.15)', width: 1, style: 3 } },
            };

            function createTVChart(container, height, extra = {}) {
                if (!container || typeof LightweightCharts === 'undefined') return null;
                return LightweightCharts.createChart(container, {
                    width: container.clientWidth || 800,
                    height: container.clientHeight || height,
                    ...TV_BASE,
                    rightPriceScale: { borderColor: '#e5e7eb', ...(extra.rightPriceScale || {}) },
                    ...extra,
                });
            }

            function bindChartResize(chart, container, height) {
                const resize = () => chart.resize(container.clientWidth || 800, container.clientHeight || height);
                window.addEventListener('resize', resize);
                setTimeout(() => { resize(); try { chart.timeScale().fitContent(); } catch(e) {} }, 80);
            }

            function showChartError(container, msg) {
                container.innerHTML = `<div style="padding:12px;color:#b91c1c;font-size:12px;background:#fef2f2;border-radius:6px;">${msg}</div>`;
            }

            function initMultiLineChart(containerId, seriesMap, options = {}) {
                const container = document.getElementById(containerId);
                if (!container || !seriesMap || Object.keys(seriesMap).length === 0) return;
                const height = options.height || 240;
                try {
                    const chart = createTVChart(container, height, options.chartOpts || {});
                    if (!chart || typeof chart.addLineSeries !== 'function') {
                        showChartError(container, options.errorMsg || '图表初始化失败');
                        return;
                    }
                    Object.keys(seriesMap).forEach(name => {
                        const data = seriesMap[name];
                        if (!data || data.length === 0) return;
                        const lw = options.lineWidth ? options.lineWidth(name) : (name === '策略' ? 2 : 1.5);
                        chart.addLineSeries({ color: chartColors[name] || '#6b7280', lineWidth: lw, title: name }).setData(data);
                    });
                    bindChartResize(chart, container, height);
                } catch (err) {
                    console.error(containerId, err);
                    showChartError(container, options.errorMsg || '图表创建出错');
                }
            }

            function initHistogramChart(containerId, data, options = {}) {
                const container = document.getElementById(containerId);
                if (!container || !data || data.length === 0) return;
                const height = options.height || 240;
                try {
                    const chart = createTVChart(container, height, options.chartOpts || {});
                    const series = chart.addHistogramSeries({ title: options.title || '', color: options.color });
                    series.setData(data);
                    bindChartResize(chart, container, height);
                } catch (err) {
                    console.error(containerId, err);
                    showChartError(container, options.errorMsg || '图表创建出错');
                }
            }

            function waitForCharts(callback, container, failMsg) {
                if (typeof LightweightCharts !== 'undefined' && typeof LightweightCharts.createChart === 'function') {
                    callback();
                    return;
                }
                let attempts = 0;
                const iv = setInterval(() => {
                    attempts++;
                    if (typeof LightweightCharts !== 'undefined' && typeof LightweightCharts.createChart === 'function') {
                        clearInterval(iv);
                        callback();
                    } else if (attempts > 12) {
                        clearInterval(iv);
                        if (container) container.innerHTML = `<div style="padding:12px;color:#666;font-size:12px;">${failMsg}</div>`;
                    }
                }, 100);
            }

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
                        const retClass = ret >= 0 ? 'val-pos' : 'val-neg';
                        retDisplay.innerHTML = `<span class="${retClass}">年收益 ${retStr}</span>`;
                    } else {
                        retDisplay.innerHTML = '';
                    }
                }

            }

            function initTradingViewEquity() {
                const container = document.getElementById('tv-equity-chart');
                waitForCharts(() => initMultiLineChart('tv-equity-chart', tvEquityData, {
                    height: 400,
                    lineWidth: (name) => name === '策略' ? 3 : 2,
                    chartOpts: { rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 } }, legend: { visible: true, position: 'top' } },
                    errorMsg: '累计权益曲线创建出错，请刷新重试。',
                }), container, 'TradingView 图表库加载失败，请检查网络后刷新。');
            }

            function initTradingViewDrawdown() {
                initMultiLineChart('tv-drawdown-chart', tvDrawdownSeries, { errorMsg: '回撤图表创建出错，请刷新。' });
            }

            function initTradingViewMonthly() {
                const coloredData = tvMonthlyData.map(d => ({ time: d.time, value: d.value, color: d.value >= 0 ? '#059669' : '#e11d48' }));
                initHistogramChart('tv-monthly-chart', coloredData, { title: '策略月度回报', errorMsg: '月度回报图表创建出错，请刷新。' });
            }

            function initTradingViewDDDist() {
                initHistogramChart('tv-dd-dist-chart', tvDDDistData, {
                    title: '回撤分布', color: '#e11d48',
                    chartOpts: { timeScale: { ...TV_BASE.timeScale, tickMarkFormatter: (time) => ((time - 100000) / 100).toFixed(1) + '%' } },
                    errorMsg: '回撤分布图表出错。',
                });
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
                if (window.refreshMTD) window.refreshMTD();
            }

            async function initLatestKChart() {
                const container = document.getElementById('tv-latest-kchart');
                if (!container || !latestKMetadata?.tickers?.length) return;
                try {
                    const chart = createTVChart(container, 260, {
                        timeScale: { ...TV_BASE.timeScale, rightOffset: 3, barSpacing: 18, minBarWidth: 4 },
                    });
                    if (!chart) return;
                    const colors = ['#059669', '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899'];
                    const seriesMap = {};
                    latestKMetadata.tickers.forEach((t, i) => {
                        const s = chart.addLineSeries({ color: colors[i % colors.length], lineWidth: 2, title: t });
                        seriesMap[t] = s;
                        if (latestKFallback?.[t]) s.setData(latestKFallback[t]);
                    });
                    try { chart.timeScale().fitContent(); } catch(e) {}
                    if (window.liveMTDs === undefined) window.liveMTDs = { ...mtdReturnsReport };
                    window.refreshMTD = function() {
                        const vals = Object.values(window.liveMTDs);
                        const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
                        const el = document.getElementById('mtd-live');
                        if (el) {
                            const pctStr = (avg * 100).toFixed(1);
                            const numClass = avg >= 0 ? 'val-pos' : 'val-neg';
                            el.innerHTML = `<span class="${numClass}">${pctStr}%</span><span class="mtd-live-tag">实时</span>`;
                        }
                    };
                    window.refreshMTD();
                    await Promise.all(latestKMetadata.tickers.map(async (t) => {
                        try {
                            const firstOpen = (latestKMetadata.firstOpens || latestKMetadata.firstCloses || {})[t];
                            const closes = await fetchYahooCloses(t, latestKMetadata.startDate);
                            if (closes.length > 0 && firstOpen) {
                                const seriesData = closes.map(d => ({ time: d.time, value: d.value / firstOpen - 1 }));
                                seriesMap[t].setData(seriesData);
                                updateMTDLive(t, seriesData[seriesData.length - 1].value);
                            }
                        } catch (e) {
                            console.warn('Live fetch failed for ' + t, e);
                        }
                    }));
                    bindChartResize(chart, container, 260);
                } catch (err) {
                    console.error('TV latest K error:', err);
                    container.innerHTML = '<div style="padding:4px;color:#666;font-size:10px;">走势图表出错，使用报告数据。</div>';
                }
            }

            function initializeAll() {
                const sel = document.getElementById('year-select');
                if (sel) {
                    const opts = Array.from(sel.options).map(o => o.value);
                    sel.value = opts.includes(defaultYear) ? defaultYear : (opts[0] || '');
                    filterByYear();
                }
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
    """计算回测指标（原生实现，无需 QuantStats）"""
    _keys = [
        "total_return", "cagr", "volatility", "sharpe", "sortino",
        "worst_monthly_return", "calmar", "win_rate", "median_return",
        "profit_factor", "max_drawdown",
    ]
    rets = returns.dropna()
    if len(rets) == 0:
        return {k: 0.0 for k in _keys}

    total_return = (1 + rets).prod() - 1
    n_months = len(rets)
    cagr = (1 + total_return) ** (12 / n_months) - 1 if n_months > 0 else 0.0
    volatility = rets.std() * np.sqrt(12) if n_months > 1 else 0.0
    sharpe = cagr / volatility if volatility > 1e-12 else 0.0

    negative_rets = rets[rets < 0]
    if len(negative_rets) >= 2:
        downside = negative_rets.std() * np.sqrt(12)
        sortino = cagr / downside if downside > 1e-12 else 0.0
    else:
        sortino = 0.0

    worst_monthly_return = rets.min()
    calmar = cagr / abs(worst_monthly_return) if abs(worst_monthly_return) > 1e-12 else 0.0

    gains = rets[rets > 0].sum()
    losses = abs(rets[rets < 0].sum())
    profit_factor = gains / losses if losses > 1e-12 else (99.99 if gains > 0 else 0.0)

    equity = (1 + rets).cumprod()
    peak = equity.cummax()
    max_drawdown = ((equity - peak) / peak).min()

    return {
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "worst_monthly_return": worst_monthly_return,
        "calmar": calmar,
        "win_rate": (rets > 0).mean(),
        "median_return": rets.median(),
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
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
