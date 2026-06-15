"""从现有 index.html 内嵌数据重新生成优化后的报告。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import generate_backtest_html  # noqa: E402


def _extract_json(name: str, html: str):
    m = re.search(rf"const {name} = (\{{.*?\}}|\[.*?\]);", html, re.DOTALL)
    if not m:
        return None
    return json.loads(m.group(1))


def _parse_month_cards(html: str) -> tuple[list[dict], list[dict]]:
    card_re = re.compile(
        r'<div class="month-card[^"]*" data-year="(\d+)" data-month="([\d-]+)">'
        r'.*?<div class="font-semibold[^"]*">([\d-]+)</div>'
        r'.*?<div class="text-\[10px\][^"]*">([^<]+)</div>'
        r'.*?bg-(?:emerald|rose)-100[^"]*">([^<]+)</div>'
        r"(.*?)</div>\s*</div>",
        re.DOTALL,
    )
    row_re = re.compile(
        r'<td class="[^"]*font-mono[^"]*">([^<]+)</td>'
        r'.*?<td class="[^"]*text-right[^"]*">([\d.]+)</td>'
        r'.*?<td class="[^"]*text-right[^"]*">([\d.]+)</td>'
        r'.*?<td class="[^"]*text-right[^"]* (?:text-emerald|text-rose)[^"]*">([+-]?[\d.]+%)</td>',
        re.DOTALL,
    )

    summary_rows: list[dict] = []
    detail_rows: list[dict] = []

    for year, month, month_label, date_range, ret_str, body in card_re.findall(html):
        dates = [d.strip() for d in date_range.split("→")]
        buy_date = dates[0] if dates else ""
        sell_date = dates[1] if len(dates) > 1 else buy_date
        ret_pct = float(ret_str.replace("%", "").replace("+", "")) / 100.0

        tickers: list[str] = []
        for ticker, buy_p, sell_p, t_ret in row_re.findall(body):
            tickers.append(ticker)
            t_ret_val = float(t_ret.replace("%", "").replace("+", "")) / 100.0
            detail_rows.append({
                "month": month,
                "ticker": ticker,
                "buy_date": buy_date,
                "buy_price": float(buy_p),
                "sell_date": sell_date,
                "sell_price": float(sell_p),
                "monthly_return": t_ret_val,
            })

        summary_rows.append({
            "month": month,
            "buy_date": buy_date,
            "sell_date": sell_date,
            "num_stocks": len(tickers),
            "monthly_return": ret_pct,
            "top_tickers": ",".join(tickers) if tickers else "CASH",
        })

    return summary_rows, detail_rows


def _monthly_from_cumulative(values: list[float]) -> list[float]:
    rets = []
    for i, v in enumerate(values):
        if i == 0:
            rets.append(v - 1.0)
        else:
            prev = values[i - 1]
            rets.append(v / prev - 1.0 if prev else 0.0)
    return rets


def rebuild_dataframes(html: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    tv_monthly = _extract_json("tvMonthlyData", html) or []
    tv_equity = _extract_json("tvEquityData", html) or {}

    card_summary, detail_rows = _parse_month_cards(html)
    card_by_month = {r["month"]: r for r in card_summary}

    summary_rows: list[dict] = []
    for d in tv_monthly:
        month = d["time"][:7]
        card = card_by_month.get(month, {})
        summary_rows.append({
            "month": month,
            "buy_date": card.get("buy_date", f"{month}-01"),
            "sell_date": card.get("sell_date", f"{month}-28"),
            "num_stocks": card.get("num_stocks", 0),
            "monthly_return": float(d["value"]),
            "top_tickers": card.get("top_tickers", ""),
        })

    df_summary = pd.DataFrame(summary_rows).sort_values("month").reset_index(drop=True)
    df_summary["cumulative_return"] = (1 + df_summary["monthly_return"]).cumprod() - 1

    for bm, series in tv_equity.items():
        if bm == "策略":
            continue
        prefix = bm.lower()
        values = [p["value"] for p in series]
        bm_months = [p["time"][:7] for p in series]
        bm_rets = _monthly_from_cumulative(values)
        ret_map = dict(zip(bm_months, bm_rets))
        df_summary[f"{prefix}_return"] = df_summary["month"].map(ret_map).fillna(0.0)
        cum_map = dict(zip(bm_months, [v - 1.0 for v in values]))
        df_summary[f"{prefix}_cumulative"] = df_summary["month"].map(cum_map).fillna(0.0)

    df_detail = pd.DataFrame(detail_rows) if detail_rows else pd.DataFrame()
    return df_summary, df_detail


def main() -> int:
    html_path = ROOT / "index.html"
    if not html_path.exists():
        print(f"[error] 未找到 {html_path}", file=sys.stderr)
        return 1

    html = html_path.read_text(encoding="utf-8")
    df_summary, df_detail = rebuild_dataframes(html)
    if df_summary.empty:
        print("[error] 无法从 index.html 解析回测数据", file=sys.stderr)
        return 1

    mtd_snapshot = None
    for key, out_key in [
        ("latestKMetadata", "metadata"),
        ("latestKFallback", "fallback"),
        ("mtdReturnsReport", "returns"),
    ]:
        val = _extract_json(key, html)
        if val:
            mtd_snapshot = mtd_snapshot or {}
            mtd_snapshot[out_key] = val
    if mtd_snapshot and mtd_snapshot.get("returns"):
        rets = mtd_snapshot["returns"].values()
        mtd_snapshot["portfolio_mtd"] = sum(rets) / len(rets) if rets else 0.0

    generate_backtest_html(
        df_summary,
        df_detail,
        html_path,
        benchmark="SPY",
        extra_benchmarks=["QQQ", "SOXX"],
        price_map=None,
        mtd_snapshot=mtd_snapshot,
    )
    print(f"[info] 已重新生成 → {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())