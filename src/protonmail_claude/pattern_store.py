"""Pattern store — auto-learned sender→action mappings from confirmed actions."""

from __future__ import annotations

import email.utils
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(os.path.expanduser("~/.config/protonmail-claude/patterns.json"))

# Thresholds for auto-apply (P1 — not used in P0, patterns are few-shot only)
MIN_CONFIRMED = 3
MIN_CONFIDENCE = 0.8


def _extract_sender_domain(sender: str) -> str:
    """Extract the domain from a sender string like 'Name <user@domain.com>'."""
    _, addr = email.utils.parseaddr(sender)
    if "@" in addr:
        return addr.split("@", 1)[1].lower()
    return sender.lower()


def _get_store_path() -> Path:
    """Return the pattern store path."""
    env = os.getenv("PATTERN_STORE_PATH")
    if env:
        return Path(env)
    return DEFAULT_PATH


def load_patterns() -> dict:
    """Load the pattern store. Returns empty dict if not found."""
    path = _get_store_path()
    if not path.exists():
        return {"version": 1, "patterns": {}}
    try:
        data = json.loads(path.read_text())
        if "patterns" not in data:
            data["patterns"] = {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read patterns from %s: %s", path, e)
        return {"version": 1, "patterns": {}}


def save_patterns(store: dict) -> None:
    """Write the pattern store to disk."""
    path = _get_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, default=str))


def record_actions(applied_actions: list[dict]) -> None:
    """Record confirmed actions into the pattern store.

    Each dict should have: sender (str), action (str), dest_folder (str|None).
    Updates confidence and confirmed counts.
    """
    if not applied_actions:
        return

    store = load_patterns()
    patterns = store["patterns"]
    now = datetime.now(tz=timezone.utc).isoformat()

    for item in applied_actions:
        domain = _extract_sender_domain(item.get("sender", ""))
        if not domain:
            continue

        action = item.get("action", "skip")
        if action == "skip":
            continue

        dest = item.get("dest_folder") or ""

        if domain in patterns:
            p = patterns[domain]
            # If same action+dest, increment confirmed
            if p["action"] == action and p.get("dest", "") == dest:
                p["confirmed"] = p.get("confirmed", 0) + 1
            else:
                # Different action — could be user correction or inconsistency
                # Increment rejected for the old pattern, update to new
                p["rejected"] = p.get("rejected", 0) + 1
                p["action"] = action
                p["dest"] = dest
                p["confirmed"] = 1
            p["last_seen"] = now
            total = p.get("confirmed", 1) + p.get("rejected", 0)
            p["confidence"] = p.get("confirmed", 1) / total if total > 0 else 0
        else:
            patterns[domain] = {
                "action": action,
                "dest": dest,
                "confidence": 1.0,
                "confirmed": 1,
                "rejected": 0,
                "last_seen": now,
            }

    save_patterns(store)


def get_patterns_for_batch(senders: list[str]) -> list[dict]:
    """Return matching patterns for a list of sender strings.

    Returns dicts with: domain, action, dest, confidence, confirmed.
    Used to inject few-shot context into the LLM prompt.
    """
    store = load_patterns()
    patterns = store.get("patterns", {})

    if not patterns:
        return []

    seen = set()
    matches = []
    for sender in senders:
        domain = _extract_sender_domain(sender)
        if domain in patterns and domain not in seen:
            p = patterns[domain]
            matches.append({
                "domain": domain,
                "action": p["action"],
                "dest": p.get("dest", ""),
                "confidence": p.get("confidence", 0),
                "confirmed": p.get("confirmed", 0),
            })
            seen.add(domain)

    return matches


def format_patterns_for_prompt(patterns: list[dict]) -> str:
    """Format matched patterns as a prompt section for the LLM."""
    if not patterns:
        return ""

    lines = [
        "## Learned Patterns (from user's past confirmed actions)",
        "Apply these patterns consistently. They reflect the user's preferences:",
        "",
    ]
    for p in sorted(patterns, key=lambda x: x["confirmed"], reverse=True):
        dest_str = f" → {p['dest']}" if p["dest"] else ""
        lines.append(
            f"- Emails from {p['domain']}: {p['action']}{dest_str} "
            f"(confirmed {p['confirmed']} times)"
        )

    return "\n".join(lines)
