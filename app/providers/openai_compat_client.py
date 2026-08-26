from typing import AsyncIterator, List, Union, Optional, Generator
import os
import time
import json
from contextlib import contextmanager
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import (
    ProviderHTTPError,
    ProviderResponseLimit,
    ProviderTimeout,
)
from app.services.metrics import relay_metrics
from app.services.redaction import (
    redact_provider_error,
    redact_text,
    safe_provider_key_detail,
)
from app.providers.transport_limits import (
    BoundedResponseHook,
    bounded_aiter_lines,
    bounded_iter_lines,
)


def _safe_provider_body(provider: Provider, status_code: int, body: str) -> str:
    """
    Build a bounded, redacted message from an untrusted provider body.

    Provider error bodies are treated as untrusted: they may echo the
    request prompt or response back to the relay. The API key is stripped
    when present, non-printable control characters are removed, and the
    text is truncated to a fixed bound so it never flows verbatim into
    error responses or logs. Delegates to the shared redaction layer
    (P6.3 dedupe of ``availability.safe_error_body``).
    """
    api_key = (
        provider.api_key
        if provider is not None and provider.has_api_key()
        else None
    )
    return redact_provider_error(api_key, status_code, body)


def _parse_provider_json(
    response: "httpx.Response",
    provider: Provider,
    status_code: int,
) -> dict:
    """
    Safely parse a provider's HTTP response body as JSON.

    Wraps ``response.json()`` to convert ``json.JSONDecodeError`` and
    ``ValueError`` into a ``ProviderHTTPError`` that flows through the
    normal error-classification and retry pipeline.  Without this guard,
    an empty or non-JSON body from a 200 OK response produces an
    unclassified exception that bypasses retry and failover logic.

    Also rejects non-``dict`` JSON (arrays, strings, numbers, null)
    which would cause ``AttributeError`` at every call site that
    accesses the result with ``.get()``.
    """
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderHTTPError(
            status_code,
            _safe_provider_body(
                provider,
                status_code,
                f"Invalid JSON from provider: {exc}",
            ),
        ) from exc

    if not isinstance(data, dict):
        raise ProviderHTTPError(
            status_code,
            _safe_provider_body(
                provider,
                status_code,
                f"Expected JSON object from provider, got {type(data).__name__}",
            ),
        )

    return data


def _text_content(content) -> str:
    """
    Extract plain text from an OpenAI message content (string or parts).

    Shared by the Anthropic and Gemini clients when translating an OpenAI
    payload to their native wire format (P6.3 dedupe).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "".join(parts)
    return ""


def _stream_error_text(response: httpx.Response) -> str:
    """
    Return the error body text of a streaming response.

    A response from httpx.stream cannot expose .text until its body has
    been consumed. Reading first keeps the provider's actual error body
    in the surfaced message instead of httpx's internal ResponseNotRead
    error.
    """
    try:
        response.read()
    except ProviderResponseLimit:
        raise
    except Exception:
        pass
    return response.text


async def _stream_error_text_async(response: httpx.Response) -> str:
    """
    Async counterpart of _stream_error_text: read a streamed error body.

    A response from ``client.stream`` cannot expose ``.text`` until its
    body has been consumed, so the body is awaited with ``aread()`` first.
    """
    try:
        await response.aread()
    except ProviderResponseLimit:
        raise
    except Exception:
        pass
    return response.text


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """
    Parse the Retry-After header into seconds to wait, or None when
    absent or unparseable. Supports integer/float seconds and the
    HTTP-date form (best effort).
    """
    headers = getattr(response, "headers", None)

    if headers is None:
        return None

    raw = headers.get("Retry-After")

    if not raw:
        return None

    raw = raw.strip()

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime
        import datetime

        retry_at = parsedate_to_datetime(raw)
        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
        remaining = (
            retry_at - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()
        return max(0.0, remaining)
    except Exception:
        return None


def _matches_no_proxy(url: str, no_proxy: str) -> bool:
    """
    True when the request URL's host is covered by a NO_PROXY-style list.

    Matches exact hosts and domain suffixes, supporting a leading dot and
    a bare "*" wildcard. The configured value is never logged.
    """
    if not no_proxy:
        return False

    host = (urlparse(url).hostname or "").lower()

    if not host:
        return False

    for entry in no_proxy.split(","):
        entry = entry.strip().lower().lstrip(".")

        if not entry:
            continue
        if entry == "*":
            return True
        if host == entry or host.endswith("." + entry):
            return True

    return False


def proxy_request_kwargs(provider: Provider, url: str) -> dict:
    """
    Compute httpx proxy kwargs for one outbound request.

    Behavior matrix:
    - Provider.proxy is a URL: force that proxy (trust_env disabled).
    - Provider.proxy is "": explicitly bypass the proxy (trust_env
      disabled, no proxy configured).
    - Provider.proxy is None and PROXY_ENABLED=true: select
      HTTP_PROXY/HTTPS_PROXY per request scheme from settings (with an
      env fallback) when configured; otherwise preserve httpx's default
      trust_env behavior. NO_PROXY is honored for explicit selections.
    - Provider.proxy is None and PROXY_ENABLED=false: no proxy at all.

    Proxy URLs and credentials are configuration only and are never
    logged or included in metrics/errors.
    """
    trust_env = False
    proxy = None

    if provider.proxy is not None:
        if provider.proxy == "":
            trust_env = False
        else:
            trust_env = False
            proxy = provider.proxy
    elif not getattr(settings, "proxy_enabled", True):
        trust_env = False
    else:
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""

        http_proxy = (getattr(settings, "http_proxy", "") or "").strip()
        https_proxy = (getattr(settings, "https_proxy", "") or "").strip()
        no_proxy = (getattr(settings, "no_proxy", "") or "").strip()

        if not http_proxy:
            http_proxy = (os.getenv("HTTP_PROXY", "") or "").strip()
        if not https_proxy:
            https_proxy = (os.getenv("HTTPS_PROXY", "") or "").strip()
        if not no_proxy:
            no_proxy = (os.getenv("NO_PROXY", "") or "").strip()

        if not http_proxy and not https_proxy:
            trust_env = True
        else:
            selected = https_proxy if scheme == "https" else http_proxy
            if selected and not _matches_no_proxy(url, no_proxy):
                proxy = selected

    return {
        "trust_env": trust_env,
        "proxy": proxy,
        "event_hooks": {
            "response": [
                BoundedResponseHook(
                    max_bytes=getattr(
                        settings, "provider_max_response_bytes", 16 * 1024 * 1024
                    ),
                    max_chunk_bytes=getattr(
                        settings, "provider_max_chunk_bytes", 1024 * 1024
                    ),
                    max_seconds=getattr(
                        settings, "provider_max_response_seconds", 600
                    ),
                )
            ]
        },
    }


def _bounded_http_client(kwargs: dict) -> httpx.Client:
    """
    Build a short-lived sync Client carrying the response budget hook.

    The top-level ``httpx.get/post/stream`` helpers construct their own
    internal Client but cannot install response event hooks (a
    Client-only argument), so the synchronous wire paths route through
    this explicitly constructed client. The ``trust_env``, ``proxy``,
    and ``event_hooks`` values produced by :func:`proxy_request_kwargs`
    configure the client; every other keyword stays a per-request
    argument. Connection behavior is unchanged: each request still uses
    its own short-lived client.
    """
    client_kwargs = {
        "trust_env": kwargs.pop("trust_env", True),
        "proxy": kwargs.pop("proxy", None),
    }
    event_hooks = kwargs.pop("event_hooks", None)
    if event_hooks:
        client_kwargs["event_hooks"] = event_hooks
    return httpx.Client(**client_kwargs)


def bounded_get(url: str, **kwargs) -> httpx.Response:
    """``httpx.get`` stand-in that carries the response budget hook."""
    with _bounded_http_client(kwargs) as client:
        return client.get(url, **kwargs)


def bounded_post(url: str, **kwargs) -> httpx.Response:
    """``httpx.post`` stand-in that carries the response budget hook."""
    with _bounded_http_client(kwargs) as client:
        return client.post(url, **kwargs)


@contextmanager
def bounded_stream(method: str, url: str, **kwargs):
    """``httpx.stream`` stand-in that carries the response budget hook."""
    with _bounded_http_client(kwargs) as client:
        with client.stream(method, url, **kwargs) as response:
            yield response


class OpenAICompatibleClient:
    """
    OpenAI-compatible chat client.

    Shared implementation for providers that expose the OpenAI REST
    protocol (chat completions and model listing): NVIDIA, OpenAI, and
    local endpoints like LM Studio. The display name is used in error
    messages so each provider keeps its own wording.

    The per-method authentication behavior is preserved exactly:
    - chat()/probe_model() send a Bearer header whenever the provider
      has a key; the header is omitted for keyless providers (the
      original per-provider clients always had a key, so sending an
      empty Bearer value was never exercised).
    - list_models() sends the header only when the provider has a key.
    """

    def __init__(self, name: str = "OpenAI compatible") -> None:
        self.name = name

    def proxy_request_kwargs(self, provider: Provider, url: str) -> dict:
        """
        Compute httpx proxy kwargs, matching the OpenAI-compatible client.
        """
        return proxy_request_kwargs(provider, url)

    def connectivity_probe(self, provider: Provider) -> tuple:
        """
        Probe provider connectivity using the Bearer convention.

        Returns ``(ok, details, latency_ms)`` for the health checker.
        """
        url = f"{provider.base_url.rstrip('/')}{provider.health_endpoint}"
        start = time.perf_counter()

        headers = {}

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        try:
            response = bounded_get(
                url,
                headers=headers,
                timeout=10,
                **proxy_request_kwargs(provider, url),
            )
            ok = response.status_code == 200
            details = f"HTTP {response.status_code}"
        except Exception as exc:
            ok = False
            details = redact_text(str(exc))

        latency = int((time.perf_counter() - start) * 1000)
        return ok, details, latency

    def chat(
        self,
        provider: Provider,
        model: str,
        message: str,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> str:

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        # Determine effective values, falling back to internal defaults
        eff_temp = 0.2 if temperature is None else temperature
        eff_max_tokens = 512 if max_tokens is None else max_tokens

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
            "temperature": eff_temp,
            "max_tokens": eff_max_tokens,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if seed is not None:
            payload["seed"] = seed

        start = time.perf_counter()

        try:
            response = bounded_post(
                f"{provider.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/chat/completions"
                ),
            )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name, "chat", response.status_code, latency_ms
            )
            raise ProviderHTTPError(
                response.status_code,
                _safe_provider_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name, "chat", response.status_code, latency_ms
        )

        try:
            data = _parse_provider_json(response, provider, response.status_code)
        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name, "chat", 0, latency_ms
            )
            raise

        choices = data.get("choices") or []
        if not choices:
            raise ProviderHTTPError(
                response.status_code, "empty provider response"
            )
        return choices[0]["message"]["content"]

    def chat_messages(
        self,
        provider: Provider,
        payload: dict,
    ) -> dict:
        """
        Send a full OpenAI chat-completions payload (message array, tools,
        tool_choice, stream_options, ...) and return the provider's parsed
        response body unchanged.

        Unlike chat(), the payload is forwarded verbatim: no defaults are
        injected and the message structure reaches the provider exactly as
        the caller sent it. Raises ProviderTimeout or ProviderHTTPError on
        failure, with the provider body bounded and redacted.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        start = time.perf_counter()

        try:
            response = bounded_post(
                f"{provider.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/chat/completions"
                ),
            )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                response.status_code,
                latency_ms,
            )
            raise ProviderHTTPError(
                response.status_code,
                _safe_provider_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name, "chat_messages", response.status_code, latency_ms
        )

        try:
            return _parse_provider_json(response, provider, response.status_code)
        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name, "chat_messages", 0, latency_ms
            )
            raise

    def list_models(self, provider: Provider) -> List[str]:
        """
        Fetch the models available from the provider API.
        """

        headers = {}

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        start = time.perf_counter()

        try:
            response = bounded_get(
                f"{provider.base_url}/models",
                headers=headers,
                timeout=30,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/models"
                ),
            )

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "list_models",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} model discovery timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "list_models",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name,
                "list_models",
                response.status_code,
                latency_ms,
            )
            raise ProviderHTTPError(
                response.status_code,
                _safe_provider_body(
                    provider, response.status_code, response.text
                ),
            )

        relay_metrics.record_provider(
            provider.name,
            "list_models",
            response.status_code,
            latency_ms,
        )

        data = _parse_provider_json(response, provider, response.status_code)

        return [
            model["id"]
            for model in data.get("data", [])
            if isinstance(model, dict) and "id" in model
        ]

    def key_check(self, provider: Provider):
        """
        Return ``(status_code, body_text)`` for a key validation request,
        or ``(None, error)`` when the provider is unreachable.

        Uses the same authenticated ``GET /models`` call as list_models so
        validation exercises the real catalog endpoint.
        """
        headers = {}

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        try:
            response = bounded_get(
                f"{provider.base_url}/models",
                headers=headers,
                timeout=30,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/models"
                ),
            )
        except httpx.HTTPError as exc:
            return None, "provider unavailable"

        if response.status_code >= 400:
            return response.status_code, safe_provider_key_detail(
                response.status_code, response.text
            )
        return response.status_code, ""

    def check_model(self, provider: Provider, model: str) -> bool:
        """
        Check whether a model is usable.
        """
        return self.probe_model(provider, model).healthy

    def probe_model(
        self,
        provider: Provider,
        model: str,
    ) -> ModelProbe:
        """
        Probe a model, returning health, latency, and failure detail.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "ping",
                }
            ],
            "max_tokens": 1,
        }

        start = time.perf_counter()

        try:
            response = bounded_post(
                f"{provider.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=10,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/chat/completions"
                ),
            )

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "probe_model",
                (time.perf_counter() - start) * 1000,
            )
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                "timeout",
            )

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "probe_model",
                0,
                (time.perf_counter() - start) * 1000,
            )
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                redact_text(str(exc)),
            )

        latency = int((time.perf_counter() - start) * 1000)

        if response.status_code == 200:
            relay_metrics.record_provider(
                provider.name,
                "probe_model",
                response.status_code,
                latency,
            )
            return ModelProbe(True, latency, 200, "")

        relay_metrics.record_provider(
            provider.name,
            "probe_model",
            response.status_code,
            latency,
        )

        return ModelProbe(
            False,
            latency,
            response.status_code,
            _safe_provider_body(
                provider, response.status_code, response.text
            ),
        )

    def chat_stream(
        self,
        provider: Provider,
        model: str,
        message: str,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> Generator[str, None, None]:
        """
        Stream chat completion from the provider.

        Yields content delta strings as they arrive.
        Raises ProviderTimeout or ProviderHTTPError on failure.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        # Determine effective values, falling back to internal defaults
        eff_temp = 0.2 if temperature is None else temperature
        eff_max_tokens = 512 if max_tokens is None else max_tokens

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
            "temperature": eff_temp,
            "max_tokens": eff_max_tokens,
            "stream": True,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if seed is not None:
            payload["seed"] = seed

        done_seen = False
        start = time.perf_counter()
        try:
            with bounded_stream(
                "POST",
                f"{provider.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/chat/completions"
                ),
            ) as response:
                if response.status_code >= 400:
                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream",
                        response.status_code,
                        (time.perf_counter() - start) * 1000,
                    )
                    raise ProviderHTTPError(
                        response.status_code,
                        _safe_provider_body(
                            provider,
                            response.status_code,
                            _stream_error_text(response),
                        ),
                        retry_after=_retry_after_seconds(response),
                    )

                for line in bounded_iter_lines(response):
                    if not line:
                        continue
                    # Each line should start with "data: "
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            done_seen = True
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk["choices"][0]["delta"]
                            content = delta.get("content")
                            if content:
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
                            # Malformed chunk, skip
                            continue

                relay_metrics.record_provider(
                    provider.name,
                    "chat_stream",
                    200,
                    (time.perf_counter() - start) * 1000,
                )

        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            if done_seen:
                return
            relay_metrics.record_provider(
                provider.name,
                "chat_stream",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

    def chat_stream_messages(
        self,
        provider: Provider,
        payload: dict,
    ) -> Generator[dict, None, None]:
        """
        Stream a full OpenAI chat-completions payload.

        Yields parsed chunk dicts (including tool_call deltas and the
        usage chunk when the provider emits one) as they arrive. Raises
        ProviderTimeout or ProviderHTTPError on failure; the provider
        body is bounded and redacted. The payload is forwarded verbatim.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        done_seen = False
        start = time.perf_counter()
        try:
            with bounded_stream(
                "POST",
                f"{provider.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(
                    provider, f"{provider.base_url}/chat/completions"
                ),
            ) as response:
                if response.status_code >= 400:
                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream_messages",
                        response.status_code,
                        (time.perf_counter() - start) * 1000,
                    )
                    raise ProviderHTTPError(
                        response.status_code,
                        _safe_provider_body(
                            provider,
                            response.status_code,
                            _stream_error_text(response),
                        ),
                        retry_after=_retry_after_seconds(response),
                    )

                for line in bounded_iter_lines(response):
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        done_seen = True
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if not chunk.get("choices") and "usage" not in chunk:
                        continue
                    yield chunk

                relay_metrics.record_provider(
                    provider.name,
                    "chat_stream_messages",
                    200,
                    (time.perf_counter() - start) * 1000,
                )

        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            if done_seen:
                return
            relay_metrics.record_provider(
                provider.name,
                "chat_stream_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

    async def achat(
        self,
        provider: Provider,
        model: str,
        message: str,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> str:
        """
        Async chat completion: returns the assistant message content.

        Mirrors chat() over httpx.AsyncClient with the same payload,
        headers, timeout, error mapping, and provider metrics.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        eff_temp = 0.2 if temperature is None else temperature
        eff_max_tokens = 512 if max_tokens is None else max_tokens

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
            "temperature": eff_temp,
            "max_tokens": eff_max_tokens,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if seed is not None:
            payload["seed"] = seed

        url = f"{provider.base_url}/chat/completions"
        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=settings.request_timeout,
                )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name, "chat", response.status_code, latency_ms
            )
            raise ProviderHTTPError(
                response.status_code,
                _safe_provider_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name, "chat", response.status_code, latency_ms
        )

        try:
            data = _parse_provider_json(response, provider, response.status_code)
        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name, "chat", 0, latency_ms
            )
            raise

        choices = data.get("choices") or []
        if not choices:
            raise ProviderHTTPError(
                response.status_code, "empty provider response"
            )
        return choices[0]["message"]["content"]

    async def achat_stream(
        self,
        provider: Provider,
        model: str,
        message: str,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """
        Async streaming chat completion.

        Yields content delta strings as they arrive. Mirrors
        chat_stream() over httpx.AsyncClient; raises ProviderTimeout or
        ProviderHTTPError on failure.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        eff_temp = 0.2 if temperature is None else temperature
        eff_max_tokens = 512 if max_tokens is None else max_tokens

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
            "temperature": eff_temp,
            "max_tokens": eff_max_tokens,
            "stream": True,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if seed is not None:
            payload["seed"] = seed

        url = f"{provider.base_url}/chat/completions"

        done_seen = False
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=settings.request_timeout,
                ) as response:
                    if response.status_code >= 400:
                        relay_metrics.record_provider(
                            provider.name,
                            "chat_stream",
                            response.status_code,
                            (time.perf_counter() - start) * 1000,
                        )
                        raise ProviderHTTPError(
                            response.status_code,
                            _safe_provider_body(
                                provider,
                                response.status_code,
                                await _stream_error_text_async(response),
                            ),
                            retry_after=_retry_after_seconds(response),
                        )

                    async for line in bounded_aiter_lines(response):
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                done_seen = True
                                break
                            try:
                                chunk = json.loads(data_str)
                                delta = chunk["choices"][0]["delta"]
                                content = delta.get("content")
                                if content:
                                    yield content
                            except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
                                continue

                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream",
                        200,
                        (time.perf_counter() - start) * 1000,
                    )

        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            if done_seen:
                return
            relay_metrics.record_provider(
                provider.name,
                "chat_stream",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

    async def achat_messages(
        self,
        provider: Provider,
        payload: dict,
    ) -> dict:
        """
        Async full-payload chat completion.

        Mirrors chat_messages() over httpx.AsyncClient: the payload is
        forwarded verbatim and the provider's parsed response body is
        returned unchanged. Raises ProviderTimeout or ProviderHTTPError on
        failure, with the provider body bounded and redacted.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        url = f"{provider.base_url}/chat/completions"
        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=settings.request_timeout,
                )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                response.status_code,
                latency_ms,
            )
            raise ProviderHTTPError(
                response.status_code,
                _safe_provider_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name, "chat_messages", response.status_code, latency_ms
        )

        try:
            return _parse_provider_json(response, provider, response.status_code)
        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name, "chat_messages", 0, latency_ms
            )
            raise

    async def achat_stream_messages(
        self,
        provider: Provider,
        payload: dict,
    ) -> AsyncIterator[dict]:
        """
        Async full-payload streaming chat completion.

        Mirrors chat_stream_messages() over httpx.AsyncClient. Yields
        parsed chunk dicts (including tool_call deltas and the usage chunk
        when the provider emits one) as they arrive. Raises
        ProviderTimeout or ProviderHTTPError on failure; the provider body
        is bounded and redacted. The payload is forwarded verbatim.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        url = f"{provider.base_url}/chat/completions"

        done_seen = False
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=settings.request_timeout,
                ) as response:
                    if response.status_code >= 400:
                        relay_metrics.record_provider(
                            provider.name,
                            "chat_stream_messages",
                            response.status_code,
                            (time.perf_counter() - start) * 1000,
                        )
                        raise ProviderHTTPError(
                            response.status_code,
                            _safe_provider_body(
                                provider,
                                response.status_code,
                                await _stream_error_text_async(response),
                            ),
                            retry_after=_retry_after_seconds(response),
                        )

                    async for line in bounded_aiter_lines(response):
                        if not line:
                            continue
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            done_seen = True
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if not chunk.get("choices") and "usage" not in chunk:
                            continue
                        yield chunk

                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream_messages",
                        200,
                        (time.perf_counter() - start) * 1000,
                    )

        except ProviderResponseLimit:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            if done_seen:
                return
            relay_metrics.record_provider(
                provider.name,
                "chat_stream_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

    async def alist_models(self, provider: Provider) -> List[str]:
        """
        Async model catalog listing. Mirrors list_models() over
        httpx.AsyncClient.
        """

        headers = {}

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        url = f"{provider.base_url}/models"
        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=30,
                )

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "list_models",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} model discovery timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "list_models",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(
                0,
                redact_text(str(exc)),
            ) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name,
                "list_models",
                response.status_code,
                latency_ms,
            )
            raise ProviderHTTPError(
                response.status_code,
                _safe_provider_body(
                    provider, response.status_code, response.text
                ),
            )

        relay_metrics.record_provider(
            provider.name,
            "list_models",
            response.status_code,
            latency_ms,
        )

        data = _parse_provider_json(response, provider, response.status_code)

        return [
            model["id"]
            for model in data.get("data", [])
            if isinstance(model, dict) and "id" in model
        ]

    async def aprobe_model(
        self,
        provider: Provider,
        model: str,
    ) -> ModelProbe:
        """
        Async model probe. Mirrors probe_model() over httpx.AsyncClient.
        """

        headers = {
            "Content-Type": "application/json",
        }

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "ping",
                }
            ],
            "max_tokens": 1,
        }

        url = f"{provider.base_url}/chat/completions"
        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=10,
                )

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "probe_model",
                (time.perf_counter() - start) * 1000,
            )
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                "timeout",
            )

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "probe_model",
                0,
                (time.perf_counter() - start) * 1000,
            )
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                redact_text(str(exc)),
            )

        latency = int((time.perf_counter() - start) * 1000)

        if response.status_code == 200:
            relay_metrics.record_provider(
                provider.name,
                "probe_model",
                response.status_code,
                latency,
            )
            return ModelProbe(True, latency, 200, "")

        relay_metrics.record_provider(
            provider.name,
            "probe_model",
            response.status_code,
            latency,
        )

        return ModelProbe(
            False,
            latency,
            response.status_code,
            _safe_provider_body(
                provider, response.status_code, response.text
            ),
        )
