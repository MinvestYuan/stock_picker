"""统一日志配置。

所有模块用 ``logging.getLogger(__name__)`` 取 logger；入口（main.py、
各脚本的 ``__main__``）调用 :func:`setup_logging` 一次即可。日志写入
stderr，与进度条保持同一通道，不干扰 stdout。
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger（幂等，多次调用只生效一次）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # 屏蔽 ib_async 的 position/portfolio 等 INFO 日志
    logging.getLogger("ib_async").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
