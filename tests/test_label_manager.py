"""Tests for label_manager module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from protonmail_claude.imap_client import EmailMessage
from protonmail_claude.label_manager import LabelManager, OrganizeResult, organize


@pytest.fixture
def label_mgr():
    """LabelManager with a mocked IMAP client."""
    mock_proton = MagicMock()
    mock_imap = MagicMock()
    mock_proton.client = mock_imap
    mgr = LabelManager(mock_proton)
    return mgr, mock_imap


class TestListFolders:
    def test_returns_folder_names(self, label_mgr):
        mgr, mock_imap = label_mgr
        mock_imap.list_folders.return_value = [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Sent"),
            ((b"\\HasNoChildren",), b"/", "Archive/Newsletters"),
        ]

        folders = mgr.list_folders()
        assert folders == ["INBOX", "Sent", "Archive/Newsletters"]


class TestCreateFolder:
    def test_creates_folder(self, label_mgr):
        mgr, mock_imap = label_mgr
        mgr.create_folder("Projects/GhostEditor")
        mock_imap.create_folder.assert_called_once_with("Projects/GhostEditor")


class TestDeleteFolder:
    def test_deletes_folder(self, label_mgr):
        mgr, mock_imap = label_mgr
        mgr.delete_folder("Old/Stuff")
        mock_imap.delete_folder.assert_called_once_with("Old/Stuff")


class TestMoveMessage:
    def test_copy_delete_expunge(self, label_mgr):
        mgr, mock_imap = label_mgr
        mgr.move_message(42, "Archive/Newsletters")

        mock_imap.select_folder.assert_called_once_with("INBOX")
        mock_imap.copy.assert_called_once_with([42], "Archive/Newsletters")
        mock_imap.set_flags.assert_called_once_with([42], [b"\\Deleted"])
        mock_imap.expunge.assert_called_once_with([42])

    def test_custom_src_folder(self, label_mgr):
        mgr, mock_imap = label_mgr
        mgr.move_message(42, "Archive", src_folder="Sent")
        mock_imap.select_folder.assert_called_once_with("Sent")


class TestApplyLabel:
    def test_adds_flag(self, label_mgr):
        mgr, mock_imap = label_mgr
        mgr.apply_label(42, "important")

        mock_imap.select_folder.assert_called_once_with("INBOX")
        mock_imap.add_flags.assert_called_once_with([42], [b"important"])


class TestRemoveLabel:
    def test_removes_flag(self, label_mgr):
        mgr, mock_imap = label_mgr
        mgr.remove_label(42, "important")

        mock_imap.select_folder.assert_called_once_with("INBOX")
        mock_imap.remove_flags.assert_called_once_with([42], [b"important"])


class TestBulkMove:
    def test_moves_matching_messages(self, label_mgr):
        mgr, mock_imap = label_mgr
        mock_imap.search.return_value = [10, 20, 30]

        moved = mgr.bulk_move(["FROM", "newsletter@example.com"], "Archive/Newsletters")

        assert moved == [10, 20, 30]
        mock_imap.select_folder.assert_called_once_with("INBOX")
        mock_imap.search.assert_called_once_with(["FROM", "newsletter@example.com"])
        mock_imap.copy.assert_called_once_with([10, 20, 30], "Archive/Newsletters")
        mock_imap.set_flags.assert_called_once_with([10, 20, 30], [b"\\Deleted"])
        mock_imap.expunge.assert_called_once_with([10, 20, 30])

    def test_no_matches_returns_empty(self, label_mgr):
        mgr, mock_imap = label_mgr
        mock_imap.search.return_value = []

        moved = mgr.bulk_move(["FROM", "nobody@example.com"], "Archive")

        assert moved == []
        mock_imap.copy.assert_not_called()


# --- Organize (natural language) tests ---


@pytest.fixture
def mock_imap_client():
    """A mocked ProtonIMAPClient for organize tests."""
    mock = MagicMock()
    mock_imap = MagicMock()
    mock.client = mock_imap

    mock_imap.list_folders.return_value = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren",), b"/", "Sent"),
    ]
    mock.fetch_recent.return_value = [
        EmailMessage(
            uid=10,
            sender="news@example.com",
            subject="Weekly Newsletter",
            date=datetime(2026, 3, 19, tzinfo=timezone.utc),
            body="Newsletter content",
            message_id="<10@example.com>",
        ),
        EmailMessage(
            uid=11,
            sender="boss@example.com",
            subject="Project deadline",
            date=datetime(2026, 3, 19, tzinfo=timezone.utc),
            body="Deadline is Friday",
            message_id="<11@example.com>",
        ),
    ]
    return mock, mock_imap


class TestOrganize:
    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_creates_folder_and_bulk_moves(self, mock_call, mock_imap_client):
        mock_client, mock_imap = mock_imap_client
        mock_call.return_value = [
            {"action": "create_folder", "name": "Archive/Newsletters"},
            {"action": "bulk_move", "search_criteria": ["FROM", "news@example.com"], "dest_folder": "Archive/Newsletters", "src_folder": "INBOX"},
        ]
        mock_imap.search.return_value = [10]

        result = organize("Move all newsletters to Archive/Newsletters", mock_client)

        assert len(result.operations) == 2
        assert len(result.executed) == 2
        assert len(result.errors) == 0
        mock_imap.create_folder.assert_called_once_with("Archive/Newsletters")
        mock_imap.search.assert_called_once_with(["FROM", "news@example.com"])

    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_dry_run_does_not_execute(self, mock_call, mock_imap_client):
        mock_client, mock_imap = mock_imap_client
        mock_call.return_value = [
            {"action": "create_folder", "name": "Archive/Newsletters"},
        ]

        result = organize("Move newsletters", mock_client, dry_run=True)

        assert len(result.operations) == 1
        assert len(result.executed) == 0
        mock_imap.create_folder.assert_not_called()

    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_move_message_by_uid(self, mock_call, mock_imap_client):
        mock_client, mock_imap = mock_imap_client
        mock_call.return_value = [
            {"action": "move_message", "uid": 11, "dest_folder": "Important", "src_folder": "INBOX"},
        ]

        result = organize("Move the project deadline email to Important", mock_client)

        assert len(result.executed) == 1
        mock_imap.copy.assert_called_once_with([11], "Important")

    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_apply_and_remove_label(self, mock_call, mock_imap_client):
        mock_client, mock_imap = mock_imap_client
        mock_call.return_value = [
            {"action": "apply_label", "uid": 11, "label": "urgent", "folder": "INBOX"},
            {"action": "remove_label", "uid": 10, "label": "important", "folder": "INBOX"},
        ]

        result = organize("Flag project deadline as urgent, unflag newsletter", mock_client)

        assert len(result.executed) == 2
        mock_imap.add_flags.assert_called_once_with([11], [b"urgent"])
        mock_imap.remove_flags.assert_called_once_with([10], [b"important"])

    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_empty_operations(self, mock_call, mock_imap_client):
        mock_client, _ = mock_imap_client
        mock_call.return_value = []

        result = organize("Do something ambiguous", mock_client)

        assert len(result.operations) == 0
        assert result.summary == "No operations to perform."

    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_unknown_action_recorded_as_error(self, mock_call, mock_imap_client):
        mock_client, _ = mock_imap_client
        mock_call.return_value = [
            {"action": "delete_everything", "target": "all"},
        ]

        result = organize("Delete everything", mock_client)

        assert len(result.errors) == 1
        assert "Unknown action" in result.errors[0]["error"]

    @patch("protonmail_claude.label_manager.call_claude_json")
    def test_imap_error_captured(self, mock_call, mock_imap_client):
        mock_client, mock_imap = mock_imap_client
        mock_call.return_value = [
            {"action": "create_folder", "name": "Test"},
        ]
        mock_imap.create_folder.side_effect = Exception("IMAP error")

        result = organize("Create Test folder", mock_client)

        assert len(result.errors) == 1
        assert "IMAP error" in result.errors[0]["error"]


class TestOrganizeResult:
    def test_summary_with_mixed_results(self):
        result = OrganizeResult(
            operations=[{}, {}, {}],
            executed=[{}, {}],
            errors=[{"error": "fail"}],
        )
        assert "2 operations executed" in result.summary
        assert "1 errors" in result.summary

    def test_summary_empty(self):
        result = OrganizeResult()
        assert result.summary == "No operations to perform."
