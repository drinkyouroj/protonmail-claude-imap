# DECISION-005: Training Interface for Auto-Organize

## Status
ACCEPTED

## Context
The auto-organize command (DECISION-002) works but has three persistent problems:

1. **Invented folder names** — The LLM recommends folders like `Labels/Marketing`
   that don't exist, causing recommendations to be dropped.
2. **Inconsistent categorization** — The same sender (e.g., Railway) gets different
   destinations across batches because each batch has no context about prior decisions.
3. **No learning** — Every run starts fresh with no memory of user preferences or
   past corrections.

A 5-agent debate evaluated: feedback loops, explicit rules, few-shot examples,
user profile documents, and a hybrid layered approach. The hybrid (Architect #5)
correctly identified these as complementary layers, not competing alternatives.

## Options Considered

### 1. Feedback loop only (corrections → SQLite → few-shot injection)
- **Pros:** Low friction, learns from normal usage
- **Cons:** Slow to converge (~60+ corrections needed), doesn't fix invented folders

### 2. Rules engine only (TOML rules pre-filter before LLM)
- **Pros:** Deterministic, fast, solves consistency for known senders
- **Cons:** Requires upfront configuration, doesn't help with novel senders

### 3. Few-shot examples only (inject approved actions into prompt)
- **Pros:** Zero-config, improves over time
- **Cons:** Token cost grows, doesn't fix invented folders, slow convergence

### 4. User profile only (markdown philosophy doc in system prompt)
- **Pros:** Fixes invented folders immediately, gives LLM stable context
- **Cons:** Requires manual authoring, no automatic learning

### 5. Hybrid three-layer stack (profile + patterns + rules)
- **Pros:** Each layer solves a different problem; layers compose cleanly;
  70-80% of emails handled without LLM once patterns mature
- **Cons:** More files to manage (mitigated by auto-generation)

## Decision
**Option 5: Hybrid three-layer stack.** Phased delivery — P0 solves the immediate
pain with minimal code, P1 adds deterministic bypass for known senders.

## Design

### Three-Layer Decision Stack

```
Email arrives in batch
        |
        v
Layer 1: Deterministic Rules (rules.toml)         [P1]
  Exact/glob match on FROM/SUBJECT → action
  < 1ms, no LLM. First match wins.
        |
        v (unmatched)
Layer 2: Pattern Cache (patterns.json)             [P0 seed, P1 auto-apply]
  Sender-domain → folder mapping, learned from confirmed actions
  confidence >= 0.8 and count >= 3 → auto-apply
        |
        v (unmatched)
Layer 3: LLM with profile context                  [P0]
  System prompt includes: profile.md + live folder list + pattern examples
  Handles novel senders and ambiguous emails
```

### File Layout

```
~/.config/protonmail-claude/
    profile.md       # User's organization philosophy (P0)
    rules.toml       # Explicit deterministic rules (P1)
    patterns.json    # Auto-learned sender→action cache (P0)
```

### P0 — v0.2.0: Solve Immediate Pain

#### 1. Inject live folder list into LLM prompt

Add the real IMAP folder list to every `_serialize_emails` payload. The system
prompt already says "Only recommend folders that appear in available_folders" —
this makes that constraint enforceable.

Already partially done: `_serialize_emails` includes `available_folders`. The
fix is to also add an explicit constraint to the system prompt: "You MUST use
only folder names from the available_folders list. Do not invent new folder
names unless create_folder_if_missing is set to true."

#### 2. Organization profile (`profile.md`)

A markdown file the user writes once describing their folder structure and
preferences. Injected into the LLM system prompt on every call.

```markdown
# My Email Organization

## Folder Structure
- CI/Builds: Build notifications (Railway, GitHub Actions, Vercel)
- Reading/Newsletters: Substack, Beehiiv, any newsletter
- Reading/Politics: Political content, campaigns, voting
- Financial: Invoices, receipts, billing
- Archive: Default for anything not matching above

## Rules
- Railway build failures are NOT urgent, move to CI/Builds
- Never flag newsletters
- Groq invoices go to Financial
- Do not invent folder names not listed above
```

Implementation:
- `load_profile()` reads from `~/.config/protonmail-claude/profile.md`
- Falls back to `$PROJECT_ROOT/.organize-profile.md`
- Appended to system prompt with "OVERRIDES default categorization" wrapper
- Token guard: truncate at ~800 tokens with a warning
- `profile init` command bootstraps from IMAP folder list via LLM
- `profile edit` opens `$EDITOR`

#### 3. Pattern cache (`patterns.json`)

After each confirmed batch, write sender-domain → action mappings for all
applied recommendations. On next run, inject matching patterns as few-shot
context in the LLM prompt.

```json
{
  "version": 1,
  "patterns": {
    "notify.railway.app": {
      "action": "move",
      "dest": "CI/Builds",
      "confidence": 0.95,
      "confirmed": 12,
      "rejected": 0,
      "last_seen": "2026-03-19"
    }
  }
}
```

P0 behavior: patterns are injected as few-shot bias ("previously, emails from
railway.app were moved to CI/Builds") but NOT auto-applied. The LLM still
makes the decision, guided by the examples.

### P1 — v0.3.0: Deterministic Bypass

#### 4. Pattern auto-apply

Patterns with `confirmed >= 3` and `confidence >= 0.8` are applied without
sending the email to the LLM. Still shown in the confirmation prompt with a
`[pattern]` tag. The user can override, which reduces the pattern's confidence.

#### 5. Explicit rules (`rules.toml`)

```toml
[[rule]]
match_from = "*@substack.com"
action = "move"
dest = "Reading/Newsletters"

[[rule]]
match_from = "*railway*"
match_subject = "*build failed*"
action = "move"
dest = "CI/Builds"
```

Rules are evaluated before patterns. First match wins. Glob matching via
`fnmatch`. CLI commands: `rules add`, `rules list`, `rules remove`.

### P2 — v0.4.0: Polish

- `patterns list` and `patterns promote` (move high-confidence to rules.toml)
- `rules suggest` (mine patterns.json for rule candidates)
- Confidence decay for patterns not seen in 90 days
- Inline correction at confirmation time (edit a recommendation → updates pattern)

### CLI Commands

**P0:**
```bash
protonmail-claude profile init       # Bootstrap from IMAP folders via LLM
protonmail-claude profile edit       # Open $EDITOR
protonmail-claude profile show       # Print current profile
```

**P1:**
```bash
protonmail-claude rules add --from "*@substack.com" --action move --dest "Reading/Newsletters"
protonmail-claude rules list
protonmail-claude rules remove <id>
protonmail-claude rules test --count 50   # Dry-run rules against recent emails
```

### New Code

**P0:**
- `src/protonmail_claude/profile.py` — load, validate, bootstrap (~100 LOC)
- `src/protonmail_claude/pattern_store.py` — read, write, match, inject (~120 LOC)
- Modify `auto_organizer.py` — inject profile + patterns into prompt (~30 LOC)
- Modify `cli.py` — `profile` sub-commands (~40 LOC)
- `prompts/auto_organize_system.txt` — strengthen folder constraint
- Tests (~100 LOC)

**P1:**
- `src/protonmail_claude/rules_engine.py` — TOML load, match, pre-filter (~150 LOC)
- Modify `auto_organizer.py` — three-layer dispatch (~50 LOC)
- Modify `cli.py` — `rules` sub-commands (~60 LOC)
- Tests (~80 LOC)

### What This Does NOT Do

- No model fine-tuning — pure prompt engineering with persistent context
- No vector embeddings or semantic search — pattern matching is exact/glob
- No automatic rule generation — rules are always user-authored (P2 suggests)
- No cross-user learning — all state is local and per-user

## Consequences
- P0 adds ~400 LOC and 2 new modules. No new dependencies.
- Profile.md becomes the single source of truth for organization preferences
- Patterns.json auto-builds from usage, requiring zero extra user effort
- The LLM sees fewer emails over time as patterns mature (cost savings)
- Folder name invention is eliminated by live folder list injection
- Cross-batch consistency improves immediately from profile context
