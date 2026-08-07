# P9 — Phase 4 (P9d) Audit: Recovery, Retention, and Operator Surfaces

Date: 2026-08-06.
Status: **Audit / research document — no code.** P9d implementation does not
begin until this document is approved. `PROJECT_LOG.md` is not modified.
No commits.

Prerequisites (all approved / landed):
- `docs/platform-p9-research-plan.md` — approved decisions (Option C memory,
  `CONTINUITY_ENABLED` default `false`, opaque headers).
- `docs/platform-p9-architecture-design.md` — approved architecture, §6
  handoff protocol, §9 loop prevention, §16 P10 boundary.
- `docs/platform-p9-implementation-plan.md` — P9d DoD (§12).
- **P9a landed** (commit `e8cb667`): schema v7, config, memory contract,
  `ConversationStore`, `ContinuityFlusher`, facade wiring, lifespan hooks.
- **P9b landed** (commit `26ada25`): `ContextManager`, `summarizer`,
  `summary_verifier`, continuity dataclasses, overflow retry helper.
- **P9c landed** (commit `18e94dafef0d83a6f82adb09514b1d6c8e6e738d`):
  `HandoffCoordinator`, `continuity_headers`, chat/OpenAI header plumbing,
  additive `relay:*` SSE lines, turn-commit via the flusher.
- Full suite green: **2182 passed / 22 skipped** (P9c commit).

Scope of this document: the mandated audit for **P9d — Recovery,
Retention, and Operator Visibility**. It studies how external agent systems
handle interrupted-task recovery, resume after model failure, duplicate-work
prevention, checkpoints, retention, and operator visibility, and maps each
finding onto Relay's landed P9a/P9b/P9c. Sections 1, 3, and 4 are grounded in
a verified read of the current code (file:line references) rather than the
plan only. **No implementation is proposed beyond defining the P9d boundary.**

---

## 1. Current state trace (verified read)

### 1.1 Persistence (P9a)

- Schema v7 continuity tables live in `state_dir/platform.db`. The
  `conversation_turns` table stores `resume_token` as **a hash, never raw**
  (`app/services/platform_store.py:221`).
- `ConversationStore.append_turn(...)` accepts `resume_token_hash`
  (`app/services/conversation_store.py:360`), writes it
  (`:390`, `:402`), and returns turns with `resume_token_hash`
  (`:434`, `:453`). The hash is written but **never consumed anywhere
  today** — there is no recovery/replay path in the codebase.
- Durable writes go through the write-behind `ContinuityFlusher` (in-memory
  enqueue → SQLite on the flusher thread); the request path stays SQLite-free.
  A process crash between enqueue and flush loses the in-flight tail.
- Lifespan hooks start the flusher and run the startup retention prune
  (`app/main.py:40-42`), stop it and flush on shutdown (`:56-61`). The
  flusher is `None` unless `CONTINUITY_ENABLED` (`app/core/relay.py:104-127`).

### 1.2 Compaction and handoff (P9b/P9c)

- `ContextManager` + `summarizer` + `summary_verifier` produce versioned,
  derived-only summaries; `HandoffCoordinator` (`app/services/handoff.py`)
  assembles the envelope (`summary + tail`), enforces switch caps
  (`MAX_SWITCHES_PER_TURN=3`, `MAX_SWITCHES_PER_WINDOW=5`), and commits
  turns. `handoff.py:9-11` explicitly declares: *"the durable resume/recovery
  protocol is P9d."*
- The request path is covered by the P9c integration seams (headers, envelope
  injection, SSE `relay:*` lines, `X-Relay-Conversation-Id` echo).

### 1.3 Config surface already present (P9a)

`app/core/config_spec.py:519-547` (continuity block) plus:

- `CONTINUITY_RETENTION_DAYS=30` (`config_spec.py:523-526`).
- `MAX_RESUME_REPLAYS=3` (`config_spec.py:574-575`, validated in
  `app/core/config.py:841-843`) — **defined but not wired to any behavior.**
- Budget knobs used by compaction: `CONTINUITY_CONTEXT_TOKEN_BUDGET=32768`,
  `CONTINUITY_OUTPUT_RESERVE_TOKENS=2048`, `CONTINUITY_SUMMARY_SHARE=0.4`,
  `CONTINUITY_SUMMARY_MAX_CHARS=4096`, `CONTINUITY_TAIL_MAX_ITEMS=20`.

### 1.4 Operator surfaces today

- **CLI**: `relay conversations list|show|archive|prune` — metadata-only;
  prints `continuity disabled` and exits 0 when the flag is off
  (`app/cli/continuity.py:93-95`); wired at `app/cli/__init__.py:191-199`,
  `:245-248`. No resume status, no dry-run, no flusher health.
- **TUI**: diagnostics screen (P2d, tab 7, `app/ui/screens/diagnostics.py`)
  has ops-log tail, health deep view, redacted export — **no continuity
  panel** (grep for continuity in the file: no matches).
- **Events**: `continuity.create/resume/switch/compact/archive/prune/denied`
  are already registered (`app/services/event_log.py:61-68`), so recovery
  and denial events have a vocabulary slot but no emitter today.

### 1.5 Gaps that bound the P9d scope

| Gap | Evidence | P9d consequence |
| --- | --- | --- |
| No recovery/replay protocol | `MAX_RESUME_REPLAYS` defined (`config.py:841`) but unused; `resume_token_hash` stored (`conversation_store.py:402`) but never read for replay | P9d builds `continuity_recovery.py` |
| No torn-turn / in-flight handling | flusher is write-behind; no started/committed marker reconciliation | P9d adds in-flight marker + startup reconciliation |
| No retention tuning beyond idle-days | only `CONTINUITY_RETENTION_DAYS` prune exists | P9d adds archived+idle pruning, size-aware summaries/tail, dry-run |
| No flusher/lag observability | no counter or surface for drain lag | P9d adds a flusher-health surface |
| TUI has no continuity view | diagnostics.py: no continuity match | P9d extends TUI diagnostics (tab 7) |
| No resume token verification path | hash stored, never compared | P9d adds hash-verified resume + `continuity.resume/denied` events |

---

## 2. Comparison with existing systems

Assessment lens (mandated): **interrupted-task recovery, resume after model
failure, preventing duplicate work/restarts, checkpoint design,
retention/cleanup, user/operator visibility**. Sources: public docs/web
research for OpenCode, Codex, Cline, Continue.dev, Aider, SWE-agent (2026).

### 2.1 OpenCode — compaction checkpoints

- **Recovery**: sessions survive restarts locally; `--continue/-c`,
  `--session/-s`, and `--fork` select/resume sessions.
- **Resume after model failure**: on a context-overflow error the session is
  compacted (checkpoint = structured summary + serialized tail, ~4
  chars/token) and retried **once**; a second overflow surfaces as an error.
  Preflight compaction runs automatically before hitting the limit.
- **Duplicate work**: prevented by keeping the tail verbatim and summarizing
  only the old region.
- **Checkpoints**: compaction replaces older context with a summary+tail
  checkpoint while durable session messages are retained for later recall;
  manual compact is available; barrier settles after compact.
- **Retention**: session files persist until deleted; no TTL described.
- **Visibility**: TUI/CLI session lists; session metadata persisted.

**Takeaway for Relay**: Relay already mirrors the checkpoint shape in P9b
(4-char heuristic, summary+tail split, preflight + overflow retry-once) and
durable storage in P9a. What Relay lacks is OpenCode's **explicit
`--continue`/resume selection** of a prior session — that is the P9d resume
protocol.

### 2.2 Codex — durable threads and rewind

- **Recovery**: threads are durable server-side; double-tap `Esc` / restore
  conversation resumes prior context.
- **Resume after model failure**: `Esc` rewind rolls the conversation back to
  a fork point and continues from there.
- **Duplicate work**: resume avoids restarting a task.
- **Checkpoints**: today rewind is **conversation-only** (it does not revert
  Codex-applied workspace edits). Open issue
  `github.com/openai/codex/issues/11626` proposes `/rewind` as a true
  checkpoint restore that reverts chat **and** Codex-applied edits with
  preview/conflict handling — not yet implemented.
- **Retention**: archive status marks open vs closed threads.
- **Visibility**: thread list / resume UI.

**Takeaway**: the conversation-only rewind is exactly the shape Relay can
support (no filesystem access, so Relay can *never* revert edits — matching
the current Codex behavior, not the proposed `/rewind`). Relay's
`archive` status (P9a) already mirrors the thread lifecycle; P9d adds the
resume/rewind-to-turn semantics within `MAX_RESUME_REPLAYS`.

### 2.3 Cline — keyed task persistence and git checkpoints

- **Recovery**: each task has a unique id, a dedicated storage dir, full
  history, and token/cost/time tracking; tasks are interruptible and
  resumable across sessions.
- **Resume after model failure**: auto-compact near the context limit, then
  continue; provider outage ends the session with state preserved.
- **Duplicate work**: `/newtask` keeps prior file changes via checkpoints;
  history supports revisit/resume.
- **Checkpoints**: **Git-based snapshots of file changes** (not conversation
  state) — this is the repo-bound model.
- **Retention**: task dirs persist until user deletion.
- **Visibility**: task list/history UI.

**Takeaway**: Cline proves the value of durable, keyed, resumable task state
(Relay's `conversations`/`conversation_turns` shape). Its checkpoints are
git/filesystem-bound — **out of scope for Relay P9** (no filesystem access;
`project_key` is opaque), exactly as decided in the P9c audit (§2.2).

### 2.4 Continue.dev — session files + history compaction

- **Recovery**: sessions persist to `~/.continue/sessions/` as JSON:
  a `sessions.json` metadata index plus one `<uuid>.json` full-history file.
  `HistoryManager` (`core/util/history.ts`) is a singleton doing
  `list/load/save/delete/clearAll`; `save` updates the metadata index
  (title, workspace, `messageCount`).
- **Resume after model failure**: `loadLastSession` includes a 1-second retry
  if the initial load fails; compaction failure is surfaced as an error, not
  silently dropped.
- **Duplicate work**: undo/redo stacks (`ChatHistoryService`) plus
  `getHistoryForLLM` returning `[system, ...slice(compactionIndex)]` keep
  work recoverable without restart.
- **Checkpoints**: `compactConversation` finds the most recent
  `conversationSummary`, prepends a `"Previous conversation summary:"` user
  message, and compacts everything after it. `shouldAutoCompact` triggers at
  `contextLimit - maxTokens - buffer` with buffer capped at 15K tokens and an
  0.8 ratio; `pruneLastMessage` never leaves the history ending mid tool-call.
- **Retention**: explicit `delete(sessionId)` / `clearAll()`; no TTL.
- **Visibility**: History UI with MiniSearch fuzzy title search; remote
  sessions (Control Plane) marked "Remote".

**Takeaway**: Continue confirms the value of a **metadata index separate from
full history** (Relay's `conversations` vs `conversation_turns`), the
summary-prepend compaction shape (Relay P9b already equivalent), and
structured failure semantics on compaction/load. Continue's remote sessions
(server-side sync) are **out of scope** for Relay P9d (no cross-device sync).

### 2.5 Aider — git as the state machine

- **Recovery**: every edit is auto-committed with a conventional-commit
  message; dirty files are committed before edits so work is never lost.
  `--restore-chat-history` + `--file "*"` restores a prior session's context
  and files; `/session save|load|list|view|delete` manages named sessions
  under `.aider/sessions/`.
- **Resume after model failure**: restart aider with
  `--restore-chat-history`; `/undo` reverts the last aider commit.
- **Duplicate work**: git history + `/undo`; `.aider.chat.history.md` /
  `.aider.input.history` persist prompts; `/run` output is stored in the chat
  history file for audit.
- **Checkpoints**: git commits are the checkpoints; `/git` exposes raw git
  (`reset --hard` for rollback) at the user's own risk.
- **Retention**: history files persist per repo.
- **Visibility**: `/session list`, `/list-sessions` (chronological with
  first/last user message snippets).

**Takeaway**: Aider's durability is **repo-model** (git), not
conversation-model — Relay cannot adopt it (no git/filesystem access).
Aider's *session restore* UX (`--restore-chat-history`, `/session load`) is
the operator-facing pattern Relay's CLI resume surface should imitate.

### 2.6 SWE-agent — trajectory replay and its limits

- **Recovery**: each run writes `<instance_id>.traj`
  (thought/action/observation steps) plus `config.yaml`, `.log`,
  `preds.json`; `run-replay` re-executes recorded actions.
- **Resume after model failure**: `run-replay` executes the recorded action
  list via a `ReplayModel`; `PartialReplayModel` branches at a step by
  replaying prior actions then switching to a live model.
- **Duplicate work**: SWE-Replay (research) recycles prior trajectories,
  branching at reasoning-critical steps instead of sampling from scratch.
- **Checkpoints**: the key finding (maintainers + SWE-Replay) is that the
  conversation log alone is **insufficient** for resume — agent actions are
  stateful (file writes, shell state, installs), so a branch must restore
  environment state. SWE-Replay restores via recorded repo diffs (cheap)
  falling back to full action replay when non-repo state changed.
- **Retention**: `.traj` files persist; no TTL.
- **Visibility**: trajectory files, hook-based `SaveTrajectoryHook`.

**Takeaway**: SWE-agent is the strongest warning **against** full action
replay for Relay — but Relay has no filesystem/environment state to restore,
so Relay's replay is limited to **conversation metadata**, which is the safe,
stateless subset. The torn-turn / in-flight concern in SWE-agent maps
directly to Relay's write-behind flusher: a crash mid-enqueue loses the tail,
so P9d needs started/committed reconciliation rather than re-execution.

### 2.7 Synthesis table

| Axis | OpenCode | Codex | Cline | Continue | Aider | SWE-agent | **Relay P9d pick** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Interrupted-task recovery | session files | durable threads | task dirs | session JSON | git commits | .traj files | resume protocol over durable turns (P9a) |
| Resume after model failure | compact+retry-once | Esc rewind | continue | load retry | --restore-chat-history | replay / branch | replay cap + rewind-to-turn |
| Duplicate-work prevention | summary+tail | resume | checkpoint/continue | undo/redo | git | trajectory reuse | summary+tail kept; replay guard |
| Checkpoint design | summary+tail | conversation-only rewind | git snapshots | summary-prepend | git commits | env-state restore | metadata checkpoints only (no FS) |
| Retention/cleanup | manual delete | archive | manual | delete/clearAll | history files | manual | retention_days + archive pruning + dry-run |
| Operator visibility | session list | thread list | task UI | History + search | /session list | traj files | CLI resume + TUI diagnostics panel |

Nothing new is required: every P9d mechanism maps to an already-landed
building block (durable turns, resume-token hash storage, event vocabulary,
CLI/TUI surfaces) or an approved boundary (no filesystem, no remote sync).

---

## 3. P9d boundary

**In scope for P9d** (implementation-plan §12; architecture §9/§16):

- **Recovery protocol** (`continuity_recovery.py`): resume a conversation at
  a prior committed turn; hash-verified `resume_token` (never raw); replay
  guard with cap `MAX_RESUME_REPLAYS=3`; S1 (soft) / S2 (hard) reconnect
  semantics per architecture §9; `continuity.resume` / `continuity.denied`
  events emitted (vocabulary already registered).
- **Torn-turn / in-flight handling**: ephemeral in-flight marker per turn;
  startup reconciliation of uncommitted turns; flusher drain-lag surfaced.
- **Retention improvements**: archived + idle pruning beyond the existing
  idle-only prune; size-aware bounds on summaries/tail are already
  configurable — P9d wires them to retention; `relay conversations prune`
  gains `--dry-run`.
- **CLI/TUI visibility**: TUI diagnostics tab 7 gains a continuity panel
  (conversation count, active/archived, flusher lag, compaction counts);
  CLI gains per-conversation resume status (last committed turn, resume
  token presence — hashes only), prune preview, flusher health.
- **Operational diagnostics**: continuity metrics for
  resume/deny/reconcile counters; deep health surface for continuity.
- **Tests**: recovery unit/integration, replay-cap enforcement, torn-turn
  reconciliation, retention dry-run, flag-off parity (byte-identical).

**Explicitly NOT P9d** (P9e or P10, or out of scope for all P9):

- Security-best-practices gate and adversarial pass → **P9e**.
- Multi-agent execution, parallel model collaboration, autonomous file
  editing, task delegation, AI developer teams → **P10** (architecture §16).
- Git/filesystem checkpoints, repo-map context, environment-state restore
  (Cline/Aider/SWE-agent model) → **out of scope for all of P9** (no
  filesystem access; `project_key` is opaque).
- Cross-device / remote session sync (Continue Control Plane, OpenCode
  server sessions) → **out of scope**.
- No provider clients, routing, decision, or context-design changes.

Migrations: **none** (v7 landed in P9a). Config: no new keys required —
`MAX_RESUME_REPLAYS`, `CONTINUITY_RETENTION_DAYS`, and the budget knobs
already exist and would finally be wired to behavior.

---

## 4. Design considerations mapped onto the six focus areas

### 4.1 Interrupted task recovery

- External norm: durable, keyed, resumable task/session state (OpenCode
  sessions, Cline task dirs, Codex threads, Continue session files).
- Relay already has the durable substrate (P9a turns + summaries +
  `resume_token_hash`). The missing piece is a **recovery entry point** that
  selects a conversation by id (key-scoped), loads its last committed turn +
  summary + tail, and presents it for continuation — the OpenCode
  `--continue` / Aider `--restore-chat-history` analogue.
- Guard: recovery never reads or writes raw tokens, prompts, or content —
  metadata only (matches P9a contract).

### 4.2 Resume after model failure

- External norm: compact-and-retry-once (OpenCode), rewind-and-continue
  (Codex), load-retry (Continue), restore-chat-history (Aider).
- Relay P9d: a failed or interrupted request leaves the conversation at its
  last committed turn. A client replay is allowed at most
  `MAX_RESUME_REPLAYS=3` times per conversation turn; each replay is an
  event (`continuity.resume`), and over-cap replays are denied
  (`continuity.denied`) — reusing the existing event vocabulary and the
  existing loop-prevention philosophy (§ architecture 9).

### 4.3 Preventing duplicate work/restarts

- External norm: keep the tail verbatim + summarize the old region (OpenCode,
  Continue); resume instead of restart (all).
- Relay already does summary+tail in P9b. P9d adds the **replay guard** so a
  flapping client cannot re-run the same turn past the cap, and the
  **torn-turn reconciliation** so a crash between enqueue and flush does not
  cause a duplicate committed turn (started/committed marker, single-writer
  flusher).

### 4.4 Checkpoint design

- External norm: conversation checkpoints range from summary+tail (OpenCode)
  through conversation-only rewind (Codex today) to full env-state restore
  (SWE-Replay) — the last is the most powerful and the most fragile.
- Relay P9d: **metadata-only checkpoints**. The checkpoint is the committed
  turn row + its summary + tail; `resume_token_hash` makes a replay
  verifiable without storing the token. Relay never reverts edits (no
  filesystem), which is the *current* Codex behavior — the proposed `/rewind`
  edit-revert is explicitly out of scope.

### 4.5 Retention/cleanup

- External norm: mostly manual (delete/clearAll/history files); Codex and
  Relay share an archive concept.
- Relay P9d: wire the existing `CONTINUITY_RETENTION_DAYS` policy to prune
  archived + idle conversations (not idle-only); keep size-aware summary/tail
  bounds already in config; add `--dry-run` to the CLI prune so operators can
  preview; surface flusher drain lag so unflushed data does not silently
  disappear at shutdown.

### 4.6 User/operator visibility

- External norm: session/task lists with metadata (OpenCode, Cline, Codex,
  Continue MiniSearch, Aider `/session list`).
- Relay P9d: extend the existing `relay conversations` CLI (already
  metadata-only and flag-safe) with resume status and prune preview; add the
  continuity panel to TUI diagnostics tab 7; add continuity metrics to the
  diagnostics/health surfaces. All surfaces stay metadata-only, consistent
  with the P9a privacy contract.

---

## 5. Required files (proposed, pending approval)

### 5.1 New files

| File | Purpose |
| --- | --- |
| `app/services/continuity_recovery.py` | recovery service: key-scoped conversation selection, hash-verified resume token check, replay guard (`MAX_RESUME_REPLAYS`), S1/S2 semantics, torn-turn reconciliation, `continuity.resume`/`denied` events |
| `tests/test_continuity_recovery.py` | recovery unit/integration: resume at turn, replay cap, denial, torn-turn reconciliation, flag-off parity |

### 5.2 Modified files (surfaces and metrics)

| File | Change |
| --- | --- |
| `app/cli/continuity.py` | per-conversation resume status (last turn, resume-token presence — hash only), `prune --dry-run`, flusher-health subcommand |
| `app/ui/screens/diagnostics.py` | continuity panel on diagnostics tab 7 (counts, flusher lag, compaction/resume counters) |
| `app/services/metrics.py` | resume / deny / reconcile counters (extend the continuity block from P9c) |
| `app/services/continuity_flusher.py` | in-flight turn marker + drain-lag telemetry; startup reconciliation hook |
| `app/main.py` / `app/core/relay.py` | wire recovery service + reconciliation into lifespan (flag-off safe) |

### 5.3 Explicitly unchanged

- **Migrations**: none (v7 landed in P9a).
- **Config / spec**: no new keys; `MAX_RESUME_REPLAYS`,
  `CONTINUITY_RETENTION_DAYS`, and budget knobs are wired, not added.
- `platform_store.py`, `conversation_store.py`, `handoff.py`,
  `context_manager.py`, `summarizer.py`, `summary_verifier.py`,
  `memory_contract.py`, `event_log.py`, `models/continuity.py`, provider
  clients, routing/decision layers, `request_log` table and privacy contract.

---

## 6. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Resume replays burn tokens / thrash a conversation | cost, provider hammering | `MAX_RESUME_REPLAYS` cap; `continuity.resume/denied` events; per-turn accounting; reuse of P9c loop-prevention philosophy |
| Torn turn double-commits after a crash | inconsistent history | started/committed marker; startup reconciliation; single-writer flusher; `UNIQUE(conversation_id, seq)` |
| Resume-token leakage | impersonation / data exposure | hash-only storage (already enforced `platform_store.py:221`); key-scoped reads; never echoed in logs/metrics/CLI |
| Unflushed tail lost on hard crash | context regression | drain-lag surfaced; shutdown flush (already in P9a lifespan); reconciliation reports |
| Retention over-prunes active work | data loss | `--dry-run` preview; archived+idle rule only; retention config bounded by existing validator |
| Flag-off regression | behavior drift | parity suite gates every P9d commit; additive-only surfaces; default off |
| Scope creep toward P10 | boundary violation | recovery/retention/surfaces only; no agents, no FS access, no remote sync (section 3) |

---

## 7. Implementation plan (proposed, pending approval)

Ordered steps, each ending with its verification. **No commit until the full
P9d DoD is green and the phase is approved for commit.**

1. **`continuity_recovery.py` core** — key-scoped conversation selection,
   hash-verified resume-token check, replay guard, S1/S2 semantics; unit
   tests for cap enforcement, denial, and flag-off behavior.
2. **Torn-turn reconciliation** — in-flight marker in the flusher; startup
   reconciliation of uncommitted turns; drain-lag telemetry; tests.
3. **CLI surfaces** — resume status, `prune --dry-run`, flusher health;
   integration tests (metadata-only, flag-off prints `continuity disabled`).
4. **TUI diagnostics panel** — continuity panel on tab 7 (counts, lag,
   resume/deny counters); screenshot-level presence tests.
5. **Metrics** — resume/deny/reconcile counters; assertion on event
   vocabulary reuse.
6. **Parity + gate** — flag-off byte-identical parity; full regression gate
   (`python -m pytest`, `python -m compileall -q app`); no ruff/mypy gate
   exists in this repo — pytest + compileall is the gate.
7. **Stop and report.** No P9e work (security gate) without a separate
   approval.

**P9d DoD (implementation-plan §12, verified after step 6):**

- Recovery at a prior committed turn works with hash-verified resume tokens;
- replay cap `MAX_RESUME_REPLAYS` enforced; over-cap denied with event;
- torn-turn reconciliation resolves uncommitted turns without duplication;
- CLI shows resume status + prune preview; TUI shows the continuity panel;
- flag-off parity green; full suite green (2182/22 baseline, plus new
  suites); `PROJECT_LOG.md` untouched.

---

**Stop condition**: this audit is delivered for approval. Implementation
does not start until approved. No code, no commits, `PROJECT_LOG.md`
untouched.
