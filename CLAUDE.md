# CLAUDE.md — protonmail-claude-integration

## Project Overview

A Python-based integration that connects ProtonMail (via Proton Bridge on macOS) to an
LLM via the Groq API (OpenAI-compatible). Supports three core workflows: **email
digest/summarization**, **auto-draft reply generation**, and **label/folder management**.
Designed to run both as a local CLI pipeline and as a backend that surfaces capability
to Claude.ai via a Gmail proxy.

---

## Architecture

```
ProtonMail (encrypted)
        ↓
Proton Bridge (localhost IMAP:1143 / SMTP:1025)
        ↓
Python IMAP/SMTP client (IMAPClient + smtplib)
        ↓
Groq API (OpenAI-compatible SDK — openai/gpt-oss-120b)
        ↓
Outputs: digest, draft replies, label mutations
```

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| IMAP client | `IMAPClient` |
| SMTP | `smtplib` (stdlib) |
| LLM | `openai` SDK (Groq API, OpenAI-compatible) |
| Config | `python-dotenv` |
| CLI | `typer` |
| Testing | `pytest` |
| Task queue (optional) | `ARQ` (Redis-backed, consistent with ClipForge) |

---

## Repository Structure

```
protonmail-claude/
├── CLAUDE.md
├── DECISION/               # DECISION docs live here — required before any feature
├── .env.example
├── pyproject.toml
├── src/
│   └── protonmail_claude/
│       ├── __init__.py
│       ├── imap_client.py      # IMAP connect, fetch, search
│       ├── smtp_client.py      # SMTP send / draft
│       ├── label_manager.py    # Folder/label CRUD
│       ├── __main__.py         # python -m entry point
│       ├── claude_client.py    # LLM API calls (Groq/OpenAI-compatible)
│       ├── digest.py           # Digest pipeline
│       ├── drafter.py          # Auto-draft reply pipeline
│       └── cli.py              # Typer CLI entry points
└── tests/
    ├── conftest.py
    ├── test_imap_client.py
    ├── test_claude_client.py
    ├── test_digest.py
    └── test_drafter.py
```

---

## Environment Variables

```bash
# .env (never commit — .gitignore enforced)
PROTON_IMAP_HOST=127.0.0.1
PROTON_IMAP_PORT=1143
PROTON_SMTP_HOST=127.0.0.1
PROTON_SMTP_PORT=1025
PROTON_EMAIL=you@proton.me
PROTON_IMAP_PASSWORD=<bridge-generated IMAP password>
PROTON_SMTP_PASSWORD=<bridge-generated SMTP password>

GROQ_API_KEY=<your key>
LLM_MODEL=openai/gpt-oss-120b

# Optional — ARQ task queue
REDIS_URL=redis://localhost:6379
```

---

## DECISION Doc Requirement

**No feature implementation without a DECISION doc.**

Before writing any non-trivial code, create `DECISION/NNN-feature-name.md` using this
template:

```markdown
# DECISION-NNN: Feature Name

## Status
PROPOSED | ACCEPTED | REJECTED | SUPERSEDED

## Context
What problem are we solving? What constraints exist?

## Options Considered
1. Option A — pros/cons
2. Option B — pros/cons

## Decision
What we're doing and why.

## Consequences
What this enables, what it forecloses, what debt it carries.
```

---

## Adversarial Three-Agent Protocol

For any non-trivial design session, use the ARCHITECT / ADVERSARY / JUDGE pattern:

- **ARCHITECT** — proposes the implementation
- **ADVERSARY** — attacks it (edge cases, failure modes, security, complexity)
- **JUDGE** — synthesizes and renders a verdict with action items

Invoke explicitly in prompts:
```
Act as ARCHITECT. Propose an implementation for [feature].
Act as ADVERSARY. Attack the ARCHITECT's proposal.
Act as JUDGE. Synthesize and decide.
```

---

## Git Conventions

### Branching (Git Flow)
```
main          — production-stable
develop       — integration branch
feature/*     — new features (branch from develop)
fix/*         — bug fixes (branch from develop)
release/*     — release prep
```

### Commit Format (Conventional Commits)
```
feat: add digest pipeline with Claude summarization
fix: handle IMAP timeout on Bridge restart
chore: add IMAPClient to dependencies
docs: add DECISION-001 for IMAP library selection
test: add pytest fixtures for mock IMAP session
```

---

## Security Rules

- **Never commit `.env`** — `.gitignore` must include `.env` from day one
- **Never log Bridge password or API key** — scrub before any debug output
- **Bridge password ≠ Proton account password** — treat as separate secret
- **IMAP connections are localhost-only** — Bridge exposes no external surface; keep it that way
- **No email content in git history** — test fixtures use synthetic/mock data only

---

## Docker Safety Block

No Docker for this project by default — Bridge runs natively on macOS and requires
access to the local Keychain. If containerization is ever needed:

- Bridge must run on the host, not in a container
- Expose Bridge ports to container via `host.docker.internal`
- Document the port mapping in a DECISION doc before implementing

---

## Feature Specs

### 1. Digest Pipeline (`digest.py`)

**Trigger:** CLI command or scheduled ARQ task  
**Flow:**
1. IMAP fetch — last N emails from INBOX (configurable, default 20)
2. Extract: sender, subject, date, body (plain text preferred)
3. Batch or stream to Claude API with summarization prompt
4. Output: structured digest (terminal, file, or Notion capture)

**Claude prompt contract:**
- System: role as email triage assistant, output format spec
- User: serialized email batch (JSON)
- Response: JSON digest with `[{sender, subject, summary, priority, suggested_action}]`

---

### 2. Auto-Draft Reply (`drafter.py`)

**Trigger:** CLI — pass message UID or subject search  
**Flow:**
1. IMAP fetch target thread by UID
2. Extract full thread context
3. Send to Claude API with drafting prompt + user voice context
4. Return draft — **never auto-send** without explicit `--send` flag confirmation
5. SMTP send only on confirmed flag

**Safety rule:** Draft generation and sending are always two separate steps. No
single-command send without a confirmation prompt in the CLI.

---

### 3. Label/Folder Management (`label_manager.py`)

**Operations:**
- `list_folders()` — enumerate all IMAP folders
- `create_folder(name)` — IMAP CREATE
- `move_message(uid, dest_folder)` — IMAP COPY + STORE \Deleted + EXPUNGE
- `apply_label(uid, label)` — IMAP STORE +FLAGS
- `bulk_move(search_criteria, dest_folder)` — search + batch move

**Claude integration (optional):** Pass a natural language instruction
(`"Move all newsletters to /Archive/Newsletters"`) to Claude, which resolves it to
structured `label_manager` calls.

---

## Testing

```bash
# Run full suite
pytest

# Run with coverage
pytest --cov=src --cov-report=term-missing

# Run specific module
pytest tests/test_digest.py -v
```

**Test conventions:**
- Mock IMAP sessions with `unittest.mock` — never connect to live Bridge in tests
- Use `pytest.fixture` for reusable email message objects
- Eval harness for Claude outputs follows GhostEditor pytest eval pattern
- All Claude API calls in tests use mocked responses — no live API calls in CI

---

## LLM API Usage Conventions

```python
# Always use the model from env — never hardcode
import os
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

# Standard call pattern (via claude_client.py helpers)
from protonmail_claude.claude_client import call_claude, call_claude_json

# Text response
result = call_claude("prompt text", system_prompt_name="digest_system")

# JSON response (strips fences, parses safely)
data = call_claude_json("prompt text", system_prompt_name="organize_system")
```

The LLM backend is Groq (OpenAI-compatible). `claude_client.py` wraps the `openai`
SDK pointed at `https://api.groq.com/openai/v1`. The module name is historical.

- Keep system prompts in `src/protonmail_claude/prompts/` as `.txt` files — not
  hardcoded in function bodies
- Log token usage in debug mode for cost awareness
- Structured JSON outputs: instruct model to return JSON, strip fences, parse safely
- Email body content sent to Groq is an accepted trade-off — Bridge decrypts
  locally, and sending plaintext to a third-party API is inherent to LLM integration

---

## CLI Commands (Typer)

```bash
# All commands via python -m
python -m protonmail_claude <command>

# Fetch and summarize inbox digest
python -m protonmail_claude digest --count 20 --output digest.json

# Draft a reply to a specific email
python -m protonmail_claude draft --uid 1042
python -m protonmail_claude draft --uid 1042 --send   # confirmation required

# Label management
python -m protonmail_claude labels list
python -m protonmail_claude labels move --uid 1042 --dest "Archive/Newsletters"
python -m protonmail_claude labels create --name "Projects/GhostEditor"
python -m protonmail_claude labels organize "Move all newsletters to Newsletters"
python -m protonmail_claude labels recommend --sample-size 200
```

---

## Out of Scope (v1)

- OAuth or web-based auth (Bridge handles all auth)
- Attachment parsing or download
- Calendar integration
- Multi-account support
- Any GUI

---

## Related Projects

- **GhostEditor** — primary build target; shares pytest eval harness pattern
- **ClipForge** — shares ARQ task queue pattern if async scheduling is added
