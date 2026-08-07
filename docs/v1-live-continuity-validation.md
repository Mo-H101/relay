# v1.0.0 Live Project-Continuity Validation (R3, hardening plan §1)

Date: 2026-08-07
Scope: live end-to-end validation of the project-continuity wire contract
against a **real** Relay uvicorn process and real provider endpoints,
following the R2 `docs/v1-release-hardening-plan.md` §1. Includes the
root-cause fix for a restart-resume stall found by the first golden run,
the regression tests added, and the clean 28/28 golden rerun evidence.

Baseline: full suite **2399 passed, 20 skipped, 0 failed** (227.93s,
measured at the R3 working tree; R2 commit `93a16d3` baseline was
2360/22/0).

---

## 1. Result summary

Golden run (`tests/run_live_continuity.py`, run `run-20260807-045802`):
**28/28 scenarios passed, exit code 0.**

| Scenario block | Result |
|---|---|
| §1.1 single-conversation soak (300 turns, 5 hard kills) | **PASS** — 300/300, seq 1..300 contiguous, 5/5 resume tokens, 0 flush failures |
| §1.2 interleaved multi-conversation soak (4×75, mid-run kill) | **PASS** — 75/75, 75/75, 74/75, 74/75 (two 1-turn kill-boundary losses, expected by design) |
| §1.3 forced switching / provider-failure observation | **PASS** — OpenAI-only outage surfaces 502; conversation resumes after restore |
| §1.4 S3 compaction over budget (live) | **PASS** — 13 compactions fired; compacted conversation 25/25 contiguous |
| §1.4 S-matrix restart-recovery | **PASS** — S9 invalid header 400; S7 unknown model 400, no conversation created; no stuck recovery state |
| §1.4 S8 active conversation never pruned | **PASS** — seed turn ok; dry-run prune preview removes 0 candidates |
| Privacy negatives | **PASS** — prompt words absent from stored rows and `/metrics` exposition |
| §1.4 S6 corrupt-file recovery | **PASS** — corrupt db backed aside, server reopens, backups=1 |
| Database integrity | **PASS** — `PRAGMA integrity_check` = `ok` |

---

## 2. Root cause found by the first golden run (now fixed)

The R2 driver build reported a FAIL (`16/19`) during the §1.2 interleaved
restart: `2c47a911` never recovered its sequence after a hard kill. Analysis
of `run-20260807-042231` (`27/28` under the R2 assertions) pinned the cause:

1. A hard kill loses the last in-flight turns (flush is asynchronous with a
   `CONTINUITY_FLUSH_INTERVAL_SECONDS=2` cadence). The stale resume token
   remains durable; post-restart `validate_resume` correctly denies it.
2. On a denied (or absent) token, the handoff coordinator previously seeded
   `next_seq = 1` for the **existing** conversation, so the first replayed
   turn collided on `UNIQUE(conversation_id, seq)` with the durable row.
3. The flusher's `turn.append` treat-as-fatal path stopped the drain; the
   resulting poison row stalled **all** durability for that conversation
   (stuck at ~38 of 75). This was silent: the consecutive-failure counter was
   reset in the prune `else` every pass, so the ≥5 WARNING never fired and no
   WARNING/ERROR line ever appeared in the server log.

Fix sites (R3):

- `app/services/continuity_recovery.py` — `validate_resume` reads the durable
  `last_turn` up front and every `_deny()` path now carries
  `last_seq: Optional[int]` in the decision (blank `next_seq=1` would collide
  on the existing seq); added best-effort `durable_last_seq()` helper.
- `app/services/handoff.py` — `start()` accepts
  `resume_last_seq: Optional[int]` and seeds
  `next_seq = max(1, resume_last_seq + 1)` for a fresh existing-conversation
  state, falling back to `recovery.durable_last_seq()` when no token is
  present; brand-new conversations still start at seq 1.
- `app/core/relay.py` — `begin_continuity_turn` passes
  `resume_last_seq=resume.get("last_seq")` (valid **or** denied) through to
  `continuity_handoff.start()`.
- `app/services/continuity_flusher.py` — `_drain_queue()` returns `clean`;
  `_consecutive_flush_failures` resets only on a clean drain (so the ≥5
  consecutive-failure WARNING fires as designed); a duplicate-`(conversation_id,
  seq)` `IntegrityError` on `turn.append` is treated as an **idempotent skip**
  (log + pop + continue) so a single poison row can no longer stall the queue.

The driver was also hardened so this class of bug cannot pass again:

- `tests/run_live_continuity.py` — `wait_seqs()` now returns
  `(seqs, complete)` and every seq assertion requires `complete`; the
  interleaved assertion is `contiguous AND complete AND
  max(1, count - _KILL_LOSS_WINDOW) <= len(seqs) <= count` with
  `_KILL_LOSS_WINDOW = 4` (the observed queue-depth bound at this cadence),
  and reports an explicit `note:` per conversation for kill-boundary losses.
  The historical stuck-at-~38 failure still fails loudly.

---

## 3. Regression tests added

7 new tests across `tests/test_continuity_recovery.py` and
`tests/test_continuity_handoff.py`:

- denied resume after a newer commit carries `last_seq` in the decision
  (the coordinator seeds the correct continuation instead of colliding at 1);
- no-token turn carries the durable `last_seq` with zero denial metric;
- `durable_last_seq()` helper returns the durable max seq without raising;
- fresh existing-conversation state seeds `next_seq = last_seq + 1`;
- fallback to `durable_last_seq()` when no token is present;
- brand-new conversation still starts at seq 1;
- flusher: duplicate-seq append is an idempotent skip — the queue still drains
  (no stall);
- flusher: the consecutive-failure WARNING fires after 5 clean-or-not cycles.

Continuity subset: **322 passed**; recovery + handoff files: **94 passed**
in 2.45s.

---

## 4. Golden run evidence (`run-20260807-045802`)

Driver output (selected):

```
[PASS] segment 1 seq contiguity (through turn 50) -- seqs=1..50 count=50/50
[PASS] segment 6 seq contiguity (through turn 300) -- seqs=1..300 count=300/300
[PASS] soak turns durable and seq-contiguous (1..300) -- count=300/300 contiguous=True
[PASS] restart resume tokens accepted (driver-observed) -- 5/5 resumed segments continued
[PASS] no flush failures during soak -- max flush_failures_total=0
  note: max continuity_rows_queued observed: 2
  note: continuity_denials_total delta (this instance): 0
[PASS] conversation 64a38e9b seq contiguity -- count=75/75
  note: 16e1ce04 durable 74/75 (1 lost at the kill boundary)
[PASS] conversation 16e1ce04 seq contiguity -- count=74/75
[PASS] conversation ba552610 seq contiguity -- count=75/75
  note: ad2b581e durable 74/75 (1 lost at the kill boundary)
[PASS] conversation ad2b581e seq contiguity -- count=74/75
[PASS] per-project key scoping distinct -- distinct project_key values=4
[PASS] compaction fires over a tight token budget -- compactions_total delta=13
[PASS] compacted conversation seqs contiguous -- count=25/25
[PASS] provider failure surfaces a 5xx (no healthy candidate) -- status=502
[PASS] conversation resumes after provider outage -- status=200
[PASS] resumed conversation seqs contiguous -- count=1
[PASS] S9 invalid conversation header -> generic 400 -- status=400
[PASS] S7 unknown model -> model_not_found, conversation not created -- status=400
[PASS] no stuck recovery state in the final health snapshot
[PASS] S8 dry-run prune keeps the active conversation -- preview={"removed": 0, "days": 0, "candidates": []}
[PASS] prompt words absent from stored turn/conversation rows
[PASS] prompt words absent from /metrics exposition
[PASS] corrupt db is backed aside and server reopens -- backups=1
[PASS] PRAGMA integrity_check -- result='ok'
28/28 R3 scenarios passed
```

The two `74/75` interleaved conversations confirm the by-design kill-window
behavior: the last in-flight commit sitting in the flush queue at the hard
kill is lost, and the restarted process continues seamlessly at the correct
durable seq (contiguous 1..74) instead of colliding at seq 1 or stalling.
Server `soak.log`: no WARNING/ERROR/flush-failure lines across the run.

Staging evidence (kept outside the repo):
`C:\Users\Loq\AppData\Local\Temp\opencode\relay-r3\run-20260807-045802\`
(`platform.db`, `soak.log`, `driver-r3-golden2.log`).

---

## 5. Scope notes

- **Switching (§1.3).** No live cross-provider switch pair exists: the live
  environment is NVIDIA-only (102 models; OpenAI returns 429 under B1, the
  account-quota blocker). The driver ran the provider-failure observation
  path instead; full switch semantics (envelope, `relay:model_switched`
  SSE, A→B→A oscillation, cap denial) remain evidenced by the deterministic
  `tests/test_continuity_http.py` suite.
- **Paid-endpoint cost.** Soak + interleaved used 300 + ~299 completion calls
  per golden run on `meta/llama-3.1-8b-instruct` via NVIDIA.
