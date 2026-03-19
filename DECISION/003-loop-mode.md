# DECISION-003: Loop Mode for auto-organize

## Status
ACCEPTED

## Context
DECISION-002 defines `auto-organize` with a `--max-emails` cap (default 50) per run.
Users with thousands of unread emails need a way to drain the entire inbox without
re-invoking the command manually. A three-agent debate (ARCHITECT / ADVERSARY / JUDGE)
evaluated a full-featured loop engine (~500 lines) vs. a minimal `--loop` flag (~50
lines). The JUDGE sided with minimalism: a shell `while` loop achieves 80% of the
value, so the in-process loop should be thin.

## Options Considered

### 1. Full loop engine with session resume, adaptive rate limiting, cost projection
- **Pros:** Self-contained, handles crashes, tracks cost, auto-adapts to API limits
- **Cons:** ~500 lines of new code — more than the entire existing codebase combined.
  Session resume is fragile (UIDVALIDITY invalidation after Bridge restart discards
  the session file). Cost projection requires Groq pricing that may drift. Adaptive
  rate limiting solves a problem not yet observed. Trust mode duplicates `--yes`.

### 2. Minimal `--loop` flag + documented shell patterns
- **Pros:** ~50 lines. Per-iteration reconnect is architecturally consistent with
  the existing context manager pattern. Stall detection prevents infinite loops.
  Shell `while` loop covers the resume use case without fragile UID tracking.
- **Cons:** No built-in resume after crash. No adaptive rate limiting.

### 3. No in-process loop — document shell patterns only
- **Pros:** Zero new code.
- **Cons:** Users must manage their own shell loop. No stall detection. No
  cumulative progress reporting across iterations.

## Decision
**Option 2: Minimal `--loop` flag.** The loop adds three things a shell loop cannot:
stall detection, cumulative progress reporting, and a clean iteration count cap. These
justify ~50 lines of new code but not ~500.

## Design

### CLI Flags (added to `auto-organize`)

| Flag | Type | Default | Purpose |
|------|------|---------|---------|
| `--loop` | bool | False | Keep processing until no unread emails remain |
| `--max-iterations` | int | 100 | Hard cap on loop iterations |
| `--inter-batch-delay` | int | 5 | Seconds to sleep between iterations (min 2) |

`--loop` is mutually exclusive with `--dry-run`. All existing flags continue to
work; `--max-emails` means "per iteration" in loop mode.

### Loop Mechanics

```
while True:
    reconnect (new __enter__/__exit__ per iteration)
    search UNSEEN → if empty, exit cleanly
    take first --max-emails UIDs
    run single-iteration pipeline (analyze/validate/present/confirm/execute)
    check stall condition → if stalled, exit with warning
    if iteration_count >= max_iterations, exit with warning
    sleep --inter-batch-delay
```

**Per-iteration reconnect** — not persistent connection. Proton Bridge drops idle
connections silently. Reconnecting per iteration is negligible overhead at 5s+ delays
and is consistent with the existing `__enter__`/`__exit__` pattern. No NOOP keepalive.

### Stall Detection

Two independent signals, tracked separately:

- **LLM stall**: the LLM returned only `skip` recommendations for two consecutive
  iterations with identical UID sets. Exit with:
  `"No actionable recommendations for remaining emails. Stopping."`
- **User refusal**: the user declined all confirmations. This is NOT a stall. The
  loop continues to the next iteration. No auto-exit on user refusal.

This distinction prevents the false positive where a selective user is kicked out
of the loop for being deliberate.

### Progress Reporting

**Per-iteration header:**
```
--- Iteration 3 | 1,847 unread remain in INBOX ---
```

**Per-iteration footer:**
```
Iteration 3: 47 applied, 2 skipped, 1 error
Session total: 138 applied, 6 skipped, 1 error
Next batch in 5s... (Ctrl+C to stop)
```

**Session summary (on clean exit):**
```
Loop complete — inbox clear.
Iterations: 12 | Applied: 541 | Skipped: 31 | Errors: 8
```

Token reporting added to footer when `--verbose` is passed — actual counts only,
no cost estimates (Groq pricing not reliable enough for dollar figures).

### Safety

- **Hard iteration cap** (default 100, max 5000 emails at default batch size)
- **Ctrl+C** completes current batch then exits cleanly
- **`trash` always requires individual confirmation** — inherited from DECISION-002,
  never relaxed in loop mode, even with `--yes`
- **UIDVALIDITY checked per iteration** — inherited from DECISION-002 P0

### What Is Explicitly NOT Built (v1)

| Feature | Reason deferred |
|---------|----------------|
| Session resume (`--resume-from`) | UIDVALIDITY invalidation makes UID-based resume unreliable after Bridge restart |
| Adaptive rate limiting | Not a known problem with Groq; add if observed |
| Pre-run cost projection | Wrong pricing model, misleading precision |
| Trust mode (confirm first, auto-apply rest) | `--yes` already covers this |
| `--loop-output` JSONL | Single `--output` per run is sufficient |
| IMAP `$AutoOrganized` keyword flags | Bridge support unconfirmed; P2 investigation |

### Shell Loop Pattern (documented alternative for resume)

For crash-resilient processing, the documented pattern is:

```bash
while python -m protonmail_claude auto-organize --max-emails 50 --yes; do
    echo "Batch complete, sleeping 10s..."
    sleep 10
done
```

This naturally resumes after any crash because `auto-organize` always re-searches
for UNSEEN emails. No session state needed.

## Resolved Assumptions

**Session files in v1?** No. Shell `while` loop pattern covers the resume use case.
Session files add file I/O, corruption risk, and cleanup overhead for a narrow
benefit (clean interruption on a stable connection). Revisit in v2 if users request.

**IMAP `$AutoOrganized` keyword flags?** Deferred to P2. Proton Bridge keyword
support is unconfirmed. Requires a live Bridge connection to test. If confirmed in
v2, this replaces the shell loop pattern as the primary resume mechanism.

**Update CLAUDE.md for Groq?** Yes. CLAUDE.md has been updated to reflect the
actual Groq/OpenAI-compatible stack (env vars, architecture diagram, usage patterns).

**Groq rate limits?** Not a known problem at current usage levels. If rate limit
errors (HTTP 429) are observed during loop execution, the existing retry wrapper
(DECISION-002 P0) handles them with exponential backoff. Adaptive inter-batch
delay is over-engineering until this is a measured problem.

## Consequences
- Adds ~50 lines to `auto_organizer.py` and `cli.py`
- Per-iteration reconnect is the correct pattern for Bridge — validates the
  existing context manager design
- No session state files, no file accumulation, no cleanup needed
- Shell loop pattern covers the resume use case without fragile UID tracking
- `--max-iterations` default of 100 prevents runaway execution
