# NVIDIA Model Evaluation — Benchmark Plan (Phase 2, rev 2)

Date: 2026-08-04 · **Plan only — no code changed yet.**
Results are recorded in `PROJECT_LOG.md` **only after explicit approval.**

## 1. Objective

Pick Relay's recommended NVIDIA defaults — an ordered `NVIDIA_MODEL_PRIORITY`
(default_general → coding → reasoning → fast) and per-task `TASK_*` values —
by measuring real hosted-NVIDIA-NIM models on the dimensions that actually
matter to Relay's users.

## 2. Priorities (Relay's actual goals)

| Priority | Goal | What is measured |
| --- | --- | --- |
| 1 | **Coding / agent work** | code generation, debugging, refactor, tool calling |
| 2 | **General assistant** | summarization, Q&A, instruction following, formatting |
| 3 | **Reasoning / university tutor** | step-by-step math, concept explanation, checking student work, logic |
| 4 | **Fast everyday usage** | TTFT, throughput, end-to-end latency at a fixed budget |

Other routing categories — vision, translation, creative — are **noted as
non-priority** for this benchmark. They are intentionally excluded from the
scoring mix and do not influence default selection.

Relay-specific behaviors are evaluated **across** the four priorities:

- Tool / agent instruction following
- Code debugging
- Long context handling
- Structured JSON output reliability
- Streaming reliability

## 3. Context — how Relay talks to NVIDIA

| Concern | Location | Notes |
| --- | --- | --- |
| Provider registry | `app/providers/registry.py:75` | `nvidia`: `NVIDIA_API_KEY`, base `https://integrate.api.nvidia.com/v1`, `NvidiaClient` |
| Client | `app/providers/nvidia_client.py` → `openai_compat_client.py` | OpenAI-compatible chat/stream/models/probe (sync + async) |
| Chat defaults | `openai_compat_client.py:237` | `temperature=0.2`, `max_tokens=512`, `REQUEST_TIMEOUT=120s` |
| Discovery | `list_models` → `GET /models` | dynamic catalog; empty on failure, provider still registers |
| Probe | `probe_model` → `POST /chat/completions` "ping", `max_tokens=1`, 10s | used by setup scan + health |
| Task taxonomy | `app/services/routing.py:7`, `task_classifier.py` | `coding, reasoning, general, ...` |
| Priority config | `NVIDIA_MODEL_PRIORITY` (`.env.example:12`) | reorders, never removes candidates |
| Capability catalog | `app/services/model_catalog.py` | seed is OpenAI-only; NVIDIA ids use keyword fallback today |

Known constraints validated at RC that shape the plan:
- **Over-listing.** `/models` returns ~221 ids but an account can invoke only
  a subset; the rest 404 `"Function ... Not found"` (`docs/known-limitations.md`
  §4). → **Availability probing is mandatory before any benchmarking.**
- **529 overloads.** Seen live mid-smoke; treated as retryable, never as
  quality data (`app/providers/availability.py:27`).
- **Cost.** Completions are billed. → all runs token-capped; spend tracked.

## 4. Two-phase model selection

**Phase 1 — Probe (availability gate).** For every candidate id in the pool
(§5): `GET /models` membership + a `max_tokens=1` probe (10s timeout). Models
that 404, 401, time out repeatedly, or return end-of-run 529s are recorded as
`inaccessible`/`unstable` and **excluded from benchmarking**.

**Phase 2 — Benchmark.** Benchmark **at most 8** passing models. Selections
ensure each priority has coverage:

| Priority | Slots (target) |
| --- | --- |
| Coding / agent | 2–3 |
| General assistant | 1–2 |
| Reasoning / tutor | 2 |
| Fast everyday | 1–2 |

If results are unclear after the 8-model run (e.g., the top-2 are within the
error band on a priority), expand to a small confirmation batch — never more
than 12 total — and record why.

## 5. Candidate pool for the probe phase

Pool is deliberately larger than the benchmark budget so availability pruning
can still leave ≥8 passing models. Final ids are re-verified against the live
catalog at run time; pool changes are recorded in the report.

| Role | Pool |
| --- | --- |
| Coding / agent | `qwen/qwen3-coder-480b-a35b-instruct`, `qwen/qwen2.5-coder-32b-instruct`, `deepseek-ai/deepseek-v4-flash`, `deepseek-ai/deepseek-v4-pro` |
| General | `meta/llama-3.3-70b-instruct`, `nvidia/nemotron-3-super-120b-a12b`, `meta/llama-3.1-8b-instruct` |
| Reasoning / tutor | `deepseek-ai/deepseek-r1`, `qwen/qwen3-next-80b-a3b-thinking`, `qwen/qwq-32b`, `nvidia/llama-3.1-nemotron-ultra-253b-v1`, `nvidia/llama-3.3-nemotron-super-49b-v1.5` |
| Fast | `meta/llama-3.2-3b-instruct`, `qwen/qwen3-next-80b-a3b-instruct` (plus any of the above flagged `-flash`/`-3b`/`-8b`) |

## 6. Harness design (built after approval)

Follows the existing live-harness pattern (`tests/run_live_smoke.py`:
standalone, non-`test_`-collected, reads live keys from `.env`).

- **New file:** `tests/bench_nvidia_models.py` + a committed prompt set
  (`bench/prompts.json`).
- **Transport:** existing `NvidiaClient` via `Provider` from
  `app.providers.registry` — the exact code path Relay uses (proxy, timeout,
  error mapping, metrics), so results transfer directly.
- **Modes:** `--list` (catalog), `--probe` (Phase 1 gate), `--run`
  (Phase 2), `--dry` (no network), `--report` (aggregate raw results only).

### Hard constraints

1. **Does not modify production code.** The harness imports `app.*` read-only;
   it never writes to `app/`, `.env`, or any config file. All output goes
   under a git-ignored `bench/` directory.
2. **Does not modify `PROJECT_LOG.md` automatically.** The harness has no
   write path to `PROJECT_LOG.md` or any repo doc. Recording rankings is a
   separate, manual, approval-gated step.
3. **Raw results are stored separately first.** Each run writes
   `bench/raw/<model>/<suite>-<n>.json` (request summary, timings, response,
   errors) plus `bench/results.json`. Nothing is committed or summarized into
   the repo until a reviewer approves.
4. **Approval gate before final rankings.** Final model rankings are presented
   to the user; `PROJECT_LOG.md` (and any `.env`/docs updates) happen **only
   after approval**.

## 7. Test suites

Deterministic prompt set; fixed `seed`, `temperature=0.2`; streamed requests
(TTFT measured from first SSE delta); 3 runs per task unless noted.

### A. Coding / agent work
1. **Codegen** — write a Python function from a spec with edge cases; local
   execution + unit tests must pass.
2. **Code debugging** *(Relay-specific)* — given a failing snippet + traceback,
   identify the root cause, fix, and explain.
3. **Refactor** — restructure supplied code, preserve behavior.
4. **Unit tests** — write pytest coverage for a given function.

### B. Reasoning / university tutor
1. Step-by-step math solution (exact answer + shown method).
2. Concept explanation (explain a CS/math concept to a student; check with a
   follow-up).
3. Check student work (find and correct the error in a supplied solution).
4. Logic puzzle (deduction, exact reasoning).

### C. General assistant
1. Summarization (source provided; length + fidelity constraints).
2. Q&A accuracy (verifiable ground truth).
3. Instruction following (exact output format).
4. Tone / brief (style constraints).

### D. Fast everyday usage (fixed 200-token budget, 3 runs)
Per model: **TTFT**, **total time**, **tokens/sec**, **p50/p90 latency**.
Latencies are also recorded per task in A/B/C so quality-vs-latency
tradeoffs share one measurement.

### E. Tool / agent instruction following *(Relay-specific)*
1. **Single tool call** — correct tool name + `arguments` JSON.
2. **Multi-turn tool use** — complete a 2–3 step tool-using conversation.
3. **Parallel tools** — emit two valid tool calls in one turn.
4. **Reject tool** — must NOT call a tool when the instruction says not to.

Graded on: tool call validity, argument JSON correctness, no hallucinated
tools, correct termination.

### F. Long context handling *(Relay-specific)*
A synthetic document injected into the conversation:
- ~32k-token retrieval (answer a fact buried at position N).
- ~64k-token instruction-at-end (format requirement stated after the doc).
- Fidelity check: answer cites content actually present (no hallucination).
Reports success + latency scaling vs. the short-context baselines.

### G. Structured JSON output reliability *(Relay-specific)*
1. `response_format={"type":"json_object"}` — parse rate + valid JSON.
2. `json_schema` adherence — required keys, types, no extra fields.
3. JSON embedded in prose — extraction success.
Graded on parse rate and schema conformance.

### H. Streaming reliability *(Relay-specific)*
1. SSE completes (`[DONE]`), content contiguous, no mid-stream gaps.
2. Stable stream id across chunks.
3. No mid-stream timeout/hang within `REQUEST_TIMEOUT` budget.
4. Error case: an intentionally malformed request yields the OpenAI error
   shape, not a hang.
Reports completion rate + chunk integrity per model.

## 8. Metrics per run

| Metric | Source |
| --- | --- |
| success (1/0, partial) | harness executor + rubric |
| HTTP status / error class (timeout/404/429/529/5xx) | client exception + status |
| TTFT, total ms, tokens/sec | harness timers + `usage` chunk |
| input/output tokens | response `usage` |
| est. cost / 1k requests | tokens × recorded NVIDIA pricing at run time |

## 9. Quality rubric (0–5)

- **5** — correct, complete, idiomatic, meets every constraint.
- **4** — correct with minor gaps (style, one missed edge case).
- **3** — works in the main path; notable defect or missing requirement.
- **2** — substantial defect; incorrect but recognizable intent.
- **1** — unrelated/off-topic or fails to run.
- **0** — refusal/empty/HTTP error.

Two passes: (1) deterministic checks (executes? format valid? exact answer?),
(2) human review of raw outputs. Optional LLM-as-judge cross-check reported
separately, never merged into the human score.

## 10. Aggregation and selection rules

**Per-priority composite** (0–100): `60% quality + 25% latency + 15%
reliability` (quality = mean rubric × 20; latency = normalized inverse of
p50 total; reliability = % runs without 404/5xx/end-of-run 529). The four
Relay-specific suites (E–H) are scored as **gate conditions** and as tiebreakers,
not merged into the composite.

**Selection:**
1. **Reliability floor** — ≥ 95% success (non-overload), else excluded.
2. **Latency cap** — p90 total ≤ 30 s for a 512-token completion.
3. **Cost cap** — ties broken by est. $/1k requests.
4. Winner per priority; then verify winner passes E–H gates. A model that
   fails a gate is replaced by the next-best in that priority.

## 11. Recommendation output format

The default is a 4-entry priority list (all ordered first→last):

```
NVIDIA_MODEL_PRIORITY:
  - <default_general>
  - <coding>
  - <reasoning>
  - <fast>
```

- `<default_general>` — the all-purpose default (first = Relay's default
  target).
- `<coding>` — coding/agent winner.
- `<reasoning>` — reasoning/tutor winner.
- `<fast>` — fast everyday winner.

Mapping to Relay config (applied after approval):
- `TASK_GENERAL=<default_general>`, `TASK_CODING=<coding>`,
  `TASK_REASONING=<reasoning>`
- `<fast>` is the recommended fallback for latency-sensitive traffic; if it
  is also a general/coding winner it may appear more than once (duplicates
  collapsed).

## 12. How results become config (after approval)

| Artifact | Change |
| --- | --- |
| `.env` / `.env.example` | `NVIDIA_MODEL_PRIORITY=<4-entry list>`; `TASK_*` per §11 |
| `docs/deployment.md` | refresh recommended priority list |
| `docs/known-limitations.md` | update over-listing mitigation to new defaults |
| `app/services/model_catalog.py` *(later, optional)* | add NVIDIA family profiles |
| `PROJECT_LOG.md` | **record only after approval** |

## 13. Execution steps

1. **Approve this plan** (no code until then).
2. Build harness + prompt set; add `bench/` to `.gitignore`; offline-test the
   harness against the existing fake clients.
3. `--list` → catalog; finalize the probe pool.
4. `--probe` → availability gate; record `inaccessible`/`unstable`.
5. Select the ≤8 model set (≥1 per priority, §4).
6. `--run` A–H with pacing; re-run any 529-affected run once.
7. Produce `bench/results.json` + `bench/report.md`; human review pass.
8. Aggregate, apply §10 rules, produce the §11 recommendation.
9. Present results + recommendation. **On approval only:** record in
   `PROJECT_LOG.md` and apply §12 config updates.

## 14. Cost and rate-limit control

- Token caps per suite (§7); worst case ≈ 8 models × ~3 runs × ~2k tokens ≈
  50k tokens ≈ sub-cent per model at preview rates (long-context runs capped
  separately and counted).
- Pacing: ≥500 ms gap between requests; on 429/529 wait for `Retry-After`
  (capped 60 s) — mirrors `RETRY_HONOR_RETRY_AFTER=true`.
- Abort after >5 consecutive overloads; record and resume.
- Honor `REQUEST_TIMEOUT`/budget from `.env` (default 120 s / unlimited).

## 15. Deliverables

1. `bench/results.json` + `bench/report.md` (full data, raw outputs kept).
2. Per-priority winner table + the §11 recommendation.
3. Gate results (E–H) per candidate model.
4. `PROJECT_LOG.md` entry (Phase 2 — NVIDIA model evaluation) — **after
   approval only.**

## Appendix A — inspected architecture pointers

- Registry / def / menu: `app/providers/registry.py:74`, `:180`.
- Factory: `app/providers/nvidia.py` (dynamic discovery, empty-on-failure).
- Client: `app/providers/openai_compat_client.py` (chat, streaming, models,
  probe; sync + async).
- Scan + persistence: `app/setup/scan.py`, `app/setup/persistence.py`.
- Task taxonomy: `app/services/routing.py:7`, `app/services/task_classifier.py`.
- Priority parsing: `app/core/config.py:258`.
- Live-harness pattern: `tests/run_live_smoke.py`.
- Catalog pitfalls: `docs/known-limitations.md` §4, §6;
  `docs/v1.0.0-readiness-report.md`.

## Appendix B — catalog observation (2026-08-04, from NVIDIA NIM docs)

Hosted catalog is dynamic (`GET /models`); the 2026 chat catalog includes
`deepseek-ai/deepseek-v4-flash`, `deepseek-ai/deepseek-v4-pro`,
`deepseek-ai/deepseek-r1`, `meta/llama-3.3-70b-instruct`,
`meta/llama-3.1-8b/70b-instruct`, `nvidia/llama-3.1-nemotron-ultra-253b-v1`,
`nvidia/llama-3.3-nemotron-super-49b-v1(.5)`, `nvidia/nemotron-3-super-120b-a12b`,
`nvidia/nemotron-3-nano-30b-a3b`, `qwen/qwen3-coder-480b-a35b-instruct`,
`qwen/qwen2.5-coder-32b-instruct`, `qwen/qwen3-next-80b-a3b-{instruct,thinking}`,
`qwen/qwq-32b`. The §5 pool is drawn from these and re-verified live.
