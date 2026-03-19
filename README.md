# protonmail-claude

A Python CLI that connects ProtonMail (via Proton Bridge) to an LLM (Groq API) for email triage, draft generation, and inbox organization.

## What It Does

- **Digest** — Fetch recent emails and generate a prioritized summary
- **Draft** — Generate reply drafts for email threads with optional send
- **Organize** — Execute natural language email organization instructions ("Move all newsletters to Archive")
- **Auto-Organize** — LLM-driven triage of unread emails: recommends actions (archive, move, flag, trash, etc.) and applies them with confirmation

## Prerequisites

- **Python 3.12+**
- **Proton Bridge** running on macOS (localhost IMAP:1143 / SMTP:1025)
- **Groq API key** (free tier works)

## Setup

```bash
# Clone and install
git clone <repo-url>
cd protonmail-claude-imap
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your Proton Bridge credentials and Groq API key
```

### Environment Variables

```bash
# Proton Bridge (localhost only)
PROTON_IMAP_HOST=127.0.0.1
PROTON_IMAP_PORT=1143
PROTON_SMTP_HOST=127.0.0.1
PROTON_SMTP_PORT=1025
PROTON_EMAIL=you@proton.me
PROTON_BRIDGE_PASSWORD=<bridge-generated password>

# Groq API (OpenAI-compatible)
GROQ_API_KEY=<your key>
LLM_MODEL=openai/gpt-oss-120b
```

## Usage

All commands run via `python -m protonmail_claude`.

### Email Digest

```bash
# Summarize last 20 emails
python -m protonmail_claude digest

# Custom count, save to file
python -m protonmail_claude digest --count 50 --output digest.json
```

### Draft Replies

```bash
# Generate a draft reply
python -m protonmail_claude draft --uid 1042

# Generate and send (with confirmation prompt)
python -m protonmail_claude draft --uid 1042 --send
```

### Label Management

```bash
# List all folders
python -m protonmail_claude labels list

# Create a folder
python -m protonmail_claude labels create --name "Projects/NewProject"

# Move a message
python -m protonmail_claude labels move --uid 1042 --dest "Archive/Newsletters"

# Natural language organization
python -m protonmail_claude labels organize "Move all newsletters to Archive/Newsletters"
python -m protonmail_claude labels organize "Move all newsletters to Archive/Newsletters" --dry-run
```

### Auto-Organize (New)

LLM-driven triage of unread emails — the LLM recommends an action for each email, you review, then apply.

```bash
# Preview recommendations (dry run)
python -m protonmail_claude auto-organize --dry-run

# Interactive mode — review and confirm
python -m protonmail_claude auto-organize

# Auto-confirm non-destructive actions (trash still prompts individually)
python -m protonmail_claude auto-organize --yes

# Process more emails, classify on metadata only
python -m protonmail_claude auto-organize --max-emails 100 --metadata-only

# Suppress specific action types
python -m protonmail_claude auto-organize --skip-actions "trash,flag"

# Save recommendations as JSON
python -m protonmail_claude auto-organize --dry-run --output recommendations.json
```

**Actions the LLM can recommend:** `archive`, `move`, `label`, `flag`, `mark_read`, `trash`, `skip`

**Safety guarantees:**
- No `delete` action — only `trash` (recoverable via Proton Trash folder)
- `trash` always requires individual confirmation, even with `--yes`
- Email bodies wrapped in content boundaries to mitigate prompt injection
- LLM-hallucinated UIDs rejected against the fetched allowlist
- Destination folders validated against available IMAP folders
- UIDVALIDITY checked before any write operation
- `--dry-run` enforced at the executor level, not just CLI

#### Shell Loop for Full Inbox Processing

To drain an entire inbox, use a shell loop:

```bash
while python -m protonmail_claude auto-organize --max-emails 50 --yes; do
    echo "Batch complete, sleeping 10s..."
    sleep 10
done
```

## Architecture

```
ProtonMail (encrypted)
        |
Proton Bridge (localhost IMAP:1143 / SMTP:1025)
        |
Python IMAP/SMTP client (IMAPClient + smtplib)
        |
Groq API (OpenAI-compatible SDK)
        |
Outputs: digest, draft replies, label mutations, auto-organize
```

## Project Structure

```
src/protonmail_claude/
    __main__.py          # python -m entry point
    imap_client.py       # IMAP connection, fetch, search, UIDVALIDITY tracking
    claude_client.py     # LLM calls via Groq (with retry wrapper)
    label_manager.py     # Folder CRUD, move, bulk move, NL organize
    digest.py            # Digest pipeline (fetch -> summarize)
    drafter.py           # Draft reply pipeline (fetch thread -> draft)
    auto_organizer.py    # Auto-organize pipeline (fetch unread -> triage -> execute)
    cli.py               # Typer CLI entry points
    prompts/             # System prompts for LLM calls
tests/
    test_imap_client.py
    test_claude_client.py
    test_digest.py
    test_drafter.py
    test_label_manager.py
    test_auto_organizer.py
DECISION/                # Architecture decision records
```

## Testing

```bash
# Run all tests (87 tests)
pytest

# With coverage
pytest --cov=src --cov-report=term-missing

# Specific module
pytest tests/test_auto_organizer.py -v
```

All tests use mocked IMAP sessions and LLM responses — no live connections in CI.

## Design Decisions

Architecture decisions are documented in `DECISION/`:

| Doc | Topic |
|-----|-------|
| [001](DECISION/001-imap-library-selection.md) | IMAP library selection (IMAPClient) |
| [002](DECISION/002-auto-organize.md) | Auto-organize command design and safety guardrails |
| [003](DECISION/003-loop-mode.md) | Loop mode for full inbox processing |
| [004](DECISION/004-folder-recommend.md) | Folder structure recommendations |

New features require a DECISION doc before implementation.

## Roadmap

- [x] IMAP client with Proton Bridge support
- [x] LLM-powered email digest
- [x] Auto-draft reply generation
- [x] Natural language email organization
- [x] Auto-organize with safety guardrails (DECISION-002)
- [ ] `--loop` flag for continuous processing (DECISION-003)
- [ ] `labels recommend` for folder structure analysis (DECISION-004)
