"""IMAP folder/label management operations."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from protonmail_claude.claude_client import call_claude_json
from protonmail_claude.imap_client import EmailMessage, ProtonIMAPClient

logger = logging.getLogger(__name__)


class LabelManager:
    """Manages IMAP folders and labels via Proton Bridge."""

    def __init__(self, imap_client: ProtonIMAPClient) -> None:
        self._client = imap_client

    @property
    def imap(self):
        return self._client.client

    def list_folders(self) -> list[str]:
        """List all IMAP folders. Returns folder names as strings."""
        raw = self.imap.list_folders()
        return [name for _flags, _delimiter, name in raw]

    def create_folder(self, name: str) -> None:
        """Create a new IMAP folder."""
        self.imap.create_folder(name)

    def delete_folder(self, name: str) -> None:
        """Delete an IMAP folder."""
        self.imap.delete_folder(name)

    def move_message(self, uid: int, dest_folder: str, src_folder: str = "INBOX") -> None:
        """Move a message from src_folder to dest_folder.

        Uses COPY + STORE \\Deleted + EXPUNGE pattern.
        """
        self.imap.select_folder(src_folder)
        self.imap.copy([uid], dest_folder)
        self.imap.set_flags([uid], [b"\\Deleted"])
        self.imap.expunge([uid])

    def apply_label(self, uid: int, label: str, folder: str = "INBOX") -> None:
        """Add a flag/label to a message."""
        self.imap.select_folder(folder)
        self.imap.add_flags([uid], [label.encode()])

    def remove_label(self, uid: int, label: str, folder: str = "INBOX") -> None:
        """Remove a flag/label from a message."""
        self.imap.select_folder(folder)
        self.imap.remove_flags([uid], [label.encode()])

    def bulk_move(
        self,
        search_criteria: list[str],
        dest_folder: str,
        src_folder: str = "INBOX",
    ) -> list[int]:
        """Search for messages and move all matches to dest_folder.

        Returns the list of UIDs that were moved.
        """
        self.imap.select_folder(src_folder)
        uids = self.imap.search(search_criteria)

        if not uids:
            return []

        self.imap.copy(uids, dest_folder)
        self.imap.set_flags(uids, [b"\\Deleted"])
        self.imap.expunge(uids)

        return uids


@dataclass
class OrganizeResult:
    """Result of a natural language organize operation."""

    operations: list[dict] = field(default_factory=list)
    executed: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.executed:
            parts.append(f"{len(self.executed)} operations executed")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts) if parts else "No operations to perform."


def _build_context(
    folders: list[str],
    recent_emails: list[EmailMessage],
) -> str:
    """Build context string with folders and recent emails for Claude."""
    email_summaries = []
    for msg in recent_emails:
        email_summaries.append({
            "uid": msg.uid,
            "sender": msg.sender,
            "subject": msg.subject,
            "date": msg.date.isoformat() if msg.date else "",
        })

    return json.dumps({
        "folders": folders,
        "recent_emails": email_summaries,
    }, indent=2)


def organize(
    instruction: str,
    imap_client: ProtonIMAPClient,
    dry_run: bool = False,
    context_count: int = 50,
    model: str | None = None,
) -> OrganizeResult:
    """Execute a natural language email organization instruction.

    Sends the instruction plus current folder list and recent email context
    to Claude, which resolves it into concrete label_manager operations.

    Args:
        instruction: Natural language instruction (e.g., "Move all newsletters to Archive/Newsletters").
        imap_client: Connected ProtonIMAPClient.
        dry_run: If True, resolve operations but don't execute them.
        context_count: Number of recent emails to include as context.
        model: Optional Claude model override.

    Returns:
        OrganizeResult with the planned and executed operations.
    """
    mgr = LabelManager(imap_client)
    folders = mgr.list_folders()
    recent = imap_client.fetch_recent(count=context_count)

    context = _build_context(folders, recent)
    prompt = f"Instruction: {instruction}\n\nContext:\n{context}"

    operations = call_claude_json(
        user_content=prompt,
        system_prompt_name="label_assistant_system",
        max_tokens=2048,
        model=model,
    )

    result = OrganizeResult(operations=operations)

    if dry_run:
        return result

    for op in operations:
        try:
            action = op["action"]
            if action == "create_folder":
                mgr.create_folder(op["name"])
            elif action == "move_message":
                mgr.move_message(
                    op["uid"],
                    op["dest_folder"],
                    src_folder=op.get("src_folder", "INBOX"),
                )
            elif action == "bulk_move":
                mgr.bulk_move(
                    op["search_criteria"],
                    op["dest_folder"],
                    src_folder=op.get("src_folder", "INBOX"),
                )
            elif action == "apply_label":
                mgr.apply_label(
                    op["uid"],
                    op["label"],
                    folder=op.get("folder", "INBOX"),
                )
            elif action == "remove_label":
                mgr.remove_label(
                    op["uid"],
                    op["label"],
                    folder=op.get("folder", "INBOX"),
                )
            else:
                result.errors.append({"op": op, "error": f"Unknown action: {action}"})
                continue

            result.executed.append(op)
        except Exception as e:
            logger.warning("Failed to execute operation %s: %s", op, e)
            result.errors.append({"op": op, "error": str(e)})

    return result
