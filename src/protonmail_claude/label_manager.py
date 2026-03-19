"""IMAP folder/label management operations."""

from __future__ import annotations

from protonmail_claude.imap_client import ProtonIMAPClient


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
