# P4.3 Phase 1 — Implementation Plan: TUI Ops/Log-Tail Row-Key Fix

Status: **Phase-1 planning only. No code yet.** This is the detailed implementation
plan for the first approved P4.3 work item: fixing the TUI `DuplicateKey` reliability
defect. It is the foundation phase — it restores an all-green full suite so every
later phase (G2/G3/G4 parity fixes, conformance suite, docs) can attribute any new
failure cleanly. Implementation starts only after this plan is approved.

Source: `docs/platform-p4.3-plan.md` §4 (Focus C). Approved decisions applied: fix
in P4.3; keep changes incremental; no `app/api/`, persistence/state, or
`PROJECT_LOG.md` changes.

## 1. Goal and acceptance criteria

**Goal:** make `test_ui_app.py::test_boots_to_dashboard_and_walks_all_tabs` (and the
Diagnostics screen generally) immune to the order-dependent `DuplicateKey` crash, so
the full suite is green in both isolation and full-suite order.

**Acceptance criteria (all must hold):**

1. Full suite: `python -m pytest tests -q` → **1366 passed, 7 skipped, 0 failed**
   (the sole prior failure turns into a pass).
2. `test_ui_app.py::test_boots_to_dashboard_and_walks_all_tabs` passes when run
   alone **and** in full-suite order (order-independence restored).
3. New deterministic regression test passes standalone and in the full suite (seeds
   the duplicate-row condition directly, no reliance on suite ordering).
4. `git diff` for the phase touches only `app/ui/screens/diagnostics.py` and
   `tests/test_ui_diagnostics.py`.
5. No `app/api/`, persistence/state-store, or `PROJECT_LOG.md` changes.

## 2. Root cause (verified by inspection)

- `app/ui/data.py:180–195` — `OpsEventView` is a `frozen` dataclass → value-based
  `__eq__`/`__hash__`.
- `app/ui/data.py:779–801` — `ServiceFacade.ops_tail()` renders each ops event into
  an `OpsEventView` whose `age_seconds` is truncated to integer seconds
  (`max(0, int(now - event.ts))`).
- `app/ui/screens/diagnostics.py:107–126` — `_refresh_ops_table()` calls
  `table.clear(columns=True)` then `table.add_row(..., key=event)` where `event` is
  the `OpsEventView` object. Two ops events recorded within the same monotonic
  second produce identical `OpsEventView` values → equal keys → Textual
  `DuplicateKey`.
- The failure surfaces on mount (`diagnostics.py:69–71` →
  `_refresh_all` → `_refresh_ops_table`). It is order-dependent because the
  `ops_store` singleton (`app/services/ops_store.py`) accumulates events across the
  suite run; in isolation the store is empty at mount time, so no collision occurs.
- `_refresh_log_table()` (`diagnostics.py:128–146`) has the **same latent bug class**:
  `key=entry` with a value-equal `LogEntryView` (`data.py:198–206`). Probability is
  far lower (microsecond-precision `ts`), but it is the identical defect and is
  fixed defensively in the same change.

## 3. Design decision

**Chosen:** pass an explicit unique row key in the screen, derived from an
enumerate index, instead of the event object. Scope confined to
`app/ui/screens/diagnostics.py`.

**Rejected alternative:** adding a stable identity field (e.g., `seq: int`) to
`OpsEventView`/`LogEntryView` in `ops_tail`/`log_tail`. Rationale: it changes a
shared data-layer contract (frozen dataclass fields consumed by the facade and by
`test_ops_tail_newest_first`), and the row key only ever needs to be unique within
one table instance after `clear(columns=True)`. The screen-local key is the minimal,
contained fix.

Key-uniqueness reasoning: `table.clear(columns=True)` drops all rows and their keys
on every refresh, so per-refresh indices are safe to reuse across refreshes.
Namespaced string keys (`"ops-0"`, `"log-0"`, …) avoid any mixing with Textual's
auto-assigned keys for the no-entries placeholder row and keep keys hashable and
obviously unique.

## 4. Exact file edits

### 4.1 `app/ui/screens/diagnostics.py` — `_refresh_ops_table` (lines 107–126)

Replace the loop body so the row key is the per-refresh index instead of the event:

```python
    for row_index, event in enumerate(self._facade.ops_tail(limit=200)):
        latency = f"{event.latency_ms:.0f}ms" if event.latency_ms else ""
        table.add_row(
            f"{event.age_seconds}s",
            event.kind,
            event.method or "-",
            event.route or event.endpoint or "-",
            str(event.status) if event.status is not None else "-",
            latency,
            event.provider or "-",
            event.model or "-",
            key=f"ops-{row_index}",
        )
```

No other changes: `ops_tail`/`OpsEventView` stay untouched; display cells unchanged.

### 4.2 `app/ui/screens/diagnostics.py` — `_refresh_log_table` (lines 135–146)

Same pattern for the entries loop (defensive fix for the same bug class):

```python
    for row_index, entry in enumerate(result.get("entries", [])):
        table.add_row(
            entry.ts[:23],
            entry.level,
            entry.event,
            entry.data,
            key=f"log-{row_index}",
        )
```

The no-entries placeholder row (line 146) keeps its no-key form — it is only added
when the entries loop is empty, so it never shares the `log-` namespace.

## 5. Test plan

### 5.1 New deterministic regression test (add to `tests/test_ui_diagnostics.py`)

`test_ops_table_renders_duplicate_same_second_events`:

- `ops_store.clear()` at start.
- Seed two identical same-second events:
  `ops_store.record_http("GET", "/health", 200, 5.0)` twice back-to-back. Both land
  in the same monotonic second → identical `OpsEventView` values (equal
  `age_seconds` and all other fields) → the exact pre-fix `DuplicateKey` condition.
- Mount the Diagnostics screen headless (mirror `test_diagnostics_screen_smoke`:
  `RelayApp(facade=_facade(), start_server=False)` → `pilot.press("7")` →
  `pilot.pause()`).
- Assert no exception; `app.screen.query_one("#ops-table")` exists and its
  `row_count == 2`.
- `ops_store.clear()` at the end (store is a module singleton).

This reproduces the failure deterministically and standalone (pre-fix it raises
`DuplicateKey`; post-fix it passes), so the suite no longer depends on ordering to
exercise the defect.

### 5.2 Existing tests — no changes expected

- `test_ui_diagnostics.py::test_ops_tail_newest_first` — data layer untouched.
- `test_ui_diagnostics.py::test_diagnostics_screen_smoke` — unchanged.
- `test_ui_app.py::test_boots_to_dashboard_and_walks_all_tabs` — unchanged; must
  pass in isolation and full-suite order (AC 2).

## 6. Verification commands

1. Targeted: `python -m pytest tests/test_ui_diagnostics.py tests/test_ui_app.py -q`
   — all pass, including the new regression test.
2. New regression test alone: `python -m pytest tests/test_ui_diagnostics.py -q -k ops_table` — pass.
3. Isolation check: `python -m pytest tests/test_ui_app.py -q` — pass.
4. Full suite: `python -m pytest tests -q` — **1366 passed, 7 skipped, 0 failed**.
5. `git status --short` / `git diff` — only the two Phase-1 files changed.

## 7. Commit

Single logical commit after all acceptance criteria hold:

`fix: use unique row keys for ops/log tails to prevent DuplicateKey (P4.3.1)`

Staged files only: `app/ui/screens/diagnostics.py`,
`tests/test_ui_diagnostics.py`. `docs/platform-p4.2-plan.md` remains untracked as
before; `PROJECT_LOG.md` is not touched.

## 8. Out of scope for Phase 1

- G2 `include_usage` normalization, G3 `connectivity_probe` symmetry, G4
  `proxy_request_kwargs` symmetry (later phases).
- Provider conformance suite (later phase).
- Provider authoring guide + capability matrix docs (later phase).
- `check_model` and legacy shims: kept, per approved decisions.

## 9. Risks and mitigations

- **Regression test flakiness (clock skew):** the two seeded records could in
  principle straddle a monotonic-second boundary. Risk is negligible (records are
  microseconds apart); mitigation: assert `row_count == 2` only, and if flakiness
  ever appears the seed can pin `age_seconds` via two records plus the test's own
  same-second guarantee. Acceptable as-is.
- **Key-type regression in Textual:** namespaced string keys avoid any interaction
  with Textual auto-generated keys and are unambiguous across the ops and log
  tables. Covered by the headless smoke assertions.
- **Placeholder-row collision:** the no-entries placeholder keeps a no-key form and
  is mutually exclusive with the entries loop, so it cannot collide with `log-N`.
