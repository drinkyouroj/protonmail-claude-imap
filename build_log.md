# Build Log — protonmail-claude

Chronological record of implementation sessions, decisions, and changes.

---

## Session 1 — 2026-03-19

### Project Bootstrap + Feature Implementation

**Starting state:** Existing codebase with IMAP client, digest pipeline, drafter, label manager, and CLI. Backend was Anthropic SDK — already migrated to Groq (OpenAI-compatible) before this session began.

### Changes Made

#### 1. Fix: Proton Bridge TLS + Module Entry Point

**Problem:** `python -m protonmail_claude` failed (`No module named protonmail_claude.__main__`). IMAP connection failed with `SSLCertVerificationError: self-signed certificate`.

**Fix:**
- Created `__main__.py` for `python -m` support
- Added SSL context with `check_hostname=False` and `verify_mode=ssl.CERT_NONE` for Bridge's self-signed cert

**Commit:** `04cec7d feat: switch from Anthropic SDK to Groq (OpenAI-compatible) and fix Bridge TLS`

#### 2. Design: Auto-Organize Command (Three-Agent Debate)

Ran the ARCHITECT / ADVERSARY / JUDGE protocol from CLAUDE.md to design the auto-organize feature.

**ARCHITECT proposed:** Full auto-organize pipeline with 7 actions, batched LLM calls, confirmation flow, `--dry-run`, folder validation.

**ADVERSARY attacked on:**
- Prompt injection via email body (P0 — accepted, mitigated with `<email_body>` tags)
- No UIDVALIDITY check (P0 — accepted, implemented)
- No schema validation (P0 — accepted, implemented)
- Data leakage to Groq (rejected — same risk as existing `digest.py`)
- Redundancy with `labels organize` (partially accepted — shared logic, different UX model)

**JUDGE verdict:** Build it with all P0 safety guardrails. Replace `delete` with `trash` (recoverable). Default `--max-emails` to 50.

**Commit:** `473ba73 docs: add DECISION docs for auto-organize, loop mode, and folder recommendations`

#### 3. Design: Loop Mode + Folder Recommendations (Two Parallel Debates)

Ran two concurrent three-agent debates for additional features:

**Loop Mode (DECISION-003):**
- ARCHITECT proposed full loop engine (~500 lines) with session resume, adaptive rate limiting, cost projection
- ADVERSARY argued: shell `while` loop achieves 80% of value; session resume is fragile (UIDVALIDITY invalidation); complexity exceeds remaining codebase
- JUDGE sided with minimal `--loop` flag (~50 lines): per-iteration reconnect, split stall detection, no session files

**Folder Recommendations (DECISION-004):**
- ARCHITECT proposed `labels recommend` with statistical profiling, 6 recommendation types, `--apply` flag
- ADVERSARY found critical safety issue: `--apply` routes through `organize()` which has zero DECISION-002 guardrails
- JUDGE ruled: ship read-only first, hard-gate `--apply` on DECISION-002 executor. 4 recommendation types in v1 (no merge/split). Substring containment for overlap detection (not edit distance).

#### 4. Implementation: DECISION-002 Auto-Organize

Implemented the full auto-organize command on `feature/auto-organize` branch.

**Infrastructure changes:**
- `imap_client.py` — Added `UIDValidityError`, `select_folder()` with UIDVALIDITY tracking, `assert_uidvalidity()`, `fetch_by_uids()`. Updated all existing methods to use `self.select_folder()` for consistent tracking.
- `claude_client.py` — Added `_call_with_retry()` with 3 retries and exponential backoff (1s/2s/4s) on `RateLimitError`. Benefits all LLM calls, not just auto-organize.

**New modules:**
- `auto_organizer.py` (290 lines) — `RecommendedAction` and `AutoOrganizeResult` dataclasses, `_serialize_emails()` with `<email_body>` prompt injection mitigation, `_validate_recommendation()` with full schema validation and UID allowlist, `_analyze_batch()` with LLM error handling, `_present_recommendations()` for human-readable table output, `_apply_recommendation()` for single-action execution, `auto_organize()` end-to-end pipeline.
- `prompts/auto_organize_system.txt` — System prompt with content boundary rules, action taxonomy, and output schema.
- `cli.py` — New `auto-organize` command with all DECISION-002 flags.

**Test coverage:**
- `test_auto_organizer.py` — 23 tests covering serialization, validation (12 edge cases), batch analysis (5 scenarios including LLM failures), and result formatting.
- Full suite: 87 tests passing, 0 regressions.

**Commit:** `e34c0c1 feat: implement auto-organize command (DECISION-002)`

#### 5. Documentation

- Updated `CLAUDE.md` to reflect actual Groq/OpenAI stack (was still referencing Anthropic SDK)
- Updated `.env.example` with `USER_CONTEXT` for future folder recommendations
- Wrote `README.md` with full usage guide
- Wrote `build_log.md` (this file)

### DECISION-002 P0 Checklist

| # | Requirement | Status | Implementation |
|---|-------------|--------|----------------|
| 1 | UIDVALIDITY check | Done | `imap_client.py:select_folder`, `assert_uidvalidity` |
| 2 | Schema validation | Done | `auto_organizer.py:_validate_recommendation` |
| 3 | Prompt injection mitigation | Done | `<email_body>` tags + system prompt rules |
| 4 | Empty inbox guard | Done | Early exit in `auto_organize()` |
| 5 | UID allowlist | Done | Validated in `_validate_recommendation` |
| 6 | Folder validation | Done | Checked against `available_folders` |
| 7 | No delete action | Done | `VALID_ACTIONS` excludes delete; only trash |
| 8 | Trash individual confirm | Done | Always prompts, even with `--yes` |
| 9 | Dry-run at executor level | Done | Checked in `auto_organize()` before execution |
| 10 | Max-emails default 50 | Done | CLI default and DECISION doc |
| 11 | Retry wrapper | Done | `claude_client.py:_call_with_retry` (3 retries) |
| 12 | Human-readable table | Done | `_present_recommendations()` grouped by action |
| 13 | Batch loop + create_folder pre-pass | Done | Folders created before any moves |
| 14 | Progress echo lines | Done | Per-phase status messages |

### Codebase Metrics

| Metric | Value |
|--------|-------|
| Source lines (src/) | 1,452 |
| Test lines (tests/) | 1,226 |
| Total tests | 87 |
| Test pass rate | 100% |
| Source modules | 8 |
| Test modules | 6 |
| System prompts | 4 |
| DECISION docs | 4 |

### What's Next

**Ready to build (designed, DECISION docs accepted):**
1. DECISION-003: `--loop` flag on `auto-organize` (~50 lines)
2. DECISION-004: `labels recommend` read-only command (~200 lines)

**Dependency order:** Both depend on DECISION-002 being complete (it is). They can be built in parallel.
