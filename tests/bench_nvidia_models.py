"""
NVIDIA hosted-model benchmark harness (docs/nvidia-model-benchmark-plan.md §6–§15).

Standalone tooling for Phase 2 of the NVIDIA model evaluation. Not
collected by pytest (no ``test_`` prefix) and run directly:

    python tests/bench_nvidia_models.py --list
    python tests/bench_nvidia_models.py --probe
    python tests/bench_nvidia_models.py --run
    python tests/bench_nvidia_models.py --report

It talks to NVIDIA through the exact code path Relay uses (``NvidiaClient``
+ ``Provider`` from ``app.providers``), reads live keys from ``.env``, and
writes all runtime output under the git-ignored ``bench/`` directory
(``bench/raw/``, ``bench/probe.json``, ``bench/results.json``,
``bench/report.md``). It never writes to ``app/``, ``.env``, or
``PROJECT_LOG.md``.

Modes:
- ``--list``    print the live NVIDIA model catalog.
- ``--probe``   Phase 1 availability gate over the §5 candidate pool.
- ``--run``     Phase 2 benchmark of the selected ≤8 models over suites A–H.
- ``--report``  aggregate ``bench/results.json`` into ``bench/report.md``.
- ``--dry``     no network; fabricate transport responses end-to-end.

Hard constraints from the plan: read-only imports of ``app.*``, no write
path to any repo doc, raw results stored before any summary, and final
rankings presented to the user (no automatic ``PROJECT_LOG.md`` update).
"""

__version__ = "1"

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from pathlib import Path

# Make the project root importable regardless of how this script is run
# (same pattern as tests/run_live_smoke.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("PERSISTENCE_ENABLED", "false")

from app.core.config import settings  # noqa: E402
from app.providers.base import ModelProbe  # noqa: E402
from app.providers.exceptions import (  # noqa: E402
    ProviderError,
    ProviderHTTPError,
    ProviderTimeout,
)
from app.providers.nvidia_client import NvidiaClient  # noqa: E402
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = PROJECT_ROOT / "bench"
RAW_DIR = BENCH_DIR / "raw"
PROBE_PATH = BENCH_DIR / "probe.json"
RESULTS_PATH = BENCH_DIR / "results.json"
REPORT_PATH = BENCH_DIR / "report.md"
PROMPTS_PATH = BENCH_DIR / "prompts.json"

# Candidate pool for the probe phase (plan §5). Re-verified against the
# live catalog at run time; pool changes are recorded in the report.
CANDIDATE_POOL = {
    "coding": [
        "qwen/qwen3-coder-480b-a35b-instruct",
        "qwen/qwen2.5-coder-32b-instruct",
        "deepseek-ai/deepseek-v4-flash",
        "deepseek-ai/deepseek-v4-pro",
    ],
    "general": [
        "meta/llama-3.3-70b-instruct",
        "nvidia/nemotron-3-super-120b-a12b",
        "meta/llama-3.1-8b-instruct",
    ],
    "reasoning": [
        "deepseek-ai/deepseek-r1",
        "qwen/qwen3-next-80b-a3b-thinking",
        "qwen/qwq-32b",
        "nvidia/llama-3.1-nemotron-ultra-253b-v1",
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    ],
    "fast": [
        "meta/llama-3.2-3b-instruct",
        "qwen/qwen3-next-80b-a3b-instruct",
    ],
}

# Per-priority benchmark slots (plan §4) sum to the 8-model budget.
DEFAULT_SLOTS = {
    "coding": 3,
    "general": 2,
    "reasoning": 2,
    "fast": 1,
}
MAX_BENCHMARK_MODELS = 8
MAX_EXPANDED_MODELS = 12

SELECTION_ORDER = ["coding", "general", "reasoning", "fast"]
RECOMMEND_ORDER = ["general", "coding", "reasoning", "fast"]
SUITE_BY_PRIORITY = {"coding": "A", "general": "C", "reasoning": "B", "fast": "D"}

# Long-context suite F (32k/64k docs) dominates the token budget, so it is
# trimmed to a subset of models. Models in this map skip the listed suites.
SUITE_SKIP = {
    "meta/llama-3.1-8b-instruct": {"F"},
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": {"F"},
}

# Why a probe-accessible model was excluded from a given benchmark run.
# Reported verbatim in the Limitations section; keyed by model id.
EXCLUSION_REASONS = {
    "deepseek-ai/deepseek-v4-flash": (
        "excluded: sustained NVIDIA 529 overload during the benchmark window "
        "(5/5 probe 529 at the health gate)"
    ),
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": (
        "excluded: streams reasoning_content only (content=null) and exhausts the "
        "token budget before emitting visible text; unscorable at current budgets"
    ),
}

TEMPERATURE = 0.2
SEED = 42
DEFAULT_RUNS = 3
PACE_MIN_SECONDS = 0.5
RETRY_AFTER_CAP_SECONDS = 60.0
RECOVERY_PAUSE_SECONDS = 60.0
MAX_CONSECUTIVE_OVERLOADS = 5

RELIABILITY_FLOOR = 0.95
LATENCY_CAP_MS = 30_000

MALFORMED_MODEL_ID = "bench/definitely-not-a-real-model"

# Optional approximate pricing, tokens per 1k, USD. Leave empty when not
# measured; cost is only a tiebreaker and unknown prices are skipped.
PRICING_PER_1K = {}

# Tools shipped to suite E tasks. The reject task proves a model can
# refrain from calling them.
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Get the current weather for a city. Returns JSON with "
                "temp (number) and sky (string)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": (
                "Get the current stock price for a ticker symbol. Returns "
                "JSON with symbol and price."
            ),
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Transport: the real NvidiaClient/Provider path and a no-network fake.
# ---------------------------------------------------------------------------


class Transport:
    """Bound async transport over the exact code path Relay uses."""

    def __init__(self, provider):
        self.provider = provider
        self.client = NvidiaClient()

    async def list_models(self):
        return await self.client.alist_models(self.provider)

    async def probe(self, model):
        return await self.client.aprobe_model(self.provider, model)

    async def stream_chunks(self, payload):
        async for chunk in self.client.achat_stream_messages(self.provider, payload):
            yield chunk


class FakeTransport:
    """Deterministic no-network transport for ``--dry`` and offline tests."""

    async def list_models(self):
        return list(_flatten_pool())

    async def probe(self, model):
        return ModelProbe(True, 50, 200, "")

    async def stream_chunks(self, payload):
        if payload.get("model") == MALFORMED_MODEL_ID:
            raise ProviderHTTPError(404, "Function not found (fake)")
        text = _fake_text(payload)
        if text:
            mid = max(1, len(text) // 2)
            for part in (text[:mid], text[mid:]):
                if part:
                    yield _fake_chunk(payload, {"content": part})
        else:
            for chunk in _fake_tool_chunks(payload.get("model", "?"), payload):
                yield chunk
        yield _fake_chunk(payload, {}, finish_reason="stop")


def _fake_chunk(payload, delta, finish_reason=None):
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": payload.get("model"),
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _fake_text(payload):
    messages = payload.get("messages") or []
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return "Lyon is warmer than Paris."
    text = _last_user_message(payload) or ""
    lower = text.lower()
    if payload.get("tools") and "do not call any tools" not in lower and "from memory only" not in lower:
        return ""
    response_format = payload.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        return '{"title": "Inception", "year": 2010, "tags": ["sci-fi"]}'
    if response_format.get("type") == "json_object":
        return '{"name": "Ada", "age": 36, "city": "Paris"}'
    m = re.search(r"warehouse W-(\d+)", text)
    if m:
        return f"The serial number is BX-{m.group(1)}."
    if "RELAY-SIGNAL-OK" in text:
        return "RELAY-SIGNAL-OK: 5000 records are described in the document."
    if "keys title (string) and author (string)" in lower:
        return 'A great pick: {"title": "Dune", "author": "Frank Herbert"}.'
    if "PONG" in text and "capital of" not in lower:
        return "PONG"
    return f"This is a benchmark response about: {text[:60]}."


def _fake_tool_chunks(model, payload):
    text = _last_user_message(payload) or ""
    lower = text.lower()
    calls = [("get_weather", '{"city": "Lyon"}')]
    if "stock price" in lower:
        calls.append(("get_stock_price", '{"symbol": "AAPL"}'))
    for i, (name, args) in enumerate(calls):
        delta = {
            "tool_calls": [
                {
                    "index": i,
                    "id": f"call_fake{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
            ]
        }
        yield _fake_chunk(payload, delta)


def _transport(dry):
    if dry:
        return FakeTransport()
    definition = PROVIDER_REGISTRY["nvidia"]
    provider = definition.build_provider(api_key=settings.nvidia_api_key)
    return Transport(provider)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _flatten_pool():
    return [m for priority in SELECTION_ORDER for m in CANDIDATE_POOL[priority]]


def _last_user_message(payload):
    for msg in reversed(payload.get("messages") or []):
        if msg.get("role") == "user" and msg.get("content"):
            return msg["content"]
    return ""


def _load_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def _ensure_dirs():
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def _write_raw(model, suite_key, run_index, tasks, meta):
    directory = RAW_DIR / model
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{suite_key}-{run_index}.json"
    doc = {
        "meta": meta,
        "model": model,
        "suite": suite_key,
        "run_index": run_index,
        "tasks": tasks,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def percentile(values, q):
    """Linear-interpolated percentile over a list, or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _count_sentences(text):
    return max(1, len([p for p in re.split(r"[.!?]+", text) if p.strip()]))


def _is_refusal(text):
    lowered = text.lower()
    patterns = (
        "i'm sorry", "i am sorry", "i cannot", "i can't", "i won't",
        "as an ai", "i'm not able", "i am not able", "i'm unable",
        "i am unable", "i don't have",
    )
    return any(p in lowered for p in patterns)


# ---------------------------------------------------------------------------
# Probe (Phase 1 availability gate).
# ---------------------------------------------------------------------------


def classify_probe(probe, in_catalog):
    if not in_catalog:
        return "inaccessible", "not_in_catalog"
    if probe.healthy:
        return "accessible", ""
    if probe.status_code in (0, None):
        return "unstable", probe.error or "timeout/network"
    if probe.status_code in (429, 529, 500, 502, 503, 504):
        return "unstable", f"http_{probe.status_code}"
    return "inaccessible", f"http_{probe.status_code}"


async def run_probe(transport, pool):
    try:
        catalog = await transport.list_models()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    catalog_set = set(catalog)
    results = {}
    consecutive_overloads = 0

    for model in pool:
        if model not in catalog_set:
            results[model] = {
                "status": "inaccessible",
                "reason": "not_in_catalog",
                "latency_ms": None,
                "error": "",
            }
            continue

        probe = await transport.probe(model)
        status, reason = classify_probe(probe, True)
        results[model] = {
            "status": status,
            "reason": reason,
            "latency_ms": probe.latency_ms,
            "error": probe.error,
        }

        if probe.status_code in (429, 529):
            consecutive_overloads += 1
            await asyncio.sleep(2.0)
        else:
            consecutive_overloads = 0

        await asyncio.sleep(PACE_MIN_SECONDS)

        if consecutive_overloads > MAX_CONSECUTIVE_OVERLOADS:
            print("Probe aborted after >5 consecutive overloads; remaining models unprobed.")
            break

    return {"ok": True, "catalog_count": len(catalog), "results": results}


def _fabricate_probe_results():
    return {
        model: {"status": "accessible", "reason": "", "latency_ms": 50, "error": ""}
        for model in _flatten_pool()
    }


def _print_probe_summary(doc):
    print(f"catalog size: {doc['catalog_count']}")
    for model, p in sorted(doc["models"].items()):
        status = p.get("status")
        detail = p.get("reason") or p.get("error") or ""
        latency = f"{p.get('latency_ms')}ms" if p.get("latency_ms") is not None else "-"
        print(f"  {status:<12} {model}  ({latency}) {detail}")


# ---------------------------------------------------------------------------
# Selection (plan §4).
# ---------------------------------------------------------------------------


def select_benchmark_set(probe_results, slots=None, max_models=MAX_BENCHMARK_MODELS):
    """Choose ≤max_models accessible models, covering every priority."""
    slots = dict(DEFAULT_SLOTS if slots is None else slots)
    accessible = {
        model for model, p in probe_results.items() if p.get("status") == "accessible"
    }
    chosen = []
    for priority in SELECTION_ORDER:
        for model in CANDIDATE_POOL[priority]:
            if model in accessible and slots.get(priority, 0) > 0:
                chosen.append(model)
                slots[priority] -= 1
    for priority in SELECTION_ORDER:
        for model in CANDIDATE_POOL[priority]:
            if model in accessible and model not in chosen:
                chosen.append(model)
    return chosen[:max_models]


def expand_selection(probe_results, chosen, max_models=MAX_EXPANDED_MODELS):
    """Confirmation-batch expansion for unclear results; never >12 total."""
    selected = set(chosen)
    extra = []
    for priority in SELECTION_ORDER:
        for model in CANDIDATE_POOL[priority]:
            if (
                model not in selected
                and probe_results.get(model, {}).get("status") == "accessible"
            ):
                extra.append(model)
    return (chosen + extra)[:max_models]


def _build_selection(probe_results, args):
    if args.only:
        chosen = []
        for model in args.only:
            if model not in probe_results:
                print(f"warning: {model} not in probe results; benchmarking anyway")
            chosen.append(model)
        return chosen[:MAX_EXPANDED_MODELS], ["forced --only selection"]
    chosen = select_benchmark_set(probe_results)
    notes = []
    if args.expand:
        chosen = expand_selection(probe_results, chosen)
        notes.append(f"expanded to {len(chosen)} models: {args.expand}")
    return chosen, notes


# ---------------------------------------------------------------------------
# Payload building.
# ---------------------------------------------------------------------------


def build_payload(model, suite_key, task_id, task_spec, suite, meta):
    payload = {
        "model": model,
        "temperature": meta.get("temperature", TEMPERATURE),
        "max_tokens": suite["max_tokens"],
        "seed": meta.get("seed", SEED),
        "stream": True,
    }
    if suite_key == "E":
        payload["messages"] = [{"role": "user", "content": task_spec["prompt"]}]
        payload["tools"] = TOOL_DEFINITIONS
        payload["tool_choice"] = "auto"
        return payload
    if suite_key == "F":
        doc, count = build_long_doc(task_spec)
        payload["messages"] = build_f_messages(task_spec, doc)
        payload["_doc_count"] = count
        return payload
    if suite_key == "G":
        payload["messages"] = [{"role": "user", "content": task_spec["prompt"]}]
        if task_id == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": task_spec.get("schema_name", "structured_output"),
                    "strict": True,
                    "schema": task_spec["schema"],
                },
            }
        elif task_id == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload
    payload["messages"] = [{"role": "user", "content": task_spec["prompt"]}]
    return payload


def build_long_doc(task_spec):
    """Deterministic synthetic document sized to a token budget."""
    block = task_spec["block"]
    target = task_spec.get("target_tokens", 32000)
    estimate = block.format(index=99999, serial="BX-99999")
    per_block = max(1, math.ceil(len(estimate) / 4.0))
    count = max(2, target // per_block)
    lines = [block.format(index=i, serial=f"BX-{i}") for i in range(1, count + 1)]
    return "\n".join(lines), count


def build_f_messages(task_spec, doc):
    tail = task_spec.get("question") or task_spec.get("instruction") or ""
    return [{"role": "user", "content": doc + "\n\n" + tail}]


# ---------------------------------------------------------------------------
# Streaming executor.
# ---------------------------------------------------------------------------


def _accumulate_tool_calls(chunk, tool_calls):
    for choice in chunk.get("choices", []):
        delta = choice.get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            while len(tool_calls) <= index:
                tool_calls.append(
                    {"id": None, "type": None, "function": {"name": None, "arguments": ""}}
                )
            slot = tool_calls[index]
            if tc.get("id"):
                slot["id"] = tc["id"]
            if tc.get("type"):
                slot["type"] = tc["type"]
            function = tc.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] = function["name"]
            if function.get("arguments"):
                slot["function"]["arguments"] += function["arguments"]


def _tool_result(tool_call):
    name = (tool_call.get("function") or {}).get("name")
    if name == "get_stock_price":
        return '{"symbol": "AAPL", "price": 212}'
    return '{"temp": 22, "sky": "sunny"}'


async def _stream_once(transport, payload):
    """Stream one payload; return timing/text/tool-call fields. Raises on error."""
    start = time.perf_counter()
    ttft_ms = None
    first_content_ms = None
    chunk_count = 0
    content_parts = []
    tool_calls = []
    finish_reason = None
    stream_ids = []
    usage = None

    async for chunk in transport.stream_chunks(payload):
        if ttft_ms is None:
            ttft_ms = (time.perf_counter() - start) * 1000
        chunk_count += 1
        chunk_id = chunk.get("id")
        if chunk_id and chunk_id not in stream_ids:
            stream_ids.append(chunk_id)
        if chunk.get("usage"):
            usage = chunk.get("usage")
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
                if first_content_ms is None:
                    first_content_ms = (time.perf_counter() - start) * 1000
            if delta.get("tool_calls"):
                _accumulate_tool_calls(chunk, tool_calls)
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    total_ms = (time.perf_counter() - start) * 1000
    output_text = "".join(content_parts)
    usage = usage or {}
    return {
        "ttft_ms": ttft_ms,
        "first_content_ms": first_content_ms,
        "total_ms": total_ms,
        "chunk_count": chunk_count,
        "stream_completed": True,
        "output_text": output_text,
        "tool_calls": tool_calls or None,
        "finish_reason": finish_reason,
        "stream_stable_id": len(set(stream_ids)) <= 1,
        "usage": usage,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


async def run_multi_turn(transport, model, payload):
    """Drive a 2–3 step tool-using conversation and aggregate its steps."""
    start = time.perf_counter()
    messages = list(payload.get("messages", []))
    base = {k: v for k, v in payload.items() if k != "messages"}
    step_fields = []
    tool_calls_used = 0

    for step in range(3):
        fields = await _stream_once(transport, {**base, "messages": messages})
        step_fields.append(fields)
        calls = fields.get("tool_calls") or []
        if not calls:
            break
        tool_calls_used += len(calls)
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": calls[0].get("id") or f"call_{step}",
                "content": _tool_result(calls[0]),
            }
        )

    total_ms = (time.perf_counter() - start) * 1000
    return {
        "ttft_ms": step_fields[0].get("ttft_ms") if step_fields else None,
        "first_content_ms": step_fields[0].get("first_content_ms") if step_fields else None,
        "total_ms": total_ms,
        "chunk_count": sum(f.get("chunk_count", 0) for f in step_fields),
        "stream_completed": all(f.get("stream_completed", False) for f in step_fields),
        "stream_stable_id": all(f.get("stream_stable_id", True) for f in step_fields),
        "output_text": step_fields[-1].get("output_text") or "" if step_fields else "",
        "tool_calls": step_fields[0].get("tool_calls"),
        "tool_calls_used": tool_calls_used,
        "steps": len(step_fields),
        "finish_reason": step_fields[-1].get("finish_reason") if step_fields else None,
        "usage": None,
        "input_tokens": None,
        "output_tokens": None,
    }


def _error_class(exc):
    if isinstance(exc, ProviderTimeout):
        return "timeout"
    if isinstance(exc, ProviderHTTPError):
        status = exc.status_code or 0
        if status == 0:
            return "network"
        if status == 404:
            return "http_404"
        if status in (429, 529):
            return f"overload_{status}"
        if 500 <= status <= 599:
            return "http_5xx"
        if 400 <= status <= 499:
            return "http_4xx"
        return f"http_{status}"
    if isinstance(exc, ProviderError):
        return "provider"
    return "network"


def _exception_fields(exc, start_wall):
    return {
        "error_class": _error_class(exc),
        "error": getattr(exc, "message", str(exc)),
        "http_status": getattr(exc, "status_code", None),
        "total_ms": (time.perf_counter() - start_wall) * 1000,
    }


def _est_cost_usd(input_tokens, output_tokens, model):
    price = PRICING_PER_1K.get(model)
    if not price:
        return None
    cost = (input_tokens or 0) / 1000.0 * price.get("input", 0.0)
    cost += (output_tokens or 0) / 1000.0 * price.get("output", 0.0)
    return cost


async def run_one(transport, model, suite_key, task_id, task_spec, suite, meta, run_index):
    payload = build_payload(model, suite_key, task_id, task_spec, suite, meta)
    expected_count = payload.pop("_doc_count", None)
    start_wall = time.perf_counter()

    try:
        if suite_key == "H" and task_id == "malformed_request":
            payload = dict(payload)
            payload["model"] = MALFORMED_MODEL_ID
        if suite_key == "E" and task_id == "multi_turn":
            fields = await run_multi_turn(transport, model, payload)
        else:
            fields = await _stream_once(transport, payload)
    except ProviderHTTPError as exc:
        fields = _exception_fields(exc, start_wall)
        if exc.status_code in (429, 529):
            fields["status"] = "overload"
            fields["retry_after"] = exc.retry_after
            fields["error_class"] = f"overload_{exc.status_code}"
    except ProviderTimeout as exc:
        fields = {
            "error_class": "timeout",
            "error": str(exc),
            "http_status": None,
            "total_ms": (time.perf_counter() - start_wall) * 1000,
        }
    except ProviderError as exc:
        fields = {
            "error_class": "provider",
            "error": str(exc),
            "http_status": None,
            "total_ms": (time.perf_counter() - start_wall) * 1000,
        }
    except Exception as exc:
        fields = {
            "error_class": "network",
            "error": f"{type(exc).__name__}: {exc}",
            "http_status": None,
            "total_ms": (time.perf_counter() - start_wall) * 1000,
        }

    if fields.get("status") == "overload":
        return {
            "model": model,
            "suite": suite_key,
            "task": task_id,
            "run_index": run_index,
            "success": False,
            "http_status": fields.get("http_status"),
            "error_class": fields.get("error_class"),
            "error": fields.get("error", ""),
            "status": "overload",
            "retry_after": fields.get("retry_after"),
            "total_ms": fields.get("total_ms"),
        }

    output_text = fields.get("output_text", "") or ""
    tool_calls = fields.get("tool_calls") or []
    intentional_error = suite_key == "H" and task_id == "malformed_request"

    if fields.get("error_class"):
        success = False
    elif intentional_error:
        success = False
    else:
        success = bool(output_text.strip() or tool_calls or fields.get("tool_calls_used"))

    usage = fields.get("usage") or {}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if output_tokens is None and output_text:
        output_tokens = max(1, len(output_text) // 4)
    total_ms = fields.get("total_ms") or ((time.perf_counter() - start_wall) * 1000)
    tokens_per_sec = (
        output_tokens / (total_ms / 1000.0)
        if output_tokens and total_ms > 0
        else None
    )

    rec = {
        "model": model,
        "suite": suite_key,
        "task": task_id,
        "run_index": run_index,
        "success": success,
        "http_status": fields.get("http_status"),
        "error_class": fields.get("error_class"),
        "error": fields.get("error", ""),
        "ttft_ms": fields.get("ttft_ms"),
        "first_content_ms": fields.get("first_content_ms"),
        "total_ms": total_ms,
        "chunk_count": fields.get("chunk_count"),
        "stream_completed": bool(fields.get("stream_completed", False)),
        "stream_stable_id": bool(fields.get("stream_stable_id", False)),
        "finish_reason": fields.get("finish_reason"),
        "output_text": output_text,
        "tool_calls": tool_calls or None,
        "tool_calls_used": fields.get("tool_calls_used", 0),
        "steps": fields.get("steps"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_per_sec": tokens_per_sec,
        "est_cost_usd": _est_cost_usd(input_tokens, output_tokens, model),
        "expected_count": expected_count,
        "intentional_error": intentional_error,
        "checks": {},
        "auto_rubric": 0,
    }

    rubric = auto_score(suite_key, task_id, task_spec, rec)
    rec["checks"] = rubric["checks"]
    rec["auto_rubric"] = rubric["score"]
    return rec


async def _pace(state):
    if os.getenv("BENCH_NO_PACE") == "1":
        return
    now = time.monotonic()
    elapsed = now - state.get("last_request_time", 0.0)
    wait = PACE_MIN_SECONDS - elapsed
    if wait > 0:
        await asyncio.sleep(wait)
    state["last_request_time"] = time.monotonic()


def _overload_delay(rec, state):
    retry_after = rec.get("retry_after")
    if retry_after and retry_after > 0:
        return min(float(retry_after), RETRY_AFTER_CAP_SECONDS)
    return 1.0


async def run_one_with_retry(transport, model, suite_key, task_id, task_spec, suite, meta, run_index, state):
    rec = await run_one(transport, model, suite_key, task_id, task_spec, suite, meta, run_index)
    if rec.get("status") != "overload":
        state["consecutive_overloads"] = 0
        return rec

    state["consecutive_overloads"] += 1
    state["overload_events"].append(
        {"suite": suite_key, "task": task_id, "status": rec.get("http_status")}
    )
    await asyncio.sleep(_overload_delay(rec, state))

    if state["consecutive_overloads"] > MAX_CONSECUTIVE_OVERLOADS:
        state["consecutive_overloads"] = 0
        state["recovery_pauses"] += 1
        await asyncio.sleep(RECOVERY_PAUSE_SECONDS)

    rec = await run_one(transport, model, suite_key, task_id, task_spec, suite, meta, run_index)
    rec["retried_once"] = True
    if rec.get("status") != "overload":
        state["consecutive_overloads"] = 0
    return rec


async def run_model(transport, model, prompts, suite_keys, runs, state):
    meta = prompts.get("meta", {})
    result = {}
    effective_suites = [s for s in suite_keys if s not in SUITE_SKIP.get(model, set())]
    for suite_key in effective_suites:
        suite = prompts["suites"][suite_key]
        result[suite_key] = {}
        run_batches = {}
        for task_id, task_spec in suite["tasks"].items():
            result[suite_key][task_id] = []
            for run_index in range(1, runs + 1):
                rec = await run_one_with_retry(
                    transport, model, suite_key, task_id, task_spec, suite, meta, run_index, state
                )
                result[suite_key][task_id].append(rec)
                run_batches.setdefault(run_index, {})[task_id] = rec
                _write_raw(model, suite_key, run_index, run_batches[run_index], meta)
                await _pace(state)
    return result


async def run_benchmark(transport, selection, prompts, options, probe_results=None, expansion_reason=None):
    suite_keys = options["suites"]
    runs = options["runs"]
    state = {
        "consecutive_overloads": 0,
        "recovery_pauses": 0,
        "overload_events": [],
        "last_request_time": -PACE_MIN_SECONDS,
    }
    model_results = {}
    for model in selection:
        print(f"benchmarking {model} ...")
        model_results[model] = await run_model(transport, model, prompts, suite_keys, runs, state)

    return {
        "meta": {
            "generated_at_utc": _utc_now(),
            "harness_version": __version__,
            "prompts_revision": prompts.get("meta", {}).get("revision"),
            "temperature": prompts.get("meta", {}).get("temperature"),
            "seed": prompts.get("meta", {}).get("seed"),
            "runs_per_task": runs,
            "models_selected": list(selection),
            "expansion_reason": expansion_reason,
            "suites_run": {
                m: [s for s in suite_keys if s not in SUITE_SKIP.get(m, set())]
                for m in selection
            },
            "recovery_pauses": state["recovery_pauses"],
            "overload_events": state["overload_events"],
            "pricing_table": dict(PRICING_PER_1K),
        },
        "probe": probe_results or {},
        "runs": model_results,
    }


# ---------------------------------------------------------------------------
# Deterministic rubric (plan §9). Returns (score 0–5, checks).
# ---------------------------------------------------------------------------


def auto_score(suite_key, task_id, task_spec, rec):
    text = (rec.get("output_text") or "").strip()
    tool_calls = rec.get("tool_calls") or []
    checks = {}

    if rec.get("error_class") and not (suite_key == "H" and task_id == "malformed_request"):
        return {"score": 0, "checks": {"http_error": True}}
    if (
        not text
        and not tool_calls
        and not (suite_key == "E" and task_id == "multi_turn")
        and not (suite_key == "H" and task_id == "malformed_request")
    ):
        return {"score": 0, "checks": {"empty": True}}
    if _is_refusal(text):
        return {"score": 0, "checks": {"refusal": True}}

    if suite_key == "A":
        return _score_coding(task_id, task_spec, text, checks)
    if suite_key == "B":
        return _score_reasoning(task_id, task_spec, text, checks)
    if suite_key == "C":
        return _score_general(task_id, task_spec, text, checks)
    if suite_key == "D":
        return {"score": 5, "checks": {"non_empty": True}}
    if suite_key == "E":
        return _score_tool(task_id, task_spec, text, tool_calls, rec, checks)
    if suite_key == "F":
        return _score_long_context(task_id, task_spec, text, checks)
    if suite_key == "G":
        return _score_json(task_id, task_spec, text, checks)
    if suite_key == "H":
        return _score_stream(task_id, rec, checks)
    return {"score": 3, "checks": checks}


def _score_coding(task_id, task_spec, text, checks):
    marker = task_spec.get("marker", "")
    checks["has_code"] = "```" in text or "def " in text
    if task_id == "codegen":
        checks["has_def"] = "def " in text
        checks["has_marker"] = marker in text
        score = 3 + int(checks["has_def"]) + int(checks["has_marker"])
    elif task_id == "debugging":
        checks["explains_fix"] = "fix" in text.lower() or "root cause" in text.lower()
        checks["mentions_error"] = "indexerror" in text.lower() or "range(" in text.lower()
        score = 3 + int(checks["explains_fix"]) + int(checks["mentions_error"])
    elif task_id == "refactor":
        checks["keeps_signature"] = marker in text
        checks["has_def"] = "def " in text
        score = 3 + int(checks["keeps_signature"]) + int(checks["has_def"])
    else:  # unit_tests
        checks["has_test"] = "def test_" in text
        checks["imports_pytest"] = "pytest" in text or "import " in text
        score = 3 + int(checks["has_test"]) + int(checks["imports_pytest"])
    return {"score": min(5, score), "checks": checks}


def _score_reasoning(task_id, task_spec, text, checks):
    if task_id == "math":
        checks["has_answer_line"] = "answer:" in text.lower()
        checks["has_unit"] = "km" in text.lower()
        score = 3 + int(checks["has_answer_line"]) + int(checks["has_unit"])
    elif task_id == "concept":
        checks["mentions_mutex"] = "mutex" in text.lower()
        checks["mentions_lost_updates"] = "lost" in text.lower()
        score = 3 + int(checks["mentions_mutex"]) + int(checks["mentions_lost_updates"])
    elif task_id == "check_student_work":
        checks["corrects_answer"] = "x = 4" in text.lower() or "x=4" in text.lower()
        checks["identifies_error"] = any(
            w in text.lower() for w in ("wrong", "error", "incorrect")
        )
        score = 3 + int(checks["corrects_answer"]) + int(checks["identifies_error"])
    else:  # logic
        checks["mentions_oranges"] = "oranges" in text.lower()
        score = 3 + (2 if checks["mentions_oranges"] else 0)
    return {"score": min(5, score), "checks": checks}


def _score_general(task_id, task_spec, text, checks):
    exact = task_spec.get("exact")
    if exact is not None:
        checks["exact_match"] = text.strip() == exact
        return {"score": 5 if checks["exact_match"] else 3, "checks": checks}
    expected = task_spec.get("expected")
    if expected:
        checks["expected_found"] = any(str(v).lower() in text.lower() for v in expected)
        return {"score": 5 if checks["expected_found"] else 3, "checks": checks}
    if task_id == "summarization":
        checks["short"] = 1 <= _count_sentences(text) <= 3
        return {"score": 3 + (2 if checks["short"] else 0), "checks": checks}
    if task_id == "tone":
        checks["no_slang"] = (
            "kinda" not in text.lower()
            and "u " not in text.lower()
            and "fix it fast" not in text.lower()
        )
        checks["single_sentence"] = _count_sentences(text) <= 1
        score = 3 + int(checks["no_slang"]) + int(checks["single_sentence"])
        return {"score": min(5, score), "checks": checks}
    return {"score": 3, "checks": checks}


def _score_tool(task_id, task_spec, text, tool_calls, rec, checks):
    names = [tc.get("function", {}).get("name") for tc in tool_calls]
    if task_id == "reject":
        checks["no_tool_call"] = not tool_calls
        checks["has_text"] = bool(text)
        if checks["no_tool_call"] and checks["has_text"]:
            return {"score": 5, "checks": checks}
        return {"score": 0 if tool_calls else 1, "checks": checks}
    if task_id == "parallel":
        checks["two_tool_calls"] = len(tool_calls) >= 2
        score = 5 if checks["two_tool_calls"] else (3 if len(tool_calls) == 1 else 1)
        return {"score": score, "checks": checks}
    if task_id == "multi_turn":
        checks["tool_used"] = rec.get("tool_calls_used", 0) >= 1
        checks["final_text"] = bool(text)
        if checks["tool_used"] and checks["final_text"]:
            return {"score": 5, "checks": checks}
        return {"score": 3 if checks["tool_used"] else 1, "checks": checks}
    expected = task_spec.get("tool", "get_weather")
    checks["correct_tool"] = names == [expected]
    score = 5 if checks["correct_tool"] else (2 if names else 1)
    return {"score": score, "checks": checks}


def _score_long_context(task_id, task_spec, text, checks):
    expected = task_spec.get("expected")
    prefix = task_spec.get("expected_prefix")
    if expected:
        checks["expected_found"] = any(str(v).lower() in text.lower() for v in expected)
    elif prefix:
        checks["expected_found"] = text.strip().startswith(prefix)
    else:
        checks["expected_found"] = bool(text)
    return {"score": 5 if checks["expected_found"] else 2, "checks": checks}


def _extract_json_object(text):
    stripped = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "")
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return stripped[start:end + 1]


def _json_schema_types_ok(data, schema):
    if not schema:
        return True
    for key, spec in (schema.get("properties") or {}).items():
        if key not in data:
            continue
        expected_type = spec.get("type")
        if expected_type == "integer" and not isinstance(data[key], int):
            return False
        if expected_type == "string" and not isinstance(data[key], str):
            return False
        if expected_type == "array" and not isinstance(data[key], list):
            return False
    return True


def _score_json(task_id, task_spec, text, checks):
    raw = _extract_json_object(text)
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
    keys = task_spec.get("schema_keys", [])
    if data is None:
        checks["json_parses"] = False
        checks["schema_conformant"] = False
        return {"score": 1 if text else 0, "checks": checks}
    missing = [k for k in keys if k not in data]
    checks["json_parses"] = True
    checks["schema_conformant"] = not missing
    if task_id == "json_schema":
        checks["schema_conformant"] = (
            checks["schema_conformant"]
            and _json_schema_types_ok(data, task_spec.get("schema"))
        )
    return {"score": 5 if checks["schema_conformant"] else 3, "checks": checks}


def _score_stream(task_id, rec, checks):
    if task_id == "malformed_request":
        error_class = rec.get("error_class") or ""
        checks["error_shape_ok"] = (
            error_class.startswith("http_") and (rec.get("http_status") or 0) >= 400
        )
        if checks["error_shape_ok"]:
            return {"score": 5, "checks": checks}
        return {"score": 1 if error_class == "timeout" else 0, "checks": checks}
    checks["completed"] = bool(rec.get("stream_completed"))
    checks["stable_id"] = bool(rec.get("stream_stable_id"))
    if checks["completed"] and checks["stable_id"]:
        return {"score": 5, "checks": checks}
    return {"score": 1 if not checks["completed"] else 3, "checks": checks}


# ---------------------------------------------------------------------------
# Aggregation and recommendation (plan §10–§11).
# ---------------------------------------------------------------------------


def priority_stats(model_runs, suite_key):
    runs = []
    for task_runs in (model_runs.get(suite_key) or {}).values():
        runs.extend(task_runs)
    if not runs:
        return {
            "quality": 0.0,
            "latency_p50_ms": None,
            "latency_p90_ms": None,
            "reliability": 0.0,
            "runs": 0,
        }
    scores = [int(r.get("auto_rubric") or 0) for r in runs]
    quality = (sum(scores) / len(scores)) * 20
    totals = [r["total_ms"] for r in runs if r.get("total_ms") is not None]
    successes = sum(1 for r in runs if r.get("success"))
    reliability = successes / len(runs)
    return {
        "quality": quality,
        "latency_p50_ms": percentile(totals, 50),
        "latency_p90_ms": percentile(totals, 90),
        "reliability": reliability,
        "runs": len(runs),
    }


def _model_cost(model_runs):
    total = 0.0
    seen = False
    for task_runs in model_runs.values():
        for runs in task_runs.values():
            for rec in runs:
                cost = rec.get("est_cost_usd")
                if cost is not None:
                    total += cost
                    seen = True
    return total if seen else None


def gate_tool(model_runs):
    E = model_runs.get("E") or {}
    single = all(
        bool(rec.get("tool_calls"))
        and rec["tool_calls"][0].get("function", {}).get("name") == "get_weather"
        for rec in E.get("single_tool", [])
    )
    parallel = all(len(rec.get("tool_calls") or []) >= 2 for rec in E.get("parallel", []))
    reject = all(
        not rec.get("tool_calls") and bool((rec.get("output_text") or "").strip())
        for rec in E.get("reject", [])
    )
    multi = all(rec.get("tool_calls_used", 0) >= 1 for rec in E.get("multi_turn", []))
    return "pass" if (single and parallel and reject and multi) else "fail"


def gate_long_context(model_runs):
    runs = []
    for task_runs in (model_runs.get("F") or {}).values():
        runs.extend(task_runs)
    if not runs:
        return "fail"
    return "pass" if all(r.get("checks", {}).get("expected_found") for r in runs) else "fail"


def gate_json(model_runs):
    runs = []
    for task_runs in (model_runs.get("G") or {}).values():
        runs.extend(task_runs)
    if not runs:
        return "fail"
    parse_rate = sum(1 for r in runs if r.get("checks", {}).get("json_parses")) / len(runs)
    conform_rate = sum(
        1 for r in runs if r.get("checks", {}).get("schema_conformant")
    ) / len(runs)
    return "pass" if parse_rate >= 0.9 and conform_rate >= 0.9 else "fail"


def gate_stream(model_runs):
    H = model_runs.get("H") or {}
    checks = []
    for task in ("completion", "stable_id"):
        for rec in H.get(task, []):
            checks.append(bool(rec.get("stream_completed")) and bool(rec.get("stream_stable_id")))
    for rec in H.get("malformed_request", []):
        checks.append(bool(rec.get("checks", {}).get("error_shape_ok")))
    if not checks:
        return "fail"
    return "pass" if all(checks) else "fail"


def gate_results(model_runs):
    gates = {}
    if model_runs.get("E"):
        gates["E"] = gate_tool(model_runs)
    if model_runs.get("F"):
        gates["F"] = gate_long_context(model_runs)
    if model_runs.get("G"):
        gates["G"] = gate_json(model_runs)
    if model_runs.get("H"):
        gates["H"] = gate_stream(model_runs)
    return gates


def build_aggregate(results):
    aggregate = {}
    for priority in RECOMMEND_ORDER:
        suite_key = SUITE_BY_PRIORITY[priority]
        stats = {}
        for model, model_runs in (results.get("runs") or {}).items():
            entry = priority_stats(model_runs, suite_key)
            entry["cost"] = _model_cost(model_runs)
            entry["gates"] = gate_results(model_runs)
            stats[model] = entry
        best = min(
            (s["latency_p50_ms"] for s in stats.values() if s["latency_p50_ms"]),
            default=None,
        )
        for entry in stats.values():
            if best and entry["latency_p50_ms"]:
                entry["latency_score"] = min(100.0, 100.0 * best / entry["latency_p50_ms"])
            else:
                entry["latency_score"] = 0.0
            entry["composite"] = (
                0.6 * entry["quality"]
                + 0.25 * entry["latency_score"]
                + 0.15 * entry["reliability"] * 100
            )
        aggregate[priority] = stats
    return aggregate


def recommend(aggregate):
    recommendation = {}
    notes = []
    for priority in RECOMMEND_ORDER:
        entries = aggregate.get(priority) or {}
        if not entries:
            recommendation[priority] = None
            notes.append(f"{priority}: no models benchmarked")
            continue
        eligible = [
            m
            for m, s in entries.items()
            if s["reliability"] >= RELIABILITY_FLOOR
            and (s["latency_p90_ms"] is None or s["latency_p90_ms"] <= LATENCY_CAP_MS)
        ]
        if not eligible:
            eligible = list(entries)
            notes.append(
                f"{priority}: no model met the reliability floor "
                f"({RELIABILITY_FLOOR:.0%}) / latency cap "
                f"({LATENCY_CAP_MS / 1000.0:.0f}s); fell back to best composite"
            )
        ranked = sorted(
            eligible,
            key=lambda m: (entries[m]["composite"], -(entries[m]["cost"] or 0.0)),
            reverse=True,
        )
        chosen = None
        for model in ranked:
            gates = entries[model].get("gates") or {}
            if all(v == "pass" for v in gates.values()):
                chosen = model
                break
        if chosen is None:
            chosen = ranked[0]
            notes.append(f"{priority}: no gate-clean model; kept {chosen} with gate warnings")
        recommendation[priority] = chosen
    return recommendation, notes


def format_recommendation(recommendation):
    lines = ["NVIDIA_MODEL_PRIORITY:"]
    for key, label in (
        ("general", "default_general"),
        ("coding", "coding"),
        ("reasoning", "reasoning"),
        ("fast", "fast"),
    ):
        lines.append(f"  - {recommendation.get(key) or '(none)'}   # {label}")
    return "\n".join(lines)


def build_report(results, aggregate, recommendation, notes):
    meta = results.get("meta", {})
    lines = ["# NVIDIA Model Benchmark Report", ""]
    lines.append(f"- Generated: {meta.get('generated_at_utc', 'unknown')}")
    lines.append(
        f"- Harness version {meta.get('harness_version', '?')} · "
        f"prompts revision {meta.get('prompts_revision', '?')}"
    )
    lines.append(
        f"- Temperature {meta.get('temperature')} · seed {meta.get('seed')} · "
        f"runs/task {meta.get('runs_per_task')}"
    )
    lines.append(
        f"- Models benchmarked: {', '.join(meta.get('models_selected', []) or ['(none)'])}"
    )
    if meta.get("expansion_reason"):
        lines.append(f"- Expanded beyond 8 models — reason: {meta['expansion_reason']}")
    if meta.get("recovery_pauses"):
        lines.append(f"- Overload recovery pauses: {meta['recovery_pauses']}")
    suites_run = meta.get("suites_run") or {}
    if suites_run:
        all_suites = set()
        for _model, suites in suites_run.items():
            all_suites.update(suites)
        skipped = {
            model: sorted(all_suites.difference(set(suites)))
            for model, suites in suites_run.items()
        }
        if any(skipped.values()):
            lines.append(
                "- Suite trim: " + "; ".join(
                    f"{m} skipped {','.join(sorted(s))}"
                    for m, s in skipped.items() if s
                )
            )
    lines.append("")

    lines.append("## Availability probe")
    lines.append("")
    lines.append("| model | status | detail |")
    lines.append("| --- | --- | --- |")
    for model, p in sorted((results.get("probe") or {}).items()):
        detail = p.get("reason") or p.get("error") or ""
        lines.append(f"| `{model}` | {p.get('status')} | {detail} |")
    lines.append("")

    lines.append("## Per-suite results")
    lines.append("")
    lines.append("| model | suite | tasks | runs ok | mean rubric /5 | p50 ms | p90 ms | mean tok/s |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for model in meta.get("models_selected", []):
        model_runs = (results.get("runs") or {}).get(model) or {}
        for suite_key in sorted(model_runs):
            tasks = model_runs[suite_key] or {}
            runs = [rec for recs in tasks.values() for rec in recs]
            ok = sum(1 for r in runs if r.get("success"))
            rubric = [r.get("auto_rubric", 0) for r in runs]
            mean_rubric = sum(rubric) / len(rubric) if rubric else 0.0
            lat = [r.get("total_ms") for r in runs if r.get("total_ms")]
            tps = [r.get("tokens_per_sec") for r in runs if r.get("tokens_per_sec")]
            p50 = percentile(lat, 50)
            p90 = percentile(lat, 90)
            mean_tps = sum(tps) / len(tps) if tps else None
            p50_s = f"{p50:.0f}" if p50 is not None else "—"
            p90_s = f"{p90:.0f}" if p90 is not None else "—"
            tps_s = f"{mean_tps:.1f}" if mean_tps is not None else "—"
            lines.append(
                f"| `{model}` | {suite_key} | {len(runs)} | {ok} | {mean_rubric:.2f} | "
                f"{p50_s} | {p90_s} | {tps_s} |"
            )
    lines.append("")

    lines.append("## Per-priority composites")
    lines.append("")
    lines.append("Composite = 60% quality + 25% latency + 15% reliability (0–100).")
    lines.append("")
    for priority in RECOMMEND_ORDER:
        suite_key = SUITE_BY_PRIORITY[priority]
        lines.append(f"### {priority} (suite {suite_key})")
        lines.append("")
        lines.append("| model | composite | quality | p50 ms | p90 ms | reliability | cost | gates E–H |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        stats = aggregate.get(priority, {})
        for model in sorted(stats, key=lambda m: stats[m]["composite"], reverse=True):
            s = stats[model]
            gate_str = "/".join(
                f"{k}:{v[0].upper()}" for k, v in (s.get("gates") or {}).items()
            )
            cost = f"${s.get('cost', 0.0):.4f}" if s.get("cost") else "—"
            p50 = f"{s['latency_p50_ms']:.0f}" if s.get("latency_p50_ms") else "—"
            p90 = f"{s['latency_p90_ms']:.0f}" if s.get("latency_p90_ms") else "—"
            lines.append(
                f"| `{model}` | {s['composite']:.1f} | {s['quality']:.1f} | "
                f"{p50} | {p90} | {s['reliability'] * 100:.1f}% | {cost} | {gate_str} |"
            )
        lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append("```")
    lines.append(format_recommendation(recommendation))
    lines.append("```")
    lines.append("")
    if recommendation.get("general"):
        lines.append(f"- `TASK_GENERAL={recommendation['general']}`")
    if recommendation.get("coding"):
        lines.append(f"- `TASK_CODING={recommendation['coding']}`")
    if recommendation.get("reasoning"):
        lines.append(f"- `TASK_REASONING={recommendation['reasoning']}`")
    if recommendation.get("fast"):
        lines.append(f"- fast fallback: `{recommendation['fast']}`")
    lines.append("")

    lines.append("## Limitations")
    lines.append("")
    models_selected = set(meta.get("models_selected") or [])
    excluded = sorted(
        m for m, p in (results.get("probe") or {}).items()
        if p.get("status") == "accessible" and m not in models_selected
    )
    for model in excluded:
        reason = EXCLUSION_REASONS.get(model) or "excluded; not benchmarked"
        lines.append(f"- `{model}` — {reason}")
    for model in meta.get("models_selected", []):
        model_runs = (results.get("runs") or {}).get(model) or {}
        gates = gate_results(model_runs)
        if model == "meta/llama-3.1-8b-instruct" and gates.get("E") != "pass":
            lines.append(
                "- `meta/llama-3.1-8b-instruct` — gate E fails: NVIDIA returns HTTP 500 "
                "`This model only supports single tool-calls at once` on multi-turn "
                "tool-call round-trips (confirmed 3/3 deterministic)."
            )
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    for note in notes or []:
        lines.append(f"- {note}")
    lines.append("")

    lines.append("## Human review")
    lines.append("")
    lines.append("Raw per-run outputs: `bench/raw/<model>/<suite>-<n>.json`.")
    lines.append(
        "Review the rubric and deterministic checks before finalizing. "
        "Any LLM-as-judge cross-check is reported separately and never merged."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI commands.
# ---------------------------------------------------------------------------


def _cmd_list(args):
    if args.dry:
        for model in _flatten_pool():
            print(model)
        return 0
    transport = _transport(False)
    try:
        catalog = asyncio.run(transport.list_models())
    except Exception as exc:
        print(f"model discovery failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"catalog ({len(catalog)} ids):")
    for model in sorted(catalog):
        print(f"  {model}")
    return 0


def _cmd_probe(args):
    _ensure_dirs()
    if args.dry:
        results = _fabricate_probe_results()
        doc = {
            "generated_at_utc": _utc_now(),
            "catalog_count": len(results),
            "models": results,
            "dry": True,
        }
        _write_json(PROBE_PATH, doc)
        print("dry probe: all pool models fabricated as accessible")
        return 0
    transport = _transport(False)
    outcome = asyncio.run(run_probe(transport, _flatten_pool()))
    if not outcome.get("ok"):
        print(f"NVIDIA API unreachable: {outcome.get('error')}")
        return 1
    doc = {
        "generated_at_utc": _utc_now(),
        "catalog_count": outcome["catalog_count"],
        "models": outcome["results"],
        "dry": False,
    }
    _write_json(PROBE_PATH, doc)
    _print_probe_summary(doc)
    return 0


def _cmd_run(args):
    prompts = _load_json(PROMPTS_PATH)
    if prompts is None:
        print(f"missing prompt set: {PROMPTS_PATH}")
        return 1

    suite_keys = (
        [s.strip() for s in args.suites.split(",") if s.strip()]
        if args.suites
        else list(prompts["suites"].keys())
    )
    unknown = [s for s in suite_keys if s not in prompts["suites"]]
    if unknown:
        print(f"unknown suites: {unknown}")
        return 1

    runs = args.runs or prompts.get("meta", {}).get("runs_per_task", DEFAULT_RUNS)

    if args.dry:
        probe_results = _fabricate_probe_results()
    else:
        probe_results = _load_json(PROBE_PATH)
        if probe_results is None:
            print("bench/probe.json missing — running availability probe first.")
            exit_code = _cmd_probe(args)
            if exit_code != 0:
                return exit_code
            probe_results = _load_json(PROBE_PATH)
        probe_results = probe_results.get("models", probe_results)

    selection, selection_notes = _build_selection(probe_results, args)
    if not selection:
        print("no accessible models to benchmark; run --probe first (or check bench/probe.json)")
        return 1

    _ensure_dirs()
    transport = _transport(args.dry)
    results = asyncio.run(
        run_benchmark(
            transport,
            selection,
            prompts,
            {"suites": suite_keys, "runs": runs},
            probe_results=probe_results,
            expansion_reason=args.expand or None,
        )
    )
    results["meta"]["selection_notes"] = selection_notes
    _write_json(RESULTS_PATH, results)

    print(f"benchmarked {len(selection)} models: {', '.join(selection)}")
    print(
        f"suites {','.join(suite_keys)} · runs/task {runs} · "
        f"overload events {results['meta']['overload_events']} · "
        f"recovery pauses {results['meta']['recovery_pauses']}"
    )
    print(f"wrote {RESULTS_PATH}")
    return 0


def _cmd_report(args):
    results = _load_json(RESULTS_PATH)
    if results is None:
        print("bench/results.json not found; run --run first.")
        return 1
    aggregate = build_aggregate(results)
    recommendation, notes = recommend(aggregate)
    _ensure_dirs()
    REPORT_PATH.write_text(
        build_report(results, aggregate, recommendation, notes), encoding="utf-8"
    )
    print(f"wrote {REPORT_PATH}")
    print(format_recommendation(recommendation))
    for note in notes:
        print(f"  note: {note}")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="bench_nvidia_models",
        description="NVIDIA hosted-model benchmark harness (plan §6–§15).",
    )
    parser.add_argument("--list", "--catalog", action="store_true", dest="list_mode",
                        help="print the NVIDIA model catalog")
    parser.add_argument("--probe", action="store_true",
                        help="Phase 1 availability gate (writes bench/probe.json)")
    parser.add_argument("--run", action="store_true",
                        help="Phase 2 benchmark run (writes bench/raw and bench/results.json)")
    parser.add_argument("--report", action="store_true",
                        help="aggregate bench/results.json into bench/report.md")
    parser.add_argument("--dry", action="store_true",
                        help="no network; fabricate transport responses")
    parser.add_argument("--only", nargs="*", default=None, metavar="MODEL",
                        help="benchmark exactly these models")
    parser.add_argument("--suites", default="", metavar="A,B",
                        help="comma-separated suite keys (default: all A–H)")
    parser.add_argument("--runs", type=int, default=None, metavar="N",
                        help="runs per task (default: from prompts.json)")
    parser.add_argument("--expand", default="", metavar="REASON",
                        help="expand selection up to 12 models; record why")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    mode = (
        "list"
        if args.list_mode
        else "probe"
        if args.probe
        else "run"
        if args.run
        else "report"
        if args.report
        else None
    )
    if mode is None:
        _parse_args(["--help"])
        return 2
    if mode in ("list", "probe", "run") and not args.dry and not settings.nvidia_api_key:
        print("NVIDIA_API_KEY is not set (read from .env via app.core.config).")
        print("Set it and re-run, or pass --dry to run the pipeline offline.")
        return 1
    if mode == "list":
        return _cmd_list(args)
    if mode == "probe":
        return _cmd_probe(args)
    if mode == "run":
        return _cmd_run(args)
    return _cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
