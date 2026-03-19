# DECISION-004: Folder Structure Recommendations

## Status
ACCEPTED

## Context
Users with large inboxes accumulate emails without a clear folder strategy. The
existing `labels organize` and `auto-organize` commands act on emails, but neither
analyzes the overall folder structure to suggest improvements. We need a read-only
analysis command that examines folder layout and email patterns, then recommends
structural changes.

A three-agent debate (ARCHITECT / ADVERSARY / JUDGE) identified a critical safety
concern: the proposed `--apply` flag would route through `organize()`, which lacks
the DECISION-002 P0 safety guardrails (UIDVALIDITY check, UID allowlist, schema
validation). The JUDGE ruled: ship read-only first, gate `--apply` on DECISION-002
executor completion.

## Options Considered

### 1. `labels recommend` with `--apply` routing through `organize()`
- **Pros:** End-to-end workflow in one command
- **Cons:** `organize()` has no UIDVALIDITY check, no UID allowlist, no schema
  validation. The second LLM call inside `organize()` uses a different 50-message
  context window than the 200-message sample used for recommendations — the LLM
  executing operations reasons about different data than the LLM that produced
  the recommendations. This is both a safety and correctness failure.

### 2. `labels recommend` read-only, with pasteable `cli_command` output
- **Pros:** No mutation risk. User reviews recommendations and executes manually.
  Each `cli_command` goes through the normal `labels organize` path with full
  user visibility. `--apply` added later via `auto_organizer.py` executor.
- **Cons:** Extra manual step for users. No single-command workflow in v1.

### 3. Standalone analysis tool with no CLI integration
- **Pros:** Simplest possible implementation
- **Cons:** No actionable output. Users must translate recommendations into
  commands manually with no guidance.

## Decision
**Option 2: Read-only `labels recommend` with pasteable commands.** Safety comes
first. `--apply` is gated on DECISION-002 `auto_organizer.py` completion.

## Design

### CLI Interface

```
python -m protonmail_claude labels recommend [OPTIONS]
```

| Flag | Type | Default | Purpose |
|------|------|---------|---------|
| `--folder` | str | `INBOX` | Primary folder to analyze |
| `--all-folders` | bool | False | Analyze every folder |
| `--sample-size` | int | 200 | Max messages to sample per folder |
| `--output` | str | None | Write JSON report to this path |
| `--min-count` | int | 10 | Ignore sender clusters smaller than N |
| `--user-context` | str | None | Free-text bias (e.g. "software engineer, client work") |
| `--verbose` | bool | False | Show progress per phase and token usage |

No `--apply` flag in v1. This is a hard gate, not a deferral.

### Data Collection

**Phase 1: Folder Inventory** (all folders, always)
- IMAP STATUS on every folder: name, message count, unseen count
- `list_folders()` updated to return `(flags, name)` tuples
- System folders filtered out: Sent, Trash, Drafts, Spam, and any folder
  with `\Noselect` or `\NonExistent` flags

**Phase 2: Sender/Subject Profile** (sampled, per target folder)
- Fetch `BODY[HEADER.FIELDS (FROM SUBJECT DATE)]` for `--sample-size` most
  recent messages. If Bridge does not support partial fetch, fall back to
  full `RFC822` with a logged warning.
- Cluster key: `(display_name, full_address)` — NOT domain. Domain is a
  secondary grouping label only. This prevents the "all google.com together"
  problem where calendar, drive, payments, and personal mail merge.
- Subject patterns: tokenized prefixes, list tags (e.g. `[project-x]`),
  repeated patterns with count and examples.
- Per-folder health signals: oversized (>1000 messages), empty (0 messages),
  single-sender folders.

**Overlap Detection:**
- Case-insensitive substring containment (NOT edit distance). This kills
  false positives like "Work"/"Worm" while catching real overlaps like
  "Projects"/"Old Projects" and "Archive"/"Archives".
- System folders excluded from overlap analysis.
- Folder hierarchy separator normalized before comparison.

**Performance:**
- Headers only: ~200-400 bytes/message vs 5-50KB for full RFC822
- Client-side aggregation produces a profile JSON of ~5-15KB regardless
  of inbox size — LLM tokens bounded by cluster count, not message count
- 200-message default sample keeps total fetch under 100KB
- Progress lines per phase with `--verbose`

### LLM Prompt Strategy

- System prompt: `prompts/folder_recommend_system.txt`
- Profile payload includes: existing folder list with counts, sender clusters
  with counts and date ranges, subject patterns, overlap candidates
- System prompt instructs the LLM to anchor to existing folders first — create
  new folders only when no existing folder fits
- `--user-context` string injected into the user message to bias recommendations
- Sampling window warning included in prompt context so LLM knows coverage limits

### Recommendation Taxonomy (4 types in v1)

| Type | Description | Trigger |
|------|-------------|---------|
| `create_folder` | Suggest a new folder for an unorganized cluster | Sender cluster with N+ messages and no matching folder |
| `refile_cluster` | Move a cluster from INBOX to an existing folder | Sender cluster matches an existing folder's purpose |
| `delete_empty_folder` | Remove a folder with 0 messages | Empty folder detected |
| `archive_folder` | Archive a folder with no recent activity | 0 unseen, 0 recent, no growth in sample window |

`merge_folders` and `split_folder` are deferred to v2 — they are structurally
destructive (require moving many messages) and need the DECISION-002 executor's
safety guardrails to apply safely.

### Output Format

**Terminal (default):**
```
Folder Recommendations — INBOX (3,241 messages, sampled: 200)
NOTE: Recommendations based on 200 most recent messages only.

HIGH IMPACT
  [1] CREATE FOLDER: Newsletters
      143 messages from newsletter senders (substack.com, beehiiv.com)
      No existing folder matches.
      → python -m protonmail_claude labels organize "Move all newsletters to Newsletters"

  [2] REFILE: 78 receipt emails → Finance/Receipts
      Senders: billing@stripe.com, invoice@digitalocean.com
      Existing folder 'Finance/Receipts' matches.
      → python -m protonmail_claude labels organize "Move receipt emails to Finance/Receipts"

LOW IMPACT
  [3] DELETE EMPTY: 'Old Newsletters' (0 messages)
      → python -m protonmail_claude labels create --name "Old Newsletters"  # already empty

2 recommendations generated. Run with --output to save as JSON.
```

**JSON (`--output`):**
```json
{
  "generated_at": "2026-03-19T14:22:00Z",
  "valid_for_minutes": 30,
  "scope": "INBOX",
  "sample_size": 200,
  "total_in_scope": 3241,
  "recommendations": [
    {
      "rank": 1,
      "type": "create_folder",
      "impact": "high",
      "title": "CREATE FOLDER: Newsletters",
      "description": "143 messages from newsletter senders in INBOX.",
      "affected_count": 143,
      "reason": "substack.com (87), beehiiv.com (56) — no existing folder matches.",
      "organize_instruction": "Move all newsletters to Newsletters",
      "cli_command": "python -m protonmail_claude labels organize \"Move all newsletters to Newsletters\""
    }
  ]
}
```

### Integration with auto-organize

**v1:** No direct integration. Recommendations are informational. Users execute
the suggested `cli_command` strings manually.

**v2 (after DECISION-002 executor exists):** `--apply` flag routes each
recommendation's `organize_instruction` through `auto_organizer.py`'s executor,
which provides UIDVALIDITY check, UID allowlist, schema validation, and
per-operation confirmation. This is gated on DECISION-002 P0 completion.

### New Code

- `src/protonmail_claude/folder_recommender.py` — data collection, aggregation,
  profile serialization, LLM call, result dataclasses
- `src/protonmail_claude/prompts/folder_recommend_system.txt`
- `cli.py` — new `@labels_app.command("recommend")`
- `imap_client.py` — add `fetch_headers_only(uids, folder)` method; update
  `list_folders()` to return `(flags, name)` tuples
- `tests/test_folder_recommender.py`

### Implementation Priority

**P0 — required before ship:**
- System folder filtering in `list_folders()` (return flags)
- Cluster key = `(display_name, full_address)`; domain is label only
- Substring containment for overlap (not edit distance)
- `generated_at` + `valid_for_minutes` in all output
- Sampling window warning in terminal output
- `reason` field citing specific cluster data in every recommendation
- No `--apply` (hard gate)

**P1 — same sprint:**
- `fetch_headers_only()` with Bridge compatibility fallback
- Hierarchy separator normalization
- Progress lines per phase
- `--user-context` flag

**P2 — defer:**
- `--apply` via `auto_organizer.py` executor
- `merge_folders` and `split_folder` recommendation types
- Stratified sampling (time buckets)
- Sender-subject co-occurrence analysis
- Semantic overlap detection
- Eval harness for recommendation quality

## Resolved Assumptions

**Read-only gate in v1?** Yes. `--apply` is hard-gated on DECISION-002 executor
completion. Users copy-paste the generated `cli_command` strings to execute
recommendations manually. This is one extra step but eliminates mutation risk.

**Header-only fetch vs. full RFC822?** Try `BODY[HEADER.FIELDS]` first with an
automatic fallback to `RFC822` if Bridge returns errors or inconsistent results.
Log a warning on fallback so the behavior is visible. The bandwidth cost of RFC822
for 200 messages (~1-10MB) is acceptable for a read-only analysis command that runs
infrequently. Header-only is a performance optimization, not a correctness requirement.

**`--user-context` persistence?** Both. CLI flag (`--user-context "..."`) for per-run
overrides, plus an optional `USER_CONTEXT` env var in `.env` for a persistent default.
The CLI flag takes precedence when both are set. This avoids new config file surface
while giving zero-friction defaults to repeat users.

**Sample size default?** 200. Conservative enough for Groq's context limits, large
enough to capture meaningful patterns in a typical inbox. Users with very large
inboxes (10K+) can increase to 500 via `--sample-size`; the aggregation step keeps
LLM token usage bounded regardless.

**Recommendation scope in v1?** 4 types only: `create_folder`, `refile_cluster`,
`delete_empty_folder`, `archive_folder`. `merge_folders` and `split_folder` are
structurally destructive (bulk moves across folders) and require the DECISION-002
safety executor to apply safely. Deferred to v2.

## Consequences
- Adds `folder_recommender.py` as a new module (~200 lines estimated)
- `list_folders()` API change (returns tuples instead of strings) — callers
  in `label_manager.py` and `cli.py` must be updated
- Header-only fetch path is new and untested against Bridge — fallback to
  RFC822 ensures correctness at the cost of bandwidth
- Read-only constraint means no risk of data loss from this command
- `--apply` explicitly deferred — creates a clear dependency on DECISION-002
  executor completion before this feature can mutate emails
