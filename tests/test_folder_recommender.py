"""Tests for folder_recommender module (DECISION-004: folder structure recommendations).

These tests specify the expected interface for folder_recommender.py and drive its
implementation. All IMAP interactions are mocked; no live Bridge connection is used.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SYSTEM_FOLDERS = ["Sent", "Trash", "Drafts", "Spam"]


def _make_imap_folder_list(names: list[str], noselect: list[str] | None = None) -> list[tuple]:
    """Build a list_folders_with_flags return value — returns (flags, name) 2-tuples."""
    noselect = noselect or []
    result = []
    for name in names:
        flags = (b"\\HasNoChildren",)
        if name in noselect:
            flags = (b"\\Noselect",)
        result.append((flags, name))
    return result


def _make_folder_status(message_count: int = 10, unseen_count: int = 2) -> dict:
    return {b"MESSAGES": message_count, b"UNSEEN": unseen_count, b"RECENT": 0}


# ---------------------------------------------------------------------------
# FolderInfo / data collection
# ---------------------------------------------------------------------------


class TestCollectFolderInventory:
    def test_collect_folder_inventory(self):
        """FolderInfo objects should be created from list_folders_with_flags + folder_status."""
        from protonmail_claude.folder_recommender import collect_folder_inventory, FolderInfo

        mock_client = MagicMock()

        mock_client.list_folders_with_flags.return_value = _make_imap_folder_list(
            ["INBOX", "Projects", "Archive"]
        )
        mock_client.folder_status.side_effect = [
            _make_folder_status(message_count=100, unseen_count=5),
            _make_folder_status(message_count=30, unseen_count=0),
            _make_folder_status(message_count=200, unseen_count=1),
        ]

        inventory = collect_folder_inventory(mock_client)

        assert len(inventory) == 3
        inbox_info = next(f for f in inventory if f.name == "INBOX")
        assert inbox_info.message_count == 100
        assert inbox_info.unseen_count == 5
        assert isinstance(inbox_info, FolderInfo)


class TestSystemFolderDetection:
    def test_system_folder_detection(self):
        """Sent, Trash, Drafts, Spam, and \\Noselect folders must be marked is_system=True."""
        from protonmail_claude.folder_recommender import collect_folder_inventory

        mock_client = MagicMock()

        all_folders = SYSTEM_FOLDERS + ["NoSelectFolder", "INBOX", "Projects"]
        mock_client.list_folders_with_flags.return_value = _make_imap_folder_list(
            all_folders, noselect=["NoSelectFolder"]
        )
        mock_client.folder_status.return_value = _make_folder_status()

        inventory = collect_folder_inventory(mock_client)
        by_name = {f.name: f for f in inventory}

        for name in SYSTEM_FOLDERS:
            assert by_name[name].is_system is True, f"{name} should be is_system=True"

        assert by_name["NoSelectFolder"].is_system is True, r"\Noselect should be is_system=True"
        assert by_name["INBOX"].is_system is False
        assert by_name["Projects"].is_system is False


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


class TestSenderClusteringByAddress:
    def test_sender_clustering_by_address(self):
        """Cluster key must be the full email address, not the domain."""
        from protonmail_claude.folder_recommender import build_sender_clusters

        # Two different senders from the same domain must produce two distinct clusters
        emails = [
            {"sender": "Alice Smith <alice@example.com>", "subject": "Hello"},
            {"sender": "alice@example.com", "subject": "Follow-up"},
            {"sender": "bob@example.com", "subject": "Different person"},
        ]

        clusters = build_sender_clusters(emails, min_count=1)
        addresses = {c.address for c in clusters}

        # alice@example.com and bob@example.com are distinct keys
        assert "alice@example.com" in addresses
        assert "bob@example.com" in addresses
        # Domain alone must NOT be a key
        assert "example.com" not in addresses


class TestSenderClusterMinCount:
    def test_sender_cluster_min_count(self):
        """Clusters with fewer messages than min_count must be excluded."""
        from protonmail_claude.folder_recommender import build_sender_clusters

        emails = [
            {"sender": "frequent@example.com", "subject": f"Msg {i}"}
            for i in range(10)
        ] + [
            {"sender": "rare@example.com", "subject": "Only one"},
        ]

        clusters = build_sender_clusters(emails, min_count=5)
        addresses = {c.address for c in clusters}

        assert "frequent@example.com" in addresses
        assert "rare@example.com" not in addresses


class TestSenderClusterSampleSubjects:
    def test_sender_cluster_sample_subjects(self):
        """Each cluster should retain at most 5 sample subjects."""
        from protonmail_claude.folder_recommender import build_sender_clusters

        emails = [
            {"sender": "news@example.com", "subject": f"Newsletter #{i}"}
            for i in range(20)
        ]

        clusters = build_sender_clusters(emails, min_count=1)
        assert len(clusters) == 1
        assert len(clusters[0].sample_subjects) <= 5


# ---------------------------------------------------------------------------
# Overlap detection
# ---------------------------------------------------------------------------


class TestOverlapSubstringDetection:
    def test_overlap_substring_detection(self):
        """'Projects' and 'Old Projects' should be detected as an overlap pair."""
        from protonmail_claude.folder_recommender import detect_folder_overlaps, FolderInfo

        folders = [
            FolderInfo(name="Projects", message_count=50, unseen_count=0, is_system=False),
            FolderInfo(name="Old Projects", message_count=10, unseen_count=0, is_system=False),
            FolderInfo(name="Finance", message_count=30, unseen_count=0, is_system=False),
        ]

        pairs = detect_folder_overlaps(folders)
        pair_names = {frozenset(p) for p in pairs}

        assert frozenset({"Projects", "Old Projects"}) in pair_names
        assert frozenset({"Projects", "Finance"}) not in pair_names


class TestOverlapIgnoresSystemFolders:
    def test_overlap_ignores_system_folders(self):
        """Sent and Trash must not appear in overlap candidates."""
        from protonmail_claude.folder_recommender import detect_folder_overlaps, FolderInfo

        folders = [
            FolderInfo(name="Sent", message_count=100, unseen_count=0, is_system=True),
            FolderInfo(name="Sent Items", message_count=5, unseen_count=0, is_system=False),
            FolderInfo(name="Trash", message_count=20, unseen_count=0, is_system=True),
            FolderInfo(name="Trash Old", message_count=0, unseen_count=0, is_system=False),
        ]

        pairs = detect_folder_overlaps(folders)
        for pair in pairs:
            for name in pair:
                assert name not in SYSTEM_FOLDERS, (
                    f"System folder '{name}' should not appear in overlap pairs"
                )


class TestOverlapCaseInsensitive:
    def test_overlap_case_insensitive(self):
        """'archive' and 'Archive' should be detected as overlapping."""
        from protonmail_claude.folder_recommender import detect_folder_overlaps, FolderInfo

        folders = [
            FolderInfo(name="archive", message_count=50, unseen_count=0, is_system=False),
            FolderInfo(name="Archive", message_count=80, unseen_count=0, is_system=False),
        ]

        pairs = detect_folder_overlaps(folders)
        pair_names = {frozenset(p) for p in pairs}

        assert frozenset({"archive", "Archive"}) in pair_names


# ---------------------------------------------------------------------------
# Recommendation validation
# ---------------------------------------------------------------------------


class TestValidRecommendationParsed:
    @patch("protonmail_claude.folder_recommender.call_claude_json")
    def test_valid_recommendation_parsed(self, mock_llm):
        """Mock LLM returning valid JSON should produce a Recommendation with correct fields."""
        from protonmail_claude.folder_recommender import get_recommendations, Recommendation

        mock_llm.return_value = [
            {
                "type": "create_folder",
                "impact": "high",
                "title": "CREATE FOLDER: Newsletters",
                "description": "143 newsletter messages in INBOX.",
                "affected_count": 143,
                "reason": "substack.com (87), beehiiv.com (56)",
                "organize_instruction": "Move all newsletters to Newsletters",
                "cli_command": "python -m protonmail_claude labels organize \"Move all newsletters to Newsletters\"",
            }
        ]

        profile = {
            "folders": [],
            "sender_clusters": [],
            "overlap_candidates": [],
            "scope": "INBOX",
            "sample_size": 200,
            "total_in_scope": 143,
        }

        recs = get_recommendations(profile)

        assert len(recs) == 1
        rec = recs[0]
        assert isinstance(rec, Recommendation)
        assert rec.type == "create_folder"
        assert rec.impact == "high"
        assert rec.affected_count == 143
        assert "Newsletters" in rec.title
        assert rec.cli_command != ""


class TestInvalidRecommendationTypeDropped:
    @patch("protonmail_claude.folder_recommender.call_claude_json")
    def test_invalid_recommendation_type_dropped(self, mock_llm):
        """'merge_folders' is not a v1 type and must be silently dropped."""
        from protonmail_claude.folder_recommender import get_recommendations

        mock_llm.return_value = [
            {
                "type": "merge_folders",
                "impact": "medium",
                "title": "MERGE: Projects + Old Projects",
                "description": "Redundant folders.",
                "affected_count": 20,
                "reason": "Substring overlap detected.",
                "organize_instruction": "Merge projects folders",
                "cli_command": "",
            }
        ]

        profile = {
            "folders": [],
            "sender_clusters": [],
            "overlap_candidates": [],
            "scope": "INBOX",
            "sample_size": 200,
            "total_in_scope": 500,
        }

        recs = get_recommendations(profile)

        assert len(recs) == 0


class TestEmptyProfileReturnsEmptyRecs:
    @patch("protonmail_claude.folder_recommender.call_claude_json")
    def test_empty_profile_returns_empty_recs(self, mock_llm):
        """An empty inbox should produce no recommendations without calling the LLM."""
        from protonmail_claude.folder_recommender import get_recommendations

        profile = {
            "folders": [],
            "sender_clusters": [],
            "overlap_candidates": [],
            "scope": "INBOX",
            "sample_size": 0,
            "total_in_scope": 0,
        }

        recs = get_recommendations(profile)

        assert recs == []
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TestRecommendResultToJson:
    def test_recommend_result_to_json(self):
        """JSON output must include generated_at, valid_for_minutes, and recommendations."""
        from protonmail_claude.folder_recommender import RecommendResult, Recommendation

        rec = Recommendation(
            rank=1,
            type="create_folder",
            impact="high",
            title="CREATE FOLDER: Newsletters",
            description="143 messages.",
            affected_count=143,
            reason="substack.com (87)",
            organize_instruction="Move newsletters",
            cli_command="python -m protonmail_claude labels organize \"Move newsletters\"",
        )
        result = RecommendResult(
            scope="INBOX",
            sample_size=200,
            total_in_scope=1000,
            recommendations=[rec],
        )

        raw = result.to_json()
        data = json.loads(raw)

        assert "generated_at" in data
        assert "valid_for_minutes" in data
        assert isinstance(data["valid_for_minutes"], int)
        assert data["valid_for_minutes"] > 0
        assert "recommendations" in data
        assert len(data["recommendations"]) == 1
        assert data["recommendations"][0]["type"] == "create_folder"


class TestPresentRecommendationsGroupsByImpact:
    def test_present_recommendations_groups_by_impact(self, capsys):
        """Terminal output must group recommendations under HIGH IMPACT, MEDIUM IMPACT, LOW IMPACT."""
        from protonmail_claude.folder_recommender import present_recommendations, Recommendation

        recs = [
            Recommendation(
                rank=1,
                type="create_folder",
                impact="high",
                title="CREATE FOLDER: Newsletters",
                description="High impact item.",
                affected_count=143,
                reason="substack.com",
                organize_instruction="Move newsletters",
                cli_command="python -m protonmail_claude labels organize \"Move newsletters\"",
            ),
            Recommendation(
                rank=2,
                type="delete_empty_folder",
                impact="low",
                title="DELETE EMPTY: Old Stuff",
                description="Low impact item.",
                affected_count=0,
                reason="Empty folder.",
                organize_instruction="",
                cli_command="",
            ),
        ]

        present_recommendations(recs, scope="INBOX", total_in_scope=500, sample_size=200)

        captured = capsys.readouterr()
        output = captured.out

        # Must show impact groupings
        assert "HIGH" in output
        assert "LOW" in output
        # High-impact item appears before low-impact item
        assert output.index("HIGH") < output.index("LOW")
        # Content present
        assert "Newsletters" in output
        assert "Old Stuff" in output
