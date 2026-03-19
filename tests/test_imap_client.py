"""Tests for imap_client module."""

from __future__ import annotations

from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

from protonmail_claude.imap_client import (
    EmailMessage,
    ProtonIMAPClient,
    _decode_header,
    _extract_body,
    _parse_message,
    _parse_references,
)


# --- Helpers ---


def _make_raw_email(
    sender: str = "alice@example.com",
    subject: str = "Test Subject",
    body: str = "Hello, world!",
    date: str = "Thu, 19 Mar 2026 10:30:00 +0000",
    message_id: str = "<msg1@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
    html_body: str | None = None,
) -> bytes:
    """Build a raw email message as bytes."""
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEText(body, "plain")

    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    return msg.as_bytes()


# --- Unit tests: helper functions ---


class TestDecodeHeader:
    def test_plain_ascii(self):
        assert _decode_header("Hello") == "Hello"

    def test_none_returns_empty(self):
        assert _decode_header(None) == ""

    def test_rfc2047_encoded(self):
        encoded = "=?utf-8?B?SGVsbG8gV29ybGQ=?="
        assert _decode_header(encoded) == "Hello World"


class TestExtractBody:
    def test_plain_text(self):
        import email as email_mod

        raw = _make_raw_email(body="Plain text body")
        msg = email_mod.message_from_bytes(raw)
        assert _extract_body(msg) == "Plain text body"

    def test_multipart_prefers_plain(self):
        import email as email_mod

        raw = _make_raw_email(body="Plain part", html_body="<p>HTML part</p>")
        msg = email_mod.message_from_bytes(raw)
        assert _extract_body(msg) == "Plain part"

    def test_multipart_falls_back_to_html(self):
        import email as email_mod

        raw = _make_raw_email(body="", html_body="<p>HTML only</p>")
        msg = email_mod.message_from_bytes(raw)
        # Plain is empty string, so falls back to HTML
        result = _extract_body(msg)
        assert "HTML only" in result


class TestParseReferences:
    def test_none(self):
        assert _parse_references(None) == []

    def test_single(self):
        assert _parse_references("<msg1@example.com>") == ["<msg1@example.com>"]

    def test_multiple(self):
        refs = "<msg1@example.com> <msg2@example.com>"
        assert _parse_references(refs) == ["<msg1@example.com>", "<msg2@example.com>"]


class TestParseMessage:
    def test_basic_email(self):
        raw = _make_raw_email()
        msg = _parse_message(100, raw)

        assert msg.uid == 100
        assert msg.sender == "alice@example.com"
        assert msg.subject == "Test Subject"
        assert msg.body == "Hello, world!"
        assert msg.message_id == "<msg1@example.com>"
        assert isinstance(msg.date, datetime)

    def test_with_thread_headers(self):
        raw = _make_raw_email(
            in_reply_to="<parent@example.com>",
            references="<root@example.com> <parent@example.com>",
        )
        msg = _parse_message(200, raw)

        assert msg.in_reply_to == "<parent@example.com>"
        assert msg.references == ["<root@example.com>", "<parent@example.com>"]


# --- Integration tests: ProtonIMAPClient (mocked) ---


@pytest.fixture
def mock_imap():
    """Create a ProtonIMAPClient with a mocked IMAPClient."""
    with patch("protonmail_claude.imap_client.IMAPClient") as MockIMAPClient:
        mock_instance = MagicMock()
        MockIMAPClient.return_value = mock_instance

        client = ProtonIMAPClient(
            host="127.0.0.1",
            port=1143,
            email_address="test@proton.me",
            password="test-password",
        )
        client.connect()

        yield client, mock_instance

        client.disconnect()


class TestProtonIMAPClientConnect:
    def test_connect_calls_starttls_and_login(self, mock_imap):
        _, mock_instance = mock_imap
        mock_instance.starttls.assert_called_once()
        mock_instance.login.assert_called_once_with("test@proton.me", "test-password")

    def test_not_connected_raises(self):
        client = ProtonIMAPClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            client.client

    def test_context_manager(self):
        with patch("protonmail_claude.imap_client.IMAPClient") as MockIMAPClient:
            mock_instance = MagicMock()
            MockIMAPClient.return_value = mock_instance

            with ProtonIMAPClient(
                host="127.0.0.1",
                port=1143,
                email_address="test@proton.me",
                password="pw",
            ) as client:
                assert client._client is not None

            mock_instance.logout.assert_called_once()


class TestFetchRecent:
    def test_fetches_last_n_emails(self, mock_imap):
        client, mock_instance = mock_imap
        raw1 = _make_raw_email(subject="Email 1", message_id="<1@test.com>")
        raw2 = _make_raw_email(subject="Email 2", message_id="<2@test.com>")

        mock_instance.search.return_value = [10, 20, 30]
        mock_instance.fetch.return_value = {
            20: {b"RFC822": raw1},
            30: {b"RFC822": raw2},
        }

        messages = client.fetch_recent(count=2)

        mock_instance.select_folder.assert_called_with("INBOX", readonly=True)
        mock_instance.fetch.assert_called_once_with([20, 30], ["RFC822"])
        assert len(messages) == 2
        assert messages[0].subject == "Email 1"
        assert messages[1].subject == "Email 2"

    def test_empty_mailbox(self, mock_imap):
        client, mock_instance = mock_imap
        mock_instance.search.return_value = []

        messages = client.fetch_recent()
        assert messages == []


class TestFetchByUid:
    def test_existing_uid(self, mock_imap):
        client, mock_instance = mock_imap
        raw = _make_raw_email(subject="Specific Email")
        mock_instance.fetch.return_value = {42: {b"RFC822": raw}}

        msg = client.fetch_by_uid(42)
        assert msg is not None
        assert msg.subject == "Specific Email"
        assert msg.uid == 42

    def test_missing_uid_returns_none(self, mock_imap):
        client, mock_instance = mock_imap
        mock_instance.fetch.return_value = {}

        msg = client.fetch_by_uid(999)
        assert msg is None


class TestSearch:
    def test_search_returns_uids(self, mock_imap):
        client, mock_instance = mock_imap
        mock_instance.search.return_value = [10, 20, 30]

        uids = client.search(["FROM", "alice@example.com"])
        mock_instance.search.assert_called_with(["FROM", "alice@example.com"])
        assert uids == [10, 20, 30]


class TestFetchThread:
    def test_fetches_related_messages(self, mock_imap):
        client, mock_instance = mock_imap

        # Start from the reply — it has in_reply_to and references pointing to root
        reply_raw = _make_raw_email(
            subject="Reply",
            message_id="<reply@test.com>",
            in_reply_to="<root@test.com>",
            references="<root@test.com>",
        )
        root_raw = _make_raw_email(
            subject="Root",
            message_id="<root@test.com>",
        )

        # fetch_by_uid fetches the reply (UID 2)
        # Then search for <reply@test.com> returns [2], search for <root@test.com> returns [1]
        # Then fetch all thread UIDs [1, 2]
        mock_instance.fetch.side_effect = [
            {2: {b"RFC822": reply_raw}},       # fetch_by_uid
            {1: {b"RFC822": root_raw}, 2: {b"RFC822": reply_raw}},  # fetch thread
        ]
        mock_instance.search.side_effect = [
            [2],  # search for <reply@test.com>
            [1],  # search for <root@test.com>
        ]

        messages = client.fetch_thread(2)
        assert len(messages) == 2
        assert messages[0].subject == "Root"
        assert messages[1].subject == "Reply"

    def test_missing_uid_returns_empty(self, mock_imap):
        client, mock_instance = mock_imap
        mock_instance.fetch.return_value = {}

        messages = client.fetch_thread(999)
        assert messages == []


class TestEnvDefaults:
    def test_reads_from_env(self):
        with patch.dict(
            "os.environ",
            {
                "PROTON_IMAP_HOST": "1.2.3.4",
                "PROTON_IMAP_PORT": "993",
                "PROTON_EMAIL": "env@proton.me",
                "PROTON_BRIDGE_PASSWORD": "envpass",
            },
        ):
            client = ProtonIMAPClient()
            assert client.host == "1.2.3.4"
            assert client.port == 993
            assert client.email_address == "env@proton.me"
            assert client.password == "envpass"
