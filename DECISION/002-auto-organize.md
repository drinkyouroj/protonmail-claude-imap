# DECISION-002: Auto-Organize Command for Unread Emails

## Status
ACCEPTED

## Context
The CLI has a `labels organize` command that takes a natural language instruction
and resolves it to structured IMAP operations. This is instruction-driven — the user
must know what they want. We need a discovery-driven counterpart: fetch all unread
emails, have the LLM recommend an action for each, present recommendations for
review, and optionally execute them.

This was designed via a three-agent (ARCHITECT / ADVERSARY / JUDGE) debate. The
ADVERSARY identified several showstopper-level concerns that shaped the final design.

## Options Considered

### 1. Extend `labels organize` with `--unseen --interactive` flags
- **Pros:** No new module, reuses existing pipeline
- **Cons:** `organize()` is instruction-driven (user supplies intent); auto-organize
  is discovery-driven (LLM supplies intent). Overloading the same command conflates
  two different UX models. The prompt strategy, action taxonomy, and confirmation
  flow are all different enough to warrant separation.

### 2. New top-level `auto-organize` command with dedicated module
- **Pros:** Clean separation of concerns, purpose-built prompt and validation,
  can enforce stricter safety guardrails (e.g., no delete, individual confirm)
- **Cons:** Some code duplication with `organize()` — mitigated by extracting
  shared logic into `auto_organizer.py` functions.

### 3. Fully autonomous scheduled organizer (ARQ-based, no confirmation)
- **Pros:** True automation
- **Cons:** Far too dangerous for v1. LLM-driven email mutation without human
  review is not acceptable given prompt injection risks and classification error rates.

## Decision
**Option 2: New top-level `auto-organize` command.** The discovery-driven workflow
is meaningfully different from instruction-driven `labels organize`. Shared execution
logic (batch apply, folder validation) will be extracted into `auto_organizer.py` so
both commands can use it.

## Design

### CLI Interface

```
python -m protonmail_claude auto-organize [OPTIONS]
```

| Flag | Type | Default | Purpose |
|------|------|---------|---------|
| `--folder` | str | `INBOX` | IMAP folder to scan |
| `--dry-run` | bool | False | Show recommendations only |
| `--yes` | bool | False | Bulk-confirm non-destructive actions |
| `--skip-actions` | str | `""` | Comma-separated actions to suppress |
| `--output` | str | None | Write recommendations as JSON |
| `--max-emails` | int | 50 | Cap on unread emails per run |
| `--batch-size` | int | 20 | Emails per LLM call |
| `--metadata-only` | bool | False | Skip body fetch, classify on sender/subject/date only |
| `--verbose` | bool | False | Print token usage per batch |

### Action Taxonomy (6 actions — no `delete`)

| Action | Description | Extra fields |
|--------|-------------|-------------|
| `archive` | Move to Archive | — |
| `move` | Move to specific folder | `dest_folder`, `create_folder_if_missing` |
| `label` | Apply IMAP flag, leave in place | `label` |
| `flag` | Star / mark important | — |
| `mark_read` | Add `\Seen` flag | — |
| `trash` | Move to Trash folder | — |
| `skip` | Do nothing | — |

`delete` is excluded from v1. `trash` moves to the Proton Trash folder, which is
recoverable. This decision can be revisited in v2 with an eval harness.

### Execution Flow

1. **Fetch** — `search(["UNSEEN"])` in target folder. Early exit if empty.
   Cap at `--max-emails` (oldest first), warn if more remain.
2. **Analyze** — Batch emails to LLM with `call_claude_json`. Include
   `available_folders` in payload so LLM targets existing folders. Bodies
   truncated to 800 chars and wrapped in `<email_body>` delimiters.
3. **Validate** — Each LLM recommendation passes through
   `_validate_recommendation()`: check required fields, verify UID is in
   fetched set, verify `dest_folder` exists or `create_folder_if_missing`
   is true. Malformed entries silently dropped with warning.
4. **Pre-pass** — Execute all `create_folder` operations before any moves.
5. **Present** — Human-readable table grouped by action type:
   `#  ACTION  SENDER  SUBJECT  DESTINATION  REASON`
6. **Confirm** — Interactive: bulk confirm for non-trash, individual confirm
   for `trash`. `--yes` skips bulk only, never skips `trash` confirm.
   `--dry-run` exits after display.
7. **Execute** — Apply via `LabelManager` methods. Check UIDVALIDITY before
   writes; abort if changed.
8. **Report** — Summary line: N applied, N skipped, N errors, tokens used.

### Safety Guardrails (P0 — non-negotiable)

1. **Prompt injection mitigation** — Email bodies wrapped in `<email_body>`
   tags. System prompt explicitly states content in these tags is untrusted
   user data, never instructions.
2. **UIDVALIDITY check** — Capture on folder select, assert unchanged before
   any write operation. Abort with clear error if stale.
3. **Schema validation** — `_validate_recommendation()` checks field types
   and required fields. Drops malformed entries.
4. **UID allowlist** — Only UIDs from the original fetch are accepted. LLM-
   hallucinated UIDs rejected.
5. **Folder validation** — `dest_folder` checked against fetched folder list.
   Unknown folders without `create_folder_if_missing=true` are rejected.
6. **No `delete` action** — Only `trash` (recoverable).
7. **`trash` requires individual confirmation** — even with `--yes`.
8. **`--dry-run` enforced at executor level** — not just CLI layer.
9. **`--max-emails` default 50** — prevents token cost surprises.
10. **Retry wrapper** — `call_claude_json` gets 3 retries with exponential
    backoff (1s/2s/4s) for rate limit errors. General fix for all commands.

### Data Model

**`RecommendedAction`** dataclass:
- `uid: int`, `action: str`, `dest_folder: str | None`, `label: str | None`
- `create_folder_if_missing: bool`, `reason: str`
- `sender: str`, `subject: str` (populated from fetched EmailMessage, not LLM)
- `body_available: bool`

**`AutoOrganizeResult`** dataclass:
- `total_analyzed: int`, `recommendations: list[RecommendedAction]`
- `applied: list[RecommendedAction]`, `skipped: list[RecommendedAction]`
- `errors: list[dict]`, `dry_run: bool`
- `summary: str` (property), `to_json() -> str`

### New Code

- `src/protonmail_claude/auto_organizer.py` — dataclasses, validation,
  batch analysis, single-action executor, end-to-end pipeline
- `src/protonmail_claude/prompts/auto_organize_system.txt` — system prompt
- `cli.py` — new `@app.command()` for `auto-organize`
- `imap_client.py` — add `fetch_by_uids()` method, UIDVALIDITY tracking
- `claude_client.py` — add retry wrapper to `call_claude_json`
- `tests/test_auto_organizer.py`

### Implementation Priority

**P0 (must have for v1):**
UIDVALIDITY check, schema validation, prompt injection mitigation, empty inbox
guard, UID allowlist, retry wrapper, trash-only (no delete), individual confirm
for trash, human-readable table, batch loop with create_folder pre-pass,
progress echo lines.

**P1 (should have, same sprint):**
Audit log (`~/.protonmail_claude/auto_organize.log` as JSON lines),
`--metadata-only` flag, token usage summary, `body_available=false` →
no `trash` recommendation enforced in executor, folder validation pre-check.

**P2 (defer to v2):**
Eval harness for classification accuracy, `rich` progress bars, per-language
accuracy investigation, partial-copy dedup recovery, merge shared code between
`auto_organizer.py` and `organize()`.

## Consequences
- Adds `auto_organizer.py` as a new module (~300 lines estimated)
- UIDVALIDITY check and retry wrapper are general improvements to the stack
- `labels organize` remains unchanged; shared logic refactored over time (P2)
- No `delete` action limits capability but eliminates irreversible data loss risk
- Body content sent to Groq API (accepted trade-off, same as `digest.py`)
- `--metadata-only` provides an opt-out for privacy-sensitive users (P1)
