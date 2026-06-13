"""
从 NPORT 缓存构建 Russell 1000 的 universe
- 最新持仓列表
- 历史每月持仓列表（基于 reportPeriodDate）

注意：新代码推荐直接从 nport_data 导入。
本文件保留为向后兼容薄封装。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Set

# 新架构：所有真实逻辑在 nport_data.py
from .nport_data import (
    get_latest_universe as _get_latest_universe,
    get_monthly_universes as _get_monthly_universes,
)

# 保留旧常量以免旧代码报错
ROOT_DIR = Path(__file__).resolve().parent.parent
NPORT_CACHE_FILE = ROOT_DIR / "cache" / "nport_holdings_cache.json"


def load_nport_holdings(cache_file: Path = NPORT_CACHE_FILE) -> Dict[str, List[dict]]:
    """加载 NPORT holdings 缓存"""
    if not cache_file.exists():
        return {}
    with open(cache_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _standardize_ticker(t: str) -> str:
    """标准化 ticker：IB 使用 '.' 而非 '/' 分隔类别股"""
    return t.strip().upper().replace("/", ".")


def _extract_tickers(holdings: List[dict]) -> List[str]:
    """从持仓列表中提取有效的股票 ticker（排除期货、权证、空值等）"""
    tickers = []
    seen = set()
    for h in holdings:
        t = h.get("ticker", "")
        if not t:
            continue
        t = _standardize_ticker(t)
        if t in seen:
            continue
        if t in ("FUTURE", "NLOP-RTS"):
            continue
        seen.add(t)
        tickers.append(t)
    return tickers


def get_latest_universe(cache_file: Path = NPORT_CACHE_FILE) -> List[str]:
    """获取最新的 Russell 1000 universe（按 reportPeriodDate 最新的 filing）"""
    return _get_latest_universe()


def get_filing_info(cache_file: Path = NPORT_CACHE_FILE) -> pd.DataFrame:
    """返回所有 filing 的基本信息 DataFrame"""
    cache = load_nport_holdings(cache_file)
    rows = []
    for acc, holdings in cache.items():
        if not holdings:
            continue
        rows.append({
            "accession": acc,
            "reportPeriodDate": holdings[0].get("reportPeriodDate", ""),
            "filingDate": holdings[0].get("filingDate", ""),
            "signatureDate": holdings[0].get("signatureDate", ""),
            "holdings_count": len(holdings),
        })
    df = pd.DataFrame(rows)
    df["reportPeriodDate"] = pd.to_datetime(df["reportPeriodDate"], errors="coerce")
    df["filingDate"] = pd.to_datetime(df["filingDate"], errors="coerce")
    return df.sort_values("reportPeriodDate").reset_index(drop=True)


def get_monthly_universes(
    start_month: str = "2020-01",
    end_month: str | None = None,
    cache_file: Path = NPORT_CACHE_FILE,
) -> Dict[str, List[str]]:
    """
    生成每个月的 universe 字典。
    对于每个月，使用最近一个 reportPeriodDate <= 该月末的 filing 的持仓。
    
    返回: {month_str (YYYY-MM): [ticker_list]}
    """
    return _get_monthly_universes(start_month=start_month, end_month=end_month)


def get_all_nport_tickers(cache_file: Path = NPORT_CACHE_FILE) -> Set[str]:
    """获取所有 NPORT filing 中出现过的所有 ticker（去重）"""
    cache = load_nport_holdings(cache_file)
    all_tickers: Set[str] = set()
    for holdings in cache.values():
        for h in holdings:
            t = h.get("ticker", "")
            if t and t not in ("FUTURE", "NLOP-RTS"):
                all_tickers.add(t)
    return all_tickers
