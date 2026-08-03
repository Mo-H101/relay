"""
Live cloud smoke test for the Release Candidate gateway.

Boots the REAL Relay application against the REAL NVIDIA and OpenAI
endpoints using the keys in ``.env`` and verifies the exact
OpenAI-compatible surface clients (Cline/OpenCode/OpenAI SDK) use:
non-stream, stream, tool calling, native ``/chat``, ``/v1/models``, and
``/health``.

This script is intentionally NOT collected by pytest (no ``test_``
prefix) because it requires live keys and hits paid endpoints. It is run
directly:

    python tests/run_live_smoke.py

Cost is minimal: every completion is capped at a handful of tokens. Set
``SMOKE_MODEL`` to override the OpenAI model used (default
``gpt-4o-mini``).

Exit code is 0 when every step passes, 1 otherwise.
"""

import asyncio
import os
import sys

# Make the project root importable regardless of how this script is run
# (`python tests/run_live_smoke.py` puts tests/ on sys.path, not the root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Apply process-level overrides before app config is imported. The config
# module calls load_dotenv(), which never overrides an existing env var,
# so these win over .env for this run only.
os.environ.setdefault("PERSISTENCE_ENABLED", "false")

# Pin NVIDIA's priority so the native /chat step does not walk the full
# dynamically-discovered model list (221+) on every run. Priority ordering
# only reorders candidates; it never removes models, so the walk still
# happens if this model is unavailable. Override with SMOKE_NVIDIA_MODEL.
os.environ.setdefault(
    "NVIDIA_MODEL_PRIORITY",
    os.getenv("SMOKE_NVIDIA_MODEL", "deepseek-ai/deepseek-v4-flash"),
)

import httpx  # noqa: E402
import openai  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

MODEL = os.getenv("SMOKE_MODEL", "gpt-4o-mini")

_results: list[bool] = []


def _report(name: str, ok: bool, detail: str = "") -> bool:
    _results.append(ok)
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)
    return ok


def _auth_headers() -> dict:
    if settings.relay_api_key:
        return {"Authorization": f"Bearer {settings.relay_api_key}"}
    return {}


def _sdk_client() -> openai.AsyncOpenAI:
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://relay-live-smoke",
    )
    return openai.AsyncOpenAI(
        base_url="http://relay-live-smoke/v1",
        api_key="smoke-key",
        http_client=http_client,
    )


async def _check_non_stream() -> None:
    client = _sdk_client()
    try:
        completion = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Reply with the word pong."}],
            max_tokens=8,
        )
    except Exception as exc:
        _report("non-stream completion", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    finally:
        await client.close()

    content = completion.choices[0].message.content or ""
    _report(
        "non-stream completion",
        bool(content),
        f"model={completion.model} id={completion.id} "
        f"finish={completion.choices[0].finish_reason} "
        f"content={content[:40]!r}",
    )


async def _check_stream() -> None:
    client = _sdk_client()
    try:
        chunks = []
        stream = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Count to three."}],
            max_tokens=8,
            stream=True,
        )
        async for chunk in stream:
            chunks.append(chunk)
    except Exception as exc:
        _report("streamed completion", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    finally:
        await client.close()

    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    ids = {c.id for c in chunks}
    _report(
        "streamed completion",
        bool(text),
        f"chunks={len(chunks)} stable_id={len(ids) == 1} "
        f"done={chunks[-1].choices[0].finish_reason} text={text[:40]!r}",
    )


async def _check_tool_call() -> None:
    client = _sdk_client()
    try:
        completion = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "user", "content": "What is the weather in Lyon?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Lyon"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"temp": 22, "sky": "clear"}',
                },
                {"role": "user", "content": "Summarize the weather."},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the weather for a city.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {"type": "string"},
                            },
                        },
                    },
                }
            ],
            tool_choice="auto",
            max_tokens=16,
        )
    except Exception as exc:
        _report("tool-call round trip", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    finally:
        await client.close()

    content = completion.choices[0].message.content or ""
    _report(
        "tool-call round trip",
        bool(content),
        f"finish={completion.choices[0].finish_reason} content={content[:40]!r}",
    )


def _check_native_chat(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "Reply with the word pong."}, headers=_auth_headers())
    if response.status_code == 200:
        body = response.json()
        _report(
            "native /chat",
            bool(body.get("response")),
            f"provider={body.get('provider')} model={body.get('model')} "
            f"response={str(body.get('response'))[:40]!r}",
        )
    else:
        _report("native /chat", False, f"status={response.status_code} body={response.text[:120]}")


def _check_models(client: TestClient) -> None:
    response = client.get("/v1/models", headers=_auth_headers())
    ok = response.status_code == 200
    detail = ""
    if ok:
        data = response.json().get("data", [])
        names = [entry.get("id") for entry in data]
        detail = f"count={len(names)}"
        if names:
            detail += f" sample={names[:3]}"
    else:
        detail = f"status={response.status_code} body={response.text[:120]}"
    _report("/v1/models", ok, detail)


def _check_health(client: TestClient) -> None:
    response = client.get("/health")
    _report(
        "public /health",
        response.status_code == 200,
        f"status={response.status_code}",
    )


def main() -> int:
    if not (settings.nvidia_api_key or settings.openai_api_key):
        print("No API keys configured in .env; nothing to smoke test.")
        return 1

    print(f"Live smoke against real NVIDIA/OpenAI endpoints (model={MODEL})")
    print(f"  NVIDIA enabled: {settings.nvidia_enabled} "
          f"| OpenAI enabled: {settings.openai_enabled}")

    with TestClient(fastapi_app) as client:
        _check_health(client)
        _check_models(client)
        _check_native_chat(client)
        asyncio.run(_check_non_stream())
        asyncio.run(_check_stream())
        asyncio.run(_check_tool_call())

    passed = sum(1 for ok in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} smoke steps passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
