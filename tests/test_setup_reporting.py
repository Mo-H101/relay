"""
Progress and summary reporting (P1).
"""

from app.providers.availability import AVAILABLE, OVERLOADED, UNAVAILABLE
from app.setup.reporting import (
    PlainProgressReporter,
    RecordingReporter,
    detail_order,
    result_line,
    summarize,
)
from app.setup.scan import ScanResult


def make_result(model, status, latency=10, error=""):
    return ScanResult(model=model, status=status, latency_ms=latency, error=error)


def test_summarize_math_with_overload():
    results = [
        make_result("a", AVAILABLE),
        make_result("b", OVERLOADED),
        make_result("c", OVERLOADED),
        make_result("d", UNAVAILABLE),
    ]

    summary = summarize(results)

    assert summary.total == 4
    assert summary.available == 3  # overloaded counts as available
    assert summary.unavailable == 1
    assert summary.overloaded == 2


def test_result_line_glyph_mapping():
    assert result_line(make_result("a", AVAILABLE)).startswith("\u2713")
    assert result_line(make_result("b", OVERLOADED)).startswith("\u26a0")
    assert result_line(make_result("c", UNAVAILABLE)).startswith("\u2717")


def test_detail_order_puts_unavailable_last():
    results = [
        make_result("a", UNAVAILABLE),
        make_result("b", AVAILABLE),
        make_result("c", OVERLOADED),
        make_result("d", UNAVAILABLE),
    ]

    ordered = detail_order(results)

    assert [r.model for r in ordered] == ["b", "c", "a", "d"]


def test_recording_reporter_transcript():
    reporter = RecordingReporter("p")
    results = [make_result("a", AVAILABLE), make_result("b", UNAVAILABLE)]

    reporter.begin_scan(2)
    reporter.update(1, 2, "a", [results[0]])
    reporter.update(2, 2, "b", results)
    reporter.end_scan(results)
    reporter.detail(results)

    begins = [t for t in reporter.transcript if t[0] == "begin"]
    assert len(begins) == 1
    assert begins[0] == ("begin", 2)

    updates = [t for t in reporter.transcript if t[0] == "update"]
    assert [u[1] for u in updates] == [1, 2]

    assert reporter.summary.total == 2


def test_recording_reporter_recent_window_bounded():
    reporter = RecordingReporter()
    results = [make_result(f"m{i}", AVAILABLE) for i in range(10)]

    for i in range(1, 11):
        reporter.update(i, 10, f"m{i - 1}", results[:i])

    assert len(reporter.recent) <= 10


def test_plain_reporter_lines_and_no_default_dump():
    reporter = PlainProgressReporter("p")
    results = [make_result("a", AVAILABLE), make_result("b", UNAVAILABLE)]

    reporter.begin_scan(2)
    reporter.update(1, 2, "a", [results[0]])
    reporter.update(2, 2, "b", results)
    reporter.end_scan(results)

    lines = "\n".join(reporter.lines)
    assert "Testing availability (p): 2 models" in lines
    assert "Total models: 2" in lines
    assert "Available: 1" in lines
    assert "Unavailable: 1" in lines
    assert "m0" not in lines  # no model dump unless detail() called


def test_plain_reporter_detail_orders_models():
    reporter = PlainProgressReporter()
    results = [
        make_result("bad", UNAVAILABLE),
        make_result("good", AVAILABLE),
        make_result("warn", OVERLOADED),
    ]

    reporter.detail(results)

    bad_index = next(i for i, line in enumerate(reporter.lines) if "bad" in line)
    good_index = next(i for i, line in enumerate(reporter.lines) if "good" in line)
    warn_index = next(i for i, line in enumerate(reporter.lines) if "warn" in line)

    assert good_index < warn_index < bad_index


def test_rich_reporter_full_scan_cycle(capsys):
    """
    RichProgressReporter runs a full scan cycle. Rich is a transitive
    dependency of httpx, so it is already loaded; this pins the reporter's
    own behaviour (summary math) end to end without a TTY.
    """
    from app.setup.reporting import RichProgressReporter

    reporter = RichProgressReporter("p")
    results = [make_result("a", AVAILABLE), make_result("b", UNAVAILABLE)]

    reporter.begin_scan(2)
    reporter.update(1, 2, "a", [results[0]])
    reporter.update(2, 2, "b", results)
    reporter.end_scan(results)
    reporter.detail(results)

    assert reporter.summary.total == 2
    assert reporter.summary.available == 1
    assert reporter.summary.unavailable == 1
