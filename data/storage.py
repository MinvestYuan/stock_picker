"""
Unified local storage for stock_picker.

- NPORT holdings + ticker resolution: SQLite (cache/stock_picker.db)
- Price OHLC: Parquet (cache/price_cache_{BENCHMARK}.parquet)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pandas as pd

from utils.logconf import get_logger
from utils.progress import ProgressBar

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"
APP_DB = CACHE_DIR / "stock_picker.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(APP_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_DB_INITIALIZED = False


def init_db() -> None:
    """创建表结构（每个进程只执行一次）。"""
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS nport_filings (
                accession TEXT PRIMARY KEY,
                filing_date TEXT,
                report_period_date TEXT,
                holdings_count INTEGER,
                last_parsed TEXT,
                has_full_holdings INTEGER DEFAULT 1,
                holdings_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_nport_report_period ON nport_filings(report_period_date);
            CREATE INDEX IF NOT EXISTS idx_nport_filing_date ON nport_filings(filing_date);

            CREATE TABLE IF NOT EXISTS ticker_unified_cache (
                cache_key TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            );
        """)
    _DB_INITIALIZED = True


# ==================== Metadata ====================

def meta_get(key: str, default: str | None = None) -> str | None:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (key, value),
        )


def meta_get_json(key: str, default=None):
    raw = meta_get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def meta_set_json(key: str, value) -> None:
    meta_set(key, json.dumps(value, ensure_ascii=False))


# ==================== Price cache (Parquet) ====================

def price_parquet_path(benchmark: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"price_cache_{benchmark.upper()}.parquet"


def price_cache_exists(benchmark: str) -> bool:
    path = price_parquet_path(benchmark)
    return path.exists() and path.stat().st_size > 0


def price_cache_mtime(benchmark: str) -> datetime | None:
    path = price_parquet_path(benchmark)
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime)
    return None


def _price_map_to_frame(price_map: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for ticker, df in price_map.items():
        if df is None or df.empty:
            continue
        part = df[["open", "high", "low", "close"]].copy()
        part["ticker"] = str(ticker)
        part = part.reset_index(names="date")
        part["date"] = pd.to_datetime(part["date"]).dt.strftime("%Y-%m-%d")
        frames.append(part[["ticker", "date", "open", "high", "low", "close"]])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_price_map(benchmark: str) -> Dict[str, pd.DataFrame]:
    benchmark = benchmark.upper()
    path = price_parquet_path(benchmark)
    if not path.exists():
        logger.info("[Parquet] %s 价格文件不存在", benchmark)
        return {}

    df = pd.read_parquet(path)
    if df.empty:
        logger.info("[Parquet] %s 无价格记录", benchmark)
        return {}

    result: Dict[str, pd.DataFrame] = {}
    grouped = df.groupby("ticker", sort=True)
    tickers = list(grouped.groups.keys())
    with ProgressBar(len(tickers), f"加载 {benchmark} 价格", unit="股") as bar:
        for ticker, g in grouped:
            ohlc = g.set_index(pd.to_datetime(g["date"]))[["open", "high", "low", "close"]].sort_index()
            ohlc = ohlc.apply(pd.to_numeric, errors="coerce").astype(float)
            result[str(ticker)] = ohlc
            bar.update(1)

    logger.info("[Parquet] %s 就绪：%d 只股票，%s 条 K 线", benchmark, len(result), f"{len(df):,}")
    return result


def save_price_map(benchmark: str, price_map: Dict[str, pd.DataFrame]) -> None:
    if not price_map:
        return
    benchmark = benchmark.upper()
    combined = _price_map_to_frame(price_map)
    if combined.empty:
        return

    path = price_parquet_path(benchmark)
    combined.to_parquet(path, index=False, engine="pyarrow")
    logger.info(
        "[Parquet] 已保存 %d 只股票（%s 条 K 线）→ %s",
        len(price_map), f"{len(combined):,}", path.name,
    )


def price_cache_stats(benchmark: str) -> dict:
    price_map = load_price_map(benchmark)
    if not price_map:
        return {}
    min_date = min((df.index.min() for df in price_map.values() if not df.empty), default=None)
    max_date = max((df.index.max() for df in price_map.values() if not df.empty), default=None)
    return {"count": len(price_map), "min_date": min_date, "max_date": max_date}


# ==================== NPORT filings ====================

def nport_save_filing(accession: str, filing_date: str, report_period_date: str, holdings: List[dict]) -> None:
    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO nport_filings
            (accession, filing_date, report_period_date, holdings_count, last_parsed, has_full_holdings, holdings_json)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                accession,
                filing_date,
                report_period_date,
                len(holdings),
                _now_iso(),
                json.dumps(holdings, ensure_ascii=False),
            ),
        )


def nport_load_all_holdings() -> Dict[str, List[dict]]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT accession, holdings_json FROM nport_filings").fetchall()
    result = {}
    for row in rows:
        try:
            result[row["accession"]] = json.loads(row["holdings_json"])
        except Exception:
            continue
    return result


def nport_known_accessions() -> set[str]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT accession FROM nport_filings").fetchall()
    return {row["accession"] for row in rows}


def nport_get_latest_filing() -> Optional[dict]:
    init_db()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT accession, report_period_date, holdings_json
            FROM nport_filings
            ORDER BY report_period_date DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    return {
        "accession": row["accession"],
        "report_period_date": row["report_period_date"],
        "holdings": json.loads(row["holdings_json"]) if row["holdings_json"] else [],
    }


def nport_update_holdings(accession: str, holdings: List[dict]) -> None:
    init_db()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE nport_filings
            SET holdings_count = ?, holdings_json = ?, last_parsed = ?
            WHERE accession = ?
            """,
            (len(holdings), json.dumps(holdings, ensure_ascii=False), _now_iso(), accession),
        )


def nport_filing_count() -> int:
    init_db()
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM nport_filings").fetchone()[0]


def nport_load_efts_cache() -> tuple[List[dict] | None, str | None]:
    data = meta_get_json("nport_efts_cache")
    if not data:
        return None, None
    cached_at = data.get("cached_at")
    filings = data.get("filings")
    if not cached_at or filings is None:
        return None, None
    try:
        cached_time = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - cached_time).total_seconds() / 3600
        if age_hours < 12:
            return filings, cached_at
    except Exception as e:
        logger.warning("解析 efts 缓存时间戳失败（将忽略缓存）: %s", e)
    return None, None


def nport_save_efts_cache(filings: List[dict]) -> None:
    meta_set_json("nport_efts_cache", {"cached_at": _now_iso(), "filings": filings})


# ==================== Ticker resolution caches ====================

def ticker_load_unified_cache() -> Dict[str, dict]:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT cache_key, data_json FROM ticker_unified_cache").fetchall()
    result = {}
    for row in rows:
        try:
            result[row["cache_key"]] = json.loads(row["data_json"])
        except Exception as e:
            logger.warning("解析 ticker 缓存条目 %s 失败，跳过: %s", row["cache_key"], e)
            continue
    return result


def ticker_save_unified_cache(cache: Dict[str, dict]) -> None:
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM ticker_unified_cache")
        conn.executemany(
            "INSERT INTO ticker_unified_cache (cache_key, data_json) VALUES (?, ?)",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in cache.items()],
        )


def get_symbol_conid_map() -> Dict[str, int]:
    """返回 {SYMBOL: conId} 映射，用于价格获取时直接指定合约 ID，绕过 SMART 路由歧义。"""
    cache = ticker_load_unified_cache()
    return {
        v["ticker"].upper(): int(v["conId"])
        for v in cache.values()
        if v.get("ticker") and v.get("conId")
    }


def ticker_clear_resolution_caches() -> None:
    """清空 ticker 解析缓存（unified），保留 NPORT 与价格数据。"""
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM ticker_unified_cache")


# ==================== 缓存清理 ====================

def purge_all_caches() -> None:
    """删除全部本地缓存（数据库 + Parquet 价格 + 下载的 XML）。"""
    xml_dir = ROOT_DIR / "nport_xmls"
    for path in CACHE_DIR.glob("price_cache_*.parquet"):
        try:
            path.unlink()
        except Exception as e:
            logger.warning("删除价格缓存 %s 失败: %s", path.name, e)
    if APP_DB.exists():
        APP_DB.unlink()
    if xml_dir.exists():
        for path in xml_dir.glob("*"):
            try:
                path.unlink()
            except Exception as e:
                logger.warning("删除 XML 文件 %s 失败: %s", path.name, e)
