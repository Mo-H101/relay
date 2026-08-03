"""
Availability scanning for the setup wizard.

``ScanEngine`` probes a provider's catalog with a bounded thread pool and
reports completion through a single ``on_update`` callback fired on the
calling thread, so UI updates stay single-threaded. Results come back in
catalog order regardless of probe completion order.

P3 defines the async provider probe (``aprobe_model``); the wizard depends
only on ``ScanEngine.scan`` and ``on_update``, and swapping the executor
body for asyncio behind this same interface remains a future seam that
won't touch the wizard or the reporters.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
from typing import Callable, List, Optional

from app.providers.availability import UNAVAILABLE, classify_probe


def _default_concurrency() -> int:
    raw = os.getenv("SETUP_SCAN_CONCURRENCY", "8")
    try:
        value = int(raw)
    except ValueError:
        value = 8
    return max(1, value)


@dataclass(frozen=True)
class ScanResult:
    """
    Probe outcome for one model.
    """

    model: str
    status: str  # available | overloaded | unavailable
    latency_ms: int = 0
    status_code: Optional[int] = None
    error: str = ""


class ScanEngine:
    """
    Concurrent availability scanner with ordered results.
    """

    def __init__(self, concurrency: Optional[int] = None) -> None:
        self.concurrency = (
            concurrency if concurrency is not None else _default_concurrency()
        )

    def scan(
        self,
        client,
        provider,
        models: List[str],
        on_update: Optional[Callable[[int, int, ScanResult], None]] = None,
    ) -> List[ScanResult]:
        """
        Probe ``models``, returning one ``ScanResult`` per model in order.

        ``on_update(done, total, result)`` is called once per completed
        probe from the calling thread.
        """
        results: List[ScanResult] = [None] * len(models)  # type: ignore[list-item]

        def probe(index: int, model: str):
            try:
                probe = client.probe_model(provider, model)
                return index, ScanResult(
                    model=model,
                    status=classify_probe(probe),
                    latency_ms=probe.latency_ms,
                    status_code=probe.status_code,
                    error=probe.error,
                )
            except Exception as exc:  # noqa: BLE001 - a probe never throws
                return index, ScanResult(
                    model=model,
                    status=UNAVAILABLE,
                    latency_ms=0,
                    status_code=None,
                    error=str(exc),
                )

        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = [
                executor.submit(probe, index, model)
                for index, model in enumerate(models)
            ]

            done = 0
            for future in as_completed(futures):
                index, result = future.result()
                results[index] = result
                done += 1
                if on_update is not None:
                    on_update(done, len(models), result)

        return results
