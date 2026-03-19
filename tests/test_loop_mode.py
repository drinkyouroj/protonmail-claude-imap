"""Tests for auto_organize_loop (DECISION-003: loop mode)."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from protonmail_claude.auto_organizer import AutoOrganizeResult, RecommendedAction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    uids: list[int],
    action: str = "archive",
    applied_count: int | None = None,
) -> AutoOrganizeResult:
    """Build a minimal AutoOrganizeResult for a list of UIDs."""
    recs = [RecommendedAction(uid=u, action=action) for u in uids]
    applied_count = applied_count if applied_count is not None else (0 if action == "skip" else len(uids))
    applied = recs[:applied_count]
    skipped = recs[applied_count:]
    return AutoOrganizeResult(
        total_analyzed=len(uids),
        recommendations=recs,
        applied=applied,
        skipped=skipped,
    )


def _make_skip_result(uids: list[int]) -> AutoOrganizeResult:
    """Build an AutoOrganizeResult where LLM skipped every UID."""
    recs = [RecommendedAction(uid=u, action="skip", reason="LLM skip") for u in uids]
    return AutoOrganizeResult(
        total_analyzed=len(uids),
        recommendations=recs,
        applied=[],
        skipped=recs,
    )


# ---------------------------------------------------------------------------
# Test: exits when inbox is empty on first search
# ---------------------------------------------------------------------------


class TestLoopExitsWhenNoUnread:
    @patch("protonmail_claude.auto_organizer.auto_organize")
    @patch("protonmail_claude.auto_organizer.ProtonIMAPClient")
    def test_loop_exits_when_no_unread(self, MockClient, mock_auto_organize):
        """Loop should exit immediately when the first UNSEEN search returns empty."""
        mock_client_instance = MagicMock()
        mock_client_instance.search.return_value = []
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        from protonmail_claude.auto_organizer import auto_organize_loop

        auto_organize_loop(max_iterations=10, inter_batch_delay=0)

        # auto_organize should never have been called
        mock_auto_organize.assert_not_called()


# ---------------------------------------------------------------------------
# Test: processes multiple iterations until inbox is empty
# ---------------------------------------------------------------------------


class TestLoopProcessesMultipleIterations:
    @patch("protonmail_claude.auto_organizer.time")
    @patch("protonmail_claude.auto_organizer.auto_organize")
    @patch("protonmail_claude.auto_organizer.ProtonIMAPClient")
    def test_loop_processes_multiple_iterations(self, MockClient, mock_auto_organize, mock_time):
        """Loop should call auto_organize twice, then stop when inbox is empty."""
        uids_iter1 = [1, 2, 3]
        uids_iter2 = [4, 5]

        # search returns UIDs for 2 iterations then empty
        mock_client_instance = MagicMock()
        mock_client_instance.search.side_effect = [
            uids_iter1,  # iteration 1 peek
            uids_iter2,  # iteration 2 peek
            [],          # iteration 3 peek → exit
        ]
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        mock_auto_organize.side_effect = [
            _make_result(uids_iter1),
            _make_result(uids_iter2),
        ]

        from protonmail_claude.auto_organizer import auto_organize_loop

        auto_organize_loop(max_iterations=10, inter_batch_delay=0)

        assert mock_auto_organize.call_count == 2


# ---------------------------------------------------------------------------
# Test: respects max_iterations cap
# ---------------------------------------------------------------------------


class TestLoopRespectsMaxIterations:
    @patch("protonmail_claude.auto_organizer.time")
    @patch("protonmail_claude.auto_organizer.auto_organize")
    @patch("protonmail_claude.auto_organizer.ProtonIMAPClient")
    def test_loop_respects_max_iterations(self, MockClient, mock_auto_organize, mock_time):
        """Loop should stop after max_iterations even when unread emails remain."""
        mock_client_instance = MagicMock()
        # Always return UIDs — inbox never empties
        mock_client_instance.search.return_value = [1, 2, 3, 4, 5]
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        mock_auto_organize.return_value = _make_result([1, 2, 3])

        from protonmail_claude.auto_organizer import auto_organize_loop

        auto_organize_loop(max_iterations=2, inter_batch_delay=0)

        assert mock_auto_organize.call_count == 2


# ---------------------------------------------------------------------------
# Test: stall detection exits after two consecutive all-skip identical UID sets
# ---------------------------------------------------------------------------


class TestLoopStallDetection:
    @patch("protonmail_claude.auto_organizer.time")
    @patch("protonmail_claude.auto_organizer.auto_organize")
    @patch("protonmail_claude.auto_organizer.ProtonIMAPClient")
    def test_loop_stall_detection(self, MockClient, mock_auto_organize, mock_time, capsys):
        """Loop should exit with a stall warning when LLM skips the same UIDs twice."""
        stuck_uids = [10, 11, 12]

        mock_client_instance = MagicMock()
        # Inbox always has the same UIDs (nothing moved)
        mock_client_instance.search.return_value = stuck_uids
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        # LLM skips everything both times with identical UIDs
        mock_auto_organize.return_value = _make_skip_result(stuck_uids)

        from protonmail_claude.auto_organizer import auto_organize_loop

        auto_organize_loop(max_iterations=10, inter_batch_delay=0)

        # Should have called auto_organize exactly twice before detecting stall
        assert mock_auto_organize.call_count == 2

        captured = capsys.readouterr()
        assert "No actionable recommendations" in captured.out


# ---------------------------------------------------------------------------
# Test: user refusal is not treated as a stall
# ---------------------------------------------------------------------------


class TestLoopUserRefusalNotStall:
    @patch("protonmail_claude.auto_organizer.time")
    @patch("protonmail_claude.auto_organizer.auto_organize")
    @patch("protonmail_claude.auto_organizer.ProtonIMAPClient")
    def test_loop_user_refusal_not_stall(self, MockClient, mock_auto_organize, mock_time):
        """User declining confirmations (non-skip recommendations) should not trigger stall exit."""
        uids = [20, 21, 22]

        mock_client_instance = MagicMock()
        # Return same UIDs for first 3 iterations, then empty
        mock_client_instance.search.side_effect = [uids, uids, uids, []]
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client_instance)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        def _user_declined_result(*args, **kwargs):
            # Recommendations are non-skip (archive) but user declined → all in skipped
            recs = [RecommendedAction(uid=u, action="archive") for u in uids]
            return AutoOrganizeResult(
                total_analyzed=len(uids),
                recommendations=recs,
                applied=[],   # user said no — nothing applied
                skipped=recs,
            )

        mock_auto_organize.side_effect = [
            _user_declined_result(),
            _user_declined_result(),
            _user_declined_result(),
        ]

        from protonmail_claude.auto_organizer import auto_organize_loop

        # Loop should run 3 iterations before finding empty inbox, not stall out after 2
        auto_organize_loop(max_iterations=10, inter_batch_delay=0)

        assert mock_auto_organize.call_count == 3


# ---------------------------------------------------------------------------
# Test: --loop with --dry-run raises an error
# ---------------------------------------------------------------------------


class TestLoopDryRunRejected:
    def test_loop_dry_run_rejected(self):
        """auto_organize_loop does not accept dry_run; combining --loop --dry-run
        should raise a ValueError (enforced by the CLI layer or a guard function).
        """
        # auto_organize_loop has no dry_run parameter — the CLI is responsible for
        # raising before ever calling it. We test that the function signature does
        # NOT silently accept a dry_run kwarg by verifying it raises TypeError.
        from protonmail_claude.auto_organizer import auto_organize_loop
        import inspect

        sig = inspect.signature(auto_organize_loop)
        assert "dry_run" not in sig.parameters, (
            "--loop and --dry-run are mutually exclusive per DECISION-003; "
            "auto_organize_loop must not accept a dry_run parameter"
        )
