"""重新运行 NPORT 月度回测并生成 index.html。

报告中的买入/卖出价来自回测引擎（backtest/tester.py）：
  - 买入：当月首个交易日开盘价
  - 卖出：下月首个交易日开盘价

需本地 IB 价格缓存；与 main.py dashboard 等价。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import cmd_dashboard, parse_args  # noqa: E402


def main() -> int:
    args = parse_args()
    args.command = "dashboard"
    args.output = ROOT / "index.html"
    return cmd_dashboard(args)


if __name__ == "__main__":
    raise SystemExit(main())