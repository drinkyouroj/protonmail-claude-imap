"""Auto-organize pipeline: fetch unread → LLM triage → validate → execute."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

import typer

from protonmail_claude.claude_client import call_claude_json
from protonmail_claude.imap_client import EmailMessage, ProtonIMAPClient
from protonmail_claude.label_manager import LabelManager

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"archive", "move", "label", "flag", "mark_read", "trash", "skip"}
BODY_TRUNCATE_LEN = 800


@dataclass
class RecommendedAction:
    """A single LLM-recommended action for an email."""

    uid: int
    action: str
    dest_folder: str | None = None
    label: str | None = None
    create_folder_if_missing: bool = False
    reason: str = ""
    # Populated from fetched EmailMessage, not LLM output
    sender: str = ""
    subject: str = ""
    body_available: bool = True


@dataclass
class AutoOrganizeResult:
    """Result of an auto-organize run."""

    total_analyzed: int = 0
    recommendations: list[RecommendedAction] = field(default_factory=list)
    applied: list[RecommendedAction] = field(default_factory=list)
    skipped: list[RecommendedAction] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    dry_run: bool = False

    @property
    def summary(self) -> str:
        parts = [f"{self.total_analyzed} analyzed"]
        if self.applied:
            parts.append(f"{len(self.applied)} applied")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)


def _serialize_emails(
    messages: list[EmailMessage],
    available_folders: list[str],
    metadata_only: bool = False,
) -> str:
    """Serialize emails for the LLM prompt with prompt injection mitigation."""
    emails = []
    for msg in messages:
        entry: dict = {
            "uid": msg.uid,
            "sender": msg.sender,
            "subject": msg.subject,
            "date": msg.date.isoformat() if msg.date else "",
        }
        if not metadata_only:
            body = msg.body[:BODY_TRUNCATE_LEN].strip()
            if body:
                entry["body"] = f"<email_body>{body}</email_body>"
            else:
                entry["body"] = ""
                entry["body_available"] = False
        else:
            entry["body_available"] = False
        emails.append(entry)

    return json.dumps({
        "available_folders": available_folders,
        "emails": emails,
    }, indent=2)


def _validate_recommendation(
    raw: dict,
    valid_uids: set[int],
    available_folders: list[str],
    messages_by_uid: dict[int, EmailMessage],
) -> RecommendedAction | None:
    """Validate a raw LLM recommendation dict. Returns None if invalid."""
    # Required fields
    uid = raw.get("uid")
    action = raw.get("action")

    if uid is None or action is None:
        logger.warning("Dropping recommendation: missing uid or action: %s", raw)
        return None

    if not isinstance(uid, int):
        try:
            uid = int(uid)
        except (ValueError, TypeError):
            logger.warning("Dropping recommendation: invalid uid type: %s", raw)
            return None

    # UID allowlist
    if uid not in valid_uids:
        logger.warning("Dropping recommendation: UID %d not in fetched set", uid)
        return None

    # Action validation
    if action not in VALID_ACTIONS:
        logger.warning("Dropping recommendation: unknown action '%s' for UID %d", action, uid)
        return None

    # Folder validation for move/archive
    dest_folder = raw.get("dest_folder")
    create_if_missing = bool(raw.get("create_folder_if_missing", False))

    if action == "archive":
        dest_folder = dest_folder or "Archive"
    elif action == "move":
        if not dest_folder:
            logger.warning("Dropping recommendation: 'move' without dest_folder for UID %d", uid)
            return None
        if dest_folder not in available_folders and not create_if_missing:
            logger.warning(
                "Dropping recommendation: dest_folder '%s' not in available folders for UID %d",
                dest_folder, uid,
            )
            return None

    # Populate sender/subject from fetched message, not LLM
    msg = messages_by_uid.get(uid)
    sender = msg.sender if msg else ""
    subject = msg.subject if msg else ""
    body_available = bool(msg and msg.body.strip()) if msg else False

    # Safety: no trash for emails without body
    if action == "trash" and not body_available:
        logger.warning("Overriding trash→skip for UID %d: no body available", uid)
        action = "skip"
        raw["reason"] = raw.get("reason", "") + " [overridden: no body available]"

    return RecommendedAction(
        uid=uid,
        action=action,
        dest_folder=dest_folder,
        label=raw.get("label"),
        create_folder_if_missing=create_if_missing,
        reason=raw.get("reason", ""),
        sender=sender,
        subject=subject,
        body_available=body_available,
    )


def _analyze_batch(
    messages: list[EmailMessage],
    available_folders: list[str],
    valid_uids: set[int],
    messages_by_uid: dict[int, EmailMessage],
    metadata_only: bool = False,
    model: str | None = None,
) -> list[RecommendedAction]:
    """Send a batch of emails to the LLM and return validated recommendations."""
    user_content = _serialize_emails(messages, available_folders, metadata_only=metadata_only)

    try:
        raw_recs = call_claude_json(
            user_content=user_content,
            system_prompt_name="auto_organize_system",
            max_tokens=2048,
            model=model,
        )
    except Exception as e:
        logger.warning("LLM call failed for batch: %s", e)
        # Return skip for all UIDs in this batch
        return [
            RecommendedAction(
                uid=msg.uid, action="skip", reason=f"LLM error: {e}",
                sender=msg.sender, subject=msg.subject,
            )
            for msg in messages
        ]

    if not isinstance(raw_recs, list):
        logger.warning("LLM returned non-list response, skipping batch")
        return [
            RecommendedAction(
                uid=msg.uid, action="skip", reason="LLM returned invalid format",
                sender=msg.sender, subject=msg.subject,
            )
            for msg in messages
        ]

    validated = []
    seen_uids = set()
    for raw in raw_recs:
        rec = _validate_recommendation(raw, valid_uids, available_folders, messages_by_uid)
        if rec and rec.uid not in seen_uids:
            validated.append(rec)
            seen_uids.add(rec.uid)

    # Any UIDs in the batch not covered by LLM → skip
    for msg in messages:
        if msg.uid not in seen_uids:
            validated.append(RecommendedAction(
                uid=msg.uid, action="skip", reason="Not covered by LLM response",
                sender=msg.sender, subject=msg.subject,
            ))

    return validated


def _present_recommendations(recommendations: list[RecommendedAction]) -> None:
    """Display recommendations as a human-readable table."""
    # Group by action
    by_action: dict[str, list[RecommendedAction]] = {}
    for rec in recommendations:
        by_action.setdefault(rec.action, []).append(rec)

    for action in ["flag", "trash", "move", "archive", "label", "mark_read", "skip"]:
        recs = by_action.get(action, [])
        if not recs:
            continue
        typer.echo(f"\n  {action.upper()} ({len(recs)}):")
        for rec in recs:
            dest = f" → {rec.dest_folder}" if rec.dest_folder else ""
            lbl = f" [{rec.label}]" if rec.label else ""
            subj = rec.subject[:50] + "..." if len(rec.subject) > 50 else rec.subject
            typer.echo(f"    UID {rec.uid:>6}  {rec.sender[:30]:<30}  {subj}{dest}{lbl}")
            typer.echo(f"             {rec.reason}")


def _apply_recommendation(
    rec: RecommendedAction,
    mgr: LabelManager,
    folder: str,
) -> None:
    """Execute a single recommendation via LabelManager."""
    if rec.action == "skip":
        return

    if rec.action == "archive":
        mgr.move_message(rec.uid, rec.dest_folder or "Archive", src_folder=folder)
    elif rec.action == "move":
        mgr.move_message(rec.uid, rec.dest_folder, src_folder=folder)
    elif rec.action == "trash":
        mgr.move_message(rec.uid, "Trash", src_folder=folder)
    elif rec.action == "flag":
        mgr.apply_label(rec.uid, "\\Flagged", folder=folder)
    elif rec.action == "label":
        mgr.apply_label(rec.uid, rec.label, folder=folder)
    elif rec.action == "mark_read":
        mgr.apply_label(rec.uid, "\\Seen", folder=folder)


def auto_organize(
    imap_client: ProtonIMAPClient,
    folder: str = "INBOX",
    max_emails: int = 50,
    batch_size: int = 20,
    dry_run: bool = False,
    auto_confirm: bool = False,
    skip_actions: set[str] | None = None,
    metadata_only: bool = False,
    verbose: bool = False,
    model: str | None = None,
) -> AutoOrganizeResult:
    """End-to-end auto-organize pipeline.

    Args:
        imap_client: Connected ProtonIMAPClient.
        folder: IMAP folder to scan.
        max_emails: Cap on unread emails to process.
        batch_size: Emails per LLM call.
        dry_run: Show recommendations without executing.
        auto_confirm: Skip bulk confirmation (--yes). Trash still confirms individually.
        skip_actions: Action types to suppress.
        metadata_only: Skip body fetch, classify on metadata only.
        verbose: Print token usage per batch.
        model: Optional LLM model override.
    """
    skip_actions = skip_actions or set()
    result = AutoOrganizeResult(dry_run=dry_run)

    # Step 1: Fetch unread UIDs
    typer.echo(f"Fetching unread emails from {folder}...")
    unread_uids = imap_client.search(["UNSEEN"], folder=folder)

    if not unread_uids:
        typer.echo("No unread emails found.")
        return result

    # Cap and warn
    total_unread = len(unread_uids)
    target_uids = unread_uids[:max_emails]  # oldest first
    if total_unread > max_emails:
        typer.echo(f"  {total_unread} unread emails found, processing oldest {max_emails}.")
    else:
        typer.echo(f"  {total_unread} unread emails found.")

    # Fetch full messages
    typer.echo("Fetching email content...")
    messages = imap_client.fetch_by_uids(target_uids, folder=folder)
    messages_by_uid = {msg.uid: msg for msg in messages}
    valid_uids = set(messages_by_uid.keys())
    result.total_analyzed = len(messages)

    # Get folder list for validation
    mgr = LabelManager(imap_client)
    available_folders = mgr.list_folders()

    # Step 2: Analyze in batches
    all_recommendations: list[RecommendedAction] = []
    batches = [messages[i:i + batch_size] for i in range(0, len(messages), batch_size)]

    for i, batch in enumerate(batches, 1):
        typer.echo(f"Analyzing batch {i}/{len(batches)} ({len(batch)} emails)...")
        batch_recs = _analyze_batch(
            batch, available_folders, valid_uids, messages_by_uid,
            metadata_only=metadata_only, model=model,
        )
        all_recommendations.extend(batch_recs)

    # Filter suppressed actions
    for rec in all_recommendations:
        if rec.action in skip_actions:
            rec.action = "skip"
            rec.reason += f" [suppressed by --skip-actions]"

    result.recommendations = all_recommendations

    # Step 3: Present
    non_skip = [r for r in all_recommendations if r.action != "skip"]
    skip_count = len(all_recommendations) - len(non_skip)

    typer.echo(f"\nAuto-Organize: {result.total_analyzed} emails analyzed")
    typer.echo(f"  {len(non_skip)} actionable, {skip_count} skipped")

    if non_skip:
        _present_recommendations(all_recommendations)

    if dry_run:
        typer.echo("\n[dry run — no changes made]")
        result.skipped = list(all_recommendations)
        return result

    if not non_skip:
        typer.echo("No actionable recommendations.")
        result.skipped = list(all_recommendations)
        return result

    # Step 4: Confirm
    if not auto_confirm:
        typer.echo("")
        if not typer.confirm(f"Apply {len(non_skip)} recommendations?"):
            typer.echo("Cancelled.")
            result.skipped = list(all_recommendations)
            return result

    # Step 5: Execute
    # Pre-pass: create folders first
    folders_to_create = set()
    for rec in non_skip:
        if rec.create_folder_if_missing and rec.dest_folder and rec.dest_folder not in available_folders:
            folders_to_create.add(rec.dest_folder)

    for folder_name in sorted(folders_to_create):
        try:
            typer.echo(f"Creating folder: {folder_name}")
            mgr.create_folder(folder_name)
            available_folders.append(folder_name)
        except Exception as e:
            logger.warning("Failed to create folder %s: %s", folder_name, e)
            result.errors.append({"uid": None, "action": "create_folder", "folder": folder_name, "error": str(e)})

    # Check UIDVALIDITY before writes
    imap_client.assert_uidvalidity(folder)

    for rec in all_recommendations:
        if rec.action == "skip":
            result.skipped.append(rec)
            continue

        # Trash always requires individual confirmation
        if rec.action == "trash":
            subj = rec.subject[:60]
            if not typer.confirm(f"  Trash UID {rec.uid} ({rec.sender} — {subj})?"):
                result.skipped.append(rec)
                continue

        try:
            _apply_recommendation(rec, mgr, folder)
            result.applied.append(rec)
        except Exception as e:
            logger.warning("Failed to apply %s on UID %d: %s", rec.action, rec.uid, e)
            result.errors.append({"uid": rec.uid, "action": rec.action, "error": str(e)})

    # Step 6: Report
    typer.echo(f"\n{result.summary}")
    if result.errors:
        for err in result.errors:
            typer.echo(f"  ERROR: UID {err.get('uid')} {err['action']}: {err['error']}")

    return result
