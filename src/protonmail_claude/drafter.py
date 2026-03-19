"""Auto-draft reply pipeline: fetch thread → generate draft via Claude → optional send."""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText

from protonmail_claude.claude_client import call_claude_json
from protonmail_claude.imap_client import EmailMessage, ProtonIMAPClient


@dataclass
class DraftReply:
    """A generated reply draft."""

    subject: str
    body: str
    tone: str
    notes: str
    in_reply_to: str | None = None
    to_address: str | None = None


def _serialize_thread(messages: list[EmailMessage]) -> str:
    """Serialize a thread of emails to JSON for the Claude prompt."""
    serialized = []
    for msg in messages:
        serialized.append({
            "sender": msg.sender,
            "subject": msg.subject,
            "date": msg.date.isoformat() if msg.date else "",
            "body": msg.body[:3000],
        })
    return json.dumps(serialized, indent=2)


def generate_draft(
    thread: list[EmailMessage],
    model: str | None = None,
) -> DraftReply:
    """Generate a reply draft for an email thread using Claude.

    Args:
        thread: List of EmailMessage objects in chronological order.
        model: Optional model override.

    Returns:
        A DraftReply with the generated reply.

    Raises:
        ValueError: If the thread is empty.
    """
    if not thread:
        raise ValueError("Cannot draft a reply to an empty thread.")

    user_content = _serialize_thread(thread)
    result = call_claude_json(
        user_content=user_content,
        system_prompt_name="drafter_system",
        max_tokens=2048,
        model=model,
    )

    latest = thread[-1]

    return DraftReply(
        subject=result["subject"],
        body=result["body"],
        tone=result["tone"],
        notes=result.get("notes", ""),
        in_reply_to=latest.message_id,
        to_address=latest.sender,
    )


def draft_reply_for_uid(
    uid: int,
    folder: str = "INBOX",
    model: str | None = None,
    imap_client: ProtonIMAPClient | None = None,
) -> DraftReply:
    """Fetch a thread by UID and generate a reply draft.

    Args:
        uid: The UID of the email to reply to.
        folder: IMAP folder containing the email.
        model: Optional Claude model override.
        imap_client: Optional pre-configured IMAP client.

    Returns:
        A DraftReply for the thread.
    """
    if imap_client:
        thread = imap_client.fetch_thread(uid, folder=folder)
    else:
        with ProtonIMAPClient() as client:
            thread = client.fetch_thread(uid, folder=folder)

    if not thread:
        raise ValueError(f"No email found with UID {uid} in {folder}.")

    return generate_draft(thread, model=model)


def send_draft(
    draft: DraftReply,
    from_address: str | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    password: str | None = None,
) -> None:
    """Send a draft reply via SMTP through Proton Bridge.

    Args:
        draft: The DraftReply to send.
        from_address: Sender address (defaults to PROTON_EMAIL env var).
        smtp_host: SMTP host (defaults to PROTON_SMTP_HOST env var).
        smtp_port: SMTP port (defaults to PROTON_SMTP_PORT env var).
        password: Bridge SMTP password (defaults to PROTON_SMTP_PASSWORD env var).

    Raises:
        ValueError: If to_address is not set on the draft.
    """
    if not draft.to_address:
        raise ValueError("Draft has no to_address — cannot send.")

    from_addr = from_address or os.getenv("PROTON_EMAIL", "")
    host = smtp_host or os.getenv("PROTON_SMTP_HOST", "127.0.0.1")
    port = smtp_port or int(os.getenv("PROTON_SMTP_PORT", "1025"))
    pwd = password or os.getenv("PROTON_SMTP_PASSWORD", "") or os.getenv("PROTON_BRIDGE_PASSWORD", "")

    msg = MIMEText(draft.body, "plain")
    msg["Subject"] = draft.subject
    msg["From"] = from_addr
    msg["To"] = draft.to_address
    if draft.in_reply_to:
        msg["In-Reply-To"] = draft.in_reply_to

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(from_addr, pwd)
        server.send_message(msg)
