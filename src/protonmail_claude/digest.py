"""Email digest pipeline: fetch → summarize via Claude → structured output."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from protonmail_claude.claude_client import call_claude_json
from protonmail_claude.imap_client import EmailMessage, ProtonIMAPClient


@dataclass
class DigestEntry:
    """A single entry in the email digest."""

    sender: str
    subject: str
    summary: str
    priority: str  # "high", "medium", "low"
    suggested_action: str


@dataclass
class Digest:
    """Complete email digest."""

    generated_at: str = ""
    email_count: int = 0
    entries: list[DigestEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _serialize_emails(messages: list[EmailMessage]) -> str:
    """Serialize email messages to JSON for the Claude prompt."""
    serialized = []
    for msg in messages:
        serialized.append({
            "sender": msg.sender,
            "subject": msg.subject,
            "date": msg.date.isoformat() if msg.date else "",
            "body": msg.body[:2000],  # Truncate long bodies to manage token usage
        })
    return json.dumps(serialized, indent=2)


def generate_digest(
    messages: list[EmailMessage],
    model: str | None = None,
) -> Digest:
    """Generate a digest from a list of email messages using Claude.

    Args:
        messages: List of EmailMessage objects to summarize.
        model: Optional model override.

    Returns:
        A Digest object with entries for each email.
    """
    if not messages:
        return Digest(
            generated_at=datetime.now().isoformat(),
            email_count=0,
        )

    user_content = _serialize_emails(messages)
    raw_entries = call_claude_json(
        user_content=user_content,
        system_prompt_name="digest_system",
        max_tokens=4096,
        model=model,
    )

    entries = [DigestEntry(**entry) for entry in raw_entries]

    return Digest(
        generated_at=datetime.now().isoformat(),
        email_count=len(messages),
        entries=entries,
    )


def fetch_and_digest(
    folder: str = "INBOX",
    count: int = 20,
    model: str | None = None,
    imap_client: ProtonIMAPClient | None = None,
) -> Digest:
    """End-to-end: connect to IMAP, fetch recent emails, generate digest.

    Args:
        folder: IMAP folder to fetch from.
        count: Number of recent emails to include.
        model: Optional Claude model override.
        imap_client: Optional pre-configured IMAP client (for testing).

    Returns:
        A Digest object.
    """
    if imap_client:
        messages = imap_client.fetch_recent(folder=folder, count=count)
        return generate_digest(messages, model=model)

    with ProtonIMAPClient() as client:
        messages = client.fetch_recent(folder=folder, count=count)
        return generate_digest(messages, model=model)
