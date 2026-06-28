from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Sequence, Tuple
import asyncio

import pandas as pd
from ib_async import IB, Stock, util

from config import (
    DEFAULT_BENCHMARK,
    DEFAULT_DURATION,
    FEATURE_EMA_SPAN,
    IB_CLIENT_ID,
    IB_HOST,
    IB_PORT,
    MOMENTUM_LONG_LOOKBACK,
    MOMENTUM_SHORT_LOOKBACK,
    PRICE_CLIENT_ID_OFFSET,
    RS_MOMENTUM_WINDOW,
    RS_RATIO_WINDOW,
)
from utils.logconf import get_logger
from utils.progress import ProgressBar
from .storage import (
    load_price_map,
    price_cache_exists,
    price_cache_mtime,
    price_parquet_path,
    save_price_map,
)

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent


# ==================== 持久化 + 增量更新价格缓存（Parquet） ====================
def get_cache_filename(benchmark: str, duration: str = DEFAULT_DURATION) -> Path:
    """返回指定 benchmark 的 Parquet 价格缓存路径。"""
    return price_parquet_path(benchmark)


def _get_cache_stats(price_map: Dict[str, pd.DataFrame]) -> dict:
    """计算缓存数据的统计信息"""
    if not price_map:
        return {}
    min_date = min((df.index.min() for df in price_map.values() if not df.empty), default=None)
    max_date = max((df.index.max() for df in price_map.values() if not df.empty), default=None)
    return {
        "count": len(price_map),
        "min_date": min_date,
        "max_date": max_date,
    }


def _format_cache_info(stats: dict, benchmark: str) -> str:
    """格式化缓存信息用于输出"""
    parts = [f"{stats['count']} 只股票"]
    if stats.get("min_date") and stats.get("max_date"):
        min_d = pd.to_datetime(stats["min_date"]).strftime("%Y-%m-%d")
        max_d = pd.to_datetime(stats["max_date"]).strftime("%Y-%m-%d")
        parts.append(f"数据范围 {min_d} ~ {max_d}")
    if price_cache_exists(benchmark):
        mtime = price_cache_mtime(benchmark)
        if mtime is not None:
            age_days = (datetime.now() - mtime).days
            parts.append(f"缓存 {age_days} 天前更新")
    return ", ".join(parts)


def load_price_cache(cache_file: Path, benchmark: str = DEFAULT_BENCHMARK) -> Dict[str, pd.DataFrame] | None:
    """从 Parquet 加载指定 benchmark 的价格缓存。"""
    benchmark = benchmark.upper()
    if not price_cache_exists(benchmark):
        return None
    try:
        price_map = load_price_map(benchmark)
        stats = _get_cache_stats(price_map)
        info = _format_cache_info(stats, benchmark)
        logger.info("已从 Parquet 缓存加载价格数据: %s (%s)", benchmark, info)
        return price_map
    except Exception as e:
        logger.warning("加载 Parquet 价格缓存失败: %s", e)
        return None


def get_cache_age_days(cache_file: Path) -> int | None:
    """返回 Parquet 缓存文件修改时间距今的天数，文件不存在返回 None"""
    if not cache_file.exists():
        return None
    mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
    return (datetime.now() - mtime).days


def get_cache_data_max_age_days(price_map: Dict[str, pd.Series]) -> int | None:
    """返回缓存中最新数据距今的天数"""
    if not price_map:
        return None
    max_date = max((s.index.max() for s in price_map.values() if not s.empty), default=None)
    if max_date is None:
        return None
    if hasattr(max_date, "tz") and max_date.tz is not None:
        max_date = max_date.tz_localize(None)
    now = pd.Timestamp.now().tz_localize(None)
    return int((now - max_date).days)


def save_price_cache(price_map: Dict[str, pd.Series], cache_file: Path, benchmark: str = DEFAULT_BENCHMARK):
    """保存价格缓存到 Parquet。"""
    if not price_map:
        return
    try:
        save_price_map(benchmark.upper(), price_map)
    except Exception as e:
        logger.warning("保存缓存失败: %s", e)


def _calculate_incremental_duration(last_date: pd.Timestamp, target_end: pd.Timestamp | None = None) -> str:
    """根据缺失天数智能选择最短的 durationStr，极大减少数据量和请求时间"""
    if target_end is None:
        target_end = pd.Timestamp.now(tz="US/Eastern").floor("D")
    # 确保时区一致
    if hasattr(target_end, "tz") and target_end.tz is not None:
        target_end = target_end.tz_localize(None)
    if hasattr(last_date, "tz") and last_date.tz is not None:
        last_date = last_date.tz_localize(None)
    days_missing = int((target_end - last_date).days) + 10  # 缓冲期（周末/节假日）
    if days_missing <= 14:
        return "2 W"
    elif days_missing <= 45:
        return "2 M"
    elif days_missing <= 120:
        return "6 M"
    elif days_missing <= 400:
        return "1 Y"
    else:
        return DEFAULT_DURATION


def _get_latest_trading_day(reference: pd.Timestamp | None = None) -> pd.Timestamp:
    """返回“最近一个交易日”（美东时区）。

    逻辑（直接追最新交易日，无冷却/天数检查）：
    - 总是基于参考日期（通常是 target_end 的 floored 日期）。
    - 如果参考日 == 真实今天，则使用真实当前时刻判断是否已过 16:30 收盘，从而决定是否把“今天”算作最新交易日。
    - 否则（历史 asof 或周末），用 BDay + 周末检查回退到合适的上一个交易日。
    - 目的：同一天多次运行时（只要缓存已有当前最新交易日的 bar），完全跳过 IB。
      只有新交易日数据可用时才触发一次针对性的短增量获取。
    """
    if reference is None:
        reference = pd.Timestamp.now(tz="US/Eastern")

    if hasattr(reference, "tz") and reference.tz is not None:
        ref = reference.tz_localize(None)
    else:
        ref = reference

    day = ref.normalize()

    real_now = pd.Timestamp.now(tz="US/Eastern").tz_localize(None)
    real_today = real_now.normalize()

    if day == real_today:
        # 实时运行今天，用真实时刻判断收盘
        effective_ref = real_now
    else:
        # 历史 asof 日或其它，视为该日的“结束”
        effective_ref = day + pd.Timedelta(hours=23, minutes=59)

    if day.weekday() >= 5:  # Sat/Sun
        return (day - pd.offsets.BDay(1)).normalize()

    close_cutoff = day + pd.Timedelta(hours=16, minutes=30)
    if effective_ref >= close_cutoff:
        return day
    else:
        return (day - pd.offsets.BDay(1)).normalize()


def _fetch_bars(ib: IB, ticker: str, end_date_str: str, duration_str: str) -> list | None:
    """底层 IB 请求"""
    contract = Stock(ticker, "SMART", "USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified or qualified[0] is None:
        logger.warning("could not qualify %s", ticker)
        return None
    bars = ib.reqHistoricalData(
        qualified[0],
        endDateTime=end_date_str,
        durationStr=duration_str,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    return bars


def _bars_to_series(bars: list, ticker: str) -> pd.DataFrame | None:
    if not bars:
        return None
    df = util.df(bars)
    if df.empty:
        return None
    df = df[["date", "open", "high", "low", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df.columns = ["open", "high", "low", "close"]
    df = df.astype(float)
    return df


def _get_fetch_info(
    ticker: str,
    existing: pd.Series | None,
    target_end: pd.Timestamp,
    default_duration: str,
) -> Tuple[str, bool, pd.Timestamp | None] | None:
    """返回 (duration_str, is_incremental, last_date) 如果需要从IB获取，否则返回 None 表示跳过。

    新逻辑（按用户要求）：
    - 不使用任何“冷却时间”（文件mtime）或“新鲜度天数检查”。
    - 直接计算“最近一个交易日”（_get_latest_trading_day）。
    - 如果缓存的 last_date 已经 >= 该最近交易日，则认为已拥有最新交易日数据，跳过 IB 请求。
    - 否则，进行增量更新（_calculate_incremental_duration 会根据缺失天数选择最短的 2W/2M 等短 duration）。
    - 效果：同一天内多次运行（只要最新交易日数据已在缓存中），不会重复请求；只有当出现新的交易日数据时，才会去获取追平。
    """
    last_date = None
    if existing is not None and not existing.empty:
        last_date = existing.index.max()
        if hasattr(last_date, "tz") and last_date.tz is not None:
            last_date = last_date.tz_localize(None)

        latest_trading_day = _get_latest_trading_day(target_end)
        if last_date >= latest_trading_day:
            return None

        duration_str = _calculate_incremental_duration(last_date, target_end)
        is_incremental = True
    else:
        duration_str = default_duration
        is_incremental = False
    return duration_str, is_incremental, last_date


def _fetch_on_connection(
    host: str,
    port: int,
    client_id: int,
    tickers_info: list,
    end_date_str: str,
    pause_seconds: float,
    existing_price_map: Dict[str, pd.Series],
    progress: ProgressBar | None = None,
) -> Dict[str, pd.Series]:
    """在一个独立的 IB 连接（clientId）上顺序处理一批 ticker，返回该批的更新结果。"""
    # ib_async uses asyncio internally; ensure an event loop exists in this worker thread
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    ib = None
    local_results: Dict[str, pd.Series] = {}
    try:
        ib = connect_ib(host, port, client_id)
    except Exception as e:
        logger.warning("[c%s] IB 连接失败: %s", client_id, e)
        if progress is not None:
            progress.update(len(tickers_info))
        return local_results
    try:
        for _idx, (ticker, duration_str, is_incremental, last_date) in enumerate(tickers_info, 1):
            try:
                bars = _fetch_bars(ib, ticker, end_date_str, duration_str)
                new_series = _bars_to_series(bars, ticker)

                if new_series is not None and not new_series.empty:
                    existing = existing_price_map.get(ticker)
                    if existing is None or existing.empty or not is_incremental:
                        local_results[ticker] = new_series
                    else:
                        new_series = new_series[new_series.index > last_date]
                        if not new_series.empty:
                            combined = pd.concat([existing, new_series])
                            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                            local_results[ticker] = combined
            except Exception as e:
                logger.warning("[c%s] %s 价格获取失败，跳过: %s", client_id, ticker, e)

            if pause_seconds > 0:
                time.sleep(pause_seconds)
            if progress is not None:
                progress.update(1)
    finally:
        if ib is not None:
            ib.disconnect()
    return local_results


def fetch_or_update_history(
    tickers: Sequence[str],
    end_date: pd.Timestamp | None,
    duration: str,
    ib: IB | None = None,
    pause_seconds: float = 0.25,
    existing_price_map: Dict[str, pd.Series] | None = None,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 17,
    num_connections: int = 4,
) -> Dict[str, pd.Series]:
    """核心增量更新函数（首次全量，后续仅更新缺失部分）。

    默认使用 4 个并行 IB 连接同时请求数据（显著加速 1000+ 只股票的价格更新，比单连接快很多）。
    - num_connections=4（默认）：推荐值，创建 4 个独立 clientId 的连接并行拉取
      （clientId = client_id + 10 + i，与 TickerResolver 的 client_id+1..N 错开）。
    - 可通过 --num-connections 1 退回单连接保守模式。
    每个连接内部仍顺序 + pause_seconds 以遵守 IB pacing 限制。

    调用时可通过 host/port/client_id 指定连接参数（当未提供 ib 或使用多连接时会内部创建）。

    增量决策：不再有冷却时间/新鲜度天数检查，直接通过 _get_latest_trading_day + _get_fetch_info
    判断是否已拥有“最近一个交易日”的数据；若缺少则用最短合理的 duration 增量获取。
    """
    results: Dict[str, pd.Series] = dict(existing_price_map) if existing_price_map is not None else {}
    end_date_str = ""
    if end_date is not None:
        end_date_str = f"{end_date.strftime('%Y%m%d')} 23:59:59 US/Eastern"

    target_end = end_date or pd.Timestamp.now(tz="US/Eastern").floor("D")
    # 统一转换为 tz-naive，避免与缓存中的 tz-naive 时间戳比较出错
    # 注意：用 tz_localize(None) 而非 tz_convert(None)，保留本地日期数字（ET midnight）
    if hasattr(target_end, "tz") and target_end.tz is not None:
        target_end = target_end.tz_localize(None)

    # 预先筛选需要从 IB 请求的 ticker（跳过已新鲜的）
    latest_trading_day = _get_latest_trading_day(target_end)

    to_process: list = []
    skipped = 0
    with ProgressBar(len(tickers), "检查价格缓存", unit="股") as bar:
        for ticker in tickers:
            existing = results.get(ticker)
            info = _get_fetch_info(ticker, existing, target_end, duration)
            if info is None:
                skipped += 1
            else:
                duration_str, is_incremental, last_date = info
                to_process.append((ticker, duration_str, is_incremental, last_date))
            bar.update(1, 跳过=skipped, 待拉=len(to_process))

    logger.info(
        "缓存检查完成（最近交易日 %s）：跳过 %d 只，需从 IB 拉取 %d 只",
        latest_trading_day.date(), skipped, len(to_process),
    )

    if not to_process:
        logger.info("价格数据已是最新，共 %d 只股票", len(results))
        return results

    if num_connections <= 1:
        own_connection = ib is None
        if own_connection:
            logger.info("连接 IB Gateway (%s:%s, clientId=%s)...", host, port, client_id)
            ib = connect_ib(host, port, client_id)
        try:
            with ProgressBar(len(to_process), "IB 拉取价格", unit="股") as bar:
                for _idx, (ticker, duration_str, is_incremental, last_date) in enumerate(to_process, start=1):
                    try:
                        bars = _fetch_bars(ib, ticker, end_date_str, duration_str)
                        new_series = _bars_to_series(bars, ticker)

                        if new_series is not None and not new_series.empty:
                            existing = results.get(ticker)
                            if existing is None or existing.empty or not is_incremental:
                                results[ticker] = new_series
                            else:
                                new_series = new_series[new_series.index > last_date]
                                if not new_series.empty:
                                    combined = pd.concat([existing, new_series])
                                    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                                    results[ticker] = combined
                    except Exception as e:
                        logger.warning("%s 价格获取失败，跳过: %s", ticker, e)

                    if pause_seconds > 0:
                        time.sleep(pause_seconds)
                    bar.update(1)
        finally:
            if own_connection and ib is not None:
                ib.disconnect()
    else:
        n = max(1, num_connections)
        chunks: list[list] = [to_process[i::n] for i in range(n)]
        chunks = [c for c in chunks if c]
        logger.info("并行拉取：%d 只股票，%d 个 IB 连接", len(to_process), len(chunks))

        with ProgressBar(len(to_process), "IB 拉取价格", unit="股") as bar:
            with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
                futures = []
                for i, chunk in enumerate(chunks):
                    c_id = client_id + PRICE_CLIENT_ID_OFFSET + i
                    fut = executor.submit(
                        _fetch_on_connection,
                        host,
                        port,
                        c_id,
                        chunk,
                        end_date_str,
                        pause_seconds,
                        results,
                        bar,
                    )
                    futures.append((c_id, fut))

                failed = 0
                for c_id, fut in futures:
                    try:
                        partial = fut.result()
                        results.update(partial)
                    except Exception as e:
                        failed += 1
                        logger.warning("[c%s] 并行价格更新失败: %s", c_id, e)
                if failed:
                    logger.warning("%d/%d 个并行连接失败", failed, len(futures))

    logger.info("价格数据更新完成，共 %d 只股票", len(results))
    return results


def connect_ib(host: str = IB_HOST, port: int = IB_PORT, client_id: int = IB_CLIENT_ID) -> IB:
    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=True)
    return ib


def prepare_feature_frame(ohlc: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    common = ohlc[["close"]].join(benchmark.to_frame("benchmark"), how="inner")
    if common.empty:
        return common
    common["ema50"] = common["close"].ewm(span=FEATURE_EMA_SPAN, adjust=False).mean()
    common["rel"] = common["close"] / common["benchmark"]

    weekly = common[["rel"]].resample("W-MON").last().dropna()
    weekly["rs_ratio"] = 100.0 + 25.0 * (weekly["rel"] / weekly["rel"].rolling(RS_RATIO_WINDOW).mean() - 1.0)
    weekly["rs_momentum"] = 100.0 + 100.0 * (
        weekly["rs_ratio"] / weekly["rs_ratio"].rolling(RS_MOMENTUM_WINDOW).mean() - 1.0
    )

    # ==================== 绝对动量（4-1 Momentum） ====================
    # 本项目统一使用 4-1 动量（过去4个月回报 − 过去1个月回报）
    common["momentum"] = (
        common["close"] / common["close"].shift(MOMENTUM_LONG_LOOKBACK) - 1
    ) - (
        common["close"] / common["close"].shift(MOMENTUM_SHORT_LOOKBACK) - 1
    )
    # ================================================================

    common = common.join(weekly[["rs_ratio", "rs_momentum"]], how="left")
    common[["rs_ratio", "rs_momentum"]] = common[
        ["rs_ratio", "rs_momentum"]
    ].ffill()
    return common


def prepare_all_features(price_map: Dict[str, pd.Series], benchmark_ticker: str) -> Dict[str, pd.DataFrame]:
    if benchmark_ticker not in price_map:
        raise ValueError(f"benchmark {benchmark_ticker} was not downloaded")
    benchmark_df = price_map[benchmark_ticker]
    benchmark = benchmark_df["close"] if isinstance(benchmark_df, pd.DataFrame) else benchmark_df
    tickers = [t for t in price_map if t != benchmark_ticker]
    features: Dict[str, pd.DataFrame] = {}
    with ProgressBar(len(tickers), "计算特征", unit="股") as bar:
        for ticker in tickers:
            features[ticker] = prepare_feature_frame(price_map[ticker], benchmark)
            bar.update(1)
    return features