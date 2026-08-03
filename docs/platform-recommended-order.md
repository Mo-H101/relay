# Relay — Recommended Order of Implementation (Phase 9, Deliverable 4)

Date: 2026-08-03 · Analysis only — no code changed yet.

Inputs: `docs/platform-missing-components-report.md` (gaps + deps),
`docs/platform-implementation-roadmap.md` (phases P0–P8).

## Order

1. **P0 — Packaging & distribution** (prerequisite for every new dependency
   and for the `relay` entry point used by P1/P2; delivers one-command
   install + "type `relay`" messaging).
2. **P4 — Provider integrations (async-first)**: first **de-string-name the
   provider system** (registry keyed by stable id + factory table), then add
   OpenRouter/Groq/Ollama/custom (low risk, OpenAI-compatible) and finally
   Anthropic (non-compatible, isolated). **Clients are built async-first
   here**, satisfying the "async operations for slow provider scans"
   requirement early and unblocking the P1 wizard and P2 panels without
   touching the hot chat path.
3. **P5 — API-key security** (independent of the async migration; gives the
   platform its core security model — app keys hashed + upstream keys out
   of plaintext; every later surface (logs, apps, TUI, request_log) keys by
   key_id).
4. **P6 — Platform database + model availability + usage** (after P5, since
   `request_log` references `api_keys`; delivers durable usage, 3-state
   model availability, and the **connected applications** surface).
5. **P1 — First-run experience & CLI** (Typer+Rich) — depends on P0, P4
   (async providers/scanning), P5 (keys command), P6 (logs/apps/status
   readers). Includes bare-`relay` auto-detect and the setup wizard spec.
6. **P2 — Terminal UI — the main interface** — depends on P1 (entry points,
   shared adapters, setup handoff) and the P4/P5/P6 data surfaces
   (async metrics, keys, apps, model_status, routing rules). Read-only
   skeleton can start once P1 lands; interactive panels as P6 lands.
7. **P3 — Async provider layer (hot path)** (the highest-risk, highest-churn
   item: the service layer + endpoints). Scheduled last on purpose — after
   the new surface (providers/keys/apps/TUI) is stable, so the async
   rewrite only preserves behavior. Async-first provider clients already
   exist from P4; this migrates chat failover + endpoints.
8. **P7 — Configuration management** (thin commands over existing reload;
   interleavable after P1; placed here as polish).
9. **P8 — Client integration guides + quality gate & CI** (client guides
   after P5 so per-app keys exist to document; CI continuous from P0,
   final pass at the end).

## Rationale

- **Risk first vs. risk last.** The async rewrite (P3) is the only item
  that rewrites the heavily-tested synchronous hot path. Everything else is
  additive and independently testable. Doing additive work first de-risks
  the core: when P3 lands, its only job is preserving parity against a
  fully exercised surface.
- **Async requirement satisfied early.** By building provider clients
  async-first in P4, the "async operations for slow provider scans"
  requirement is met before the wizard (P1) and TUI (P2) ship — without a
  hot-path rewrite.
- **Key ordering constraints:**
  - P0 → everything (new deps + `relay` entry point).
  - P4 step 1 → P4 step 2 → `relay providers`; P4 async clients → P1 wizard
    scanning and P2 panels.
  - P5 → P6 (request_log keys api_keys).
  - P1 ← P4, P5, P6 (wizard, keys, apps/status readers).
  - P2 ← P1, P4/P5/P6 (data surfaces), P3 (live async metrics).
  - P8 is continuous; a hard gate runs after each phase.
- **Quick wins early:** packaging (P0), extra/custom providers + async
  scans (P4), and per-app keys (P5) each deliver a visible platform
  capability with small, well-isolated diffs — good for momentum and
  feedback.
- **Parallelizable tracks once P0 lands:**
  - Track A: P4 (providers) ‖ P5 (keys) ‖ P7 (config commands) — all
    additive, no shared hot path.
  - Track B: P1 (CLI/wizard) after P4/P5/P6 surfaces settle.
  - Track C: P2 (TUI) + P3 (async) after the read adapter exists; P2 smoke
    first, P3 rewrite last.

## Where each workstream should land

| Track | Phases | Depends on | Risk |
| --- | --- | --- | --- |
| A (additive surface) | P0 → P4 → P5 → P6 → P7 | — | low |
| B (CLI + TUI shell) | P1 → P2 | A | low |
| C (async hot path) | P3 | A, read adapter | **high** |
| D (quality gate) | P8 | all | low |

## Milestone cut-lines (optional but recommended)

- **M3 (platform core):** after P4+P5+P6 — installable `relay`, extra
  providers, per-app keys, durable usage + model availability + connected
  apps. Marker: `relay keys create` + a routed chat call + `relay apps`
  shows it after restart.
- **M4 (zero-friction UX):** after P1+P2 — `relay` → wizard → main
  interface, seamless.
- **M5 (performance & hardening):** after P3+P7+P8 — async hot path,
  config management, client guides, CI green.

## Confirmation gate

Target-design assumptions were approved (rev 2). No code changes before the
user re-confirms after this validation pass.
