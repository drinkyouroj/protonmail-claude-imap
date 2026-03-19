"""Tests for label_manager module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from protonmail_claude.label_manager import LabelManager


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
