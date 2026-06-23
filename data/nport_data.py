"""
NPORT Data Manager — 核心数据层

目标（严格对齐用户核心需求）：
- 每次运行 Russell 命令时自动轻量检查持仓是否有更新
- 只有真正有变化时才下载 primary_doc.xml + 解析 + 补 ticker
- 价格数据已有优秀增量逻辑（data_fetcher），这里专注 NPORT 持仓
- 对外提供干净的 get_latest_universe() / get_monthly_universes()

获取 filing 列表已优化为使用 SEC efts 全文搜索（q=seriesId & forms=NPORT-P），
直接命中 IWB，无需拉取 CIK 下全部 NPORT-P 再 probe 过滤。

本地持久化：统一 SQLite（cache/stock_picker.db）
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from config import (
    NPORT_BOOTSTRAP_START_DATE as BOOTSTRAP_START_DATE,
    SEC_CIK as CIK,
    SEC_REQUEST_DELAY as REQUEST_DELAY,
    SEC_SERIES_ID as SERIES_ID,
)
from utils.logconf import get_logger, setup_logging
from utils.progress import ProgressBar
from .storage import (
    APP_DB,
    init_db,
    meta_get,
    meta_set,
    nport_filing_count,
    nport_get_latest_filing,
    nport_known_accessions,
    nport_load_all_holdings,
    nport_load_efts_cache,
    nport_save_efts_cache,
    nport_save_filing,
)
from .ticker_resolver import TickerResolver

logger = get_logger(__name__)

# ==================== 配置 ====================

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache"
NPORT_XML_DIR = ROOT_DIR / "nport_xmls"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 (contact: user@example.com)",
    "Accept-Encoding": "gzip, deflate",
}

# NPORT assetCat：EC = Equity Common（普通股），仅保留此类资产
EQUITY_COMMON_ASSET_CAT = "EC"


def _is_equity_common(holding: Dict) -> bool:
    return holding.get("assetCat") == EQUITY_COMMON_ASSET_CAT


def _filter_ec_holdings(holdings: List[Dict]) -> List[Dict]:
    return [h for h in holdings if _is_equity_common(h)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_meta(key: str, default: str | None = None) -> str | None:
    init_db()
    return meta_get(key, default)


def _set_meta(key: str, value: str) -> None:
    init_db()
    meta_set(key, value)


def _load_holdings_cache() -> Dict[str, List[Dict]]:
    init_db()
    return nport_load_all_holdings()


def _save_filing(accession: str, filing_date: str, holdings: List[Dict]) -> None:
    holdings = _filter_ec_holdings(holdings)
    if not holdings:
        return
    report_period = holdings[0].get("reportPeriodDate", "")
    nport_save_filing(accession, filing_date, report_period, holdings)


# ==================== SEC 元数据获取（轻量）——改用 efts 全文搜索直接按 series 命中 ====================

def fetch_iwb_filings_via_efts(start_date: str = BOOTSTRAP_START_DATE, end_date: str | None = None) -> List[Dict]:
    """通过 SEC 全文搜索直接定位 IWB (series=S000004347) 的 NPORT-P 申报。"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    want_full = start_date <= BOOTSTRAP_START_DATE and end_date >= datetime.now().strftime("%Y-%m-%d")
    if want_full:
        cached_filings, _ = nport_load_efts_cache()
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
        url = base_url if from_idx == 0 else f"{base_url}&from={from_idx}&size={size}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("efts search 请求失败: %s", e)
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
        time.sleep(REQUEST_DELAY)

    seen = set()
    unique = []
    for f in sorted(filings, key=lambda x: x.get("filingDate") or ""):
        if f["accessionNumber"] and f["accessionNumber"] not in seen:
            seen.add(f["accessionNumber"])
            unique.append(f)

    if want_full:
        nport_save_efts_cache(unique)

    logger.info("全文搜索完成：命中 %d 个 IWB 申报 (range %s..%s)", len(unique), start_date, end_date)
    return unique


def fetch_nport_filings_metadata() -> List[Dict]:
    return fetch_iwb_filings_via_efts(start_date=BOOTSTRAP_START_DATE)


def download_and_parse_full_xml(accession: str) -> Optional[List[Dict]]:
    """下载完整 XML 并解析持仓（仅 EC 普通股）。"""
    from lxml import etree

    acc_nodash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc_nodash}/primary_doc.xml"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        root = etree.fromstring(resp.content)

        NPORT_XML_DIR.mkdir(exist_ok=True)
        xml_path = NPORT_XML_DIR / f"{accession}_primary_doc.xml"
        xml_path.write_bytes(resp.content)

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
                "filingDate": "",
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
            if _is_equity_common(row):
                holdings.append(row)
        return holdings

    except Exception as e:
        logger.warning("下载/解析 %s 失败: %s", accession, e)
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
    ib_num_connections: int = 4,
) -> Dict:
    init_db()

    is_bootstrap = nport_filing_count() == 0
    last_checked = _get_meta("last_checked")
    max_local_filing = _get_meta("max_filing_date", "")

    if not force and not is_bootstrap:
        today = datetime.now().strftime("%Y-%m-%d")
        if _get_meta("last_sec_check_date") == today:
            latest = nport_get_latest_filing()
            latest_date = latest["report_period_date"] if latest else ""
            return {
                "changed": False,
                "new_filings": 0,
                "latest_report_date": latest_date,
                "message": f"今天({today})已查询过SEC，直接跳过（使用本地缓存）",
            }

    if not force and not is_bootstrap and last_checked and max_age_hours > 0:
        try:
            last = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if age_hours < max_age_hours:
                latest = nport_get_latest_filing()
                latest_date = latest["report_period_date"] if latest else ""
                return {
                    "changed": False,
                    "new_filings": 0,
                    "latest_report_date": latest_date,
                    "message": f"持仓已是最新（{latest_date}），{age_hours:.1f}h 内已检查过（DB）",
                }
        except Exception as e:
            logger.warning("解析 last_checked 时间戳失败（将继续检查）: %s", e)

    if not is_bootstrap:
        _set_meta("last_sec_check_date", datetime.now().strftime("%Y-%m-%d"))
    logger.info("正在轻量检查 SEC 最新 NPORT-P 申报...")

    known = nport_known_accessions()
    max_local_filing = _get_meta("max_filing_date", "") if not is_bootstrap else ""

    if is_bootstrap:
        logger.info("本地无 NPORT 数据，开始全量 bootstrap（2019 年起）...")
        search_start = BOOTSTRAP_START_DATE
        all_filings = fetch_iwb_filings_via_efts(start_date=search_start)
        new_target_filings = [f for f in all_filings if f["accessionNumber"] not in known]
        new_target_filings.sort(key=lambda x: x.get("filingDate", ""))
    else:
        cutoff = (datetime.now() - pd.DateOffset(months=lookback_months)).strftime("%Y-%m-%d")
        search_start = cutoff
        if max_local_filing:
            try:
                ml = pd.to_datetime(max_local_filing)
                margin_start = (ml - pd.DateOffset(months=3)).strftime("%Y-%m-%d")
                search_start = min(cutoff, margin_start)
            except Exception as e:
                logger.warning("解析 max_filing_date=%s 失败，使用默认 cutoff: %s", max_local_filing, e)

        all_filings = fetch_iwb_filings_via_efts(start_date=search_start)
        recent_filings = [
            f for f in all_filings
            if f.get("filingDate", "") >= cutoff
            and (not max_local_filing or f.get("filingDate", "") > max_local_filing)
        ]
        recent_filings.sort(key=lambda x: x.get("filingDate", ""), reverse=True)

        new_target_filings = []
        for f in recent_filings:
            acc = f["accessionNumber"]
            if acc in known:
                if new_target_filings:
                    logger.info("已处理 %d 个新 filing，遇到已知 filing %s，暂停读取更早的。", len(new_target_filings), acc)
                else:
                    logger.info("最近的 filing 已知，跳过本次检查。")
                break
            new_target_filings.append(f)

    if not new_target_filings and not force:
        now_iso = _now_iso()
        _set_meta("last_checked", now_iso)
        latest = nport_get_latest_filing()
        latest_date = latest["report_period_date"] if latest else ""
        return {
            "changed": False,
            "new_filings": 0,
            "latest_report_date": latest_date,
            "message": f"未发现新持仓申报，当前最新 {latest_date}",
        }

    logger.info("发现 %d 个新申报，开始下载解析...", len(new_target_filings))

    resolver = TickerResolver(
        ib_host=ib_host, ib_port=ib_port, ib_client_id=ib_client_id,
        num_connections=ib_num_connections,
    ) if auto_resolve_tickers else None
    newly_added = 0
    new_filing_dates: List[str] = []

    try:
        with ProgressBar(len(new_target_filings), "NPORT 申报", unit="个") as bar:
            for f in new_target_filings:
                acc = f["accessionNumber"]

                holdings = download_and_parse_full_xml(acc)
                if not holdings:
                    bar.update(1)
                    continue

                for h in holdings:
                    h["filingDate"] = f["filingDate"]

                if resolver:
                    resolver.resolve_holdings(holdings)

                _save_filing(acc, f["filingDate"], holdings)
                newly_added += 1
                new_filing_dates.append(f["filingDate"])
                time.sleep(REQUEST_DELAY)
                bar.update(1, 已入库=newly_added)
    finally:
        if resolver:
            resolver.close()

    if new_filing_dates:
        new_max = max(new_filing_dates)
        if new_max > (max_local_filing or ""):
            _set_meta("max_filing_date", new_max)

    now_iso = _now_iso()
    _set_meta("last_checked", now_iso)
    _set_meta("last_sec_check_date", datetime.now().strftime("%Y-%m-%d"))
    latest = nport_get_latest_filing()
    latest_date = latest["report_period_date"] if latest else ""

    return {
        "changed": newly_added > 0,
        "new_filings": newly_added,
        "latest_report_date": latest_date,
        "message": f"成功更新 {newly_added} 个新持仓申报，当前最新报告期 {latest_date}",
    }


# ==================== 对外查询接口 ====================

def _standardize_ticker(t: str) -> str:
    return t.strip().upper().replace("/", ".")


def _extract_tickers(holdings: List[Dict]) -> List[str]:
    holdings = _filter_ec_holdings(holdings)
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
    latest = nport_get_latest_filing()
    if latest and latest.get("holdings"):
        return _extract_tickers(latest["holdings"])
    return []


def get_monthly_universes(start_month: str = "2019-12", end_month: str | None = None) -> Dict[str, List[str]]:
    cache = _load_holdings_cache()
    if not cache:
        return {}

    filings = []
    for holdings in cache.values():
        if not holdings:
            continue
        report_date = holdings[0].get("reportPeriodDate", "")
        filing_date = holdings[0].get("filingDate", "")
        if report_date and filing_date:
            filings.append({
                "report_date": pd.to_datetime(report_date),
                "filing_date": pd.to_datetime(filing_date),
                "tickers": _extract_tickers(holdings),
            })
        elif report_date:
            rd = pd.to_datetime(report_date)
            filings.append({
                "report_date": rd,
                "filing_date": rd + pd.Timedelta(days=60),
                "tickers": _extract_tickers(holdings),
            })

    filings = sorted(filings, key=lambda x: x["report_date"])

    if end_month is None:
        end_month = pd.Timestamp.now().strftime("%Y-%m")

    month_starts = pd.date_range(start=start_month + "-01", end=end_month + "-01", freq="MS")
    result: Dict[str, List[str]] = {}

    for month_start in month_starts:
        month_str = month_start.strftime("%Y-%m")
        available = [f for f in filings if f["filing_date"] < month_start]
        if available:
            best = max(available, key=lambda x: x["report_date"])
            result[month_str] = best["tickers"].copy()

    return result


def get_status() -> Dict:
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
        "last_checked": _get_meta("last_checked"),
        "known_filings": nport_filing_count(),
        "latest_report_period": latest,
        "total_holdings_across_all_filings": total_holdings,
        "db_file": str(APP_DB),
    }


if __name__ == "__main__":
    setup_logging()
    print("=== NPORT Data Status ===")
    print(json.dumps(get_status(), indent=2, ensure_ascii=False))
    print("\n正在执行轻量同步检查...")
    result = sync_holdings_if_needed(max_age_hours=0)
    print(result)
