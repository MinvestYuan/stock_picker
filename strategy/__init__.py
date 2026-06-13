"""
Part 2: 选股策略实现 (Stock Selection Strategy)

- stock_selector.py: 4-1 Momentum + RS(RRG) + 固定 6:4 加权（Momentum 60% + RRG 40%），无 EMA50 硬过滤；backtest 含 QQQ 50/200 MA 熊市保护
"""
from . import stock_selector
