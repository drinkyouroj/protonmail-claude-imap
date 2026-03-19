"""IMAP client for Proton Bridge connection."""

from __future__ import annotations

import email
import email.header
import email.utils
import os
from dataclasses import dataclass, field
from datetime import datetime

from imapclient import IMAPClient


@dataclass
class EmailMessage:
    """Parsed email message."""

    uid: int
    sender: str
    subject: str
    date: datetime | None
    body: str
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)


def _decode_header(raw: str | None) -> str:
    """Decode an RFC 2047 encoded header value."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain-text body from an email message, falling back to HTML."""
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""

    plain = ""
    html = ""
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type == "text/plain" and not plain:
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            plain = payload.decode(charset, errors="replace") if payload else ""
        elif content_type == "text/html" and not html:
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            html = payload.decode(charset, errors="replace") if payload else ""

    return plain or html


def _parse_references(raw: str | None) -> list[str]:
    """Parse References header into a list of message IDs."""
    if not raw:
        return []
    return raw.strip().split()


def _parse_message(uid: int, raw_bytes: bytes) -> EmailMessage:
    """Parse raw email bytes into an EmailMessage."""
    msg = email.message_from_bytes(raw_bytes)

    date_str = msg.get("Date")
    parsed_date = None
    if date_str:
        date_tuple = email.utils.parsedate_to_datetime(date_str)
        parsed_date = date_tuple

    return EmailMessage(
        uid=uid,
        sender=_decode_header(msg.get("From")),
        subject=_decode_header(msg.get("Subject")),
        date=parsed_date,
        body=_extract_body(msg),
        message_id=msg.get("Message-ID"),
        in_reply_to=msg.get("In-Reply-To"),
        references=_parse_references(msg.get("References")),
    )


class ProtonIMAPClient:
    """High-level IMAP client for Proton Bridge."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        email_address: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host or os.getenv("PROTON_IMAP_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("PROTON_IMAP_PORT", "1143"))
        self.email_address = email_address or os.getenv("PROTON_EMAIL", "")
        self.password = password or os.getenv("PROTON_BRIDGE_PASSWORD", "")
        self._client: IMAPClient | None = None

    def connect(self) -> None:
        """Connect and authenticate to Proton Bridge."""
        self._client = IMAPClient(self.host, port=self.port, ssl=False)
        self._client.starttls()
        self._client.login(self.email_address, self.password)

    def disconnect(self) -> None:
        """Logout and close the connection."""
        if self._client:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    @property
    def client(self) -> IMAPClient:
        """Return the active IMAP client, raising if not connected."""
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    def fetch_recent(self, folder: str = "INBOX", count: int = 20) -> list[EmailMessage]:
        """Fetch the most recent `count` emails from `folder`."""
        self.client.select_folder(folder, readonly=True)
        uids = self.client.search(["ALL"])
        recent_uids = uids[-count:] if len(uids) > count else uids

        if not recent_uids:
            return []

        raw_messages = self.client.fetch(recent_uids, ["RFC822"])
        messages = []
        for uid in recent_uids:
            if uid in raw_messages and b"RFC822" in raw_messages[uid]:
                messages.append(_parse_message(uid, raw_messages[uid][b"RFC822"]))

        return messages

    def fetch_by_uid(self, uid: int, folder: str = "INBOX") -> EmailMessage | None:
        """Fetch a single email by UID."""
        self.client.select_folder(folder, readonly=True)
        raw_messages = self.client.fetch([uid], ["RFC822"])

        if uid not in raw_messages or b"RFC822" not in raw_messages[uid]:
            return None

        return _parse_message(uid, raw_messages[uid][b"RFC822"])

    def search(self, criteria: list[str], folder: str = "INBOX") -> list[int]:
        """Search for messages matching IMAP criteria. Returns list of UIDs."""
        self.client.select_folder(folder, readonly=True)
        return self.client.search(criteria)

    def fetch_thread(self, uid: int, folder: str = "INBOX") -> list[EmailMessage]:
        """Fetch all messages in the same thread as the given UID.

        Uses References/In-Reply-To headers to find related messages.
        """
        root = self.fetch_by_uid(uid, folder)
        if root is None:
            return []

        # Collect all message IDs in the thread
        thread_ids: set[str] = set()
        if root.message_id:
            thread_ids.add(root.message_id)
        if root.in_reply_to:
            thread_ids.add(root.in_reply_to)
        thread_ids.update(root.references)

        if not thread_ids:
            return [root]

        # Search for each message ID in the thread
        self.client.select_folder(folder, readonly=True)
        thread_uids: set[int] = {uid}
        for msg_id in thread_ids:
            found = self.client.search(["HEADER", "Message-ID", msg_id])
            thread_uids.update(found)

        if len(thread_uids) <= 1:
            return [root]

        # Fetch all thread messages
        raw_messages = self.client.fetch(list(thread_uids), ["RFC822"])
        messages = []
        for msg_uid in sorted(thread_uids):
            if msg_uid in raw_messages and b"RFC822" in raw_messages[msg_uid]:
                messages.append(_parse_message(msg_uid, raw_messages[msg_uid][b"RFC822"]))

        return messages

    def __enter__(self) -> ProtonIMAPClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.disconnect()
