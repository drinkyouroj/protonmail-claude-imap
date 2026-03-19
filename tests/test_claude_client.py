"""Tests for claude_client module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from protonmail_claude.claude_client import call_claude, call_claude_json, _load_prompt


class TestLoadPrompt:
    def test_loads_existing_prompt(self):
        prompt = _load_prompt("digest_system")
        assert "email triage assistant" in prompt

    def test_missing_prompt_raises(self):
        with pytest.raises(FileNotFoundError):
            _load_prompt("nonexistent_prompt")


class TestCallClaude:
    @patch("protonmail_claude.claude_client.anthropic.Anthropic")
    def test_basic_call(self, MockAnthropic):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello from Claude")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_client.messages.create.return_value = mock_response

        result = call_claude("Say hello", system_prompt="You are helpful.")
        assert result == "Hello from Claude"

        mock_client.messages.create.assert_called_once_with(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system="You are helpful.",
            messages=[{"role": "user", "content": "Say hello"}],
        )

    @patch("protonmail_claude.claude_client.anthropic.Anthropic")
    def test_call_with_prompt_name(self, MockAnthropic):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="digest result")]
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 100
        mock_client.messages.create.return_value = mock_response

        result = call_claude("emails here", system_prompt_name="digest_system")
        assert result == "digest result"

        # Verify system prompt was loaded from file
        call_args = mock_client.messages.create.call_args
        assert "email triage assistant" in call_args.kwargs["system"]


class TestCallClaudeJson:
    @patch("protonmail_claude.claude_client.anthropic.Anthropic")
    def test_parses_json_response(self, MockAnthropic):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='[{"key": "value"}]')]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_client.messages.create.return_value = mock_response

        result = call_claude_json("input", system_prompt="return json")
        assert result == [{"key": "value"}]

    @patch("protonmail_claude.claude_client.anthropic.Anthropic")
    def test_strips_code_fences(self, MockAnthropic):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client

        fenced = '```json\n[{"key": "value"}]\n```'
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=fenced)]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_client.messages.create.return_value = mock_response

        result = call_claude_json("input", system_prompt="return json")
        assert result == [{"key": "value"}]

    @patch("protonmail_claude.claude_client.anthropic.Anthropic")
    def test_invalid_json_raises(self, MockAnthropic):
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="not json at all")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_client.messages.create.return_value = mock_response

        with pytest.raises(Exception):
            call_claude_json("input", system_prompt="return json")
