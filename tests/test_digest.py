"""Tests for digest pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from protonmail_claude.digest import (
    Digest,
    DigestEntry,
    _serialize_emails,
    fetch_and_digest,
    generate_digest,
)
from protonmail_claude.imap_client import EmailMessage


# --- Fixtures ---


@pytest.fixture
def sample_messages() -> list[EmailMessage]:
    return [
        EmailMessage(
            uid=1,
            sender="alice@example.com",
            subject="Q1 Budget Review",
            date=datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc),
            body="Please review the attached Q1 budget proposal.",
            message_id="<1@example.com>",
        ),
        EmailMessage(
            uid=2,
            sender="bob@example.com",
            subject="Team Standup Notes",
            date=datetime(2026, 3, 19, 11, 0, tzinfo=timezone.utc),
            body="Here are the standup notes from today's meeting.",
            message_id="<2@example.com>",
        ),
    ]


@pytest.fixture
def mock_claude_digest_response():
    return [
        {
            "sender": "alice@example.com",
            "subject": "Q1 Budget Review",
            "summary": "Requesting review of Q1 budget proposal.",
            "priority": "high",
            "suggested_action": "reply today",
        },
        {
            "sender": "bob@example.com",
            "subject": "Team Standup Notes",
            "summary": "Summary of today's standup meeting.",
            "priority": "low",
            "suggested_action": "archive",
        },
    ]


# --- Tests ---


class TestSerializeEmails:
    def test_serializes_messages(self, sample_messages):
        result = _serialize_emails(sample_messages)
        import json

        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["sender"] == "alice@example.com"
        assert parsed[1]["subject"] == "Team Standup Notes"

    def test_truncates_long_bodies(self):
        msg = EmailMessage(
            uid=1,
            sender="test@test.com",
            subject="Long Email",
            date=datetime.now(tz=timezone.utc),
            body="x" * 5000,
        )
        result = _serialize_emails([msg])
        import json

        parsed = json.loads(result)
        assert len(parsed[0]["body"]) == 2000

    def test_empty_list(self):
        result = _serialize_emails([])
        import json

        assert json.loads(result) == []


class TestGenerateDigest:
    @patch("protonmail_claude.digest.call_claude_json")
    def test_generates_digest(self, mock_call, sample_messages, mock_claude_digest_response):
        mock_call.return_value = mock_claude_digest_response

        digest = generate_digest(sample_messages)

        assert digest.email_count == 2
        assert len(digest.entries) == 2
        assert digest.entries[0].sender == "alice@example.com"
        assert digest.entries[0].priority == "high"
        assert digest.entries[1].suggested_action == "archive"
        assert digest.generated_at  # non-empty timestamp

        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args.kwargs
        assert call_kwargs["system_prompt_name"] == "digest_system"

    def test_empty_messages_returns_empty_digest(self):
        digest = generate_digest([])
        assert digest.email_count == 0
        assert digest.entries == []
        assert digest.generated_at


class TestDigest:
    def test_to_json(self):
        digest = Digest(
            generated_at="2026-03-19T10:00:00",
            email_count=1,
            entries=[
                DigestEntry(
                    sender="test@test.com",
                    subject="Test",
                    summary="A test email.",
                    priority="low",
                    suggested_action="archive",
                )
            ],
        )
        import json

        result = json.loads(digest.to_json())
        assert result["email_count"] == 1
        assert result["entries"][0]["sender"] == "test@test.com"


class TestFetchAndDigest:
    @patch("protonmail_claude.digest.call_claude_json")
    @patch("protonmail_claude.digest.ProtonIMAPClient")
    def test_end_to_end(self, MockIMAPClient, mock_call, sample_messages, mock_claude_digest_response):
        mock_instance = MockIMAPClient.return_value.__enter__.return_value
        mock_instance.fetch_recent.return_value = sample_messages
        mock_call.return_value = mock_claude_digest_response

        digest = fetch_and_digest(folder="INBOX", count=10)

        assert digest.email_count == 2
        assert len(digest.entries) == 2
        mock_instance.fetch_recent.assert_called_once_with(folder="INBOX", count=10)

    @patch("protonmail_claude.digest.call_claude_json")
    def test_with_injected_client(self, mock_call, sample_messages, mock_claude_digest_response):
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.fetch_recent.return_value = sample_messages
        mock_call.return_value = mock_claude_digest_response

        digest = fetch_and_digest(imap_client=mock_client, count=5)

        assert digest.email_count == 2
        mock_client.fetch_recent.assert_called_once_with(folder="INBOX", count=5)
