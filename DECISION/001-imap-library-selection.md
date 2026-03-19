# DECISION-001: IMAP Library Selection

## Status
ACCEPTED

## Context
We need a Python IMAP client to connect to Proton Bridge running on localhost
(IMAP on port 1143). The client must support:
- IMAP4rev1 over STARTTLS/SSL on localhost
- UID-based fetch and search
- Folder operations (CREATE, DELETE, LIST, SELECT)
- Flag manipulation (STORE +FLAGS/-FLAGS)
- COPY and EXPUNGE for message moves
- Reliable connection handling (Bridge may restart)

## Options Considered

### 1. `imaplib` (stdlib)
- **Pros:** Zero dependencies, always available, stable
- **Cons:** Low-level byte-string API, no built-in UID support helpers,
  verbose error handling, painful to parse responses manually

### 2. `IMAPClient` (imapclient)
- **Pros:** High-level Pythonic API, built-in UID mode (default), clean folder
  operations, well-maintained (active since 2009), good documentation,
  handles response parsing internally
- **Cons:** External dependency (~50KB), slightly less control than raw imaplib

### 3. `aioimaplib`
- **Pros:** Async-native, good for concurrent mailbox operations
- **Cons:** Smaller community, async adds complexity we don't need yet (our
  pipeline is sequential fetch → process → output), less mature

## Decision
**IMAPClient** (`imapclient` package). It provides the right abstraction level —
high enough to avoid boilerplate parsing, low enough to do everything we need
(UID ops, folder CRUD, flag manipulation). The sequential nature of our pipeline
doesn't justify async complexity. If we later add ARQ-based concurrent fetching,
IMAPClient works fine in threaded workers.

## Consequences
- Adds `imapclient>=3.0.0` as a runtime dependency
- All IMAP code uses IMAPClient's API — if we ever need raw imaplib, we can
  access it via `client._imap` but should avoid this
- Test mocking targets IMAPClient methods, not imaplib
- Connection retry/reconnect logic is our responsibility (IMAPClient doesn't
  auto-reconnect)
