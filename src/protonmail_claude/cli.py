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


@app.command("auto-organize")
def auto_organize_cmd(
    folder: str = typer.Option("INBOX", help="IMAP folder to scan."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show recommendations without executing."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bulk-confirm non-destructive actions."),
    skip_actions: str = typer.Option("", help="Comma-separated actions to suppress (e.g. 'trash,flag')."),
    output: Optional[str] = typer.Option(None, help="Write recommendations as JSON to this path."),
    max_emails: int = typer.Option(50, help="Max unread emails to process per iteration."),
    batch_size: int = typer.Option(20, help="Emails per LLM call."),
    metadata_only: bool = typer.Option(False, "--metadata-only", help="Classify on sender/subject/date only, skip body."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print token usage per batch."),
    loop: bool = typer.Option(False, "--loop", help="Keep processing until no unread emails remain."),
    max_iterations: int = typer.Option(100, help="Hard cap on loop iterations (only used with --loop)."),
    inter_batch_delay: int = typer.Option(5, help="Seconds to sleep between iterations (only used with --loop)."),
) -> None:
    """Fetch unread emails and recommend actions using the LLM."""
    from protonmail_claude.auto_organizer import auto_organize, auto_organize_loop
    from protonmail_claude.imap_client import ProtonIMAPClient

    if loop and dry_run:
        raise typer.BadParameter("--loop and --dry-run are mutually exclusive.")

    skip_set = {s.strip() for s in skip_actions.split(",") if s.strip()}

    if loop:
        auto_organize_loop(
            folder=folder,
            max_emails=max_emails,
            batch_size=batch_size,
            auto_confirm=yes,
            skip_actions=skip_set,
            metadata_only=metadata_only,
            verbose=verbose,
            max_iterations=max_iterations,
            inter_batch_delay=inter_batch_delay,
        )
        return

    with ProtonIMAPClient() as client:
        result = auto_organize(
            imap_client=client,
            folder=folder,
            max_emails=max_emails,
            batch_size=batch_size,
            dry_run=dry_run,
            auto_confirm=yes,
            skip_actions=skip_set,
            metadata_only=metadata_only,
            verbose=verbose,
        )

    if output:
        with open(output, "w") as f:
            f.write(result.to_json())
        typer.echo(f"Recommendations written to {output}")


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


@labels_app.command("recommend")
def labels_recommend(
    folder: str = typer.Option("INBOX", help="Primary folder to analyze."),
    all_folders: bool = typer.Option(False, "--all-folders", help="Analyze every folder."),
    sample_size: int = typer.Option(200, help="Max messages to sample per folder."),
    output: Optional[str] = typer.Option(None, help="Write JSON report to this path."),
    min_count: int = typer.Option(10, help="Ignore sender clusters smaller than N."),
    user_context: Optional[str] = typer.Option(
        None,
        help='Free-text bias, e.g. "software engineer, client work". '
        "Falls back to USER_CONTEXT env var if not set.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show progress per phase and token usage."),
) -> None:
    """Analyze folder structure and recommend reorganization steps.

    Prints a prioritized list of recommendations grouped by impact. Each
    recommendation includes a pasteable `labels organize` command — no changes
    are made automatically. Review the output and run the suggested commands
    manually.

    NOTE: --apply is not available in v1. It is gated on DECISION-002 executor
    completion. Copy and run the printed cli_command strings to act on any
    recommendation.
    """
    import os

    from protonmail_claude.folder_recommender import RecommendResult, present_recommendations, recommend
    from protonmail_claude.imap_client import ProtonIMAPClient

    # USER_CONTEXT env var provides a persistent default; CLI flag takes precedence.
    effective_context = user_context or os.getenv("USER_CONTEXT")

    with ProtonIMAPClient() as client:
        result: RecommendResult = recommend(
            imap_client=client,
            folder=folder,
            sample_size=sample_size,
            min_count=min_count,
            all_folders=all_folders,
            user_context=effective_context,
            verbose=verbose,
        )

    present_recommendations(result)

    if output:
        with open(output, "w") as f:
            f.write(result.to_json())
        typer.echo(f"\nJSON report written to {output}")


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
