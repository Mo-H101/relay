"""
Self-contained Prometheus metrics registry.

Emits the Prometheus text exposition format (version 0.0.4) with no
external dependency. All values are in-memory counters, gauges, and
histograms that reset on process restart; nothing is persisted.

Security: labels come from fixed, bounded sets (route templates,
provider names, status bands, failure kinds). Prompts, responses, API
keys, proxy credentials, and user-controlled path/query strings are
never recorded. Unmatched request paths are labeled "unmatched".
"""

import threading
import time
from typing import Dict, Iterable, Optional, Tuple

_DEFAULT_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)


def _escape_label(value: str) -> str:
    """
    Escape a label value per the Prometheus text format.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _fmt(value) -> str:
    """
    Short float/int formatting accepted by Prometheus scrapers.
    """
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


class _Labeled:
    """
    Shared label keying for Counter and Gauge.
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        label_names: Tuple[str, ...],
        metric_type: str,
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.label_names = label_names
        self.metric_type = metric_type
        self._lock = threading.Lock()
        self._values: Dict[Tuple[str, ...], float] = {}

    def _key(self, label_values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(label_values) != len(self.label_names):
            raise ValueError(
                f"metric {self.name} expects {len(self.label_names)} "
                f"labels, got {len(label_values)}"
            )
        return tuple(str(value) for value in label_values)

    def _build(self, labels: Dict[str, object]) -> Tuple[str, ...]:
        missing = set(self.label_names) - set(labels)
        if missing:
            raise ValueError(
                f"metric {self.name} missing labels: "
                f"{', '.join(sorted(missing))}"
            )
        return tuple(str(labels[name]) for name in self.label_names)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def _type_line(self) -> str:
        name = self.name
        if self.metric_type == "counter" and name.endswith("_total"):
            name = name[: -len("_total")]
        return f"# TYPE {name} {self.metric_type}"

    def _render(self) -> list:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            self._type_line(),
        ]

        with self._lock:
            items = sorted(self._values.items())

        for key, value in items:
            label_text = ",".join(
                f'{name}="{_escape_label(value)}"'
                for name, value in zip(self.label_names, key)
            )
            if label_text:
                lines.append(f"{self.name}{{{label_text}}} {_fmt(value)}")
            else:
                lines.append(f"{self.name} {_fmt(value)}")

        return lines


class Counter(_Labeled):
    """
    Monotonic counter with labels.
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        label_names: Tuple[str, ...] = (),
    ) -> None:
        super().__init__(name, documentation, label_names, "counter")

    def inc(self, amount: float = 1, **labels) -> None:
        if amount < 0:
            raise ValueError("counter increment must be non-negative")
        key = self._key(self._build(labels))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels) -> float:
        key = self._key(self._build(labels))
        with self._lock:
            return self._values.get(key, 0.0)

    def total(self) -> float:
        with self._lock:
            return sum(self._values.values())


class Gauge(_Labeled):
    """
    Settable value with labels.
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        label_names: Tuple[str, ...] = (),
    ) -> None:
        super().__init__(name, documentation, label_names, "gauge")

    def set(self, value: float, **labels) -> None:
        key = self._key(self._build(labels))
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1, **labels) -> None:
        key = self._key(self._build(labels))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1, **labels) -> None:
        key = self._key(self._build(labels))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) - amount

    def value(self, **labels) -> float:
        key = self._key(self._build(labels))
        with self._lock:
            return self._values.get(key, 0.0)


class Histogram:
    """
    Latency histogram with labels and fixed buckets.
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        label_names: Tuple[str, ...] = (),
        buckets: Tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.label_names = label_names
        self.buckets = tuple(float(bucket) for bucket in buckets)
        self._lock = threading.Lock()
        self._values: Dict[
            Tuple[str, ...], Tuple[Dict[float, int], float, int]
        ] = {}

    def _build(self, labels: Dict[str, object]) -> Tuple[str, ...]:
        missing = set(self.label_names) - set(labels)
        if missing:
            raise ValueError(
                f"metric {self.name} missing labels: "
                f"{', '.join(sorted(missing))}"
            )
        return tuple(str(labels[name]) for name in self.label_names)

    def observe(self, value: float, **labels) -> None:
        value = float(value)
        if value < 0:
            raise ValueError("histogram observation must be non-negative")
        key = self._build(labels)

        with self._lock:
            entry = self._values.get(key)

            if entry is None:
                counts = {bucket: 0 for bucket in self.buckets}
                entry = (counts, 0.0, 0)
                self._values[key] = entry

            counts, total, count = entry

            for bucket in self.buckets:
                if value <= bucket:
                    counts[bucket] += 1

            entry = (counts, total + value, count + 1)
            self._values[key] = entry

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def _render(self) -> list:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} histogram",
        ]

        with self._lock:
            items = sorted(self._values.items())

        for key, (counts, total, count) in items:
            base_labels = ",".join(
                f'{name}="{_escape_label(value)}"'
                for name, value in zip(self.label_names, key)
            )

            def _series(suffix: str, extra: str) -> str:
                parts = [base_labels, extra]
                label_text = ",".join(part for part in parts if part)
                if label_text:
                    return f"{self.name}_{suffix}{{{label_text}}}"
                return f"{self.name}_{suffix}"

            for bucket in self.buckets:
                lines.append(
                    f"{_series('bucket', f'le=\"{_fmt(bucket)}\"')} "
                    f"{counts[bucket]}"
                )
            lines.append(
                f"{_series('bucket', 'le=\"+Inf\"')} {count}"
            )
            lines.append(f"{_series('sum', '')} {_fmt(total)}")
            lines.append(f"{_series('count', '')} {count}")

        return lines


class MetricsRegistry:
    """
    Owns every declared metric and renders the exposition text.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}

    def counter(
        self,
        name: str,
        documentation: str,
        labels: Iterable[str] = (),
    ) -> Counter:
        with self._lock:
            metric = self._counters.get(name)
            if metric is None:
                metric = Counter(name, documentation, tuple(labels))
                self._counters[name] = metric
            return metric

    def gauge(
        self,
        name: str,
        documentation: str,
        labels: Iterable[str] = (),
    ) -> Gauge:
        with self._lock:
            metric = self._gauges.get(name)
            if metric is None:
                metric = Gauge(name, documentation, tuple(labels))
                self._gauges[name] = metric
            return metric

    def histogram(
        self,
        name: str,
        documentation: str,
        labels: Iterable[str] = (),
        buckets: Tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> Histogram:
        with self._lock:
            metric = self._histograms.get(name)
            if metric is None:
                metric = Histogram(name, documentation, tuple(labels), buckets)
                self._histograms[name] = metric
            return metric

    def clear(self) -> None:
        with self._lock:
            counters = list(self._counters.values())
            gauges = list(self._gauges.values())
            histograms = list(self._histograms.values())

        for metric in counters + gauges + histograms:
            metric.clear()

    def render(self) -> str:
        with self._lock:
            counters = sorted(self._counters.values(), key=lambda m: m.name)
            gauges = sorted(self._gauges.values(), key=lambda m: m.name)
            histograms = sorted(
                self._histograms.values(), key=lambda m: m.name
            )

        lines: list = []
        for metric in counters:
            lines.extend(metric._render())
        for metric in gauges:
            lines.extend(metric._render())
        for metric in histograms:
            lines.extend(metric._render())

        return "\n".join(lines) + ("\n" if lines else "")


def _max_tokens_band(value) -> str:
    """
    Bucket a max_tokens value into a small, bounded label set.
    """
    if value is None or value <= 0:
        return "unset"
    if value <= 512:
        return "<=512"
    if value <= 1024:
        return "<=1k"
    if value <= 2048:
        return "<=2k"
    if value <= 4096:
        return "<=4k"
    return ">4k"


def _status_band(status) -> str:
    """
    Bucket a provider status code into a bounded label set.
    """
    status = int(status) if status else 0
    if status == 0:
        return "network"
    if status < 400:
        return "success"
    if status < 500:
        return "http_4xx"
    return "http_5xx"


class RelayMetrics:
    """
    Declares every Relay metric and provides recording helpers.

    Recording helpers never raise and never inspect payloads, so they
    are safe to call from request paths, background threads, and error
    handlers.
    """

    def __init__(self, registry: Optional[MetricsRegistry] = None) -> None:
        self._r = registry or MetricsRegistry()
        r = self._r

        # HTTP
        self.http_requests = r.counter(
            "relay_http_requests_total",
            "Total HTTP requests by method, route, and status.",
            ("method", "route", "status"),
        )
        self.http_success = r.counter(
            "relay_http_success_total",
            "Successful HTTP requests (status < 400).",
            ("method", "route"),
        )
        self.http_failure = r.counter(
            "relay_http_failure_total",
            "Failed HTTP requests (status >= 400).",
            ("method", "route"),
        )
        self.http_duration = r.histogram(
            "relay_http_request_duration_seconds",
            "HTTP request duration to completion.",
            ("method", "route"),
        )
        self.http_ttfb = r.histogram(
            "relay_http_ttfb_seconds",
            "HTTP time to first byte.",
            ("method", "route"),
        )
        self.http_active = r.gauge(
            "relay_http_active_requests",
            "Currently active HTTP requests.",
        )

        # Chat
        self.chat_requests = r.counter(
            "relay_chat_requests_total",
            "Chat requests by endpoint, streaming mode, and max_tokens band.",
            ("endpoint", "stream", "max_tokens_band"),
        )
        self.chat_outcomes = r.counter(
            "relay_chat_outcomes_total",
            "Chat request outcomes.",
            ("endpoint", "stream", "success", "fallback"),
        )
        self.chat_latency = r.histogram(
            "relay_chat_latency_seconds",
            "Chat request latency to completion.",
            ("endpoint", "stream"),
        )
        self.chat_parameter = r.counter(
            "relay_chat_parameter_used_total",
            "Chat generation parameters used per endpoint.",
            ("endpoint", "parameter"),
        )

        # Providers
        self.provider_requests = r.counter(
            "relay_provider_requests_total",
            "Provider requests by operation.",
            ("provider", "operation"),
        )
        self.provider_outcomes = r.counter(
            "relay_provider_outcomes_total",
            "Provider outcomes by status band.",
            ("provider", "operation", "status"),
        )
        self.provider_latency = r.histogram(
            "relay_provider_latency_seconds",
            "Provider operation latency.",
            ("provider", "operation"),
        )
        self.provider_health = r.gauge(
            "relay_provider_health_info",
            "Provider health state (1 for the active status).",
            ("provider", "status"),
        )
        self.provider_connectivity = r.gauge(
            "relay_provider_connectivity",
            "Provider connectivity (1 = reachable).",
            ("provider",),
        )

        # Routing
        self.routing_selected = r.counter(
            "relay_routing_selected_provider_total",
            "Chat requests completed on each provider.",
            ("provider",),
        )
        self.routing_fallbacks = r.counter(
            "relay_routing_fallbacks_total",
            "Failovers from the first candidate.",
            ("provider", "failure_type"),
        )
        self.routing_retries = r.counter(
            "relay_routing_retries_total",
            "Retried attempts.",
            ("provider", "model", "failure_type"),
        )

        # Security
        self.auth_failures = r.counter(
            "relay_auth_failures_total",
            "Authentication failures.",
            ("reason",),
        )
        self.auth_success = r.counter(
            "relay_auth_success_total",
            "Authenticated requests by credential method.",
            ("method",),
        )
        self.auth_enabled = r.gauge(
            "relay_auth_enabled",
            "Whether API-key authentication is enabled.",
        )
        self.auth_by_key = r.counter(
            "relay_auth_by_key_total",
            "Successful authentications per store key (P5 Phase 4).",
            ("key_id",),
        )
        self.key_admin_actions = r.counter(
            "relay_key_admin_actions_total",
            "Administrative key-management actions by action and outcome.",
            ("action", "outcome"),
        )
        self.events_written = r.counter(
            "relay_events_written_total",
            "Security events durably written to the events table.",
        )
        self.events_failed = r.counter(
            "relay_events_failed_total",
            "Security events that could not be durably written "
            "(audit degraded).",
        )

        # Persistence
        self.persistence_enabled = r.gauge(
            "relay_persistence_enabled",
            "Whether persistence is available.",
        )
        self.persistence_flush_failures = r.counter(
            "relay_persistence_flush_failures_total",
            "Persistent state flush failures.",
        )
        self.persistence_load_failures = r.counter(
            "relay_persistence_load_failures_total",
            "Persistent state load failures.",
        )

        # P9 project continuity
        self.continuity_enabled = r.gauge(
            "relay_continuity_enabled",
            "Whether project continuity is enabled.",
        )
        self.continuity_rows_queued = r.gauge(
            "relay_continuity_rows_queued",
            "Continuity rows waiting in the write-behind buffer.",
        )
        self.continuity_flushes = r.counter(
            "relay_continuity_flushes_total",
            "Continuity background flush passes.",
        )
        self.continuity_pruned = r.counter(
            "relay_continuity_pruned_total",
            "Conversations pruned by continuity retention.",
        )
        self.continuity_flush_failures = r.counter(
            "relay_continuity_flush_failures_total",
            "Continuity flush failures.",
        )
        self.continuity_switches = r.counter(
            "relay_continuity_switches_total",
            "Model handoffs allowed by the continuity coordinator.",
        )
        self.continuity_denials = r.counter(
            "relay_continuity_denials_total",
            "Model handoffs denied by the continuity coordinator.",
        )
        self.continuity_turns_committed = r.counter(
            "relay_continuity_turns_committed_total",
            "Turns committed to the continuity store.",
        )
        self.continuity_compactions = r.counter(
            "relay_continuity_compactions_total",
            "Envelope compactions performed by the continuity coordinator.",
        )
        self.continuity_resumes = r.counter(
            "relay_continuity_resumes_total",
            "Resume tokens accepted by continuity recovery.",
        )
        self.continuity_resume_denials = r.counter(
            "relay_continuity_resume_denials_total",
            "Resume attempts denied by continuity recovery.",
        )
        self.continuity_reconciliations = r.counter(
            "relay_continuity_reconciliations_total",
            "Startup continuity reconciliation passes.",
        )

        # Process
        self.uptime = r.gauge(
            "relay_process_uptime_seconds",
            "Process uptime in seconds.",
        )
        self._provider_statuses: Dict[str, str] = {}
        self._provider_statuses_lock = threading.Lock()
        self._started = time.monotonic()

    def reset(self) -> None:
        """
        Clear all values (test isolation and manual reset).
        """
        self._r.clear()
        self._provider_statuses: Dict[str, str] = {}
        self._started = time.monotonic()

    def render(self) -> str:
        """
        Render the full exposition text, refreshing the uptime gauge.
        """
        self.uptime.set(time.monotonic() - self._started)
        return self._r.render()

    # ------------------------- recording helpers -------------------------

    def record_http(
        self,
        method: str,
        route: str,
        status: Optional[int],
        duration: float,
        ttfb: Optional[float],
    ) -> None:
        """
        Record a completed HTTP request. Called by the metrics middleware.
        """
        status = status or 500
        self.http_active.dec()
        self.http_requests.inc(method=method, route=route, status=status)

        if status < 400:
            self.http_success.inc(method=method, route=route)
        else:
            self.http_failure.inc(method=method, route=route)

        self.http_duration.observe(max(0.0, duration), method=method, route=route)

        if ttfb is not None:
            self.http_ttfb.observe(max(0.0, ttfb), method=method, route=route)

    def record_auth(
        self,
        enabled: bool,
        granted: bool,
        method: str,
        failure_reason: str = "invalid",
        key_id: Optional[str] = None,
    ) -> None:
        """
        Record an authentication decision from the auth dependency.
        ``key_id`` is the KeyStore key that satisfied the request (P5
        Phase 4); it is never present for bootstrap-key authentication.
        """
        self.auth_enabled.set(1 if enabled else 0)

        if granted:
            self.auth_success.inc(method=method)
            if key_id:
                self.auth_by_key.inc(key_id=key_id)
        else:
            self.auth_failures.inc(reason=failure_reason)

    def record_key_action(
        self,
        action: str,
        outcome: str,
    ) -> None:
        """
        Record an administrative key-management action from the admin
        key API. ``key_id`` is not recorded in metrics (it is an opaque
        identifier only); key-level detail is emitted via ops events.
        """
        self.key_admin_actions.inc(action=action, outcome=outcome)

    def record_provider(
        self,
        provider: str,
        operation: str,
        status: int | None,
        latency_ms: float,
    ) -> None:
        """
        Record a single provider operation from the provider client.
        """
        self.provider_requests.inc(provider=provider, operation=operation)
        self.provider_outcomes.inc(
            provider=provider,
            operation=operation,
            status=_status_band(status),
        )
        self.provider_latency.observe(
            max(0.0, latency_ms) / 1000.0,
            provider=provider,
            operation=operation,
        )

    def record_provider_timeout(
        self,
        provider: str,
        operation: str,
        latency_ms: float,
    ) -> None:
        """
        Record a provider timeout outcome.
        """
        self.provider_requests.inc(provider=provider, operation=operation)
        self.provider_outcomes.inc(
            provider=provider,
            operation=operation,
            status="timeout",
        )
        self.provider_latency.observe(
            max(0.0, latency_ms) / 1000.0,
            provider=provider,
            operation=operation,
        )

    def update_provider_health(self, report) -> None:
        """
        Record a provider health report from HealthChecker. The previous
        status sample is cleared so only the active status reports 1.
        """
        provider = report.name

        with self._provider_statuses_lock:
            previous = self._provider_statuses.get(provider)

            if previous is not None and previous != report.status:
                self.provider_health.set(0, provider=provider, status=previous)

            self._provider_statuses[provider] = report.status
            self.provider_health.set(
                1, provider=provider, status=report.status
            )

        self.provider_connectivity.set(
            1 if report.connectivity else 0,
            provider=provider,
        )

    def record_chat(
        self,
        endpoint: str,
        stream: bool,
        result: dict,
        latency_ms: float,
        gen_kwargs: Optional[dict] = None,
    ) -> None:
        """
        Record chat request entry, completion, and routing/fallback/retry
        signals from a chat result dict.
        """
        stream_text = "true" if stream else "false"
        max_tokens_band = _max_tokens_band(
            (gen_kwargs or {}).get("max_tokens")
        )
        self.chat_requests.inc(
            endpoint=endpoint,
            stream=stream_text,
            max_tokens_band=max_tokens_band,
        )

        for parameter in (gen_kwargs or {}):
            self.chat_parameter.inc(endpoint=endpoint, parameter=parameter)

        success = bool(result.get("success"))
        fallback = bool(result.get("fallback_reason"))
        self.chat_outcomes.inc(
            endpoint=endpoint,
            stream=stream_text,
            success="true" if success else "false",
            fallback="true" if fallback else "false",
        )
        self.chat_latency.observe(
            max(0.0, latency_ms) / 1000.0,
            endpoint=endpoint,
            stream=stream_text,
        )

        provider = result.get("provider")
        if provider:
            self.routing_selected.inc(provider=provider)

        attempts = result.get("attempts") or []
        fallback_failure_types = set()

        for attempt in attempts:
            attempt_failure = attempt.get("failure_type")
            if not attempt.get("success") and attempt_failure:
                fallback_failure_types.add(attempt_failure)

            if attempt.get("attempt", 0) > 0:
                self.routing_retries.inc(
                    provider=attempt.get("provider") or "unknown",
                    model=attempt.get("model") or "unknown",
                    failure_type=attempt_failure or "none",
                )

        if fallback:
            for failure_type in fallback_failure_types or {"unknown"}:
                self.routing_fallbacks.inc(
                    provider=provider or "unknown",
                    failure_type=failure_type,
                )


relay_metrics = RelayMetrics()
