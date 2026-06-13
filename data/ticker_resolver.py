"""
Ticker Resolver — Unified, cached, multi-source ticker lookup for NPORT holdings.

Priority order (per security):
1. Local cache (cusip / isin)
2. External manual overrides (cache/manual_ticker_overrides.json) — highest trust for acquired/delisted
3. IB Gateway (by ISIN → CUSIP → name)
4. Fail gracefully (leave ticker empty)

说明：OpenFIGI 已于 2026 年移除。目前主要依靠 IB + 手动覆盖，实测覆盖率已达 92%+。

This module is designed to be called both:
- Automatically during nport_data sync
- Manually via CLI for one-off repair (see fill_tickers.py)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

def _now_iso():
    return datetime.now().isoformat()

import pandas as pd
import requests
from ib_insync import IB, Contract

# ==================== 配置 ====================

# All caches are now centralized in the cache/ folder
ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"
DEFAULT_CUSIP_CACHE = CACHE_DIR / "cusip_to_ticker_cache.json"
DEFAULT_ISIN_CACHE = CACHE_DIR / "isin_to_ticker_cache.json"

# New unified resolution metadata cache (recommended for future use)
DEFAULT_UNIFIED_CACHE = CACHE_DIR / "ticker_resolution_cache.json"

# 外部手动覆盖文件（单一真相源，替代硬编码 MANUAL_ISIN_MAP）
DEFAULT_MANUAL_OVERRIDES = CACHE_DIR / "manual_ticker_overrides.json"

# 失败解析缓存：记录查不到的 ISIN/CUSIP，避免重复浪费 IB 请求
DEFAULT_FAILED_CACHE = CACHE_DIR / "failed_ticker_resolutions.json"

RESOLUTION_SOURCES = {
    "manual_override": "User-maintained JSON override (highest priority, for acquired/delisted names)",
    "manual": "Legacy hardcoded map (being phased out)",
    "ib_isin": "Interactive Brokers by ISIN",
    "ib_cusip": "Interactive Brokers by CUSIP",
    "ib_name": "Interactive Brokers by cleaned company name",
    "openfigi": "OpenFIGI by CUSIP",
    "cache": "Previously resolved (from cache)"
}


def _load_manual_overrides(path: Path = DEFAULT_MANUAL_OVERRIDES) -> dict:
    """加载外部手动 ticker 覆盖表。返回结构化的 overrides 数据。"""
    if not path.exists():
        return {"by_isin": {}, "by_cusip": {}, "special_cases": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "by_isin": data.get("by_isin", {}),
            "by_cusip": data.get("by_cusip", {}),
            "special_cases": data.get("special_cases", {}),
        }
    except Exception as e:
        print(f"[warn] 加载 manual_ticker_overrides.json 失败: {e}", file=sys.stderr)
        return {"by_isin": {}, "by_cusip": {}, "special_cases": {}}


def _load_failed_resolutions(path: Path = DEFAULT_FAILED_CACHE) -> dict:
    """加载失败记录：key 是 ISIN 或 CUSIP，value 包含 last_attempt, reason 等。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_failed_resolutions(failed: dict, path: Path = DEFAULT_FAILED_CACHE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(failed, f, ensure_ascii=False, indent=2)


US_EXCHANGES = {"SMART", "NYSE", "NASDAQ", "AMEX", "ARCA", "VALUE", "BATS", "IEX"}

# OpenFIGI 已移除（2026-05）
# 现在完全依赖 IB + 手动覆盖 + 失败缓存，OpenFIGI 带来的边际收益很小且维护成本较高。


# ==================== 缓存工具 ====================

def load_cache(path: Path) -> Dict[str, str]:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: Dict[str, str], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ==================== IB 相关 ====================

def connect_ib(host: str = "127.0.0.1", port: int = 4001, client_id: int = 17) -> Optional[IB]:
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=10)
        return ib
    except Exception:
        return None


def pick_us_ticker(details: list) -> Optional[str]:
    if not details:
        return None
    candidates = []
    for d in details:
        c = d.contract
        if c.secType != "STK":
            continue
        exch = (c.exchange or "").upper()
        score = 0
        if exch in US_EXCHANGES:
            score += 100
        if exch in {"SMART", "NYSE", "NASDAQ"}:
            score += 40
        if 1 <= len(c.symbol) <= 5:
            score += 20
        candidates.append((score, c.symbol))
    if not candidates:
        return details[0].contract.symbol if details else None
    candidates.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return candidates[0][1]


def lookup_ticker_by_secid(ib: IB, secid_type: str, secid: str) -> Optional[str]:
    """Legacy simple version - kept for backward compat."""
    details = lookup_contract_details_by_secid(ib, secid_type, secid)
    if not details:
        return None
    return pick_us_ticker([details]) if not isinstance(details, list) else pick_us_ticker(details)


def lookup_contract_details_by_secid(ib: IB, secid_type: str, secid: str) -> Optional[object]:
    """返回最佳的 ContractDetails 对象（包含 conId, primaryExchange 等丰富信息）。"""
    contract = Contract()
    contract.secIdType = secid_type
    contract.secId = secid
    try:
        details_list = ib.reqContractDetails(contract)
    except Exception:
        return None

    if not details_list:
        return None

    # 优先选择美股 STK
    for d in details_list:
        c = d.contract
        if c.secType == "STK" and (c.exchange or "").upper() in US_EXCHANGES:
            return d
    return details_list[0]  # fallback


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


def lookup_ticker_by_name(ib: IB, name: str) -> Optional[str]:
    """Legacy simple version."""
    info = lookup_contract_info_by_name(ib, name)
    return info.get("ticker") if info else None


def lookup_contract_info_by_name(ib: IB, name: str) -> Optional[dict]:
    """通过名称搜索，返回 ticker + 丰富合同信息（用于 name 匹配场景）。"""
    if not name or len(name) < 2:
        return None
    search_terms = []
    clean = name.replace(",", " ").replace(".", " ").upper()
    words = clean.split()
    suffixes = {"INC", "LTD", "PLC", "CORP", "CORPORATION", "COMPANY", "CO", "GROUP",
                "HOLDINGS", "HOLDING", "LIMITED", "LLC", "LP", "RTS"}
    trimmed = [w for w in words if w not in suffixes]
    for n in [1, 2, 3]:
        if len(trimmed) >= n:
            search_terms.append(" ".join(trimmed[:n]))
    search_terms.append(name)
    search_terms = list(dict.fromkeys(search_terms))

    for term in search_terms:
        try:
            results = ib.reqMatchingSymbols(term)
        except Exception:
            continue
        if results:
            candidates = []
            for r in results:
                c = r.contract
                if c.secType != "STK":
                    continue
                exch = (c.exchange or "").upper()
                score = 0
                if exch in US_EXCHANGES:
                    score += 100
                if 1 <= len(c.symbol) <= 5:
                    score += 20
                candidates.append((score, c.symbol, c))
            if candidates:
                candidates.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
                best = candidates[0]
                return {
                    "ticker": best[1],
                    "conId": getattr(best[2], "conId", None),
                    "primaryExchange": getattr(best[2], "primaryExchange", "") or getattr(best[2], "exchange", ""),
                    "currency": getattr(best[2], "currency", ""),
                    "secType": getattr(best[2], "secType", ""),
                }
    return None


# OpenFIGI 相关代码已完全移除（2026）


# ==================== 主解析逻辑 ====================

class TickerResolver:
    """
    NPORT 持仓 Ticker 解析的唯一权威模块（2026 重构版）。

    特点：
    - 外部手动覆盖：cache/manual_ticker_overrides.json（推荐维护方式）
    - 统一缓存 + 失败缓存：大幅减少重复请求和无效 IB 调用
    - 清晰的解析策略（优先级：缓存 → 手动覆盖 → IB）
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
        cusip_cache_path: Path = DEFAULT_CUSIP_CACHE,
        isin_cache_path: Path = DEFAULT_ISIN_CACHE,
        unified_cache_path: Path = DEFAULT_UNIFIED_CACHE,
        manual_overrides_path: Path = DEFAULT_MANUAL_OVERRIDES,
        ib_host: str = "127.0.0.1",
        ib_port: int = 4001,
        ib_client_id: int = 17,
        enable_ib: bool = True,
        proxies: dict | None = None,
    ):
        self.cusip_cache_path = cusip_cache_path
        self.isin_cache_path = isin_cache_path
        self.unified_cache_path = unified_cache_path
        self.manual_overrides_path = manual_overrides_path
        self.ib_host = ib_host
        self.ib_port = ib_port
        self.ib_client_id = ib_client_id
        self.enable_ib = enable_ib
        self.proxies = proxies  # 仍保留 proxies 参数（未来如需其他代理场景可用）

        self.cusip_cache = load_cache(cusip_cache_path)
        self.isin_cache = load_cache(isin_cache_path)
        self.unified = load_cache(unified_cache_path)  # 主力统一元数据缓存
        self.manual_overrides = _load_manual_overrides(manual_overrides_path)
        self.failed = _load_failed_resolutions(DEFAULT_FAILED_CACHE)  # 失败缓存，防重复请求

        # A 改进：支持 IB 连接复用
        self._ib = None

    def save_caches(self):
        save_cache(self.cusip_cache, self.cusip_cache_path)
        save_cache(self.isin_cache, self.isin_cache_path)
        save_cache(self.unified, self.unified_cache_path)
        _save_failed_resolutions(self.failed, DEFAULT_FAILED_CACHE)

    # ==================== IB 连接复用（A 关键改进） ====================
    def _get_ib(self):
        """懒连接 + 复用同一个 IB 会话（大幅提升批量解析速度）。"""
        if not self.enable_ib:
            return None
        if self._ib is not None:
            try:
                if self._ib.isConnected():
                    return self._ib
            except Exception:
                pass
        self._ib = connect_ib(self.ib_host, self.ib_port, self.ib_client_id)
        return self._ib

    def close(self):
        """显式关闭 IB 连接 + 保存失败缓存（推荐在批量结束后调用）。"""
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass
            self._ib = None
        # 确保失败记录持久化
        try:
            _save_failed_resolutions(self.failed, DEFAULT_FAILED_CACHE)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ==================== 失败缓存 & 去重防护（本次核心修复） ====================
    def _is_recently_failed(self, isin: str = "", cusip: str = "", days: int = 30) -> bool:
        """检查该 ISIN 或 CUSIP 是否在近期失败过，避免重复请求。"""
        key = None
        if isin:
            key = f"ISIN:{isin}"
        elif cusip:
            key = f"CUSIP:{cusip}"
        if not key or key not in self.failed:
            return False

        entry = self.failed[key]
        try:
            last = datetime.fromisoformat(entry.get("last_attempt", ""))
            age_days = (datetime.now() - last).days
            return age_days < days
        except Exception:
            return False

    def _record_failure(self, isin: str = "", cusip: str = "", reason: str = "ib_no_definition"):
        """记录一次失败查询。"""
        key = None
        if isin:
            key = f"ISIN:{isin}"
        elif cusip:
            key = f"CUSIP:{cusip}"
        if not key:
            return

        self.failed[key] = {
            "last_attempt": _now_iso(),
            "reason": reason,
            "count": self.failed.get(key, {}).get("count", 0) + 1
        }

    def resolve(self, cusip: str = "", isin: str = "", name: str = "") -> Optional[str]:
        """单条解析，优先使用缓存 + 手动映射"""
        cusip = str(cusip or "").strip().upper()
        isin = str(isin or "").strip()
        name = str(name or "").strip()

        # 1. 缓存命中
        if cusip and cusip in self.cusip_cache:
            return self.cusip_cache[cusip]
        if isin and isin in self.isin_cache:
            return self.isin_cache[isin]

        # 2. 手动覆盖（外部 JSON，最高优先级）
        if isin and isin in self.manual_overrides.get("by_isin", {}):
            entry = self.manual_overrides["by_isin"][isin]
            ticker = entry["ticker"] if isinstance(entry, dict) else entry
            self.isin_cache[isin] = ticker
            return ticker

        # 3. 需要外部查询（IB / OpenFIGI）时才真正尝试
        # 先检查失败缓存，避免重复请求同一个死 ISIN
        if self._is_recently_failed(isin=isin, cusip=cusip):
            return None

        ticker = None

        if self.enable_ib:
            ib = connect_ib(self.ib_host, self.ib_port, self.ib_client_id)
            if ib:
                try:
                    if isin:
                        ticker = lookup_ticker_by_secid(ib, "ISIN", isin)
                    if not ticker and cusip:
                        ticker = lookup_ticker_by_secid(ib, "CUSIP", cusip)
                    if not ticker and name:
                        ticker = lookup_ticker_by_name(ib, name)
                finally:
                    try:
                        ib.disconnect()
                    except Exception:
                        pass

        # OpenFIGI 已移除，此处不再有额外 fallback

        # 如果彻底失败，记录下来
        if not ticker and (isin or cusip):
            self._record_failure(isin=isin, cusip=cusip, reason="no_result_from_ib")

        # 写缓存
        if ticker:
            if cusip:
                self.cusip_cache[cusip] = ticker
            if isin:
                self.isin_cache[isin] = ticker
            self.save_caches()

        return ticker

    def resolve_with_meta(self, cusip: str = "", isin: str = "", name: str = "") -> tuple[Optional[str], dict]:
        """返回 (ticker, metadata) - 重构核心方法"""
        cusip = str(cusip or "").strip().upper()
        isin = str(isin or "").strip()
        name = str(name or "").strip()
        key = self._make_key(cusip, isin)

        if key and key in self.unified:
            entry = self.unified[key]
            return entry.get("ticker"), entry

        # 手动覆盖（外部 JSON 文件，最高优先级）
        if isin and isin in self.manual_overrides.get("by_isin", {}):
            entry = self.manual_overrides["by_isin"][isin]
            ticker = entry["ticker"] if isinstance(entry, dict) else entry
            reason = entry.get("reason", "") if isinstance(entry, dict) else ""
            self._save_resolution(key or f"ISIN:{isin}", ticker, "manual_override", "manual")
            return ticker, {
                "source": "manual_override",
                "method": "manual",
                "reason": reason,
                "resolved_at": _now_iso()
            }

        # 失败缓存检查：避免对同一个死 ISIN/CUSIP 反复请求
        if self._is_recently_failed(isin=isin, cusip=cusip):
            return None, {"source": "failed_recently", "method": "skipped", "reason": "recent_failure_cache"}

        ticker = None
        method = source = ""
        extra_meta = {}

        if self.enable_ib:
            ib = self._get_ib()
            if ib:
                try:
                    if isin and not ticker:
                        details = lookup_contract_details_by_secid(ib, "ISIN", isin)
                        if details:
                            t = pick_us_ticker([details]) if not isinstance(details, list) else pick_us_ticker(details)
                            if t:
                                ticker, method, source = t, "ib_isin", "ib"
                                extra_meta = extract_contract_info(details)
                    if cusip and not ticker:
                        details = lookup_contract_details_by_secid(ib, "CUSIP", cusip)
                        if details:
                            t = pick_us_ticker([details]) if not isinstance(details, list) else pick_us_ticker(details)
                            if t:
                                ticker, method, source = t, "ib_cusip", "ib"
                                extra_meta = extract_contract_info(details)
                    if name and not ticker:
                        info = lookup_contract_info_by_name(ib, name)
                        if info and info.get("ticker"):
                            ticker, method, source = info["ticker"], "ib_name", "ib"
                            extra_meta = {k: v for k, v in info.items() if k != "ticker"}
                        time.sleep(0.05)  # name 匹配较重，稍微节流
                except Exception:
                    pass

        # OpenFIGI 已移除，不再作为 fallback

        if ticker:
            self._save_resolution(key, ticker, source or "unknown", method, extra=extra_meta)
            meta_out = {"source": source, "method": method, "resolved_at": _now_iso()}
            meta_out.update(extra_meta)
            return ticker, meta_out

        # 记录失败，避免下次重复浪费请求
        if isin or cusip:
            self._record_failure(isin=isin, cusip=cusip, reason="no_result_after_ib_openfigi")
            self.save_caches()  # 立即持久化失败记录

        return None, {"source": "failed", "method": "none"}

    def _make_key(self, cusip: str, isin: str) -> str:
        if cusip: return f"CUSIP:{cusip}"
        if isin: return f"ISIN:{isin}"
        return ""

    def _save_resolution(self, key: str, ticker: str, source: str, method: str, extra: dict | None = None):
        if not key: return
        entry = {
            "ticker": ticker,
            "source": source,
            "method": method,
            "resolved_at": _now_iso()
        }
        if extra:
            entry.update({k: v for k, v in extra.items() if v})
        self.unified[key] = entry
        self.save_caches()

    # ==================== 高效批量去重解析（解决重复请求核心问题） ====================
    def resolve_unique_identifiers(self, identifiers: list[dict]) -> dict:
        """
        高效去重解析接口。
        输入示例: [{"isin": "...", "cusip": "...", "name": "..."}, ...]
        返回: { "ISIN:xxx": {"ticker": "ABC", "source": "...", ...}, ... }

        全量回填时强烈建议使用此方法，可将重复请求从 N 倍降到 1 倍。
        """
        # 1. 收集所有唯一键
        unique_keys = {}
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
                unique_keys[key] = {"isin": isin, "cusip": cusip, "name": name, "original_key": key}

        results = {}

        # 2. 对每个唯一键解析一次
        for key, data in unique_keys.items():
            isin = data["isin"]
            cusip = data["cusip"]
            name = data["name"]

            # 先查失败缓存
            if self._is_recently_failed(isin=isin, cusip=cusip):
                results[key] = {"ticker": None, "source": "failed_recently", "method": "skipped"}
                continue

            # 正常解析
            ticker, meta = self.resolve_with_meta(cusip=cusip, isin=isin, name=name)
            results[key] = {"ticker": ticker, **meta}

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
            # 超过 300 条时默认开启去重（历史数据很容易重复 20+ 次）
            use_dedup = len(holdings) > 300

        if not use_dedup:
            # 老路径（兼容小批量）
            resolved = 0
            if self.enable_ib:
                self._get_ib()

            for i, row in enumerate(holdings):
                if row.get("ticker") and not force:
                    continue
                cusip = str(row.get("cusip") or "").strip()
                isin = str(row.get("isin") or "").strip()
                name = str(row.get("name") or row.get("title") or "").strip()
                ticker, meta = self.resolve_with_meta(cusip=cusip, isin=isin, name=name)
                if ticker:
                    row["ticker"] = ticker
                    row["_ticker_source"] = meta.get("source", "unknown")
                    row["_ticker_resolved_at"] = meta.get("resolved_at") or _now_iso()
                    row["_ticker_method"] = meta.get("method", "")
                    for k in ("conId", "primaryExchange", "currency", "secType"):
                        if k in meta and meta[k]:
                            row[f"_ticker_{k.lower()}"] = meta[k]
                    resolved += 1
                if (i + 1) % progress_every == 0:
                    print(f"[TickerResolver] 已处理 {i+1}/{len(holdings)}，本次新增 {resolved}")
            if resolved:
                self.save_caches()
            return resolved

        # ==================== 高效去重路径（推荐用于全量回填） ====================
        print(f"[TickerResolver] 启用高效去重模式处理 {len(holdings)} 条持仓...")

        # 1. 收集需要解析的行
        to_resolve_rows = [row for row in holdings if not row.get("ticker") or force]

        if not to_resolve_rows:
            return 0

        # 2. 构建唯一标识列表
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

        print(f"  去重后唯一标识数: {len(unique_inputs)} (原 {len(to_resolve_rows)} 条)")

        # 3. 去重解析
        resolution_map = self.resolve_unique_identifiers(unique_inputs)

        # 4. 应用结果
        resolved = self.apply_resolution_to_holdings(to_resolve_rows, resolution_map)

        print(f"  本次去重解析新增: {resolved} 条")
        self.save_caches()
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
