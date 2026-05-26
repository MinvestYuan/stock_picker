from __future__ import annotations
import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence
import numpy as np
import pandas as pd
import requests
from ib_insync import IB, Stock, util

# Reverse-engineered from QuantGT public history plus feature fitting.
# 【用户修改版评分规则】：
# - total_score = momentum_score * 2 + rrg_score * 3 （权重 2:3，总分最大为 5）
# - momentum_score 仅和 50 日均线（close / ema50）有关
# - rrg_score 仅和 RRG_RS_RATIO、RRG_RS_MOMENTUM 有关
# - 两者均归一化到 [0, 1] 区间
#
# 原拟合公式中的其他因子（ret252、ret126、rel_ret63、rel_ret4w）已被移除。
MOMENTUM_INTERCEPT = 1.5883847005580216
MOMENTUM_RET252 = 0.02225370
MOMENTUM_RET126 = 0.02790204
MOMENTUM_EMA50 = 0.14489851
RRG_INTERCEPT = 1.8602
RRG_REL_RET63 = 0.1819
RRG_REL_RET4W = -0.2993
RRG_RS_RATIO = 0.0393
RRG_RS_MOMENTUM = -0.0382
MOMENTUM_MIN = 0.0
MOMENTUM_MAX = 2.0
RRG_MIN = 0.0
RRG_MAX = 3.0

DEFAULT_HISTORY_FILE = Path(__file__).with_name("converted_data.xlsx")
DEFAULT_BENCHMARK = "SPY"
DEFAULT_DURATION = "11 Y"
DEFAULT_MIN_MARKET_CAP = 10_000_000_000
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
NASDAQ_HEADERS = {
    "user-agent": "Mozilla/5.0",
    "accept": "application/json, text/plain, */*",
    "origin": "https://www.nasdaq.com",
    "referer": "https://www.nasdaq.com/",
}
EQUITY_NAME_PATTERN = re.compile(
    r"Common Stock|Common Shares|Ordinary Shares|Shares of Beneficial Interest|"
    r"Common Units|American Depositary Shares|American Depositary Share|"
    r"American Depositary Receipt|ADR",
    re.IGNORECASE,
)


@dataclass
class PickRow:
    ticker: str
    close: float
    momentum_score: float
    rrg_score: float
    total_score: float
    ret126: float
    ret252: float
    close_over_ema50: float
    rel_ret63: float
    rel_ret4w: float
    rs_ratio: float
    rs_momentum: float


def clip(value: float, low: float, high: float) -> float:
    return float(min(max(value, low), high))


def normalize_ticker(value: object) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    ticker = str(value).strip().upper()
    return ticker or None


def load_history_rows(history_file: Path) -> pd.DataFrame:
    raw = pd.read_excel(history_file)
    rows: List[dict] = []
    for _, row in raw.iterrows():
        month = str(row["month"])
        for i in range(5):
            prefix = f"picks[{i}]"
            ticker = normalize_ticker(row.get(f"{prefix}.ticker"))
            if not ticker:
                continue
            rows.append(
                {
                    "month": month,
                    "ticker": ticker,
                    "entry_ts": pd.to_datetime(row[f"{prefix}.entry_ts"]),
                    "score": float(row[f"{prefix}.score"]),
                    "ema50_score": float(row[f"{prefix}.ema50_score"]),
                    "rrg_score": float(row[f"{prefix}.rrg_score"]),
                }
            )
    return pd.DataFrame(rows).sort_values(["entry_ts", "ticker"]).reset_index(drop=True)


def load_tickers_from_history(history_file: Path) -> List[str]:
    history = load_history_rows(history_file)
    return sorted(history["ticker"].dropna().unique().tolist())


def load_tickers_from_file(path: Path) -> List[str]:
    suffix = path.suffix.lower()
    tickers: List[str] = []
    if suffix in {".csv", ".xlsx", ".xls"}:
        if suffix == ".csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
        first_col = df.columns[0]
        tickers = [t for t in (normalize_ticker(v) for v in df[first_col]) if t]
    else:
        tickers = [t for t in (normalize_ticker(line) for line in path.read_text().splitlines()) if t]
    return sorted(set(tickers))


def fetch_nasdaq_universe(min_market_cap: float) -> List[str]:
    response = requests.get(NASDAQ_SCREENER_URL, headers=NASDAQ_HEADERS, timeout=60)
    response.raise_for_status()
    payload = response.json()
    rows = payload["data"]["rows"]
    tickers: List[str] = []
    for row in rows:
        ticker = normalize_ticker(row.get("symbol"))
        name = str(row.get("name") or "")
        if not ticker or not name:
            continue
        if not EQUITY_NAME_PATTERN.search(name):
            continue
        market_cap = pd.to_numeric(row.get("marketCap"), errors="coerce")
        if pd.isna(market_cap) or float(market_cap) < float(min_market_cap):
            continue
        tickers.append(ticker)
    return sorted(set(tickers))


def build_universe(
    universe_source: str,
    min_market_cap: float,
    history_file: Path | None,
    universe_file: Path | None,
) -> List[str]:
    if universe_source == "file":
        if universe_file is None:
            raise ValueError("--universe-file is required when --universe-source=file")
        return load_tickers_from_file(universe_file)
    if universe_source == "history":
        if history_file is None or not history_file.exists():
            raise FileNotFoundError("history file is required when --universe-source=history")
        return load_tickers_from_history(history_file)
    return fetch_nasdaq_universe(min_market_cap)


def connect_ib(host: str, port: int, client_id: int) -> IB:
    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=True)
    return ib


def fetch_daily_history(
    ib: IB,
    tickers: Sequence[str],
    end_date: pd.Timestamp | None,
    duration: str,
    pause_seconds: float,
) -> Dict[str, pd.Series]:
    results: Dict[str, pd.Series] = {}
    end_date_str = ""
    if end_date is not None:
        end_date_str = f"{end_date.strftime('%Y%m%d')} 23:59:59 US/Eastern"
    for idx, ticker in enumerate(tickers, start=1):
        contract = Stock(ticker, "SMART", "USD")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            print(f"[warn] could not qualify {ticker}", file=sys.stderr)
            continue
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime=end_date_str,
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            print(f"[warn] no history returned for {ticker}", file=sys.stderr)
            continue
        df = util.df(bars)
        if df.empty:
            print(f"[warn] empty history frame for {ticker}", file=sys.stderr)
            continue
        series = pd.Series(df["close"].astype(float).values, index=pd.to_datetime(df["date"]), name=ticker)
        results[ticker] = series.sort_index()
        if pause_seconds > 0:
            time.sleep(pause_seconds)
        if idx % 25 == 0 or idx == len(tickers):
            print(f"[info] downloaded {idx}/{len(tickers)} tickers", file=sys.stderr)
    return results


def prepare_feature_frame(price: pd.Series, benchmark: pd.Series) -> pd.DataFrame:
    common = price.to_frame("close").join(benchmark.to_frame("benchmark"), how="inner")
    if common.empty:
        return common
    common["ema50"] = common["close"].ewm(span=50, adjust=False).mean()
    common["ret126"] = common["close"].pct_change(126)
    common["ret252"] = common["close"].pct_change(252)
    common["rel"] = common["close"] / common["benchmark"]
    common["rel_ret63"] = common["rel"].pct_change(63)
    weekly = common[["rel"]].resample("W-MON").last().dropna()
    weekly["rel_ret4w"] = weekly["rel"].pct_change(4)
    weekly["rs_ratio"] = 100.0 + 25.0 * (weekly["rel"] / weekly["rel"].rolling(26).mean() - 1.0)
    weekly["rs_momentum"] = 100.0 + 100.0 * (
        weekly["rs_ratio"] / weekly["rs_ratio"].rolling(13).mean() - 1.0
    )
    common = common.join(weekly[["rel_ret4w", "rs_ratio", "rs_momentum"]], how="left")
    common[["rel_ret4w", "rs_ratio", "rs_momentum"]] = common[
        ["rel_ret4w", "rs_ratio", "rs_momentum"]
    ].ffill()
    return common


def prepare_all_features(price_map: Dict[str, pd.Series], benchmark_ticker: str) -> Dict[str, pd.DataFrame]:
    if benchmark_ticker not in price_map:
        raise ValueError(f"benchmark {benchmark_ticker} was not downloaded")
    benchmark = price_map[benchmark_ticker]
    features: Dict[str, pd.DataFrame] = {}
    for ticker, series in price_map.items():
        if ticker == benchmark_ticker:
            continue
        features[ticker] = prepare_feature_frame(series, benchmark)
    return features


def compute_momentum_score(row: pd.Series) -> float:
    """【修改后】仅使用 50 日均线 + 原拟合截距，归一化到 [0, 1]"""
    raw = (
        MOMENTUM_INTERCEPT
        + MOMENTUM_EMA50 * (row["close"] / row["ema50"] - 1.0)
        # 仅保留与 50 日均线相关的部分（ret252 / ret126 已移除）
    )
    clipped = clip(raw, MOMENTUM_MIN, MOMENTUM_MAX)
    return clipped / MOMENTUM_MAX   # 归一化到 [0, 1]


def compute_rrg_score(row: pd.Series) -> float:
    """【修改后】仅使用 RRG_RS_RATIO + RRG_RS_MOMENTUM + 原拟合截距，归一化到 [0, 1]"""
    raw = (
        RRG_INTERCEPT
        + RRG_RS_RATIO * (row["rs_ratio"] - 100.0)
        + RRG_RS_MOMENTUM * (row["rs_momentum"] - 100.0)
        # 仅保留与 rs_ratio / rs_momentum 相关的部分（rel_ret63 / rel_ret4w 已移除）
    )
    clipped = clip(raw, RRG_MIN, RRG_MAX)
    return clipped / RRG_MAX   # 归一化到 [0, 1]


def score_universe(features: Dict[str, pd.DataFrame], asof_date: pd.Timestamp) -> List[PickRow]:
    picks: List[PickRow] = []
    for ticker, frame in features.items():
        frame = frame.loc[:asof_date]
        if frame.empty:
            continue
        row = frame.iloc[-1]
        required = [
            "close",
            "ema50",
            "ret126",
            "ret252",
            "rel_ret63",
            "rel_ret4w",
            "rs_ratio",
            "rs_momentum",
        ]
        if any(pd.isna(row[col]) for col in required):
            continue
        momentum_score = compute_momentum_score(row)
        rrg_score = compute_rrg_score(row)
        total_score = momentum_score * 2 + rrg_score * 3   # 新版加权总分
        picks.append(
            PickRow(
                ticker=ticker,
                close=float(row["close"]),
                momentum_score=momentum_score,
                rrg_score=rrg_score,
                total_score=total_score,
                ret126=float(row["ret126"]),
                ret252=float(row["ret252"]),
                close_over_ema50=float(row["close"] / row["ema50"]),
                rel_ret63=float(row["rel_ret63"]),
                rel_ret4w=float(row["rel_ret4w"]),
                rs_ratio=float(row["rs_ratio"]),
                rs_momentum=float(row["rs_momentum"]),
            )
        )
    return sorted(
        picks,
        key=lambda x: (x.total_score, x.rrg_score, x.momentum_score, x.ticker),
        reverse=True,
    )


def pick_rows_to_frame(picks: Sequence[PickRow]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": p.ticker,
                "close": p.close,
                "momentum_score": p.momentum_score,
                "rrg_score": p.rrg_score,
                "total_score": p.total_score,
                "ret126": p.ret126,
                "ret252": p.ret252,
                "close_over_ema50": p.close_over_ema50,
                "rel_ret63": p.rel_ret63,
                "rel_ret4w": p.rel_ret4w,
                "rs_ratio": p.rs_ratio,
                "rs_momentum": p.rs_momentum,
            }
            for p in picks
        ]
    )


def evaluate_against_history(features: Dict[str, pd.DataFrame], history_file: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    history = load_history_rows(history_file)
    rows: List[dict] = []
    for entry_ts, group in history.groupby("entry_ts"):
        selection_date = pd.to_datetime(entry_ts) - pd.Timedelta(days=1)
        ranked = score_universe(features, selection_date)
        predicted = [p.ticker for p in ranked[:5]]
        actual = sorted(group["ticker"].tolist())
        overlap = len(set(predicted) & set(actual))
        rows.append(
            {
                "entry_ts": pd.to_datetime(entry_ts).date().isoformat(),
                "month": group["month"].iloc[0],
                "actual": ",".join(actual),
                "predicted": ",".join(predicted),
                "overlap_count": overlap,
                "overlap_ratio": overlap / 5.0,
            }
        )
    detail = pd.DataFrame(rows).sort_values("entry_ts").reset_index(drop=True)
    summary = pd.DataFrame(
        [
            {
                "months": len(detail),
                "avg_overlap_count": detail["overlap_count"].mean(),
                "avg_overlap_ratio": detail["overlap_ratio"].mean(),
                "perfect_5_of_5_months": int((detail["overlap_count"] == 5).sum()),
                "at_least_3_of_5_months": int((detail["overlap_count"] >= 3).sum()),
            }
        ]
    )
    return summary, detail


def resolve_asof_date(price_map: Dict[str, pd.Series], benchmark_ticker: str, asof: str | None) -> pd.Timestamp:
    benchmark = price_map[benchmark_ticker]
    if asof is None:
        return pd.to_datetime(benchmark.index.max())
    requested = pd.to_datetime(asof)
    eligible = benchmark.loc[:requested]
    if eligible.empty:
        raise ValueError(f"no benchmark bars on or before {requested.date()}")
    return pd.to_datetime(eligible.index.max())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reverse-engineered QuantGT monthly stock-pick replica (已按用户要求修改评分逻辑)。")
    parser.add_argument(
        "command",
        nargs="?",
        default="select",
        choices=["select", "evaluate"],
        help="select current top 5 or evaluate monthly overlap against converted_data.xlsx",
    )
    parser.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_FILE)
    parser.add_argument(
        "--universe-source",
        choices=["nasdaq", "file", "history"],
        default="nasdaq",
        help="nasdaq dynamically fetches current US-listed equities from Nasdaq screener and filters by market cap",
    )
    parser.add_argument("--universe-file", type=Path, default=None)
    parser.add_argument(
        "--min-market-cap",
        type=float,
        default=DEFAULT_MIN_MARKET_CAP,
        help="minimum current market cap for the dynamic Nasdaq universe",
    )
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002, help="IB Gateway paper default is usually 4002")
    parser.add_argument("--client-id", type=int, default=17)
    parser.add_argument("--asof", default=None, help="Score using data up to this date, e.g. 2026-05-22")
    parser.add_argument("--duration", default=DEFAULT_DURATION, help="IB historical duration string, e.g. '11 Y'")
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None, help="Optional CSV output path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "evaluate" and not args.history_file.exists():
        raise FileNotFoundError(f"history file not found: {args.history_file}")
    if args.command == "evaluate" and args.universe_source != "history":
        print(
            "[warn] evaluate with a current dynamic universe introduces survivorship bias; "
            "use --universe-source=history only if you want the old proxy universe from converted_data.xlsx",
            file=sys.stderr,
        )
    universe = build_universe(
        universe_source=args.universe_source,
        min_market_cap=args.min_market_cap,
        history_file=args.history_file if args.history_file.exists() else None,
        universe_file=args.universe_file,
    )
    tickers = sorted(set(universe + [args.benchmark]))
    print(
        f"[info] universe source: {args.universe_source}, size: {len(universe)}, "
        f"min_market_cap: {args.min_market_cap:,.0f}",
        file=sys.stderr,
    )
    ib = connect_ib(args.host, args.port, args.client_id)
    try:
        end_date = pd.to_datetime(args.asof) if args.asof else None
        price_map = fetch_daily_history(
            ib=ib,
            tickers=tickers,
            end_date=end_date,
            duration=args.duration,
            pause_seconds=args.pause_seconds,
        )
    finally:
        ib.disconnect()
    features = prepare_all_features(price_map, args.benchmark)
    if args.command == "evaluate":
        summary, detail = evaluate_against_history(features, args.history_file)
        print(summary.to_string(index=False))
        print()
        print(detail.tail(20).to_string(index=False))
        if args.output:
            detail.to_csv(args.output, index=False)
        return 0
    asof_date = resolve_asof_date(price_map, args.benchmark, args.asof)
    ranked = score_universe(features, asof_date)
    top = pick_rows_to_frame(ranked[: args.top])
    print(f"Scored through {asof_date.date().isoformat()}")
    print(top.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    if args.output:
        top.to_csv(args.output, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())