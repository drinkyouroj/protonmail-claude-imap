"""Typer CLI entry points for protonmail-claude."""

from __future__ import annotations

import json
import sys
from typing import Optional

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(help="ProtonMail + Claude API integration via Proton Bridge.")
labels_app = typer.Typer(help="Label and folder management.")
app.add_typer(labels_app, name="labels")


@app.command()
def digest(
    count: int = typer.Option(20, help="Number of recent emails to include."),
    folder: str = typer.Option("INBOX", help="IMAP folder to fetch from."),
    output: Optional[str] = typer.Option(None, help="Output file path (JSON). Prints to stdout if omitted."),
) -> None:
    """Fetch recent emails and generate a Claude-powered digest."""
    from protonmail_claude.digest import fetch_and_digest

    digest_result = fetch_and_digest(folder=folder, count=count)

    if output:
        with open(output, "w") as f:
            f.write(digest_result.to_json())
        typer.echo(f"Digest written to {output} ({digest_result.email_count} emails)")
    else:
        typer.echo(digest_result.to_json())


@app.command()
def draft(
    uid: int = typer.Option(..., help="UID of the email to reply to."),
    folder: str = typer.Option("INBOX", help="IMAP folder containing the email."),
    send: bool = typer.Option(False, help="Send the draft after confirmation."),
) -> None:
    """Generate a reply draft for an email thread."""
    from protonmail_claude.drafter import draft_reply_for_uid, send_draft

    draft_result = draft_reply_for_uid(uid=uid, folder=folder)

    typer.echo(f"\n--- Draft Reply ({draft_result.tone}) ---")
    typer.echo(f"To: {draft_result.to_address}")
    typer.echo(f"Subject: {draft_result.subject}")
    typer.echo(f"\n{draft_result.body}")
    if draft_result.notes:
        typer.echo(f"\n[Notes: {draft_result.notes}]")
    typer.echo("---\n")

    if send:
        confirm = typer.confirm("Send this reply?")
        if confirm:
            send_draft(draft_result)
            typer.echo("Reply sent.")
        else:
            typer.echo("Send cancelled.")


@labels_app.command("list")
def labels_list(
) -> None:
    """List all IMAP folders."""
    from protonmail_claude.imap_client import ProtonIMAPClient
    from protonmail_claude.label_manager import LabelManager

    with ProtonIMAPClient() as client:
        mgr = LabelManager(client)
        for folder in mgr.list_folders():
            typer.echo(folder)


@labels_app.command("create")
def labels_create(
    name: str = typer.Option(..., help="Folder name to create."),
) -> None:
    """Create a new IMAP folder."""
    from protonmail_claude.imap_client import ProtonIMAPClient
    from protonmail_claude.label_manager import LabelManager

    with ProtonIMAPClient() as client:
        mgr = LabelManager(client)
        mgr.create_folder(name)
        typer.echo(f"Created folder: {name}")


@labels_app.command("move")
def labels_move(
    uid: int = typer.Option(..., help="UID of the message to move."),
    dest: str = typer.Option(..., help="Destination folder."),
    src: str = typer.Option("INBOX", help="Source folder."),
) -> None:
    """Move a message to a different folder."""
    from protonmail_claude.imap_client import ProtonIMAPClient
    from protonmail_claude.label_manager import LabelManager

    with ProtonIMAPClient() as client:
        mgr = LabelManager(client)
        mgr.move_message(uid, dest, src_folder=src)
        typer.echo(f"Moved UID {uid} from {src} to {dest}")


@labels_app.command("bulk-move")
def labels_bulk_move(
    criteria: str = typer.Option(..., help='Search criteria as JSON array, e.g. \'["FROM", "news@example.com"]\''),
    dest: str = typer.Option(..., help="Destination folder."),
    src: str = typer.Option("INBOX", help="Source folder."),
) -> None:
    """Search for messages and move all matches to a folder."""
    from protonmail_claude.imap_client import ProtonIMAPClient
    from protonmail_claude.label_manager import LabelManager

    parsed_criteria = json.loads(criteria)

    with ProtonIMAPClient() as client:
        mgr = LabelManager(client)
        moved = mgr.bulk_move(parsed_criteria, dest, src_folder=src)
        typer.echo(f"Moved {len(moved)} messages to {dest}")


@labels_app.command("organize")
def labels_organize(
    instruction: str = typer.Argument(..., help='Natural language instruction, e.g. "Move all newsletters to Archive/Newsletters"'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned operations without executing."),
    context_count: int = typer.Option(50, help="Number of recent emails to include as context."),
) -> None:
    """Organize emails using a natural language instruction."""
    from protonmail_claude.imap_client import ProtonIMAPClient
    from protonmail_claude.label_manager import organize

    with ProtonIMAPClient() as client:
        result = organize(
            instruction=instruction,
            imap_client=client,
            dry_run=dry_run,
            context_count=context_count,
        )

    if not result.operations:
        typer.echo("No operations resolved from instruction.")
        return

    typer.echo(f"\nPlanned operations ({len(result.operations)}):")
    for i, op in enumerate(result.operations, 1):
        typer.echo(f"  {i}. {op['action']}: {json.dumps({k: v for k, v in op.items() if k != 'action'})}")

    if dry_run:
        typer.echo("\n[dry run — no changes made]")
    else:
        typer.echo(f"\n{result.summary}")
