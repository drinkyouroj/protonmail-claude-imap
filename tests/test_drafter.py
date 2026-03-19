"""Tests for drafter module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from protonmail_claude.drafter import (
    DraftReply,
    _serialize_thread,
    draft_reply_for_uid,
    generate_draft,
    send_draft,
)
from protonmail_claude.imap_client import EmailMessage


# --- Fixtures ---


@pytest.fixture
def sample_thread() -> list[EmailMessage]:
    return [
        EmailMessage(
            uid=1,
            sender="alice@example.com",
            subject="Project Update",
            date=datetime(2026, 3, 18, 14, 0, tzinfo=timezone.utc),
            body="Hey, how's the project going? Any blockers?",
            message_id="<msg1@example.com>",
        ),
        EmailMessage(
            uid=2,
            sender="bob@example.com",
            subject="Re: Project Update",
            date=datetime(2026, 3, 19, 9, 0, tzinfo=timezone.utc),
            body="Going well! Just waiting on the API keys from DevOps.",
            message_id="<msg2@example.com>",
            in_reply_to="<msg1@example.com>",
            references=["<msg1@example.com>"],
        ),
    ]


@pytest.fixture
def mock_claude_draft_response():
    return {
        "subject": "Re: Project Update",
        "body": "Hi Bob,\n\nThanks for the update. I'll ping DevOps about those API keys.\n\nBest,\nAlice",
        "tone": "neutral",
        "notes": "Might want to set a deadline for the keys.",
    }


# --- Tests ---


class TestSerializeThread:
    def test_serializes_thread(self, sample_thread):
        import json

        result = _serialize_thread(sample_thread)
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["sender"] == "alice@example.com"
        assert parsed[1]["sender"] == "bob@example.com"

    def test_truncates_long_bodies(self):
        msg = EmailMessage(
            uid=1,
            sender="test@test.com",
            subject="Long",
            date=datetime.now(tz=timezone.utc),
            body="x" * 5000,
        )
        import json

        result = _serialize_thread([msg])
        parsed = json.loads(result)
        assert len(parsed[0]["body"]) == 3000


class TestGenerateDraft:
    @patch("protonmail_claude.drafter.call_claude_json")
    def test_generates_draft(self, mock_call, sample_thread, mock_claude_draft_response):
        mock_call.return_value = mock_claude_draft_response

        draft = generate_draft(sample_thread)

        assert draft.subject == "Re: Project Update"
        assert "API keys" in draft.body
        assert draft.tone == "neutral"
        assert draft.in_reply_to == "<msg2@example.com>"
        assert draft.to_address == "bob@example.com"

        mock_call.assert_called_once()
        assert mock_call.call_args.kwargs["system_prompt_name"] == "drafter_system"

    def test_empty_thread_raises(self):
        with pytest.raises(ValueError, match="empty thread"):
            generate_draft([])


class TestDraftReplyForUid:
    @patch("protonmail_claude.drafter.call_claude_json")
    def test_with_injected_client(self, mock_call, sample_thread, mock_claude_draft_response):
        mock_call.return_value = mock_claude_draft_response
        mock_client = MagicMock()
        mock_client.fetch_thread.return_value = sample_thread

        draft = draft_reply_for_uid(2, imap_client=mock_client)

        assert draft.subject == "Re: Project Update"
        mock_client.fetch_thread.assert_called_once_with(2, folder="INBOX")

    @patch("protonmail_claude.drafter.call_claude_json")
    @patch("protonmail_claude.drafter.ProtonIMAPClient")
    def test_end_to_end(self, MockIMAPClient, mock_call, sample_thread, mock_claude_draft_response):
        mock_instance = MockIMAPClient.return_value.__enter__.return_value
        mock_instance.fetch_thread.return_value = sample_thread
        mock_call.return_value = mock_claude_draft_response

        draft = draft_reply_for_uid(2)

        assert draft.subject == "Re: Project Update"

    @patch("protonmail_claude.drafter.ProtonIMAPClient")
    def test_missing_uid_raises(self, MockIMAPClient):
        mock_instance = MockIMAPClient.return_value.__enter__.return_value
        mock_instance.fetch_thread.return_value = []

        with pytest.raises(ValueError, match="No email found"):
            draft_reply_for_uid(999)


class TestSendDraft:
    @patch("protonmail_claude.drafter.smtplib.SMTP")
    def test_sends_via_smtp(self, MockSMTP):
        mock_server = MagicMock()
        MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
        MockSMTP.return_value.__exit__ = MagicMock(return_value=False)

        draft = DraftReply(
            subject="Re: Test",
            body="Reply body",
            tone="neutral",
            notes="",
            in_reply_to="<orig@example.com>",
            to_address="recipient@example.com",
        )

        send_draft(
            draft,
            from_address="me@proton.me",
            smtp_host="127.0.0.1",
            smtp_port=1025,
            password="bridge-pw",
        )

        MockSMTP.assert_called_once_with("127.0.0.1", 1025)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("me@proton.me", "bridge-pw")
        mock_server.send_message.assert_called_once()

        # Verify the sent message
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Re: Test"
        assert sent_msg["To"] == "recipient@example.com"
        assert sent_msg["In-Reply-To"] == "<orig@example.com>"

    def test_no_to_address_raises(self):
        draft = DraftReply(
            subject="Re: Test",
            body="Body",
            tone="neutral",
            notes="",
            to_address=None,
        )
        with pytest.raises(ValueError, match="no to_address"):
            send_draft(draft)
