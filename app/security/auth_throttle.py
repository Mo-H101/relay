"""
Authentication abuse throttling (CPU-amplification guard).

``KeyStore.authenticate`` derives scrypt digests for every stored key on
every request that misses the bootstrap key, so a flood of bad tokens
from one address costs O(keys x scrypt) CPU per attempt. This module
bounds that work two ways:

* ``AuthThrottle`` counts failed store authentications per client bucket
  (SHA-256 of the direct peer address) inside a rolling window. Once a
  bucket exceeds the failure limit, further attempts are rejected before
  the KeyStore is consulted until the window expires. Buckets are capped
  with an LRU bound so memory cannot grow with spoofed traffic, and the
  client address itself is never retained -- only its digest. A throttled
  rejection does not extend the lockout, so the window always expires a
  bounded time after the last counted failure and legitimate clients
  sharing an egress address recover automatically.

* ``AuthGate`` holds a process-wide semaphore bounding how many store
  authentications run concurrently. Excess requests fail closed
  immediately instead of queueing unbounded scrypt work. The semaphore
  is rebuilt under the lock when the configured concurrency changes so
  live-reloaded settings take effect without a restart.

Both helpers never raise and never touch I/O: a throttle fault must
degrade to pass-through rather than break authentication.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict

# Maximum tracked client buckets. Eviction is LRU; at typical request
# rates a full table means sustained multi-address abuse, which the
# global semaphore still bounds.
_MAX_BUCKETS = 4096

# Seconds an ``AuthGate.acquire`` caller waits for a slot before the
# request fails closed. Generous relative to one scrypt pass so bursts of
# legitimate traffic queue briefly instead of being rejected.
_GATE_TIMEOUT_SECONDS = 0.5


class AuthThrottle:
    """
    Per-client-bucket failure throttle for store-backed authentication.

    The failure limit and window are passed per call so live-reloaded
    settings apply immediately. All state lives behind one lock; buckets
    older than the window are pruned opportunistically on access.
    """

    def __init__(self, max_buckets: int = _MAX_BUCKETS) -> None:
        self._lock = threading.Lock()
        # bucket digest -> [failure_count, window_start]
        self._buckets: OrderedDict = OrderedDict()
        self._max_buckets = max(1, int(max_buckets))

    @staticmethod
    def bucket_for(client_host) -> str:
        """
        Digest the direct peer address into a stable bucket key. The raw
        address is never stored or logged.
        """
        host = client_host if isinstance(client_host, str) else ""
        return hashlib.sha256(host.encode("utf-8", "replace")).hexdigest()

    def check(self, bucket: str, limit: int, window_seconds: float) -> bool:
        """
        True when this bucket has exhausted its failure budget inside the
        active window and must be rejected without consulting the store.
        Never raises; internal faults fail open (not throttled).
        """
        try:
            now = time.monotonic()
            with self._lock:
                entry = self._buckets.get(bucket)
                if entry is None:
                    return False
                count, started = entry
                if now - started >= window_seconds:
                    del self._buckets[bucket]
                    return False
                return count >= limit
        except Exception:  # noqa: BLE001 - throttle must never break auth
            return False

    def record_failure(
        self, bucket: str, window_seconds: float
    ) -> None:
        """
        Count one failed authentication against the bucket, starting (or
        resetting) its window. Never raises.
        """
        try:
            now = time.monotonic()
            with self._lock:
                entry = self._buckets.get(bucket)
                if entry is not None and now - entry[1] < window_seconds:
                    entry[0] += 1
                    self._buckets.move_to_end(bucket)
                    return
                self._buckets[bucket] = [1, now]
                while len(self._buckets) > self._max_buckets:
                    self._buckets.popitem(last=False)
        except Exception:  # noqa: BLE001 - throttle must never break auth
            pass

    def record_success(self, bucket: str) -> None:
        """
        Clear the bucket after a successful authentication so one bad
        credential followed by the right one never accumulates.
        Never raises.
        """
        try:
            with self._lock:
                self._buckets.pop(bucket, None)
        except Exception:  # noqa: BLE001 - throttle must never break auth
            pass

    def reset(self) -> None:
        """Drop every bucket (test isolation / operational reset)."""
        with self._lock:
            self._buckets.clear()


class AuthGate:
    """
    Process-wide concurrency gate for expensive store authentications.

    ``enter`` returns a slot token, or None when no slot frees up within
    the timeout; callers pass the token back to ``exit`` and deny closed
    when it is None. When ``max_concurrent`` differs from the value the
    current semaphore was built with, a new semaphore replaces it under
    the lock; existing holders keep and release their own token, so a
    live reload can never inflate or deflate unrelated slots.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(1)
        self._configured = 1

    def enter(self, max_concurrent: int):
        """
        Acquire a slot, waiting up to the fixed timeout. Returns the slot
        token on success and None on timeout or internal fault so callers
        fail closed.
        """
        try:
            with self._lock:
                wanted = max(1, int(max_concurrent))
                if wanted != self._configured:
                    self._semaphore = threading.BoundedSemaphore(wanted)
                    self._configured = wanted
                semaphore = self._semaphore
            return semaphore if semaphore.acquire(
                timeout=_GATE_TIMEOUT_SECONDS
            ) else None
        except Exception:  # noqa: BLE001 - gate must never break auth
            return None

    def exit(self, token) -> None:
        """
        Release a slot previously returned by ``enter``. Never raises.
        """
        try:
            token.release()
        except Exception:  # noqa: BLE001 - gate must never break auth
            pass


_THROTTLE_SINGLETON: AuthThrottle | None = None
_GATE_SINGLETON: AuthGate | None = None


def auth_throttle() -> AuthThrottle:
    """Process-wide throttle instance."""
    global _THROTTLE_SINGLETON
    if _THROTTLE_SINGLETON is None:
        _THROTTLE_SINGLETON = AuthThrottle()
    return _THROTTLE_SINGLETON


def auth_gate() -> AuthGate:
    """Process-wide concurrency gate instance."""
    global _GATE_SINGLETON
    if _GATE_SINGLETON is None:
        _GATE_SINGLETON = AuthGate()
    return _GATE_SINGLETON


def reset_auth_throttle() -> None:
    """Reset the singletons' state (test isolation)."""
    global _THROTTLE_SINGLETON, _GATE_SINGLETON
    if _THROTTLE_SINGLETON is not None:
        _THROTTLE_SINGLETON.reset()
    if _GATE_SINGLETON is not None:
        _GATE_SINGLETON = AuthGate()


__all__ = [
    "AuthGate",
    "AuthThrottle",
    "auth_gate",
    "auth_throttle",
    "reset_auth_throttle",
]
