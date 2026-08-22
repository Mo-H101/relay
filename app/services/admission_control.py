"""Process-local admission control for expensive chat execution."""

from __future__ import annotations

import threading


DEFAULT_MAX_CHAT_INFLIGHT = 8


class AdmissionLease:
    """An idempotent lease held while one expensive chat executes."""

    def __init__(self, controller: "ChatAdmission") -> None:
        self._controller = controller
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._controller._release()


class ChatAdmission:
    """Bound concurrent provider executions without queuing requests."""

    def __init__(self, limit: int = DEFAULT_MAX_CHAT_INFLIGHT) -> None:
        if limit < 1:
            raise ValueError("chat admission limit must be positive")
        self._limit = limit
        self._active = 0
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> AdmissionLease | None:
        with self._lock:
            if self._active >= self._limit:
                return None
            self._active += 1
        return AdmissionLease(self)

    def _release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("chat admission lease underflow")
            self._active -= 1


chat_admission = ChatAdmission()
