"""Timing harness and latency statistics shared by every benchmark scenario.

The methodology (see ``references/benchmarking.md`` in the
``aerospike-backend-integration`` skill) requires: a monotonic wall-clock timer
wrapped tightly around the operation call itself, a discarded warmup period,
and at least 100 timed samples per (backend, operation, scale) cell.
"""

from __future__ import annotations

import shutil
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class Samples:
    """Raw latency samples for one (backend, operation, scale) cell."""

    interface: str
    operation: str
    backend: str
    scale: int
    latencies_s: list[float] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, float]:
        """Return mean/median/p95/p99/throughput/n computed from the raw samples."""
        if not self.latencies_s:
            return {"mean_ms": 0.0, "median_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "throughput_ops_s": 0.0, "n": 0}
        sorted_samples = sorted(self.latencies_s)
        mean_s = statistics.mean(sorted_samples)
        return {
            "mean_ms": mean_s * 1000,
            "median_ms": statistics.median(sorted_samples) * 1000,
            "p95_ms": _percentile(sorted_samples, 0.95) * 1000,
            "p99_ms": _percentile(sorted_samples, 0.99) * 1000,
            "throughput_ops_s": (1 / mean_s) if mean_s else 0.0,
            "n": len(sorted_samples),
        }


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile over an already-sorted sequence."""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    lower = int(k)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (k - lower)


def time_sync(fn: Callable[[int], None], *, reps: int, warmup: int) -> list[float]:
    """Time ``reps`` calls to a synchronous, per-iteration-indexed operation.

    ``fn`` receives the iteration index (including warmup iterations, which run
    first with indices ``0..warmup-1`` before timed indices ``warmup..warmup+reps-1``)
    so callers can vary the argument per call (e.g. a distinct key per write)
    without that bookkeeping leaking into the timed section.
    """
    for i in range(warmup):
        fn(i)
    samples: list[float] = []
    for i in range(warmup, warmup + reps):
        start = time.perf_counter()
        fn(i)
        samples.append(time.perf_counter() - start)
    return samples


async def time_async(fn: Callable[[int], Awaitable[None]], *, reps: int, warmup: int) -> list[float]:
    """Async counterpart to :func:`time_sync`."""
    for i in range(warmup):
        await fn(i)
    samples: list[float] = []
    for i in range(warmup, warmup + reps):
        start = time.perf_counter()
        await fn(i)
        samples.append(time.perf_counter() - start)
    return samples


class _ProgressBar:
    r"""An in-place terminal progress bar for a long seeding or timing loop.

    On a tty, redraws one line in place via ``\r``. Off a tty (output piped/redirected),
    in-place redraws aren't visible, so it falls back to one line per 10% instead of
    spamming a line per update.
    """

    def __init__(self, total: int, prefix: str, width: int = 24) -> None:
        self.total = max(total, 1)
        self.prefix = prefix
        self.width = width
        self.count = 0
        self._is_tty = sys.stdout.isatty()
        self._last_decile = -1

    def update(self, n: int = 1) -> None:
        self.count = min(self.count + n, self.total)
        pct = int(self.count * 100 / self.total)
        if self._is_tty:
            filled = int(self.width * self.count / self.total)
            bar = "#" * filled + "-" * (self.width - filled)
            line = f"{self.prefix} [{bar}] {self.count}/{self.total} ({pct}%)"
            columns = shutil.get_terminal_size(fallback=(100, 24)).columns
            print("\r" + line[: columns - 1].ljust(columns - 1), end="", flush=True)
        else:
            decile = pct // 10
            if decile != self._last_decile:
                self._last_decile = decile
                print(f"{self.prefix} {pct}% ({self.count}/{self.total})", flush=True)

    def clear(self) -> None:
        """Erase the in-place bar so the next print starts on a clean line. No-op off a tty."""
        if self._is_tty:
            columns = shutil.get_terminal_size(fallback=(100, 24)).columns
            print("\r" + " " * (columns - 1) + "\r", end="", flush=True)
