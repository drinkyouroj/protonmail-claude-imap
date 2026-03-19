"""User organization profile — loaded into every LLM system prompt."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_PROFILE_CHARS = 3200  # ~800 tokens at 4 chars/token

PROFILE_TEMPLATE = """\
# My Email Organization

## Folder Structure
<!-- List your folders and what goes in each. The LLM will ONLY use these names. -->
- Archive: Default for anything not matching below
- Reading/Newsletters: Substack, Beehiiv, any newsletter

## Preferences
<!-- Describe how you want emails handled. Be specific. -->
- Never flag newsletters
- Build failure notifications are NOT urgent
"""

DEFAULT_PATHS = [
    Path(os.path.expanduser("~/.config/protonmail-claude/profile.md")),
    Path("profile.md"),
    Path(".organize-profile.md"),
]


def get_profile_path() -> Path:
    """Return the first existing profile path, or the default location."""
    for p in DEFAULT_PATHS:
        if p.exists():
            return p
    return DEFAULT_PATHS[0]


def load_profile() -> str | None:
    """Load the organization profile, returning None if not found."""
    path = get_profile_path()
    if not path.exists():
        return None

    text = path.read_text().strip()
    if not text:
        return None

    if len(text) > MAX_PROFILE_CHARS:
        logger.warning(
            "Profile at %s is %d chars (~%d tokens), truncating to %d chars. "
            "Consider trimming for best results.",
            path, len(text), len(text) // 4, MAX_PROFILE_CHARS,
        )
        text = text[:MAX_PROFILE_CHARS]

    return text


def build_system_prompt(base_prompt: str, profile: str | None) -> str:
    """Append the user profile to the base system prompt."""
    if not profile:
        return base_prompt
    return f"""{base_prompt}

---
## User Organization Profile
The following profile was written by the user and OVERRIDES any default categorization logic.
You MUST use only folder names from the available_folders list in the input payload.
Do NOT invent new folder names unless create_folder_if_missing is set to true.

{profile}
---"""


def init_profile(folders: list[str]) -> Path:
    """Bootstrap a profile from the IMAP folder list."""
    path = DEFAULT_PATHS[0]
    path.parent.mkdir(parents=True, exist_ok=True)

    folder_lines = "\n".join(f"- {f}: " for f in sorted(folders) if f != "INBOX")

    content = f"""\
# My Email Organization

## Folder Structure
<!-- Edit descriptions for each folder. The LLM will use ONLY these folder names. -->
- INBOX: Unprocessed emails
{folder_lines}
- Archive: Default for anything not matching above

## Preferences
<!-- Describe how you want emails handled. Be specific about senders and actions. -->
- Never flag newsletters or marketing emails
- Build/deploy notifications are not urgent
"""
    path.write_text(content)
    return path


def edit_profile() -> None:
    """Open the profile in $EDITOR."""
    path = get_profile_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PROFILE_TEMPLATE)

    editor = os.getenv("EDITOR", os.getenv("VISUAL", ""))
    if editor:
        subprocess.run([editor, str(path)])
    else:
        print(f"No $EDITOR set. Edit manually: {path}")
