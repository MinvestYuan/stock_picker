"""
Ticker Resolver — Unified, cached, multi-source ticker lookup for NPORT holdings.

Priority order (per security):
1. Unified cache (ticker_unified_cache)
2. IB Gateway (by ISIN → CUSIP)
3. Fail gracefully (leave ticker empty)
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ib_async import IB, Contract

from config import (
    IB_CLIENT_ID,
    IB_HOST,
    IB_NUM_CONNECTIONS,
    IB_PORT,
    TICKER_CLIENT_ID_OFFSET,
)
from utils.logconf import get_logger
from utils.progress import ProgressBar
from .storage import (
    ticker_load_unified_cache,
    ticker_save_unified_cache,
)

logger = get_logger(__name__)


def _now_iso():
    return datetime.now().isoformat()


ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"

DEFAULT_NUM_CONNECTIONS = IB_NUM_CONNECTIONS

RESOLUTION_SOURCES = {
    "manual_override": "Legacy manual override (removed)",
    "manual": "Legacy hardcoded map (being phased out)",
    "ib_isin": "Interactive Brokers by ISIN",
    "ib_cusip": "Interactive Brokers by CUSIP",
    "openfigi": "OpenFIGI by CUSIP",
    "cache": "Previously resolved (from cache)"
}


US_PRIMARY_EXCHANGES = {
    "NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "IEX",
    "NYSE MKT", "NMS", "NGM", "NCM", "PINK", "OTC",
}


def _contract_us_score(details) -> int:
    """Score IB ContractDetails for US-listed USD equity suitability."""
    c = details.contract
    if c.secType != "STK":
        return -1
    currency = (c.currency or "").upper()
    if currency and currency != "USD":
        return -1
    primary = (getattr(c, "primaryExchange", "") or c.exchange or "").upper()
    if primary in US_PRIMARY_EXCHANGES:
        score = 200
    elif primary in {"", "SMART"}:
        score = 50
    else:
        return -1
    if 1 <= len(c.symbol) <= 5:
        score += 20
    return score


def pick_best_us_contract(details_list) -> Optional[object]:
    if not details_list:
        return None
    if not isinstance(details_list, list):
        details_list = [details_list]
    scored = [(s, d) for d in details_list if (s := _contract_us_score(d)) >= 0]
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], len(x[1].contract.symbol), x[1].contract.symbol))
    return scored[0][1]


def pick_us_ticker(details: list) -> Optional[str]:
    best = pick_best_us_contract(details)
    return best.contract.symbol if best else None


def _make_identifier_key(cusip: str = "", isin: str = "") -> str:
    cusip = str(cusip or "").strip().upper()
    isin = str(isin or "").strip()
    if cusip:
        return f"CUSIP:{cusip}"
    if isin:
        return f"ISIN:{isin}"
    return ""


def _lookup_cache_entry(unified: dict, key: str) -> Optional[dict]:
    if not key or key not in unified:
        return None
    entry = unified[key]
    if (entry.get("currency") or "USD").upper() == "USD":
        return entry
    return None


def _resolve_via_ib(ib: IB, cusip: str, isin: str) -> tuple[Optional[str], str, str, dict]:
    ticker = None
    method = ""
    source = ""
    extra_meta: dict = {}

    if isin and not ticker:
        details = lookup_contract_details_by_secid(ib, "ISIN", isin)
        if details:
            t = pick_us_ticker([details])
            if t:
                ticker, method, source = t, "ib_isin", "ib"
                extra_meta = extract_contract_info(details)
    if cusip and not ticker:
        details = lookup_contract_details_by_secid(ib, "CUSIP", cusip)
        if details:
            t = pick_us_ticker([details])
            if t:
                ticker, method, source = t, "ib_cusip", "ib"
                extra_meta = extract_contract_info(details)

    return ticker, method, source, extra_meta


def _build_resolution_result(
    ticker: Optional[str],
    method: str = "",
    source: str = "",
    extra: dict | None = None,
    cached_entry: dict | None = None,
) -> dict:
    if cached_entry and cached_entry.get("ticker"):
        return {"ticker": cached_entry.get("ticker"), **cached_entry}
    if ticker:
        meta = {"ticker": ticker, "source": source, "method": method, "resolved_at": _now_iso()}
        if extra:
            meta.update({k: v for k, v in extra.items() if v})
        return meta
    return {"ticker": None, "source": "failed", "method": "none"}


def _resolve_identifiers_on_connection(
    host: str,
    port: int,
    client_id: int,
    work_items: list[tuple[str, dict]],
    unified_snapshot: dict,
    pause_seconds: float = 0.05,
    progress: ProgressBar | None = None,
) -> dict[str, dict]:
    """在独立 IB 连接上顺序解析一批唯一标识。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

    ib = connect_ib(host, port, client_id)
    if ib is None:
        logger.warning("[TickerResolver c%s] IB 连接失败", client_id)
        if progress is not None:
            progress.update(len(work_items))
        return {}

    out: dict[str, dict] = {}
    try:
        for key, data in work_items:
            isin = data.get("isin", "")
            cusip = data.get("cusip", "")

            cached = _lookup_cache_entry(unified_snapshot, key)
            if cached:
                out[key] = _build_resolution_result(None, cached_entry=cached)
            elif not isin and not cusip:
                out[key] = _build_resolution_result(None)
            else:
                ticker, method, source, extra = _resolve_via_ib(ib, cusip, isin)
                out[key] = _build_resolution_result(ticker, method, source, extra)
                if pause_seconds > 0:
                    time.sleep(pause_seconds)

            if progress is not None:
                progress.update(1)
    finally:
        try:
            ib.disconnect()
        except Exception as e:
            logger.warning("[TickerResolver c%s] 断开 IB 连接失败: %s", client_id, e)
    return out


def connect_ib(host: str = IB_HOST, port: int = IB_PORT, client_id: int = IB_CLIENT_ID) -> Optional[IB]:
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=10)
        return ib
    except Exception as e:
        logger.warning("连接 IB (%s:%s clientId=%s) 失败: %s", host, port, client_id, e)
        return None


def lookup_ticker_by_secid(ib: IB, secid_type: str, secid: str) -> Optional[str]:
    """Legacy simple version - kept for backward compat."""
    details = lookup_contract_details_by_secid(ib, secid_type, secid)
    if not details:
        return None
    return pick_us_ticker([details])


def lookup_contract_details_by_secid(ib: IB, secid_type: str, secid: str) -> Optional[object]:
    """返回最佳的美股 USD ContractDetails；无合格结果时返回 None。"""
    contract = Contract()
    contract.secIdType = secid_type
    contract.secId = secid
    contract.secType = "STK"
    contract.currency = "USD"
    contract.exchange = "SMART"
    try:
        details_list = ib.reqContractDetails(contract)
    except Exception:
        return None

    if not details_list:
        return None

    return pick_best_us_contract(details_list)


def extract_contract_info(details: object) -> dict:
    """从 ContractDetails 提取我们关心的丰富字段，用于持久化。"""
    if details is None:
        return {}
    try:
        c = details.contract
        return {
            "conId": getattr(c, "conId", None),
            "primaryExchange": getattr(c, "primaryExchange", "") or getattr(c, "exchange", ""),
            "currency": getattr(c, "currency", ""),
            "secType": getattr(c, "secType", ""),
            "longName": getattr(details, "longName", "") or getattr(c, "localSymbol", ""),
            "exchange": getattr(c, "exchange", ""),
        }
    except Exception:
        return {}


# OpenFIGI 相关代码已完全移除（2026）


# ==================== 主解析逻辑 ====================

class TickerResolver:
    """
    NPORT 持仓 Ticker 解析的唯一权威模块（2026 重构版）。

    特点：
    - 统一缓存：减少重复 IB 请求
    - 清晰的解析策略（优先级：缓存 → IB）
    - OpenFIGI 已移除（实测收益很小，维护成本较高）
    - 自动在 holdings 记录上附加 _ticker_source / _ticker_conid 等元数据
    - 支持 force 重新解析 + 高效去重批量模式

    推荐用法：
        resolver = TickerResolver()
        ticker, meta = resolver.resolve_with_meta(cusip=..., isin=..., name=...)
        # 或批量（大批量自动去重）
        resolver.resolve_holdings(holdings, force=False)
    """

    def __init__(
        self,
        ib_host: str = IB_HOST,
        ib_port: int = IB_PORT,
        ib_client_id: int = IB_CLIENT_ID,
        enable_ib: bool = True,
        num_connections: int = DEFAULT_NUM_CONNECTIONS,
        pause_seconds: float = 0.05,
        proxies: dict | None = None,
        **_kwargs,
    ):
        self.ib_host = ib_host
        self.ib_port = ib_port
        self.ib_client_id = ib_client_id
        self.enable_ib = enable_ib
        self.num_connections = max(1, num_connections)
        self.pause_seconds = pause_seconds
        self.proxies = proxies

        self.unified = ticker_load_unified_cache()

        self._ib = None

    def save_caches(self):
        ticker_save_unified_cache(self.unified)

    # ==================== IB 连接复用 ====================
    def _get_ib(self):
        """懒连接 + 复用同一个 IB 会话（大幅提升批量解析速度）。"""
        if not self.enable_ib:
            return None
        if self._ib is not None:
            try:
                if self._ib.isConnected():
                    return self._ib
            except Exception as e:
                logger.warning("检查 IB 连接状态失败，将重连: %s", e)
        self._ib = connect_ib(self.ib_host, self.ib_port, self.ib_client_id)
        return self._ib

    def close(self):
        """显式关闭 IB 连接（推荐在批量结束后调用）。"""
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception as e:
                logger.warning("断开 IB 连接失败: %s", e)
            self._ib = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def resolve_with_meta(
        self, cusip: str = "", isin: str = "", name: str = "", persist: bool = True
    ) -> tuple[Optional[str], dict]:
        """返回 (ticker, metadata) - 重构核心方法。

        ``persist=False`` 时仅写入内存缓存、不落库，供批量循环调用后统一持久化，
        避免每解析一条就重写整张 ticker 表（O(N^2)）。
        """
        cusip = str(cusip or "").strip().upper()
        isin = str(isin or "").strip()
        key = self._make_key(cusip, isin)

        cached = _lookup_cache_entry(self.unified, key)
        if cached:
            return cached.get("ticker"), cached

        ticker = None
        method = source = ""
        extra_meta = {}

        if self.enable_ib:
            ib = self._get_ib()
            if ib:
                try:
                    ticker, method, source, extra_meta = _resolve_via_ib(ib, cusip, isin)
                except Exception as e:
                    logger.warning("IB 解析 (cusip=%s isin=%s) 失败: %s", cusip, isin, e)

        if ticker:
            self._save_resolution(key, ticker, source or "unknown", method, extra=extra_meta, persist=persist)
            meta_out = {"source": source, "method": method, "resolved_at": _now_iso()}
            meta_out.update(extra_meta)
            return ticker, meta_out

        return None, {"source": "failed", "method": "none"}

    def _make_key(self, cusip: str, isin: str) -> str:
        return _make_identifier_key(cusip, isin)

    def _save_resolution(
        self,
        key: str,
        ticker: str,
        source: str,
        method: str,
        extra: dict | None = None,
        persist: bool = True,
    ):
        if not key:
            return
        entry = {
            "ticker": ticker,
            "source": source,
            "method": method,
            "resolved_at": _now_iso(),
        }
        if extra:
            entry.update({k: v for k, v in extra.items() if v})
        self.unified[key] = entry
        if persist:
            self.save_caches()

    def _merge_resolution_results(self, partial: dict[str, dict]) -> None:
        for key, info in partial.items():
            ticker = info.get("ticker")
            if not ticker:
                continue
            extra = {
                k: info[k]
                for k in ("conId", "primaryExchange", "currency", "secType", "longName", "exchange")
                if k in info and info[k]
            }
            self._save_resolution(
                key,
                ticker,
                info.get("source", "unknown"),
                info.get("method", ""),
                extra=extra,
                persist=False,
            )

    # ==================== 高效批量去重解析（解决重复请求核心问题） ====================
    def resolve_unique_identifiers(self, identifiers: list[dict]) -> dict:
        """
        高效去重解析接口。
        输入示例: [{"isin": "...", "cusip": "...", "name": "..."}, ...]
        返回: { "ISIN:xxx": {"ticker": "ABC", "source": "...", ...}, ... }

        全量回填时强烈建议使用此方法，可将重复请求从 N 倍降到 1 倍。
        num_connections > 1 时使用多 IB 连接并行解析。
        """
        unique_keys: dict[str, dict] = {}
        for item in identifiers:
            isin = str(item.get("isin") or "").strip()
            cusip = str(item.get("cusip") or "").strip().upper()
            name = str(item.get("name") or item.get("title") or "").strip()

            if isin:
                key = f"ISIN:{isin}"
            elif cusip:
                key = f"CUSIP:{cusip}"
            else:
                key = f"NAME:{name[:50]}" if name else None

            if key and key not in unique_keys:
                unique_keys[key] = {"isin": isin, "cusip": cusip, "name": name}

        results: dict[str, dict] = {}
        ib_work: list[tuple[str, dict]] = []

        for key, data in unique_keys.items():
            cached = _lookup_cache_entry(self.unified, key)
            if cached:
                results[key] = _build_resolution_result(None, cached_entry=cached)
                continue
            if not data.get("isin") and not data.get("cusip"):
                results[key] = _build_resolution_result(None)
                continue
            ib_work.append((key, data))

        if not ib_work:
            return results

        if not self.enable_ib:
            for key, _data in ib_work:
                results[key] = _build_resolution_result(None)
            return results

        num_conn = min(self.num_connections, len(ib_work))
        if num_conn <= 1:
            ib = self._get_ib()
            if ib is None:
                for key, _data in ib_work:
                    results[key] = _build_resolution_result(None)
                return results
            with ProgressBar(len(ib_work), "解析 ticker", unit="个") as bar:
                for key, data in ib_work:
                    ticker, method, source, extra = _resolve_via_ib(
                        ib, data.get("cusip", ""), data.get("isin", "")
                    )
                    info = _build_resolution_result(ticker, method, source, extra)
                    results[key] = info
                    if self.pause_seconds > 0:
                        time.sleep(self.pause_seconds)
                    bar.update(1)
            self._merge_resolution_results({k: results[k] for k, _ in ib_work})
        else:
            chunk_size = max(1, (len(ib_work) + num_conn - 1) // num_conn)
            chunks = [ib_work[i : i + chunk_size] for i in range(0, len(ib_work), chunk_size)]
            unified_snapshot = dict(self.unified)
            failed = 0
            with ProgressBar(len(ib_work), "解析 ticker", unit="个") as bar:
                with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
                    futures = []
                    for i, chunk in enumerate(chunks):
                        c_id = self.ib_client_id + TICKER_CLIENT_ID_OFFSET + i
                        fut = executor.submit(
                            _resolve_identifiers_on_connection,
                            self.ib_host,
                            self.ib_port,
                            c_id,
                            chunk,
                            unified_snapshot,
                            self.pause_seconds,
                            bar,
                        )
                        futures.append((c_id, fut))

                    for c_id, fut in futures:
                        try:
                            partial = fut.result()
                            results.update(partial)
                            self._merge_resolution_results(partial)
                        except Exception as e:
                            failed += 1
                            logger.warning("[TickerResolver c%s] 并行解析失败: %s", c_id, e)
                if failed:
                    logger.warning("[TickerResolver] %d/%d 个并行连接失败", failed, len(futures))

        ib_keys = {k for k, _ in ib_work}
        if any(results.get(k, {}).get("ticker") for k in ib_keys):
            self.save_caches()
        return results

    def apply_resolution_to_holdings(self, holdings: list[dict], resolution_map: dict) -> int:
        """把 resolve_unique_identifiers 的结果应用回 holdings 列表。"""
        resolved = 0
        for row in holdings:
            isin = str(row.get("isin") or "").strip()
            cusip = str(row.get("cusip") or "").strip().upper()

            key = None
            if isin:
                key = f"ISIN:{isin}"
            elif cusip:
                key = f"CUSIP:{cusip}"

            if key and key in resolution_map:
                info = resolution_map[key]
                ticker = info.get("ticker")
                if ticker:
                    row["ticker"] = ticker
                    row["_ticker_source"] = info.get("source", "unknown")
                    row["_ticker_resolved_at"] = info.get("resolved_at") or _now_iso()
                    row["_ticker_method"] = info.get("method", "")
                    for k in ("conId", "primaryExchange", "currency", "secType"):
                        if k in info and info[k]:
                            row[f"_ticker_{k.lower()}"] = info[k]
                    resolved += 1
        return resolved

    # ==================== 重构后的主力 API ====================

    def resolve_holdings(self, holdings: List[dict], force: bool = False, progress_every: int = 100, use_dedup: bool | None = None) -> int:
        """
        推荐的批量解析入口（重构版 + 去重优化）。
        大批量（尤其是 --full-backfill）时会自动启用唯一键去重，大幅减少重复 ISIN 请求。
        """
        if use_dedup is None:
            use_dedup = len(holdings) > 300 or self.num_connections > 1

        if not use_dedup:
            resolved = 0
            if self.enable_ib:
                self._get_ib()

            pending = [
                row for row in holdings
                if force or not row.get("ticker")
            ]
            with ProgressBar(len(pending), "解析 ticker", unit="条") as bar:
                for row in pending:
                    cusip = str(row.get("cusip") or "").strip()
                    isin = str(row.get("isin") or "").strip()
                    name = str(row.get("name") or row.get("title") or "").strip()
                    ticker, meta = self.resolve_with_meta(cusip=cusip, isin=isin, name=name, persist=False)
                    if ticker:
                        row["ticker"] = ticker
                        row["_ticker_source"] = meta.get("source", "unknown")
                        row["_ticker_resolved_at"] = meta.get("resolved_at") or _now_iso()
                        row["_ticker_method"] = meta.get("method", "")
                        for k in ("conId", "primaryExchange", "currency", "secType"):
                            if k in meta and meta[k]:
                                row[f"_ticker_{k.lower()}"] = meta[k]
                        resolved += 1
                    bar.update(1, 新增=resolved)
            if resolved:
                self.save_caches()
            return resolved

        # ==================== 高效去重路径（推荐用于全量回填） ====================
        to_resolve_rows = [row for row in holdings if not row.get("ticker") or force]

        if not to_resolve_rows:
            return 0

        unique_inputs = []
        seen = set()
        for row in to_resolve_rows:
            isin = str(row.get("isin") or "").strip()
            cusip = str(row.get("cusip") or "").strip().upper()
            name = str(row.get("name") or row.get("title") or "").strip()
            key = f"ISIN:{isin}" if isin else (f"CUSIP:{cusip}" if cusip else f"NAME:{name[:40]}")
            if key not in seen:
                seen.add(key)
                unique_inputs.append({"isin": isin, "cusip": cusip, "name": name})

        logger.info(
            "去重解析 %d 个唯一标识（原 %d 条持仓）", len(unique_inputs), len(to_resolve_rows)
        )

        resolution_map = self.resolve_unique_identifiers(unique_inputs)
        resolved = self.apply_resolution_to_holdings(to_resolve_rows, resolution_map)

        logger.info("本次新增 %d 条 ticker", resolved)
        return resolved

    # 为了向后兼容保留旧签名（会调用新逻辑）
    def resolve(self, cusip: str = "", isin: str = "", name: str = "") -> Optional[str]:
        ticker, _ = self.resolve_with_meta(cusip, isin, name)
        return ticker


# ==================== 便捷函数（兼容旧代码） ====================

def resolve_missing_tickers_in_holdings(holdings: List[dict], **resolver_kwargs) -> int:
    """最简单的批量补全入口"""
    resolver = TickerResolver(**resolver_kwargs)
    return resolver.resolve_holdings(holdings)
