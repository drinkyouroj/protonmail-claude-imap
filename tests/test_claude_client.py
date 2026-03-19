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


def _mock_openai_response(text="Hello", prompt_tokens=10, completion_tokens=5):
    """Build a mock OpenAI-style chat completion response."""
    mock_message = MagicMock()
    mock_message.content = text

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = prompt_tokens
    mock_usage.completion_tokens = completion_tokens

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage
    return mock_response


class TestCallClaude:
    @patch("protonmail_claude.claude_client._get_client")
    def test_basic_call(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_openai_response("Hello from Groq")

        result = call_claude("Say hello", system_prompt="You are helpful.")
        assert result == "Hello from Groq"

        mock_client.chat.completions.create.assert_called_once_with(
            model="openai/gpt-oss-120b",
            max_tokens=2048,
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Say hello"},
            ],
        )

    @patch("protonmail_claude.claude_client._get_client")
    def test_call_with_prompt_name(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_openai_response("digest result")

        result = call_claude("emails here", system_prompt_name="digest_system")
        assert result == "digest result"

        # Verify system prompt was loaded from file
        call_args = mock_client.chat.completions.create.call_args
        system_msg = call_args.kwargs["messages"][0]
        assert system_msg["role"] == "system"
        assert "email triage assistant" in system_msg["content"]


class TestCallClaudeJson:
    @patch("protonmail_claude.claude_client._get_client")
    def test_parses_json_response(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_openai_response('[{"key": "value"}]')

        result = call_claude_json("input", system_prompt="return json")
        assert result == [{"key": "value"}]

    @patch("protonmail_claude.claude_client._get_client")
    def test_strips_code_fences(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        fenced = '```json\n[{"key": "value"}]\n```'
        mock_client.chat.completions.create.return_value = _mock_openai_response(fenced)

        result = call_claude_json("input", system_prompt="return json")
        assert result == [{"key": "value"}]

    @patch("protonmail_claude.claude_client._get_client")
    def test_invalid_json_raises(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_openai_response("not json at all")

        with pytest.raises(Exception):
            call_claude_json("input", system_prompt="return json")
