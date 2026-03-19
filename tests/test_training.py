"""Tests for profile and pattern_store modules (DECISION-005 P0)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from protonmail_claude.profile import (
    MAX_PROFILE_CHARS,
    build_system_prompt,
    init_profile,
    load_profile,
)
from protonmail_claude.pattern_store import (
    _extract_sender_domain,
    format_patterns_for_prompt,
    get_patterns_for_batch,
    load_patterns,
    record_actions,
    save_patterns,
)


# ---------------------------------------------------------------------------
# Profile tests
# ---------------------------------------------------------------------------


class TestLoadProfile:
    def test_returns_none_when_no_file(self, tmp_path):
        with patch("protonmail_claude.profile.get_profile_path", return_value=tmp_path / "nope.md"):
            assert load_profile() is None

    def test_loads_existing_profile(self, tmp_path):
        p = tmp_path / "profile.md"
        p.write_text("# My Profile\nArchive newsletters.")
        with patch("protonmail_claude.profile.get_profile_path", return_value=p):
            result = load_profile()
            assert "My Profile" in result
            assert "newsletters" in result

    def test_truncates_long_profile(self, tmp_path):
        p = tmp_path / "profile.md"
        p.write_text("x" * (MAX_PROFILE_CHARS + 500))
        with patch("protonmail_claude.profile.get_profile_path", return_value=p):
            result = load_profile()
            assert len(result) == MAX_PROFILE_CHARS

    def test_returns_none_for_empty_file(self, tmp_path):
        p = tmp_path / "profile.md"
        p.write_text("")
        with patch("protonmail_claude.profile.get_profile_path", return_value=p):
            assert load_profile() is None


class TestBuildSystemPrompt:
    def test_no_profile_returns_base(self):
        base = "You are an email assistant."
        assert build_system_prompt(base, None) == base

    def test_profile_appended(self):
        base = "You are an email assistant."
        profile = "Archive newsletters."
        result = build_system_prompt(base, profile)
        assert base in result
        assert "Archive newsletters." in result
        assert "OVERRIDES" in result

    def test_folder_constraint_included(self):
        result = build_system_prompt("base", "profile text")
        assert "MUST use only folder names" in result


class TestInitProfile:
    def test_creates_profile_with_folders(self, tmp_path):
        target = tmp_path / "profile.md"
        with patch("protonmail_claude.profile.DEFAULT_PATHS", [target]):
            path = init_profile(["INBOX", "Archive", "Projects", "Reading/Newsletters"])
            assert path.exists()
            content = path.read_text()
            assert "Archive" in content
            assert "Projects" in content
            assert "Reading/Newsletters" in content


# ---------------------------------------------------------------------------
# Pattern store tests
# ---------------------------------------------------------------------------


class TestExtractSenderDomain:
    def test_full_address(self):
        assert _extract_sender_domain("Alice <alice@example.com>") == "example.com"

    def test_bare_address(self):
        assert _extract_sender_domain("bob@test.org") == "test.org"

    def test_no_at_sign(self):
        assert _extract_sender_domain("unknown") == "unknown"


class TestPatternStore:
    def test_load_empty_returns_default(self, tmp_path):
        with patch("protonmail_claude.pattern_store._get_store_path", return_value=tmp_path / "none.json"):
            store = load_patterns()
            assert store["version"] == 1
            assert store["patterns"] == {}

    def test_record_and_load(self, tmp_path):
        store_path = tmp_path / "patterns.json"
        with patch("protonmail_claude.pattern_store._get_store_path", return_value=store_path):
            record_actions([
                {"sender": "alerts@railway.app", "action": "move", "dest_folder": "CI/Builds"},
                {"sender": "alerts@railway.app", "action": "move", "dest_folder": "CI/Builds"},
            ])
            store = load_patterns()
            assert "railway.app" in store["patterns"]
            p = store["patterns"]["railway.app"]
            assert p["action"] == "move"
            assert p["dest"] == "CI/Builds"
            assert p["confirmed"] == 2
            assert p["confidence"] == 1.0

    def test_record_skip_ignored(self, tmp_path):
        store_path = tmp_path / "patterns.json"
        with patch("protonmail_claude.pattern_store._get_store_path", return_value=store_path):
            record_actions([
                {"sender": "test@example.com", "action": "skip", "dest_folder": None},
            ])
            store = load_patterns()
            assert "example.com" not in store["patterns"]

    def test_conflicting_action_updates(self, tmp_path):
        store_path = tmp_path / "patterns.json"
        with patch("protonmail_claude.pattern_store._get_store_path", return_value=store_path):
            # First action
            record_actions([{"sender": "a@test.com", "action": "archive", "dest_folder": "Archive"}])
            # Different action for same domain
            record_actions([{"sender": "a@test.com", "action": "move", "dest_folder": "Projects"}])
            store = load_patterns()
            p = store["patterns"]["test.com"]
            assert p["action"] == "move"
            assert p["dest"] == "Projects"
            assert p["confirmed"] == 1
            assert p["rejected"] == 1
            assert p["confidence"] == 0.5


class TestGetPatternsForBatch:
    def test_matches_sender_domains(self, tmp_path):
        store_path = tmp_path / "patterns.json"
        with patch("protonmail_claude.pattern_store._get_store_path", return_value=store_path):
            record_actions([
                {"sender": "bot@github.com", "action": "archive", "dest_folder": "Archive"},
            ])
            matches = get_patterns_for_batch(["noreply@github.com", "unknown@other.com"])
            assert len(matches) == 1
            assert matches[0]["domain"] == "github.com"

    def test_deduplicates_domains(self, tmp_path):
        store_path = tmp_path / "patterns.json"
        with patch("protonmail_claude.pattern_store._get_store_path", return_value=store_path):
            record_actions([
                {"sender": "a@example.com", "action": "archive", "dest_folder": "Archive"},
            ])
            # Two senders from same domain
            matches = get_patterns_for_batch(["a@example.com", "b@example.com"])
            assert len(matches) == 1


class TestFormatPatternsForPrompt:
    def test_empty_returns_empty(self):
        assert format_patterns_for_prompt([]) == ""

    def test_formats_patterns(self):
        patterns = [
            {"domain": "github.com", "action": "archive", "dest": "", "confidence": 0.9, "confirmed": 5},
            {"domain": "substack.com", "action": "move", "dest": "Newsletters", "confidence": 0.8, "confirmed": 10},
        ]
        result = format_patterns_for_prompt(patterns)
        assert "github.com" in result
        assert "archive" in result
        assert "substack.com" in result
        assert "Newsletters" in result
        assert "Learned Patterns" in result
