from __future__ import annotations
import sys
import time
import pickle
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Optional, Tuple
import asyncio
import json

import numpy as np
import pandas as pd
from ib_insync import IB, Stock, util

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY_FILE = ROOT_DIR / "converted_data.xlsx"
DEFAULT_BENCHMARK = "SPY"
DEFAULT_DURATION = "11 Y"
DEFAULT_MIN_MARKET_CAP = 10_000_000_000


# ==================== 持久化 + 增量更新价格缓存（Parquet 优化版） ====================
def get_cache_filename(benchmark: str, duration: str = DEFAULT_DURATION) -> Path:
    """
    返回价格缓存文件路径。
    
    优化说明（2026）：
    - 主格式已从 pickle.gz 升级为 Parquet（更稳健、可演进、压缩率更高、跨工具兼容）。
    - 缓存文件名不再依赖 duration，避免碎片文件。
    - 统一规范名：cache/price_cache_{benchmark}.parquet
    
    自动迁移：
    - 检测到旧的 .pkl.gz（含带 duration 的旧版）会自动加载并在下次保存时转为 .parquet。
    - duration 参数仅用于首次缺失数据时向 IB 请求的时长，不影响文件名。
    """
    cache_dir = ROOT_DIR / "cache"
    cache_dir.mkdir(exist_ok=True)
    
    # 新规范文件名（Parquet）
    canonical = cache_dir / f"price_cache_{benchmark}.parquet"
    
    # 兼容旧的 pickle 缓存（自动迁移逻辑）
    legacy_pkl = cache_dir / f"price_cache_{benchmark}.pkl.gz"
    duration_clean = duration.replace(" ", "")
    legacy_with_duration = cache_dir / f"price_cache_{benchmark}_{duration_clean}.pkl.gz"
    old_root_legacy = ROOT_DIR / f"price_cache_{benchmark}_{duration_clean}.pkl.gz"
    root_old_pkl = ROOT_DIR / f"price_cache_{benchmark}.pkl.gz"
    
    # 如果新 parquet 不存在，但任意旧 pkl 存在，记录待迁移（实际迁移在 load 时完成）
    # 这里只做最基本的旧文件提示/清理
    for old_path in [legacy_with_duration, old_root_legacy, root_old_pkl, legacy_pkl]:
        if old_path.exists() and not canonical.exists():
            print(f"[info] 检测到旧价格缓存 {old_path.name}，下次保存时将自动迁移为 Parquet 格式", file=sys.stderr)
            break
            
    return canonical


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


def _format_cache_info(stats: dict, cache_file: Path) -> str:
    """格式化缓存信息用于输出"""
    parts = [f"{stats['count']} 只股票"]
    if stats.get("min_date") and stats.get("max_date"):
        min_d = pd.to_datetime(stats["min_date"]).strftime("%Y-%m-%d")
        max_d = pd.to_datetime(stats["max_date"]).strftime("%Y-%m-%d")
        parts.append(f"数据范围 {min_d} ~ {max_d}")
    # 文件修改时间
    if cache_file.exists():
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
        age_days = (datetime.now() - mtime).days
        parts.append(f"文件 {age_days} 天前更新")
    return ", ".join(parts)


def _load_legacy_pickle_cache(pkl_path: Path) -> Dict[str, pd.Series] | None:
    """加载旧版 pickle.gz 格式（用于自动迁移）。"""
    if not pkl_path.exists():
        return None
    try:
        with gzip.open(pkl_path, "rb") as f:
            data = pickle.load(f)
        print(f"[info] 从旧版 pickle 缓存加载成功: {pkl_path.name}（准备迁移到 Parquet）", file=sys.stderr)
        return data
    except Exception as e:
        print(f"[warn] 旧版 pickle 缓存加载失败: {e}", file=sys.stderr)
        return None


def load_price_cache(cache_file: Path) -> Dict[str, pd.Series] | None:
    """
    加载价格缓存。
    优先级：
      1. 新版 .parquet（推荐）
      2. 自动检测并加载同目录下的旧版 .pkl.gz（返回数据，后续保存会迁移）
    """
    if not cache_file.exists():
        # 尝试自动发现旧版 pickle（同基准名）
        if cache_file.suffix == ".parquet":
            legacy = cache_file.with_suffix(".pkl.gz")
            if legacy.exists():
                return _load_legacy_pickle_cache(legacy)
            # 也尝试不带 .gz 的极旧版
            legacy2 = cache_file.parent / (cache_file.stem + ".pkl.gz")
            if legacy2.exists() and legacy2 != legacy:
                return _load_legacy_pickle_cache(legacy2)
        return None

    # 新版 Parquet
    if str(cache_file).endswith(".parquet"):
        try:
            df = pd.read_parquet(cache_file)
            if df is None or df.empty:
                return {}
            # 兼容不同列名
            cols = {c.lower(): c for c in df.columns}
            ticker_col = cols.get("ticker", "ticker")
            date_col = cols.get("date", cols.get("index", "date"))

            price_map: Dict[str, pd.DataFrame] = {}
            for ticker, g in df.groupby(ticker_col):
                g = g.set_index(pd.to_datetime(g[date_col])).sort_index()
                if "open" in g.columns and "high" in g.columns:
                    ohlc = g[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce").dropna()
                else:
                    # old cache only had close
                    close = pd.to_numeric(g[cols.get("close", "close")], errors="coerce")
                    ohlc = pd.DataFrame({
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close
                    }, index=g.index).dropna()
                if not ohlc.empty:
                    price_map[str(ticker)] = ohlc
            stats = _get_cache_stats(price_map)
            info = _format_cache_info(stats, cache_file)
            print(f"[info] 已从 Parquet 缓存加载价格数据: {cache_file.name} ({info})", file=sys.stderr)
            return price_map
        except Exception as e:
            print(f"[warn] 加载 Parquet 缓存失败: {e}", file=sys.stderr)
            return None

    # 兜底：直接是旧 pkl.gz 路径被调用
    return _load_legacy_pickle_cache(cache_file)


def get_cache_age_days(cache_file: Path) -> int | None:
    """返回缓存文件修改时间距今的天数，文件不存在返回 None"""
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


# ==================== 失败 ticker 缓存（避免每次回测都重试无法获取价格的垃圾 ticker） ====================
FAILED_PRICE_FILE = ROOT_DIR / "cache" / "failed_price_tickers.json"

def load_failed_price_tickers() -> set[str]:
    if FAILED_PRICE_FILE.exists():
        try:
            with open(FAILED_PRICE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("tickers", []))
        except Exception:
            return set()
    return set()

def save_failed_price_tickers(tickers: set[str]):
    FAILED_PRICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FAILED_PRICE_FILE, "w", encoding="utf-8") as f:
        json.dump({"tickers": sorted(tickers)}, f, indent=2)


def save_price_cache(price_map: Dict[str, pd.Series], cache_file: Path):
    """
    保存价格缓存。
    - 目标为 .parquet 时使用 Parquet（zstd 压缩 + pyarrow）。
    - 检测到传入旧的 .pkl.gz 路径时仍兼容写入（但新调用已统一为 .parquet）。
    - 保存成功后会尝试清理同目录下的旧版 pkl.gz（迁移完成）。
    """
    if not price_map:
        return
    try:
        target_is_parquet = str(cache_file).endswith(".parquet")

        if target_is_parquet:
            # 转为长表格式（极适合 Parquet 列式存储 + 未来扩展 OHLCV）
            rows = []
            for t, df in price_map.items():
                if df is None or df.empty:
                    continue
                tmp = df.reset_index()
                tmp["ticker"] = t
                rows.append(tmp[["ticker", "date", "open", "high", "low", "close"]])

            if not rows:
                return
            full = pd.concat(rows, ignore_index=True)
            full["date"] = pd.to_datetime(full["date"]).dt.tz_localize(None)  # 确保 naive timestamp
            # 让 pandas 自动选择可用引擎（优先 pyarrow，其次 fastparquet）
            # zstd 压缩率好、速度快；如报错请 pip install pyarrow
            try:
                full.to_parquet(cache_file, compression="zstd", index=False)
            except ImportError as ie:
                raise ImportError(
                    "Parquet 引擎缺失。请安装推荐依赖：\n"
                    "  pip install pyarrow\n"
                    "或：pip install fastparquet"
                ) from ie

            print(f"[info] 价格数据已持久化保存 → {cache_file.name} ({len(price_map)} 只股票, Parquet)", file=sys.stderr)

            # 迁移成功后清理旧版 pickle（同目录）
            _cleanup_legacy_price_caches(cache_file)

        else:
            # 旧格式兜底（极少触发）
            with gzip.open(cache_file, "wb") as f:
                pickle.dump(price_map, f)
            print(f"[info] 价格数据已持久化保存 → {cache_file.name} ({len(price_map)} 只股票) [legacy pickle]", file=sys.stderr)

    except Exception as e:
        print(f"[warn] 保存缓存失败: {e}", file=sys.stderr)


def _cleanup_legacy_price_caches(parquet_path: Path):
    """迁移完成后静默清理同基准的旧 pkl 缓存文件。"""
    try:
        stem = parquet_path.stem  # price_cache_SPY
        for f in parquet_path.parent.glob("price_cache_*.pkl.gz"):
            try:
                # 只要文件名包含这个基准（SPY / QQQ 等），就视为同系列旧缓存
                if stem in f.name or f.name.startswith(stem):
                    f.unlink(missing_ok=True)
                    print(f"[info] 已清理旧版缓存: {f.name}", file=sys.stderr)
            except Exception:
                pass
    except Exception:
        pass


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
    if not qualified:
        print(f"[warn] could not qualify {ticker}", file=sys.stderr)
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
) -> Dict[str, pd.Series]:
    """在一个独立的 IB 连接（clientId）上顺序处理一批 ticker，返回该批的更新结果。"""
    # ib_insync uses asyncio internally; ensure an event loop exists in this worker thread
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    ib = connect_ib(host, port, client_id)
    local_results: Dict[str, pd.Series] = {}
    try:
        for idx, (ticker, duration_str, is_incremental, last_date) in enumerate(tickers_info, 1):
            print(
                f"[info] [c{client_id}] {'增量更新' if is_incremental else '完整下载'} {ticker} → {duration_str}",
                file=sys.stderr,
            )
            bars = _fetch_bars(ib, ticker, end_date_str, duration_str)
            new_series = _bars_to_series(bars, ticker)

            if new_series is not None and not new_series.empty:
                existing = existing_price_map.get(ticker)
                if existing is None or existing.empty:
                    local_results[ticker] = new_series
                else:
                    new_series = (
                        new_series[new_series.index > last_date] if last_date is not None else new_series
                    )
                    if not new_series.empty:
                        combined = pd.concat([existing, new_series])
                        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                        local_results[ticker] = combined

            if pause_seconds > 0:
                time.sleep(pause_seconds)
    finally:
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
    port: int = 4001,
    client_id: int = 17,
    num_connections: int = 4,
) -> Dict[str, pd.Series]:
    """核心增量更新函数（首次全量，后续仅更新缺失部分）。

    默认使用 4 个并行 IB 连接同时请求数据（显著加速 1000+ 只股票的价格更新，比单连接快很多）。
    - num_connections=4（默认）：推荐值，创建 4 个独立 clientId 的连接并行拉取。
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

    # 预先筛选需要从 IB 请求的 ticker（跳过已新鲜的 + 已知失败的垃圾 ticker）
    failed = load_failed_price_tickers()
    to_process: list = []
    for ticker in tickers:
        existing = results.get(ticker)
        if ticker in failed and (existing is None or existing.empty):
            continue  # 持久跳过之前无法获取价格的 ticker，避免每次回测都“新”下载尝试
        info = _get_fetch_info(ticker, existing, target_end, duration)
        if info is None:
            continue
        duration_str, is_incremental, last_date = info
        to_process.append((ticker, duration_str, is_incremental, last_date))

    if not to_process:
        print(f"[info] 所有目标股票数据均已最新，无需从 IB 更新", file=sys.stderr)
        print(f"[info] 价格数据更新完成，共 {len(results)} 只股票", file=sys.stderr)
        return results

    print(
        f"[info] 需要从 IB 获取/更新数据：{len(to_process)} / {len(tickers)} 只股票 "
        f"（num_connections={num_connections}）",
        file=sys.stderr,
    )

    if num_connections <= 1:
        # 单连接路径（完全兼容原有 ib 传入方式）
        own_connection = ib is None
        if own_connection:
            ib = connect_ib(host, port, client_id)
        try:
            for idx, (ticker, duration_str, is_incremental, last_date) in enumerate(to_process, start=1):
                print(
                    f"[info] {'增量更新' if is_incremental else '完整下载'} {ticker} → {duration_str}",
                    file=sys.stderr,
                )
                bars = _fetch_bars(ib, ticker, end_date_str, duration_str)
                new_series = _bars_to_series(bars, ticker)

                if new_series is not None and not new_series.empty:
                    existing = results.get(ticker)
                    if existing is None or existing.empty:
                        results[ticker] = new_series
                    else:
                        new_series = new_series[new_series.index > last_date] if last_date is not None else new_series
                        if not new_series.empty:
                            combined = pd.concat([existing, new_series])
                            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                            results[ticker] = combined

                if pause_seconds > 0:
                    time.sleep(pause_seconds)

                if idx % 25 == 0 or idx == len(to_process):
                    print(f"[info] 已处理 {idx}/{len(to_process)} 只股票", file=sys.stderr)
        finally:
            if own_connection and ib is not None:
                ib.disconnect()
    else:
        # 多连接并行路径
        n = max(1, num_connections)
        chunks: list[list] = [to_process[i::n] for i in range(n)]
        chunks = [c for c in chunks if c]
        print(f"[info] 已拆分到 {len(chunks)} 个并行连接", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            futures = []
            for i, chunk in enumerate(chunks):
                c_id = client_id + i
                fut = executor.submit(
                    _fetch_on_connection,
                    host,
                    port,
                    c_id,
                    chunk,
                    end_date_str,
                    pause_seconds,
                    results,  # 传递初始快照供读取 existing 数据用于合并
                )
                futures.append(fut)

            for fut in as_completed(futures):
                partial = fut.result()
                results.update(partial)

        # 统一进度提示
        print(f"[info] 已处理 {len(to_process)}/{len(to_process)} 只股票", file=sys.stderr)

    print(f"[info] 价格数据更新完成，共 {len(results)} 只股票", file=sys.stderr)

    # 持久化标记本次结束后仍然没有任何价格数据的 ticker（主要是 NPORT 里无法在 IB 找到的垃圾符号）
    # 下次回测时会直接跳过它们的完整下载尝试，不会再显示“完整下载 XXX → 11 Y”
    current_failed = load_failed_price_tickers()
    for ticker in tickers:
        ser = results.get(ticker)
        if ser is None or (hasattr(ser, "empty") and ser.empty):
            current_failed.add(ticker)
    if current_failed:
        save_failed_price_tickers(current_failed)

    return results


# 保持原有接口完全兼容（旧代码无需修改）
def fetch_daily_history(
    ib: IB | None = None,
    tickers: Sequence[str] = (),
    end_date: pd.Timestamp | None = None,
    duration: str = "1 Y",
    pause_seconds: float = 0.25,
    existing_price_map: Dict[str, pd.Series] | None = None,
    host: str = "127.0.0.1",
    port: int = 4001,
    client_id: int = 17,
    num_connections: int = 4,
) -> Dict[str, pd.Series]:
    return fetch_or_update_history(
        ib=ib,
        tickers=tickers,
        end_date=end_date,
        duration=duration,
        pause_seconds=pause_seconds,
        existing_price_map=existing_price_map,
        host=host,
        port=port,
        client_id=client_id,
        num_connections=num_connections,
    )


# ==================== 以下为原有函数（完全保持不变） ====================
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
    # 默认（以及显式指定 nport）均使用 Russell 1000 NPORT 持仓
    from .nport_universe import get_latest_universe
    return get_latest_universe()


def connect_ib(host: str, port: int, client_id: int) -> IB:
    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=True)
    return ib


def prepare_feature_frame(ohlc: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    common = ohlc[["close"]].join(benchmark.to_frame("benchmark"), how="inner")
    if common.empty:
        return common
    common["ema50"] = common["close"].ewm(span=50, adjust=False).mean()
    common["rel"] = common["close"] / common["benchmark"]

    weekly = common[["rel"]].resample("W-MON").last().dropna()
    weekly["rs_ratio"] = 100.0 + 25.0 * (weekly["rel"] / weekly["rel"].rolling(26).mean() - 1.0)
    weekly["rs_momentum"] = 100.0 + 100.0 * (
        weekly["rs_ratio"] / weekly["rs_ratio"].rolling(13).mean() - 1.0
    )

    # ==================== 绝对动量（4-1 Momentum） ====================
    # 本项目统一使用 4-1 动量（过去4个月回报 − 过去1个月回报）
    common["momentum"] = (
        common["close"] / common["close"].shift(84) - 1
    ) - (
        common["close"] / common["close"].shift(21) - 1
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
    features: Dict[str, pd.DataFrame] = {}
    for ticker, ohlc in price_map.items():
        if ticker == benchmark_ticker:
            continue
        features[ticker] = prepare_feature_frame(ohlc, benchmark)
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