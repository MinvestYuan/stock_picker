"""
NPORT Data Manager — 核心数据层

目标（严格对齐用户核心需求）：
- 每次运行 Russell 命令时自动轻量检查持仓是否有更新
- 只有真正有变化时才下载 primary_doc.xml + 解析 + 补 ticker
- 价格数据已有优秀增量逻辑（data_fetcher），这里专注 NPORT 持仓
- 对外提供干净的 get_latest_universe() / get_monthly_universes()

获取 filing 列表已优化为使用 SEC efts 全文搜索（q=seriesId & forms=NPORT-P），
直接命中 IWB，无需拉取 CIK 下全部 NPORT-P 再 probe 过滤。

数据文件：
- nport_filings_index.json   （轻量索引，永远快速加载）
- nport_holdings_cache.json  （完整持仓数据，保持向后兼容）
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
import requests

from .ticker_resolver import TickerResolver

# ==================== 配置 ====================

# All data caches centralized in cache/ folder for better organization
ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

NPORT_INDEX_FILE = CACHE_DIR / "nport_filings_index.json"
NPORT_HOLDINGS_CACHE = CACHE_DIR / "nport_holdings_cache.json"
NPORT_DB = CACHE_DIR / "nport.db"
NPORT_XML_DIR = ROOT_DIR / "nport_xmls"  # kept at root for now (raw downloads)

CIK = "0001100663"
SERIES_ID = "S000004347"
BOOTSTRAP_START_DATE = "2019-01-01"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 (contact: user@example.com)",
    "Accept-Encoding": "gzip, deflate",
}

REQUEST_DELAY = 0.2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_index() -> Dict:
    if NPORT_INDEX_FILE.exists():
        try:
            with open(NPORT_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_checked": None, "filings": {}}


def _save_index(idx: Dict):
    NPORT_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NPORT_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


# ==================== 元数据短时缓存（让“轻量检查”真正增量） ====================
NPORT_METADATA_CACHE = CACHE_DIR / "nport_metadata_cache.json"
METADATA_CACHE_TTL_HOURS = 12


def _load_metadata_cache() -> tuple[List[Dict] | None, str | None]:
    """返回 (filings, cached_at_iso) 或 (None, None)"""
    if not NPORT_METADATA_CACHE.exists():
        return None, None
    try:
        with open(NPORT_METADATA_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cached_at = data.get("cached_at")
        if cached_at:
            cached_time = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - cached_time).total_seconds() / 3600
            if age < METADATA_CACHE_TTL_HOURS:
                return data.get("filings"), cached_at
    except Exception:
        pass
    return None, None


def _save_metadata_cache(filings: List[Dict]):
    NPORT_METADATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "cached_at": _now_iso(),
        "filings": filings,
    }
    with open(NPORT_METADATA_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==================== SQLite 存储层（推荐用于长期增量与查询效率） ====================
import sqlite3

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(NPORT_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _init_nport_db():
    """创建必要的表（幂等）。"""
    conn = _get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS filings (
                accession TEXT PRIMARY KEY,
                filing_date TEXT,
                report_period_date TEXT,
                holdings_count INTEGER,
                last_parsed TEXT,
                has_full_holdings INTEGER DEFAULT 1,
                holdings_json TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_report_period ON filings(report_period_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_filing_date ON filings(filing_date)")

        # 轻量元数据表：last_checked, max_filing_date 等
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _migrate_from_json_if_needed():
    """
    一次性迁移：如果存在旧的 JSON 缓存但 SQLite 为空，则导入数据。
    迁移成功后可手动删除旧 JSON（或我们以后自动清理）。
    """
    if NPORT_DB.exists():
        conn = _get_db()
        count = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        conn.close()
        if count > 0:
            return  # 已有数据，无需迁移

    # 尝试从旧 holdings cache 迁移
    old_holdings = {}
    if NPORT_HOLDINGS_CACHE.exists():
        try:
            with open(NPORT_HOLDINGS_CACHE, "r", encoding="utf-8") as f:
                old_holdings = json.load(f)
        except Exception:
            pass

    if not old_holdings:
        return

    print("[nport_data] 检测到旧 JSON 缓存，正在迁移到 SQLite（一次性操作）...")

    _init_nport_db()
    conn = _get_db()
    try:
        for acc, holdings in old_holdings.items():
            if not holdings:
                continue
            rep_date = holdings[0].get("reportPeriodDate", "") if holdings else ""
            filing_date = holdings[0].get("filingDate", "") if holdings else ""
            conn.execute("""
                INSERT OR REPLACE INTO filings
                (accession, filing_date, report_period_date, holdings_count, last_parsed, has_full_holdings, holdings_json)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (
                acc,
                filing_date,
                rep_date,
                len(holdings),
                _now_iso(),
                json.dumps(holdings, ensure_ascii=False)
            ))
        conn.commit()
        print(f"[nport_data] 迁移完成，共导入 {len(old_holdings)} 个 filing。")
    except Exception as e:
        print(f"[nport_data] 迁移失败: {e}")
    finally:
        conn.close()


def _load_holdings_from_db() -> Dict[str, List[Dict]]:
    """从 SQLite 加载全部 holdings（兼容旧接口）。"""
    _init_nport_db()
    conn = _get_db()
    try:
        rows = conn.execute("SELECT accession, holdings_json FROM filings").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["accession"]] = json.loads(row["holdings_json"])
            except Exception:
                continue
        return result
    finally:
        conn.close()


def _save_holdings_to_db(holdings_dict: Dict[str, List[Dict]]):
    """增量保存 holdings 到 SQLite。"""
    _init_nport_db()
    conn = _get_db()
    try:
        for acc, holdings in holdings_dict.items():
            if not holdings:
                continue
            rep_date = holdings[0].get("reportPeriodDate", "") if holdings else ""
            filing_date = holdings[0].get("filingDate", "") if holdings else ""
            conn.execute("""
                INSERT OR REPLACE INTO filings
                (accession, filing_date, report_period_date, holdings_count, last_parsed, has_full_holdings, holdings_json)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (
                acc,
                filing_date,
                rep_date,
                len(holdings),
                _now_iso(),
                json.dumps(holdings, ensure_ascii=False)
            ))
        conn.commit()
    finally:
        conn.close()


def _get_latest_filing_from_db():
    """高效获取最新一期 filing（利用索引）。"""
    _init_nport_db()
    conn = _get_db()
    try:
        row = conn.execute("""
            SELECT accession, report_period_date, holdings_json
            FROM filings
            ORDER BY report_period_date DESC
            LIMIT 1
        """).fetchone()
        if row:
            return {
                "accession": row["accession"],
                "report_period_date": row["report_period_date"],
                "holdings": json.loads(row["holdings_json"]) if row["holdings_json"] else []
            }
        return None
    finally:
        conn.close()


def _get_meta(key: str, default: str | None = None) -> str | None:
    _init_nport_db()
    conn = _get_db()
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def _set_meta(key: str, value: str):
    _init_nport_db()
    conn = _get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


# 旧 JSON 版本（仅用于迁移）
def _load_holdings_cache_json() -> Dict[str, List[Dict]]:
    if NPORT_HOLDINGS_CACHE.exists():
        try:
            with open(NPORT_HOLDINGS_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_holdings_cache_json(cache: Dict[str, List[Dict]]):
    with open(NPORT_HOLDINGS_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# 对外兼容接口：优先使用 SQLite
def _load_holdings_cache() -> Dict[str, List[Dict]]:
    # 尝试从 DB 加载，如果为空则回退 JSON（迁移前）
    db_data = _load_holdings_from_db()
    if db_data:
        return db_data
    return _load_holdings_cache_json()


def _save_holdings_cache(cache: Dict[str, List[Dict]]):
    # 同时写入 DB（主存储）和 JSON（向后兼容/迁移）
    _save_holdings_to_db(cache)
    _save_holdings_cache_json(cache)  # 可在迁移完成后移除此行


# ==================== SEC 元数据获取（轻量）——改用 efts 全文搜索直接按 series 命中 ====================

def fetch_iwb_filings_via_efts(start_date: str = BOOTSTRAP_START_DATE, end_date: str | None = None) -> List[Dict]:
    """通过 SEC 全文搜索直接定位 IWB (series=S000004347) 的 NPORT-P 申报。

    相比旧的 submissions/CIK + 按 series 流式 probe 方式：
    - 一次请求直接命中目标 series 的 filing（无需下载整个 CIK 下的上千个 NPORT-P）
    - 返回结果仅包含 IWB，无需二次过滤
    - 支持自定义日期范围，日常检查时可做窄查询
    - 历史总量极小（~季度 4 次，6 年仅 ~27 条）
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # 只有请求“全量 bootstrap”时才尝试 12h 短时缓存（窄范围查询总是 fresh，以获取最新）
    want_full = start_date <= BOOTSTRAP_START_DATE and end_date >= datetime.now().strftime("%Y-%m-%d")
    if want_full:
        cached_filings, _ = _load_metadata_cache()
        if cached_filings is not None:
            return cached_filings

    base_url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{SERIES_ID}%22&forms=NPORT-P"
        f"&dateRange=custom&startdt={start_date}&enddt={end_date}"
    )

    filings: List[Dict] = []
    from_idx = 0
    size = 100
    while True:
        if from_idx == 0:
            url = base_url
        else:
            url = f"{base_url}&from={from_idx}&size={size}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[nport_data] efts search 请求失败: {e}")
            break

        hits = data.get("hits", {}).get("hits", [])
        for hit in hits:
            src = hit.get("_source", {})
            acc = src.get("adsh") or ""
            if not acc:
                continue
            filings.append({
                "accessionNumber": acc,
                "filingDate": src.get("file_date", ""),
                "reportDate": src.get("period_ending", ""),
            })

        if len(hits) < size:
            break
        from_idx += size
        # 礼貌延迟（efts 也是 SEC，防止过快）
        time.sleep(REQUEST_DELAY)

    # 去重 + 按 filingDate 升序（与旧行为保持一致，便于上游处理）
    seen = set()
    unique = []
    for f in sorted(filings, key=lambda x: x.get("filingDate") or ""):
        if f["accessionNumber"] and f["accessionNumber"] not in seen:
            seen.add(f["accessionNumber"])
            unique.append(f)

    if want_full:
        _save_metadata_cache(unique)

    print(f"[nport_data] 全文搜索完成：命中 {len(unique)} 个 IWB 申报 (range {start_date}..{end_date})")
    return unique


def fetch_nport_filings_metadata() -> List[Dict]:
    """仅获取 IWB 的 NPORT-P 元数据（向后兼容接口）。

    现在内部委托给 fetch_iwb_filings_via_efts 实现全文搜索，
    彻底避免拉取 CIK 下大量无关 series 的 NPORT-P 列表及后续 probe。
    """
    return fetch_iwb_filings_via_efts(start_date=BOOTSTRAP_START_DATE)


def download_and_parse_full_xml(accession: str) -> Optional[List[Dict]]:
    """下载完整 XML 并解析持仓（复用原有解析逻辑的简化版）"""
    from lxml import etree

    acc_nodash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc_nodash}/primary_doc.xml"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        root = etree.fromstring(resp.content)

        # 保存原始 XML（便于审计）
        NPORT_XML_DIR.mkdir(exist_ok=True)
        xml_path = NPORT_XML_DIR / f"{accession}_primary_doc.xml"
        xml_path.write_bytes(resp.content)

        # 解析关键字段（简化版，保持与旧代码一致）
        rep_pd_date = ""
        for elem in root.iter():
            if etree.QName(elem).localname == "repPdDate":
                rep_pd_date = (elem.text or "").strip()
                break

        holdings = []
        for inv in root.iter():
            if etree.QName(inv).localname != "invstOrSec":
                continue
            row = {
                "accessionNumber": accession,
                "reportPeriodDate": rep_pd_date,
                "filingDate": "",  # 后面补
            }
            for child in inv:
                tag = etree.QName(child).localname
                text = (child.text or "").strip()
                if tag in ("name", "title", "cusip", "isin", "balance", "valUSD", "pctVal", "assetCat"):
                    row[tag] = text
                elif tag == "identifiers":
                    for idc in child:
                        if etree.QName(idc).localname == "isin":
                            row["isin"] = idc.get("value", "")
                        elif etree.QName(idc).localname == "ticker":
                            row["ticker"] = idc.get("value", "")
            holdings.append(row)
        return holdings

    except Exception as e:
        print(f"[nport_data] 下载/解析 {accession} 失败: {e}")
        return None


# ==================== 核心同步逻辑 ====================

def sync_holdings_if_needed(
    force: bool = False,
    max_age_hours: int = 0,
    lookback_months: int = 6,
    auto_resolve_tickers: bool = True,
    ib_host: str = "127.0.0.1",
    ib_port: int = 4001,
    ib_client_id: int = 17,
) -> Dict:
    """
    核心函数：每次运行时调用。
    按用户需求设计：
    - 今天只进行一次SEC查询（日历日级别）：如果今天已经完整检查过SEC，后续同日运行（包括 python main.py backtest）直接跳过，不再访问SEC（除非 force=True）。
    - 默认 max_age_hours=0（不使用小时节流），但新增的“今天一次”逻辑优先。
    - 每次只拉取最近 lookback_months 个月的 filing（默认6个月，覆盖最近1-2个季报）
    - 按 filingDate 降序处理，一旦遇到本地已知的 filing，立即暂停读取更早的（高效早停）

    内部仍保留元数据12h缓存（作为对archive请求的二级保护）。

    IB 连接参数（用于 ticker 自动补全）：
    - ib_port 默认 4001（Gateway Live 真实账户）；Paper 交易请传 4002
    - 可通过 main.py 的 --port 透传

    返回示例：
    {
        "changed": False,
        "new_filings": 0,
        "latest_report_date": "2025-12-31",
        "message": "持仓已是最新（2025-12-31），无需更新"
    }
    """
    _migrate_from_json_if_needed()   # 一次性 JSON → SQLite 迁移
    _init_nport_db()

    # 优先从 DB 读取轻量元数据
    last_checked = _get_meta("last_checked")
    max_local_filing = _get_meta("max_filing_date", "")

    # 新增：今天已完整查询过 SEC 就直接跳过（用户需求：一天只查一次SEC，避免重复网络请求）
    # 除非 force=True （用于手动强制刷新）
    if not force:
        today = datetime.now().strftime("%Y-%m-%d")
        last_sec_date = _get_meta("last_sec_check_date")
        if last_sec_date == today:
            latest_row = _get_db().execute(
                "SELECT report_period_date FROM filings ORDER BY report_period_date DESC LIMIT 1"
            ).fetchone()
            latest = latest_row["report_period_date"] if latest_row else ""
            return {
                "changed": False,
                "new_filings": 0,
                "latest_report_date": latest,
                "message": f"今天({today})已查询过SEC，直接跳过（使用本地缓存）",
            }

    # 1. 节流检查（默认 max_age_hours=0 表示每次都检查）
    if not force and last_checked and max_age_hours > 0:
        try:
            last = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if age_hours < max_age_hours:
                latest_row = _get_db().execute(
                    "SELECT report_period_date FROM filings ORDER BY report_period_date DESC LIMIT 1"
                ).fetchone()
                latest = latest_row["report_period_date"] if latest_row else ""
                return {
                    "changed": False,
                    "new_filings": 0,
                    "latest_report_date": latest,
                    "message": f"持仓已是最新（{latest}），{age_hours:.1f}h 内已检查过（DB）",
                }
        except Exception:
            pass

    # 仍保留对旧 index 的读取（用于兼容 max_filing_date 等），但 holdings 已走 DB
    index = _load_index()
    holdings_cache = _load_holdings_cache()

    # 到达这里说明今天第一次决定做SEC检查（daily guard已通过，且未被小时节流跳过）
    # 立即标记“今天已查”，这样本日后续 python main.py backtest 等直接跳过
    _set_meta("last_sec_check_date", datetime.now().strftime("%Y-%m-%d"))

    # 真正需要做网络检查时才打印（避免无意义的噪音）
    print("[nport_data] 正在轻量检查 SEC 最新 NPORT-P 申报...", flush=True)

    # 2. 使用 efts 全文搜索直接获取 IWB 目标 filing（支持日期范围窄查询，零无关结果）
    db_known = set(_load_holdings_from_db().keys())
    json_known = set(index.get("filings", {}).keys())
    known = db_known | json_known
    max_local_filing = index.get("max_filing_date", "") or _get_meta("max_filing_date", "")

    cutoff = (datetime.now() - pd.DateOffset(months=lookback_months)).strftime("%Y-%m-%d")

    # 选择 efts 查询的 startdt：日常用 cutoff，必要时往前 margin 避免漏掉迟报/修订
    search_start = cutoff
    if max_local_filing:
        try:
            ml = pd.to_datetime(max_local_filing)
            margin_start = (ml - pd.DateOffset(months=3)).strftime("%Y-%m-%d")
            search_start = min(cutoff, margin_start)
        except Exception:
            pass

    all_filings = fetch_iwb_filings_via_efts(start_date=search_start)

    recent_filings = [
        f for f in all_filings
        if f.get("filingDate", "") >= cutoff
        and (not max_local_filing or f.get("filingDate", "") > max_local_filing)
    ]
    # 按 filingDate 降序排序（最新在前），以便早停
    recent_filings.sort(key=lambda x: x.get("filingDate", ""), reverse=True)

    # 3. 顺序处理 + 早停：所有 efts 返回的均为 IWB 目标 series，无需再 probe
    new_target_filings = []
    for f in recent_filings:
        acc = f["accessionNumber"]
        if acc in known:
            # 遇到之前已读过的 filing，暂停继续读取更早的（假设列表已降序）
            if new_target_filings:
                print(f"[nport_data] 已处理 {len(new_target_filings)} 个新 filing，遇到已知 filing {acc}，暂停读取更早的。")
            else:
                print(f"[nport_data] 最近的 filing 已知，跳过本次检查。")
            break
        new_target_filings.append(f)

    if not new_target_filings and not force:
        now_iso = _now_iso()
        index["last_checked"] = now_iso
        _save_index(index)
        _set_meta("last_checked", now_iso)
        latest = max((f.get("reportPeriodDate", "") for f in index["filings"].values()), default="")
        return {
            "changed": False,
            "new_filings": 0,
            "latest_report_date": latest,
            "message": f"未发现新持仓申报，当前最新 {latest}",
        }

    print(f"[nport_data] 发现 {len(new_target_filings)} 个新的目标 ETF 持仓申报，开始下载解析...")

    # 4. 真正处理新 filing
    resolver = TickerResolver(
        ib_host=ib_host, ib_port=ib_port, ib_client_id=ib_client_id
    ) if auto_resolve_tickers else None
    newly_added = 0

    for f in new_target_filings:
        acc = f["accessionNumber"]
        print(f"  处理新申报 {acc} (filingDate={f['filingDate']}) ...")

        holdings = download_and_parse_full_xml(acc)
        if not holdings:
            continue

        # 补 filingDate
        for h in holdings:
            h["filingDate"] = f["filingDate"]

        # 自动补 ticker（使用重构后的统一解析器，会记录来源）
        if resolver:
            resolved = resolver.resolve_holdings(holdings)
            print(f"    本次自动解析 ticker: {resolved} 条（已记录来源到 holdings）")

        holdings_cache[acc] = holdings
        index["filings"][acc] = {
            "filingDate": f["filingDate"],
            "reportPeriodDate": holdings[0].get("reportPeriodDate", "") if holdings else "",
            "has_full_holdings": True,
            "holdings_count": len(holdings),
            "last_parsed": _now_iso(),
        }
        newly_added += 1
        time.sleep(REQUEST_DELAY)

    # 5. 保存 holdings 到 SQLite（主存储）
    if newly_added:
        _save_holdings_cache(holdings_cache)

    # 更新轻量元数据（同时写 index JSON + DB metadata）
    all_filing_dates = [f.get("filingDate", "") for f in index["filings"].values()]
    new_max_filing = max(all_filing_dates + [f.get("filingDate", "") for f in new_target_filings], default="")
    if new_max_filing:
        index["max_filing_date"] = max(index.get("max_filing_date", ""), new_max_filing)
        _set_meta("max_filing_date", index["max_filing_date"])

    now_iso = _now_iso()
    index["last_checked"] = now_iso
    _save_index(index)
    _set_meta("last_checked", now_iso)

    latest = max((f.get("reportPeriodDate", "") for f in index["filings"].values()), default="")

    return {
        "changed": True,
        "new_filings": newly_added,
        "latest_report_date": latest,
        "message": f"成功更新 {newly_added} 个新持仓申报，当前最新报告期 {latest}",
    }


# ==================== 对外查询接口（替代 nport_universe.py） ====================

def _standardize_ticker(t: str) -> str:
    return t.strip().upper().replace("/", ".")


def _extract_tickers(holdings: List[Dict]) -> List[str]:
    tickers = []
    seen = set()
    for h in holdings:
        t = h.get("ticker", "")
        if not t:
            continue
        t = _standardize_ticker(t)
        if t in seen or t in ("FUTURE", "NLOP-RTS"):
            continue
        seen.add(t)
        tickers.append(t)
    return tickers


def get_latest_universe() -> List[str]:
    """获取最新一期 Russell 1000 持仓的 ticker 列表（优先走 SQLite 高效路径）"""
    # 优先使用 DB 的高效查询
    latest = _get_latest_filing_from_db()
    if latest and latest.get("holdings"):
        return _extract_tickers(latest["holdings"])

    # 回退到旧缓存加载
    cache = _load_holdings_cache()
    if not cache:
        return []

    latest_report = ""
    latest_holdings = []
    for holdings in cache.values():
        if not holdings:
            continue
        report_date = holdings[0].get("reportPeriodDate", "")
        if report_date > latest_report:
            latest_report = report_date
            latest_holdings = holdings

    return _extract_tickers(latest_holdings)


def get_monthly_universes(start_month: str = "2020-01", end_month: str | None = None) -> Dict[str, List[str]]:
    """
    生成每月可用的 universe。
    规则：使用 reportPeriodDate <= 该月末 的最新一期持仓。
    优先使用 SQLite 数据。
    """
    cache = _load_holdings_cache()
    if not cache:
        return {}

    filings = []
    for holdings in cache.values():
        if not holdings:
            continue
        report_date = holdings[0].get("reportPeriodDate", "")
        if report_date:
            filings.append({
                "report_date": pd.to_datetime(report_date),
                "tickers": _extract_tickers(holdings),
            })

    filings = sorted(filings, key=lambda x: x["report_date"])

    if end_month is None:
        end_month = pd.Timestamp.now().strftime("%Y-%m")

    month_starts = pd.date_range(start=start_month + "-01", end=end_month + "-01", freq="MS")

    result: Dict[str, List[str]] = {}
    filing_idx = 0

    for month_start in month_starts:
        month_end = month_start + pd.offsets.MonthEnd(1)
        month_str = month_start.strftime("%Y-%m")

        while filing_idx < len(filings) - 1 and filings[filing_idx + 1]["report_date"] <= month_end:
            filing_idx += 1

        if filing_idx < len(filings) and filings[filing_idx]["report_date"] <= month_end:
            result[month_str] = filings[filing_idx]["tickers"].copy()

    return result


def get_status() -> Dict:
    """给用户看的当前数据状态"""
    index = _load_index()
    holdings = _load_holdings_cache()

    latest = ""
    total_holdings = 0
    for h in holdings.values():
        if h:
            d = h[0].get("reportPeriodDate", "")
            if d > latest:
                latest = d
            total_holdings += len(h)

    return {
        "last_checked": index.get("last_checked") or _get_meta("last_checked"),
        "known_filings": len(index.get("filings", {})),
        "latest_report_period": latest,
        "total_holdings_across_all_filings": total_holdings,
        "db_file": str(NPORT_DB),
        "index_file": str(NPORT_INDEX_FILE),
        "holdings_cache_file": str(NPORT_HOLDINGS_CACHE),
        "using_sqlite": NPORT_DB.exists(),
    }


if __name__ == "__main__":
    # 方便直接 python nport_data.py 测试
    print("=== NPORT Data Status ===")
    print(json.dumps(get_status(), indent=2, ensure_ascii=False))
    print("\n正在执行轻量同步检查...")
    result = sync_holdings_if_needed(max_age_hours=0)  # 强制检查一次
    print(result)
