"""Thread-safe ASCII progress bars on stderr."""

from __future__ import annotations

import sys
import threading
from typing import Iterator, TypeVar

T = TypeVar("T")


class ProgressBar:
    """Thread-safe progress bar; safe to update from parallel IB workers."""

    def __init__(self, total: int, desc: str, unit: str = "it", leave: bool = True):
        self._total = max(total, 0)
        self._desc = desc
        self._lock = threading.Lock()
        self._closed = False
        self._current = 0
        self._postfix: dict = {}

    def update(self, n: int = 1, **postfix) -> None:
        with self._lock:
            if self._closed:
                return
            self._current += n
            if postfix:
                self._postfix.update(postfix)
            if self._total > 0:
                self._render()

    def set_postfix(self, **kwargs) -> None:
        with self._lock:
            if self._closed:
                return
            self._postfix.update(kwargs)
            if self._total > 0:
                self._render()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._current > 0:
                sys.stderr.write("\n")
                sys.stderr.flush()

    def _render(self) -> None:
        width = 36
        tot = self._total or 1
        pct = min(1.0, self._current / tot)
        filled = int(width * pct)
        bar = "#" * filled + "-" * (width - filled)
        extra = ""
        if self._postfix:
            extra = " " + " ".join(f"{k}={v}" for k, v in self._postfix.items())
        sys.stderr.write(f"\r{self._desc} [{bar}] {self._current}/{tot}{extra}  ")
        sys.stderr.flush()

    def __enter__(self) -> ProgressBar:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def iter_progress(
    items: Iterator[T] | list[T],
    desc: str,
    unit: str = "it",
    total: int | None = None,
) -> Iterator[T]:
    if total is None:
        try:
            total = len(items)  # type: ignore[arg-type]
        except TypeError:
            total = None

    if not total:
        yield from items
        return

    with ProgressBar(total, desc, unit=unit) as bar:
        for item in items:
            yield item
            bar.update(1)
