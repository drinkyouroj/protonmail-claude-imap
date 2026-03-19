"""Tests for auto_organizer module."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from protonmail_claude.auto_organizer import (
    AutoOrganizeResult,
    RecommendedAction,
    _analyze_batch,
    _serialize_emails,
    _validate_recommendation,
)
from protonmail_claude.imap_client import EmailMessage


def _make_email(uid: int, sender: str = "test@example.com", subject: str = "Test", body: str = "Hello") -> EmailMessage:
    return EmailMessage(
        uid=uid, sender=sender, subject=subject,
        date=datetime(2026, 3, 19, 12, 0), body=body,
    )


class TestSerializeEmails:
    def test_basic_serialization(self):
        msgs = [_make_email(1, sender="alice@test.com", subject="Hi", body="Hello world")]
        result = _serialize_emails(msgs, ["INBOX", "Archive"])
        import json
        data = json.loads(result)
        assert data["available_folders"] == ["INBOX", "Archive"]
        assert len(data["emails"]) == 1
        assert data["emails"][0]["uid"] == 1
        assert "<email_body>" in data["emails"][0]["body"]
        assert "Hello world" in data["emails"][0]["body"]

    def test_metadata_only_skips_body(self):
        msgs = [_make_email(1, body="Secret content")]
        result = _serialize_emails(msgs, ["INBOX"], metadata_only=True)
        import json
        data = json.loads(result)
        assert data["emails"][0].get("body_available") is False
        assert "Secret content" not in result

    def test_empty_body_flagged(self):
        msgs = [_make_email(1, body="")]
        result = _serialize_emails(msgs, ["INBOX"])
        import json
        data = json.loads(result)
        assert data["emails"][0]["body"] == ""
        assert data["emails"][0]["body_available"] is False

    def test_body_truncation(self):
        long_body = "x" * 2000
        msgs = [_make_email(1, body=long_body)]
        result = _serialize_emails(msgs, ["INBOX"])
        import json
        data = json.loads(result)
        # Body should be truncated to 800 chars inside the tags
        body_content = data["emails"][0]["body"]
        # Remove the tags to get actual content
        inner = body_content.replace("<email_body>", "").replace("</email_body>", "")
        assert len(inner) == 800


class TestValidateRecommendation:
    def setup_method(self):
        self.msg = _make_email(100, sender="alice@test.com", subject="Test email", body="Some body")
        self.valid_uids = {100, 200, 300}
        self.folders = ["INBOX", "Archive", "Projects"]
        self.messages_by_uid = {100: self.msg}

    def test_valid_archive(self):
        raw = {"uid": 100, "action": "archive", "reason": "Routine email"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is not None
        assert rec.action == "archive"
        assert rec.dest_folder == "Archive"
        assert rec.sender == "alice@test.com"

    def test_valid_move(self):
        raw = {"uid": 100, "action": "move", "dest_folder": "Projects", "reason": "Project email"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is not None
        assert rec.action == "move"
        assert rec.dest_folder == "Projects"

    def test_move_to_new_folder_with_create(self):
        raw = {"uid": 100, "action": "move", "dest_folder": "NewFolder", "create_folder_if_missing": True, "reason": "New"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is not None
        assert rec.create_folder_if_missing is True

    def test_move_to_nonexistent_folder_rejected(self):
        raw = {"uid": 100, "action": "move", "dest_folder": "DoesNotExist", "reason": "Move it"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is None

    def test_unknown_uid_rejected(self):
        raw = {"uid": 999, "action": "archive", "reason": "Bad uid"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is None

    def test_unknown_action_rejected(self):
        raw = {"uid": 100, "action": "delete", "reason": "Not allowed"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is None

    def test_missing_uid_rejected(self):
        raw = {"action": "archive", "reason": "No uid"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is None

    def test_missing_action_rejected(self):
        raw = {"uid": 100, "reason": "No action"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is None

    def test_string_uid_coerced(self):
        raw = {"uid": "100", "action": "flag", "reason": "String uid"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is not None
        assert rec.uid == 100

    def test_move_without_dest_folder_rejected(self):
        raw = {"uid": 100, "action": "move", "reason": "No dest"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is None

    def test_trash_overridden_when_no_body(self):
        msg_no_body = _make_email(200, body="")
        messages = {200: msg_no_body}
        raw = {"uid": 200, "action": "trash", "reason": "Looks like spam"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, messages)
        assert rec is not None
        assert rec.action == "skip"  # overridden

    def test_skip_action_passthrough(self):
        raw = {"uid": 100, "action": "skip", "reason": "Not sure"}
        rec = _validate_recommendation(raw, self.valid_uids, self.folders, self.messages_by_uid)
        assert rec is not None
        assert rec.action == "skip"


class TestAnalyzeBatch:
    def setup_method(self):
        self.msgs = [_make_email(1, sender="a@b.com", subject="Hello")]
        self.valid_uids = {1}
        self.messages_by_uid = {1: self.msgs[0]}
        self.folders = ["INBOX", "Archive"]

    @patch("protonmail_claude.auto_organizer.call_claude_json")
    def test_successful_batch(self, mock_llm):
        mock_llm.return_value = [
            {"uid": 1, "action": "archive", "reason": "Routine", "dest_folder": "Archive",
             "label": None, "create_folder_if_missing": False}
        ]
        recs = _analyze_batch(
            self.msgs, self.folders, self.valid_uids, self.messages_by_uid,
        )
        assert len(recs) == 1
        assert recs[0].action == "archive"

    @patch("protonmail_claude.auto_organizer.call_claude_json")
    def test_llm_failure_returns_skip(self, mock_llm):
        mock_llm.side_effect = Exception("API error")
        recs = _analyze_batch(
            self.msgs, self.folders, self.valid_uids, self.messages_by_uid,
        )
        assert len(recs) == 1
        assert recs[0].action == "skip"
        assert "LLM error" in recs[0].reason

    @patch("protonmail_claude.auto_organizer.call_claude_json")
    def test_llm_returns_non_list(self, mock_llm):
        mock_llm.return_value = {"not": "a list"}
        recs = _analyze_batch(
            self.msgs, self.folders, self.valid_uids, self.messages_by_uid,
        )
        assert len(recs) == 1
        assert recs[0].action == "skip"

    @patch("protonmail_claude.auto_organizer.call_claude_json")
    def test_missing_uid_gets_skip(self, mock_llm):
        # LLM returns empty list — UID not covered
        mock_llm.return_value = []
        recs = _analyze_batch(
            self.msgs, self.folders, self.valid_uids, self.messages_by_uid,
        )
        assert len(recs) == 1
        assert recs[0].action == "skip"
        assert "Not covered" in recs[0].reason

    @patch("protonmail_claude.auto_organizer.call_claude_json")
    def test_duplicate_uids_deduplicated(self, mock_llm):
        mock_llm.return_value = [
            {"uid": 1, "action": "archive", "reason": "First", "dest_folder": "Archive",
             "label": None, "create_folder_if_missing": False},
            {"uid": 1, "action": "flag", "reason": "Duplicate"},
        ]
        recs = _analyze_batch(
            self.msgs, self.folders, self.valid_uids, self.messages_by_uid,
        )
        assert len(recs) == 1
        assert recs[0].action == "archive"  # first one wins


class TestAutoOrganizeResult:
    def test_summary(self):
        result = AutoOrganizeResult(
            total_analyzed=10,
            applied=[RecommendedAction(uid=1, action="archive")],
            skipped=[RecommendedAction(uid=2, action="skip")],
            errors=[{"uid": 3, "action": "move", "error": "fail"}],
        )
        assert "10 analyzed" in result.summary
        assert "1 applied" in result.summary
        assert "1 skipped" in result.summary
        assert "1 errors" in result.summary

    def test_to_json(self):
        result = AutoOrganizeResult(total_analyzed=5)
        j = result.to_json()
        import json
        data = json.loads(j)
        assert data["total_analyzed"] == 5
