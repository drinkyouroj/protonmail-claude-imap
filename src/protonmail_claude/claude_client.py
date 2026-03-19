"""Thin wrapper around the Groq API (OpenAI-compatible) for LLM calls."""

from __future__ import annotations

import json
import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(name: str) -> str:
    """Load a system prompt from the prompts directory."""
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    with open(path) as f:
        return f.read().strip()


def _get_client() -> OpenAI:
    """Create an OpenAI client pointed at Groq's API."""
    return OpenAI(
        api_key=os.getenv("GROQ_API_KEY", ""),
        base_url=GROQ_BASE_URL,
    )


def call_claude(
    user_content: str,
    system_prompt: str | None = None,
    system_prompt_name: str | None = None,
    max_tokens: int = 2048,
    model: str | None = None,
) -> str:
    """Send a message to the LLM and return the text response.

    Provide either `system_prompt` (raw string) or `system_prompt_name`
    (filename stem in prompts/ directory, e.g. "digest_system").
    """
    if system_prompt_name and not system_prompt:
        system_prompt = _load_prompt(system_prompt_name)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})

    client = _get_client()
    response = client.chat.completions.create(
        model=model or MODEL,
        max_tokens=max_tokens,
        messages=messages,
    )

    text = response.choices[0].message.content

    usage = response.usage
    if usage:
        logger.debug(
            "LLM API usage: input_tokens=%d, output_tokens=%d",
            usage.prompt_tokens,
            usage.completion_tokens,
        )

    return text


def call_claude_json(
    user_content: str,
    system_prompt_name: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int = 2048,
    model: str | None = None,
) -> list | dict:
    """Call the LLM and parse the response as JSON.

    Strips markdown code fences if present before parsing.
    """
    raw = call_claude(
        user_content=user_content,
        system_prompt=system_prompt,
        system_prompt_name=system_prompt_name,
        max_tokens=max_tokens,
        model=model,
    )

    # Strip markdown code fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    return json.loads(text)
