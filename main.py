from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
import sys

from data_fetcher import (
    build_universe,
    connect_ib,
    fetch_daily_history,
    prepare_all_features,
    resolve_asof_date,
    get_cache_filename,
    load_price_cache,
    save_price_cache,
    DEFAULT_HISTORY_FILE,
    DEFAULT_BENCHMARK,
    DEFAULT_DURATION,
    DEFAULT_MIN_MARKET_CAP,
)
from stock_selector import score_universe, pick_rows_to_frame
from tester import evaluate_against_history, backtest_monthly_returns


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QuantGT 月度选股策略（支持过去任意年数回测 + HTML仪表盘）")
    parser.add_argument(
        "command",
        nargs="?",
        default="select",
        choices=["select", "evaluate", "backtest"],
    )
    parser.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--universe-source", choices=["nasdaq", "file", "history"], default="nasdaq")
    parser.add_argument("--universe-file", type=Path, default=None)
    parser.add_argument("--min-market-cap", type=float, default=DEFAULT_MIN_MARKET_CAP)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=17)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--duration", default=DEFAULT_DURATION)
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--start-month", default=None)
    parser.add_argument("--years", type=int, default=1, help="回测过去多少年（默认1年，指定 --years 6 即可回测过去6年）")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--use-cache", action="store_true", default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    universe = build_universe(
        universe_source=args.universe_source,
        min_market_cap=args.min_market_cap,
        history_file=args.history_file if args.history_file.exists() else None,
        universe_file=args.universe_file,
    )
    tickers = sorted(set(universe + [args.benchmark]))

    print(f"[info] universe size: {len(universe)}", file=sys.stderr)

    # ==================== 价格数据（缓存） ====================
    ib = connect_ib(args.host, args.port, args.client_id)
    try:
        end_date = pd.to_datetime(args.asof) if args.asof else None
        cache_file = get_cache_filename(args.benchmark, args.duration, args.asof)

        if args.use_cache:
            price_map = load_price_cache(cache_file)
            if price_map is None:
                print("[info] 缓存不存在，开始从IB下载...", file=sys.stderr)
                price_map = fetch_daily_history(ib, tickers, end_date, args.duration, args.pause_seconds)
                save_price_cache(price_map, cache_file)
        else:
            price_map = fetch_daily_history(ib, tickers, end_date, args.duration, args.pause_seconds)
            save_price_cache(price_map, cache_file)
    finally:
        ib.disconnect()

    features = prepare_all_features(price_map, args.benchmark)

    if args.command == "evaluate":
        # （保持不变）
        summary, detail = evaluate_against_history(features, args.history_file)
        print(summary.to_string(index=False))
        print("\n" + detail.tail(20).to_string(index=False))
        if args.output:
            detail.to_csv(args.output, index=False)
        return 0

    elif args.command == "backtest":
        df_summary, df_detail = backtest_monthly_returns(
            price_map=price_map,
            features=features,
            benchmark_ticker=args.benchmark,
            start_month=args.start_month,
            top_n=args.top_n,
            years=args.years,          # ← 新增传递
        )

        if args.output:
            output_path = args.output.with_suffix(".xlsx") if args.output.suffix.lower() != ".xlsx" else args.output
            with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
                df_summary.to_excel(writer, sheet_name="月度汇总", index=False)
                df_detail.to_excel(writer, sheet_name="个股明细", index=False)
            print(f"[info] 回测结果已保存 → {output_path}")

            # 生成HTML Dashboard
            html_path = output_path.with_name(f"backtest_dashboard_{args.years}years.html")
            generate_html_dashboard(df_summary, df_detail, html_path, years=args.years)
            print(f"[info] HTML仪表盘已生成 → {html_path}")
        return 0

    # select 命令（保持不变）
    asof_date = resolve_asof_date(price_map, args.benchmark, args.asof)
    ranked = score_universe(features, asof_date)
    top = pick_rows_to_frame(ranked[: args.top])
    print(f"Scored through {asof_date.date().isoformat()}")
    print(top.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    if args.output:
        top.to_csv(args.output, index=False)
    return 0


def generate_html_dashboard(df_summary: pd.DataFrame, df_detail: pd.DataFrame, output_path: Path, years: int = 1):
    """生成HTML仪表盘（已适配过去N年标题）"""
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=3, cols=1,
        subplot_titles=("累计回报曲线", "月度回报率", "最大回撤"),
        vertical_spacing=0.12,
        row_heights=[0.4, 0.3, 0.3]
    )

    fig.add_trace(go.Scatter(x=df_summary["month"], y=df_summary["cumulative_return"],
                             mode="lines+markers", name="累计回报"), row=1, col=1)
    fig.add_trace(go.Bar(x=df_summary["month"], y=df_summary["monthly_return"],
                         name="月度回报", marker_color="#2ca02c"), row=2, col=1)

    cum_max = df_summary["cumulative_return"].cummax()
    drawdown = df_summary["cumulative_return"] - cum_max
    fig.add_trace(go.Scatter(x=df_summary["month"], y=drawdown,
                             mode="lines", name="回撤", fill="tozeroy", line_color="red"), row=3, col=1)

    fig.update_layout(
        height=900,
        title_text=f"QuantGT 策略回测仪表盘（过去 {years} 年）",
        showlegend=False
    )
    fig.write_html(str(output_path), include_plotlyjs="cdn")
    print(f"[info] HTML Dashboard 已生成（过去 {years} 年，共 {len(df_summary)} 个月）")


if __name__ == "__main__":
    raise SystemExit(main())