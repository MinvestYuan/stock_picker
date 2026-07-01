from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.tester import open_at
from config import COST_PER_TRADE, DEFAULT_BENCHMARK, DEFAULT_TOP_N
from strategy.risk_overlay import is_qqq_bear_market
from strategy.stock_selector import score_universe
from utils.logconf import get_logger
from utils.market_calendar import next_trading_day

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent


def add_benchmark_returns(
    df_summary: pd.DataFrame,
    price_map: dict,
    benchmark_ticker: str,
    col_prefix: str | None = None,
) -> pd.DataFrame:
    """为 summary 添加 benchmark 的月度收益列。"""
    prefix = col_prefix or "benchmark"
    if benchmark_ticker not in price_map:
        logger.warning("价格缓存中不存在 benchmark %s，将使用 0 填充其收益", benchmark_ticker)
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
        logger.warning(
            "%s 有 %d/%d 个月份的价格数据缺失，使用 0 填充",
            benchmark_ticker, missing_dates, len(df_summary),
        )

    df_summary[f"{prefix}_return"] = benchmark_rets
    df_summary[f"{prefix}_cumulative"] = (1 + df_summary[f"{prefix}_return"]).cumprod() - 1
    return df_summary


def compute_next_signals(
    price_map: dict,
    features: dict,
    universe_tickers: list[str],
    top_n: int = DEFAULT_TOP_N,
    momentum_col: str = "momentum",
) -> tuple[pd.Timestamp | None, pd.Timestamp | None, list]:
    """基于最新数据和 universe，计算下一个开盘日应该买入的前 N 只股票。"""
    benchmark_ticker = DEFAULT_BENCHMARK
    if benchmark_ticker not in price_map:
        return None, None, []

    benchmark_dates = price_map[benchmark_ticker].index
    asof_date = pd.to_datetime(benchmark_dates.max())
    next_trade_date = next_trading_day(asof_date)

    if is_qqq_bear_market(price_map, asof_date):
        return asof_date, next_trade_date, []

    universe_features = {t: features[t] for t in universe_tickers if t in features}
    if not universe_features:
        return asof_date, next_trade_date, []

    ranked = score_universe(universe_features, asof_date, momentum_col=momentum_col)
    return asof_date, next_trade_date, ranked[:top_n]


def _drawdown_from_cumulative(cum_returns: pd.Series) -> pd.Series:
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


def _calculate_metrics(returns: pd.Series) -> dict:
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

    ann_mean = rets.mean() * 12
    sharpe = ann_mean / volatility if volatility > 1e-12 else 0.0

    downside = np.sqrt((np.minimum(rets, 0) ** 2).mean()) * np.sqrt(12)
    sortino = ann_mean / downside if downside > 1e-12 else 0.0

    worst_monthly_return = rets.min()

    equity = (1 + rets).cumprod()
    peak = equity.cummax()
    max_drawdown = ((equity - peak) / peak).min()

    calmar = cagr / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else 0.0

    gains = rets[rets > 0].sum()
    losses = abs(rets[rets < 0].sum())
    profit_factor = gains / losses if losses > 1e-12 else (99.99 if gains > 0 else 0.0)

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


def _fmt_pct(v: float) -> str:
    return f"{v:.1%}" if abs(v) < 10 else f"{v:.0%}"


def _return_color(v: float) -> str:
    return "val-pos" if v >= 0 else "val-neg"


def _build_monthly_data(df_summary: pd.DataFrame) -> tuple[list, list]:
    """把 summary 行转成前端用的 monthly_data，并返回倒序年份列表。"""
    monthly_data = []
    for _, row in df_summary.iterrows():
        top_list = [t.strip() for t in str(row.get("top_tickers", "")).split(",") if t.strip()]
        monthly_data.append({
            "month": row["month"],
            "year": row["month"][:4],
            "buy_date": row["buy_date"],
            "sell_date": row["sell_date"],
            "num_stocks": int(row["num_stocks"]),
            "monthly_return": float(row["monthly_return"]),
            "top_tickers": top_list,
        })
    years = sorted(set(m["year"] for m in monthly_data), reverse=True)
    return monthly_data, years


def _compute_annual_returns(monthly_data: list, years: list) -> dict:
    annual_returns = {}
    for y in years:
        year_ms = [m for m in monthly_data if m["year"] == y]
        if year_ms:
            ret = 1.0
            for m in year_ms:
                ret *= (1 + m["monthly_return"])
            annual_returns[y] = ret - 1
    return annual_returns


def _build_equity_series(df_summary: pd.DataFrame, benchmark: str, extra_benchmarks: list) -> dict:
    series = {
        "策略": [
            {"time": f"{row['month']}-01", "value": round(1.0 + float(row["cumulative_return"]), 6)}
            for _, row in df_summary.iterrows()
        ]
    }
    for bm in [benchmark] + extra_benchmarks:
        col = f"{bm.lower()}_cumulative"
        if col in df_summary.columns:
            series[bm] = [
                {"time": f"{row['month']}-01", "value": round(1.0 + float(row[col]), 6)}
                for _, row in df_summary.iterrows()
            ]
    return series


def _build_drawdown_series(
    df_summary: pd.DataFrame, benchmark: str, extra_benchmarks: list, strategy_drawdown: pd.Series
) -> dict:
    series = {
        "策略": [
            {"time": f"{row['month']}-01", "value": round(float(d), 6)}
            for (_, row), d in zip(df_summary.iterrows(), strategy_drawdown)
        ]
    }
    for bm in [benchmark] + extra_benchmarks:
        col = f"{bm.lower()}_cumulative"
        if col in df_summary.columns:
            bm_dd = _drawdown_from_cumulative(df_summary[col])
            series[bm] = [
                {"time": f"{row['month']}-01", "value": round(float(d), 6)}
                for (_, row), d in zip(df_summary.iterrows(), bm_dd)
            ]
    return series


def _build_return_distribution(monthly_returns: pd.Series) -> list:
    rets = [float(r) for r in monthly_returns if pd.notna(r)]
    if not rets:
        return []
    min_r, max_r = min(rets), max(rets)
    bin_width = 0.05
    low = np.floor(min_r / bin_width) * bin_width
    high = np.ceil(max_r / bin_width) * bin_width
    if high <= low:
        high = low + bin_width
    bin_edges = np.arange(low, high + bin_width * 0.5, bin_width)
    if len(bin_edges) < 3:
        pad = bin_width / 2
        bin_edges = np.linspace(min_r - pad, max_r + pad, 6)
    counts, edges = np.histogram(rets, bins=bin_edges)
    bin_centers = (edges[:-1] + edges[1:]) / 2
    return [
        {"time": round(c * 10000) + 100000, "value": int(cnt)}
        for c, cnt in zip(bin_centers, counts)
    ]


def _build_metrics_map(df_summary: pd.DataFrame, benchmark: str, extra_benchmarks: list) -> dict:
    all_metrics = {"策略": _calculate_metrics(df_summary["monthly_return"])}
    for bm in [benchmark] + extra_benchmarks:
        col = f"{bm.lower()}_return"
        if col in df_summary.columns:
            all_metrics[bm] = _calculate_metrics(df_summary[col])
        else:
            logger.warning(
                "%s 的收益数据在 df_summary 中缺失（列 %s 不存在），HTML 中将不显示该 benchmark",
                bm, col,
            )
    return all_metrics


def _bm_max_naive(price_map: dict, benchmark: str) -> pd.Timestamp | None:
    if not (price_map and benchmark in price_map):
        return None
    bm_max = price_map[benchmark].index.max()
    if hasattr(bm_max, "tz") and bm_max.tz is not None:
        bm_max = bm_max.tz_localize(None)
    return bm_max


def _append_current_month_picks(
    monthly_data: list,
    details_by_month: dict,
    current_month_picks: list | None,
    current_month_buy_date: pd.Timestamp | None,
    price_map: dict | None,
    benchmark: str,
    cost_per_trade: float,
) -> bool:
    """把"本月持仓中"的 MTD 估算追加到 monthly_data / details_by_month。返回是否追加。"""
    current_month_str = datetime.now().strftime("%Y-%m")
    if not (current_month_picks and current_month_str not in [m["month"] for m in monthly_data]):
        return False

    buy_date_cm = current_month_buy_date
    buy_date_str_cm = buy_date_cm.strftime("%Y-%m-%d") if buy_date_cm else ""
    latest_date_cm = pd.Timestamp.now().normalize()
    bm_max = _bm_max_naive(price_map, benchmark)
    if bm_max is not None:
        latest_date_cm = min(latest_date_cm, bm_max)
    latest_date_str_cm = latest_date_cm.strftime("%Y-%m-%d")

    cm_rets = []
    for p in current_month_picks:
        ticker = p.ticker
        if price_map and ticker in price_map:
            try:
                ohlc = price_map[ticker]
                valid_buy_dates = ohlc.index[ohlc.index >= buy_date_cm]
                if len(valid_buy_dates) == 0:
                    continue
                actual_buy_date = valid_buy_dates[0]
                first = open_at(ohlc, actual_buy_date)
                valid_end_dates = ohlc.index[ohlc.index <= latest_date_cm]
                if len(valid_end_dates) == 0:
                    continue
                last_date = valid_end_dates[-1]
                last = float(ohlc.loc[last_date, "close"])
                ret = (last / first - 1 - cost_per_trade) if first != 0 else 0
                cm_rets.append(ret)

                details_by_month[current_month_str].append({
                    "ticker": ticker,
                    "buy_price": round(first, 4),
                    "sell_price": round(last, 4),
                    "monthly_return": round(ret, 6),
                    "total_score": round(getattr(p, "total_score", 0), 4),
                    "momentum_score": round(getattr(p, "momentum_score", 0), 4),
                    "rrg_score": round(getattr(p, "rrg_score", 0), 4),
                    "close_over_ema50": round(getattr(p, "close_over_ema50", 0), 4),
                })
            except Exception as e:
                logger.warning("计算本月持仓 %s 的 MTD 收益失败: %s", ticker, e)

    cm_portfolio_ret = float(np.mean(cm_rets)) if cm_rets else 0.0
    monthly_data.append({
        "month": current_month_str,
        "year": current_month_str[:4],
        "buy_date": buy_date_str_cm,
        "sell_date": f"{latest_date_str_cm} (持仓中)",
        "num_stocks": len(cm_rets),
        "monthly_return": cm_portfolio_ret,
        "top_tickers": [p.ticker for p in current_month_picks],
    })
    return True


def _resolve_latest_k_window(
    df_summary: pd.DataFrame,
    df_detail: pd.DataFrame,
    current_month_picks: list | None,
    current_month_buy_date: pd.Timestamp | None,
    next_picks: list | None,
    next_trade_date: pd.Timestamp | None,
    price_map: dict | None,
    benchmark: str,
) -> tuple[list, pd.Timestamp, pd.Timestamp]:
    """确定"最新持仓 K 线图"展示的 ticker 列表与买入/截止日期。"""
    bm_max = _bm_max_naive(price_map, benchmark)

    if current_month_picks:
        top5_tickers = [p.ticker for p in current_month_picks]
        buy_date = current_month_buy_date
        sell_date = pd.Timestamp.now().normalize()
        if bm_max is not None:
            sell_date = min(sell_date, bm_max)
        return top5_tickers, buy_date, sell_date

    if next_picks:
        top5_tickers = [p.ticker for p in next_picks]
        sell_date = pd.Timestamp.now().normalize()
        if bm_max is not None:
            sell_date = min(sell_date, bm_max)
        if next_trade_date and next_trade_date <= sell_date:
            buy_date = next_trade_date
        else:
            month_start = pd.to_datetime(datetime.now().strftime("%Y-%m-01"))
            buy_date = month_start
            if price_map and benchmark in price_map:
                bm_dates = price_map[benchmark].index
                valid = bm_dates[(bm_dates >= month_start) & (bm_dates <= sell_date)]
                if len(valid) > 0:
                    buy_date = valid[0]
        return top5_tickers, buy_date, sell_date

    latest_month = df_summary["month"].iloc[-1]
    if df_detail.empty or "month" not in df_detail.columns:
        top5_tickers = []
    else:
        latest_details = df_detail[df_detail["month"] == latest_month]
        top5_tickers = latest_details["ticker"].head(5).tolist()
    latest_row = df_summary[df_summary["month"] == latest_month].iloc[0]
    buy_date = pd.to_datetime(latest_row["buy_date"])
    sell_date = pd.to_datetime(latest_row["sell_date"])
    return top5_tickers, buy_date, sell_date


def _build_latest_k(
    top5_tickers: list,
    buy_date: pd.Timestamp,
    sell_date: pd.Timestamp,
    price_map: dict | None,
    cost_per_trade: float,
) -> tuple[dict, dict, dict, float]:
    """构建最新持仓 K 线元数据、回退曲线与 MTD 收益。"""
    latest_k_metadata = {
        "tickers": top5_tickers,
        "startDate": buy_date.strftime("%Y-%m-%d"),
        "firstOpens": {},
    }
    latest_k_fallback: dict = {}
    mtd_returns: dict = {}
    for t in top5_tickers:
        if price_map and t in price_map:
            ohlc = price_map[t]
            try:
                end_date = min(sell_date, ohlc.index.max())
                valid_dates = ohlc.index[ohlc.index >= buy_date]
                if len(valid_dates) == 0:
                    continue
                actual_buy_date = valid_dates[0]
                if actual_buy_date > end_date:
                    continue
                month_df = ohlc.loc[actual_buy_date.strftime("%Y-%m-%d"): end_date.strftime("%Y-%m-%d")]
                if len(month_df) > 0:
                    first = open_at(ohlc, actual_buy_date)
                    latest_k_metadata["firstOpens"][t] = first
                    latest_k_fallback[t] = [
                        {"time": d.strftime("%Y-%m-%d"), "value": float(row["close"] / first - 1)}
                        for d, row in month_df.iterrows()
                    ]
                    last = float(month_df["close"].iloc[-1])
                    mtd = (last / first - 1 - cost_per_trade) if first != 0 else 0
                    mtd_returns[t] = mtd
            except Exception as e:
                logger.warning("构建 %s 的最新持仓 K 线失败: %s", t, e)

    portfolio_mtd = sum(mtd_returns.values()) / len(mtd_returns) if mtd_returns else 0
    return latest_k_metadata, latest_k_fallback, mtd_returns, portfolio_mtd


def _build_kpis(
    current_year: str,
    ytd_return: float,
    strategy_metrics: dict,
    surv_diag: dict,
) -> list:
    missing_pct = surv_diag.get("missing_pct", 0.0)
    missing_slots = surv_diag.get("missing_slots", 0)
    total_slots = surv_diag.get("total_pick_slots", 0)

    performance_kpis = [
        (f"{current_year} YTD", _fmt_pct(ytd_return)),
        ("累计回报", _fmt_pct(strategy_metrics.get("total_return", 0))),
        ("年化收益", _fmt_pct(strategy_metrics.get("cagr", 0))),
        ("最大回撤", _fmt_pct(strategy_metrics.get("max_drawdown", 0))),
        ("夏普比率", f"{strategy_metrics.get('sharpe', 0):.2f}"),
        ("胜率", f"{strategy_metrics.get('win_rate', 0) * 100:.0f}%"),
        ("索提诺比率", f"{strategy_metrics.get('sortino', 0):.2f}"),
        ("Calmar 比率", f"{strategy_metrics.get('calmar', 0):.2f}"),
        ("年化波动率", _fmt_pct(strategy_metrics.get("volatility", 0))),
        ("盈亏比", f"{strategy_metrics.get('profit_factor', 0):.2f}"),
        ("最差月份", _fmt_pct(strategy_metrics.get("worst_monthly_return", 0))),
        ("中位月回报", _fmt_pct(strategy_metrics.get("median_return", 0))),
    ]
    if total_slots > 0:
        performance_kpis.append(
            ("无价仓位占比", f"{missing_slots}/{total_slots} ({missing_pct:.1%})")
        )

    return [
        {"label": label, "value": value, "text_color": _kpi_text_color(label, value)}
        for label, value in performance_kpis
    ]


def _build_comparison_rows(all_metrics: dict, colors_map: dict) -> list:
    comparison_rows = []
    for name, m in all_metrics.items():
        total_ret = m.get("total_return", 0)
        cagr = m.get("cagr", 0)
        comparison_rows.append({
            "row_cls": "row-highlight" if name == "策略" else "",
            "dot_color": colors_map.get(name, "#888888"),
            "name": name,
            "ret_cls": _return_color(total_ret),
            "total_return": _fmt_pct(total_ret),
            "cagr_cls": _return_color(cagr),
            "cagr": _fmt_pct(cagr),
            "max_drawdown": _fmt_pct(m.get("max_drawdown", 0)),
            "sharpe": f"{m.get('sharpe', 0):.2f}",
            "win_rate": f"{m.get('win_rate', 0) * 100:.0f}%",
        })
    return comparison_rows


def _render_html(payload: dict, output_path: Path) -> None:
    template_dir = ROOT_DIR / "templates"
    template_html = (template_dir / "report_template.html").read_text(encoding="utf-8")
    script_js = (template_dir / "report.js").read_text(encoding="utf-8")

    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = template_html.replace("__REPORT_DATA__", payload_json)
    html = html.replace("__REPORT_SCRIPT__", script_js)

    output_path.write_text(html, encoding="utf-8")


def generate_backtest_html(
    df_summary: pd.DataFrame,
    df_detail: pd.DataFrame,
    output_path: Path,
    benchmark: str = DEFAULT_BENCHMARK,
    extra_benchmarks: list[str] | None = None,
    price_map: dict[str, pd.DataFrame] | None = None,
    mtd_snapshot: dict | None = None,
    next_picks: list | None = None,
    next_trade_date: pd.Timestamp | None = None,
    asof_date: pd.Timestamp | None = None,
    current_month_picks: list | None = None,
    current_month_buy_date: pd.Timestamp | None = None,
    cost_per_trade: float = COST_PER_TRADE,
):
    """生成回测 HTML 仪表盘。"""
    extra_benchmarks = extra_benchmarks or []
    months = len(df_summary)
    if months == 0:
        return

    monthly_data, years = _build_monthly_data(df_summary)
    annual_returns = _compute_annual_returns(monthly_data, years)
    tv_equity_series = _build_equity_series(df_summary, benchmark, extra_benchmarks)
    details_by_month = defaultdict(list)
    for _, row in df_detail.iterrows():
        details_by_month[row["month"]].append({
            "ticker": row["ticker"],
            "buy_price": row.get("buy_price", 0),
            "sell_price": row.get("sell_price", 0),
            "monthly_return": row.get("monthly_return", 0),
            "total_score": row.get("total_score", 0),
            "momentum_score": row.get("momentum_score", 0),
            "rrg_score": row.get("rrg_score", 0),
            "close_over_ema50": row.get("close_over_ema50", 0),
        })

    if _append_current_month_picks(
        monthly_data, details_by_month, current_month_picks, current_month_buy_date,
        price_map, benchmark, cost_per_trade,
    ):
        monthly_data.sort(key=lambda x: x["month"])
        years = sorted(set(m["year"] for m in monthly_data), reverse=True)
        annual_returns = _compute_annual_returns(monthly_data, years)

    current_year = str(datetime.now().year)
    ytd_months = [m for m in monthly_data if m["year"] == current_year]
    ytd_return = 0.0
    if ytd_months:
        ytd = 1.0
        for m in ytd_months:
            ytd *= (1 + m["monthly_return"])
        ytd_return = ytd - 1
    logger.info("%s YTD 收益: %.4f%% (基于 %d 个月数据)", current_year, ytd_return * 100, len(ytd_months))

    all_metrics = _build_metrics_map(df_summary, benchmark, extra_benchmarks)
    strategy_metrics = all_metrics["策略"]
    drawdown = _drawdown_from_cumulative(df_summary["cumulative_return"])

    colors_map = {"策略": "#059669", benchmark: "#3b82f6"}
    extra_colors = ["#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6"]
    for i, bm in enumerate(extra_benchmarks):
        colors_map[bm] = extra_colors[i % len(extra_colors)]

    tv_drawdown_series = _build_drawdown_series(df_summary, benchmark, extra_benchmarks, drawdown)
    tv_monthly_data = [
        {"time": f"{row['month']}-01", "value": round(float(row["monthly_return"]), 6)}
        for _, row in df_summary.iterrows()
    ]
    tv_return_dist_data = _build_return_distribution(df_summary["monthly_return"])

    top5_tickers, buy_date, sell_date = _resolve_latest_k_window(
        df_summary, df_detail, current_month_picks, current_month_buy_date,
        next_picks, next_trade_date, price_map, benchmark,
    )
    latest_k_metadata, latest_k_fallback, mtd_returns, portfolio_mtd = _build_latest_k(
        top5_tickers, buy_date, sell_date, price_map, cost_per_trade,
    )

    if mtd_snapshot:
        latest_k_metadata = mtd_snapshot.get("metadata", latest_k_metadata)
        latest_k_fallback = mtd_snapshot.get("fallback", latest_k_fallback)
        mtd_returns = mtd_snapshot.get("returns", mtd_returns)
        portfolio_mtd = mtd_snapshot.get("portfolio_mtd", portfolio_mtd)

    date_range = f"{df_summary['month'].iloc[0]} 至 {df_summary['month'].iloc[-1]}"
    next_date_str = next_trade_date.strftime("%Y-%m-%d") if next_trade_date else "N/A"
    asof_date_str = asof_date.strftime("%Y-%m-%d") if asof_date else "N/A"
    next_picks_data = [
        {
            "ticker": p.ticker,
            "total_score": float(p.total_score),
            "momentum_score": float(p.momentum_score),
            "rrg_score": float(p.rrg_score),
            "close_over_ema50": float(p.close_over_ema50),
        }
        for p in (next_picks or [])
    ]

    default_year = current_year if current_year in years else years[0]
    surv_diag = df_summary.attrs.get("survivorship_diagnostic", {})

    payload = {
        "date_range": date_range,
        "asof_date_str": asof_date_str,
        "next_date_str": next_date_str,
        "next_picks": next_picks_data,
        "performance_kpis": _build_kpis(current_year, ytd_return, strategy_metrics, surv_diag),
        "comparison_rows": _build_comparison_rows(all_metrics, colors_map),
        "colors_map": colors_map,
        "annual_returns": annual_returns,
        "years": years,
        "default_year": default_year,
        "monthly_data": monthly_data,
        "details_by_month": dict(details_by_month),
        "portfolio_mtd": portfolio_mtd,
        "mtd_returns": mtd_returns,
        "tv_equity_series": tv_equity_series,
        "tv_drawdown_series": tv_drawdown_series,
        "tv_monthly_data": tv_monthly_data,
        "tv_return_dist_data": tv_return_dist_data,
        "latest_k_metadata": latest_k_metadata,
        "latest_k_fallback": latest_k_fallback,
    }

    _render_html(payload, output_path)
