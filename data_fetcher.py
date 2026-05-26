from __future__ import annotations
import re
import sys
import time
import pickle
import gzip
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd
import requests
from ib_insync import IB, Stock, util

# ==================== 原有常量（保持不变） ====================
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

DEFAULT_HISTORY_FILE = Path(__file__).with_name("converted_data.xlsx")
DEFAULT_BENCHMARK = "SPY"
DEFAULT_DURATION = "11 Y"
DEFAULT_MIN_MARKET_CAP = 10_000_000_000


# ==================== 新增：IB价格缓存 ====================
def get_cache_filename(benchmark: str, duration: str, asof: str | None = None) -> Path:
    date_str = datetime.now().strftime("%Y%m%d") if asof is None else pd.to_datetime(asof).strftime("%Y%m%d")
    duration_clean = duration.replace(" ", "")
    return Path(__file__).parent / f"price_cache_{benchmark}_{date_str}_{duration_clean}.pkl.gz"


def load_price_cache(cache_file: Path) -> Dict[str, pd.Series] | None:
    if not cache_file.exists():
        return None
    try:
        with gzip.open(cache_file, "rb") as f:
            data = pickle.load(f)
        print(f"[info] 已从缓存加载价格数据: {cache_file.name} ({len(data)} 只股票)", file=sys.stderr)
        return data
    except Exception as e:
        print(f"[warn] 加载缓存失败: {e}", file=sys.stderr)
        return None


def save_price_cache(price_map: Dict[str, pd.Series], cache_file: Path):
    try:
        with gzip.open(cache_file, "wb") as f:
            pickle.dump(price_map, f)
        print(f"[info] 价格数据已缓存 → {cache_file.name}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] 保存缓存失败: {e}", file=sys.stderr)


# ==================== 以下为原有函数（保持不变） ====================
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
            rows.append({
                "month": month,
                "ticker": ticker,
                "entry_ts": pd.to_datetime(row[f"{prefix}.entry_ts"]),
                "score": float(row[f"{prefix}.score"]),
                "ema50_score": float(row[f"{prefix}.ema50_score"]),
                "rrg_score": float(row[f"{prefix}.rrg_score"]),
            })
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
    # （保持您原来的实现不变）
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
    # （保持您原来的实现不变）
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


def resolve_asof_date(price_map: Dict[str, pd.Series], benchmark_ticker: str, asof: str | None) -> pd.Timestamp:
    benchmark = price_map[benchmark_ticker]
    if asof is None:
        return pd.to_datetime(benchmark.index.max())
    requested = pd.to_datetime(asof)
    eligible = benchmark.loc[:requested]
    if eligible.empty:
        raise ValueError(f"no benchmark bars on or before {requested.date()}")
    return pd.to_datetime(eligible.index.max())