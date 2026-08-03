"""
Progress and summary reporting for provider availability scans.

Two concrete reporters plus a recording one for tests:

- ``RichProgressReporter`` — one global progress bar per provider scan with
  the current model rendered beneath it and a rolling list of the most
  recently tested models. Used only on a TTY.
- ``PlainProgressReporter`` — deterministic line output, no ANSI, for
  non-TTY runs and CI.
- ``RecordingReporter`` — captures every callback for assertions.

Rich is used only here (the per-scan progress bar and result rendering),
and is imported at module level. Note it is a transitive dependency of
``httpx`` (``click`` -> ``pygments`` -> ``rich``), so it is loaded by the
CLI and server regardless of this module.
"""

from dataclasses import dataclass
from typing import List, Protocol

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn

from app.providers.availability import AVAILABLE, GLYPH, OVERLOADED, UNAVAILABLE

ROLLING_WINDOW = 5


@dataclass(frozen=True)
class ScanSummary:
    """
    Bucketed totals. Overloaded models are usable but flagged, so they
    count toward ``available``; ``overloaded`` is reported separately.
    """

    total: int
    available: int
    unavailable: int
    overloaded: int


def summarize(results) -> ScanSummary:
    total = len(results)
    overloaded = sum(1 for result in results if result.status == OVERLOADED)
    unavailable = sum(1 for result in results if result.status != OVERLOADED and not _available(result))
    available = total - unavailable
    return ScanSummary(total, available, unavailable, overloaded)


def _available(result) -> bool:
    return result.status == AVAILABLE


def result_line(result) -> str:
    return f"{GLYPH.get(result.status, '?')} {result.model}"


def detail_order(results):
    """
    Available and overloaded first (in probe order), unavailable last —
    the spec's ``✓/⚠`` list before ``✗``.
    """
    ok = [result for result in results if result.status != UNAVAILABLE]
    bad = [result for result in results if result.status == UNAVAILABLE]
    return ok + bad


class ProgressReporter(Protocol):
    def begin_scan(self, total: int) -> None: ...

    def update(
        self,
        done: int,
        total: int,
        current: str,
        recent: List[tuple],
    ) -> None: ...

    def end_scan(self, results) -> None: ...

    def detail(self, results) -> None: ...


class RecordingReporter:
    """
    Test reporter: records the full callback transcript and the rolling
    window without rendering anything.
    """

    def __init__(self, title: str = "scan") -> None:
        self.title = title
        self.transcript = []
        self.recent = []
        self.summary = None

    def begin_scan(self, total: int) -> None:
        self.transcript.append(("begin", total))

    def update(self, done, total, current, recent) -> None:
        self.transcript.append(("update", done, total, current))
        self.recent = recent

    def end_scan(self, results) -> None:
        self.summary = summarize(results)
        self.transcript.append(("end", len(results)))

    def detail(self, results) -> None:
        self.transcript.append(("detail", len(results)))


class PlainProgressReporter:
    """
    Non-TTY reporter: a few progress lines and the final totals, no ANSI.
    """

    def __init__(self, title: str = "scan") -> None:
        self.title = title
        self.lines: List[str] = []
        self.total = 0
        self.summary = None

    def begin_scan(self, total: int) -> None:
        self.total = total
        self.lines.append(
            f"Testing availability ({self.title}): {total} models"
        )

    def update(self, done, total, current, recent) -> None:
        if done == total or done % 25 == 0:
            percent = int(done * 100 / total) if total else 100
            self.lines.append(f"  {done}/{total} ({percent}%) {current}")

    def end_scan(self, results) -> None:
        self.summary = summarize(results)
        self.lines.append(f"Total models: {self.summary.total}")
        self.lines.append(f"Available: {self.summary.available}")
        self.lines.append(f"Unavailable: {self.summary.unavailable}")
        if self.summary.overloaded:
            self.lines.append(f"Overloaded: {self.summary.overloaded}")

    def detail(self, results) -> None:
        for result in detail_order(results):
            self.lines.append(f"  {result_line(result)}")


class RichProgressReporter:
    """
    TTY reporter: one global progress bar with the current model name and
    a rolling list of recently tested models rendered beneath it.
    """

    def __init__(self, title: str = "scan") -> None:
        self.title = title
        self.console = None
        self.progress = None
        self._live = None
        self._recent: List[tuple] = []

    def _ensure(self):
        if self.progress is None:
            self.console = Console()
            self.progress = Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                TextColumn("{task.percentage:>3.0f}%"),
                console=self.console,
            )

    def begin_scan(self, total: int) -> None:
        self._ensure()
        self._task_id = self.progress.add_task(
            f"Testing availability ({self.title})",
            total=total,
        )
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def update(self, done, total, current, recent) -> None:
        self._ensure()
        self._recent = recent
        self.progress.update(
            self._task_id,
            completed=done,
            description=f"Testing: {current}",
        )
        if self._live is not None:
            self._live.update(self._render())

    def end_scan(self, results) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self.summary = summarize(results)
        self.console.print()
        self.console.print("Results:")
        self.console.print(f"  Total models: {self.summary.total}")
        self.console.print(f"  Available: {self.summary.available}")
        self.console.print(f"  Unavailable: {self.summary.unavailable}")
        if self.summary.overloaded:
            self.console.print(f"  Overloaded: {self.summary.overloaded}")

    def detail(self, results) -> None:
        for result in detail_order(results):
            self.console.print(f"  {result_line(result)}")

    def _render(self):
        recent_lines = [
            f"  {result_line(result)}"
            for result in reversed(self._recent)
        ] or ["  (no results yet)"]

        panel = Panel(
            "\n".join(recent_lines),
            title="Recently tested",
            border_style="dim",
            padding=(0, 1),
        )

        return Group(self.progress, panel)
