"""Auto-organize pipeline: fetch unread → LLM triage → validate → execute."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field

import typer

from protonmail_claude.claude_client import call_claude_json, _load_prompt
from protonmail_claude.imap_client import EmailMessage, ProtonIMAPClient
from protonmail_claude.label_manager import LabelManager
from protonmail_claude.pattern_store import (
    format_patterns_for_prompt,
    get_patterns_for_batch,
    record_actions,
)
from protonmail_claude.profile import build_system_prompt, load_profile

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


FLAG_THRESHOLDS = {
    "low": "Flag only emails that require an urgent response within 24 hours (e.g. payment failures, security alerts, account lockouts). Most emails should NOT be flagged.",
    "medium": "Flag emails that are time-sensitive or require personal action within a few days. Newsletters, promotions, and notifications should NOT be flagged.",
    "high": "Flag any email that might benefit from follow-up, including interesting content, requests, and time-sensitive offers.",
}
DEFAULT_FLAG_THRESHOLD = "low"


def _serialize_emails(
    messages: list[EmailMessage],
    available_folders: list[str],
    metadata_only: bool = False,
    flag_threshold: str = DEFAULT_FLAG_THRESHOLD,
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

    payload: dict = {
        "available_folders": available_folders,
        "emails": emails,
    }

    # Inject flag threshold guidance
    threshold_guidance = FLAG_THRESHOLDS.get(flag_threshold, FLAG_THRESHOLDS[DEFAULT_FLAG_THRESHOLD])
    payload["flag_guidance"] = threshold_guidance

    return json.dumps(payload, indent=2)


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
    flag_threshold: str = DEFAULT_FLAG_THRESHOLD,
    system_prompt: str | None = None,
) -> list[RecommendedAction]:
    """Send a batch of emails to the LLM and return validated recommendations."""
    user_content = _serialize_emails(messages, available_folders, metadata_only=metadata_only, flag_threshold=flag_threshold)

    # Use pre-built system prompt (with profile + patterns) or fall back to base
    prompt_to_use = system_prompt or _load_prompt("auto_organize_system")

    try:
        raw_recs = call_claude_json(
            user_content=user_content,
            system_prompt=prompt_to_use,
            max_tokens=4096,
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
    """Execute a single recommendation using raw IMAP ops.

    Assumes the folder is already selected — avoids redundant SELECT calls
    that overwhelm Bridge on large mailboxes.
    """
    if rec.action == "skip":
        return

    imap = mgr.imap

    if rec.action in ("archive", "move", "trash"):
        dest = {"archive": rec.dest_folder or "Archive",
                "move": rec.dest_folder,
                "trash": "Trash"}[rec.action]
        imap.copy([rec.uid], dest)
        imap.set_flags([rec.uid], [b"\\Deleted"])
        imap.expunge([rec.uid])
    elif rec.action == "flag":
        imap.add_flags([rec.uid], [b"\\Flagged"])
    elif rec.action == "label":
        imap.add_flags([rec.uid], [rec.label.encode()])
    elif rec.action == "mark_read":
        imap.add_flags([rec.uid], [b"\\Seen"])


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
    flag_threshold: str = DEFAULT_FLAG_THRESHOLD,
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

    # Build enriched system prompt once (profile + patterns for this batch)
    base_prompt = _load_prompt("auto_organize_system")
    user_profile = load_profile()
    enriched_prompt = build_system_prompt(base_prompt, user_profile)

    # Inject learned patterns as few-shot context
    all_senders = [msg.sender for msg in messages]
    matched_patterns = get_patterns_for_batch(all_senders)
    if matched_patterns:
        pattern_section = format_patterns_for_prompt(matched_patterns)
        enriched_prompt = enriched_prompt + "\n\n" + pattern_section
        typer.echo(f"  {len(matched_patterns)} learned pattern(s) injected into prompt.")

    # Step 2: Analyze in batches
    all_recommendations: list[RecommendedAction] = []
    batches = [messages[i:i + batch_size] for i in range(0, len(messages), batch_size)]

    for i, batch in enumerate(batches, 1):
        typer.echo(f"Analyzing batch {i}/{len(batches)} ({len(batch)} emails)...")
        batch_recs = _analyze_batch(
            batch, available_folders, valid_uids, messages_by_uid,
            metadata_only=metadata_only, model=model,
            flag_threshold=flag_threshold,
            system_prompt=enriched_prompt,
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
    # Reconnect — the original connection likely died during LLM analysis.
    # Bridge drops idle connections after a few minutes and needs time to
    # clean up stale sockets before accepting a new one.
    typer.echo("Reconnecting to Bridge for execution...")
    imap_client.disconnect()
    for attempt in range(3):
        try:
            time.sleep(2)  # give Bridge time to clean up
            imap_client.connect()
            break
        except Exception as e:
            if attempt < 2:
                typer.echo(f"  Reconnect attempt {attempt + 1} failed ({e}), retrying...")
            else:
                typer.echo(f"  Could not reconnect to Bridge after 3 attempts: {e}")
                raise

    # Clear stale UIDVALIDITY cache — we just reconnected, so the old
    # values are from a dead session. The write operations will select
    # the folder as needed via LabelManager methods.
    imap_client._uidvalidity.clear()

    # Pre-pass: create folders first
    mgr = LabelManager(imap_client)
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

    # Select folder once before the execution loop — avoids 50 redundant
    # SELECT calls on a 40K mailbox which overwhelms Bridge.
    try:
        mgr.imap.select_folder(folder)
    except Exception:
        pass  # Will be retried on first operation

    def _reconnect_and_select() -> None:
        """Reconnect to Bridge and re-select the folder."""
        typer.echo("  Connection lost, reconnecting...")
        imap_client.disconnect()
        time.sleep(2)
        imap_client.connect()
        mgr._client = imap_client  # rebind the label manager
        mgr.imap.select_folder(folder)

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

        # Try the operation, reconnect once on timeout, then fail
        for attempt in range(2):
            try:
                _apply_recommendation(rec, mgr, folder)
                result.applied.append(rec)
                break
            except Exception as e:
                if attempt == 0 and "timed out" in str(e).lower():
                    try:
                        _reconnect_and_select()
                        continue  # retry the operation
                    except Exception as reconnect_err:
                        logger.warning("Reconnect failed: %s", reconnect_err)
                logger.warning("Failed to apply %s on UID %d: %s", rec.action, rec.uid, e)
                result.errors.append({"uid": rec.uid, "action": rec.action, "error": str(e)})

    # Step 6: Record patterns from applied actions
    if result.applied:
        record_actions([
            {"sender": rec.sender, "action": rec.action, "dest_folder": rec.dest_folder}
            for rec in result.applied
        ])

    # Step 7: Report
    typer.echo(f"\n{result.summary}")
    if result.errors:
        for err in result.errors:
            typer.echo(f"  ERROR: UID {err.get('uid')} {err['action']}: {err['error']}")

    return result


def auto_organize_loop(
    folder: str = "INBOX",
    max_emails: int = 50,
    batch_size: int = 20,
    auto_confirm: bool = False,
    skip_actions: set[str] | None = None,
    metadata_only: bool = False,
    verbose: bool = False,
    model: str | None = None,
    max_iterations: int = 100,
    inter_batch_delay: int = 5,
    flag_threshold: str = DEFAULT_FLAG_THRESHOLD,
) -> None:
    """Run auto_organize in a loop until the inbox is clear or a stop condition is hit.

    Opens a fresh ProtonIMAPClient connection per iteration to handle Bridge idle drops.
    Tracks cumulative totals and detects LLM stalls (two consecutive all-skip iterations
    on identical UID sets). User declining confirmations is not a stall — the loop continues.
    """
    total_applied = 0
    total_skipped = 0
    total_errors = 0

    # Stall detection state
    prev_skip_only_uids: frozenset[int] | None = None
    consecutive_llm_skips = 0

    try:
        for iteration in range(1, max_iterations + 1):
            with ProtonIMAPClient() as client:
                # Peek at unread count for the header
                unread_uids = client.search(["UNSEEN"], folder=folder)

            if not unread_uids:
                typer.echo(f"\nLoop complete — inbox clear.")
                break

            unread_count = len(unread_uids)
            typer.echo(f"\n--- Iteration {iteration} | {unread_count:,} unread remain in {folder} ---")

            with ProtonIMAPClient() as client:
                result = auto_organize(
                    imap_client=client,
                    folder=folder,
                    max_emails=max_emails,
                    batch_size=batch_size,
                    dry_run=False,
                    auto_confirm=auto_confirm,
                    skip_actions=skip_actions,
                    metadata_only=metadata_only,
                    verbose=verbose,
                    model=model,
                    flag_threshold=flag_threshold,
                )

            iter_applied = len(result.applied)
            iter_skipped = len(result.skipped)
            iter_errors = len(result.errors)
            total_applied += iter_applied
            total_skipped += iter_skipped
            total_errors += iter_errors

            typer.echo(
                f"Iteration {iteration}: {iter_applied} applied, "
                f"{iter_skipped} skipped, {iter_errors} error(s)"
            )
            typer.echo(
                f"Session total: {total_applied} applied, "
                f"{total_skipped} skipped, {total_errors} error(s)"
            )

            # LLM stall detection: all recommendations were skip and UIDs unchanged
            processed_uids = frozenset(r.uid for r in result.recommendations)
            all_skipped_by_llm = all(r.action == "skip" for r in result.recommendations)

            if all_skipped_by_llm and result.recommendations:
                if prev_skip_only_uids == processed_uids:
                    consecutive_llm_skips += 1
                else:
                    consecutive_llm_skips = 1
                prev_skip_only_uids = processed_uids
            else:
                consecutive_llm_skips = 0
                prev_skip_only_uids = None

            if consecutive_llm_skips >= 2:
                typer.echo(
                    "\nNo actionable recommendations for remaining emails. Stopping."
                )
                break

            if iteration >= max_iterations:
                typer.echo(f"\nReached max iterations ({max_iterations}). Stopping.")
                break

            typer.echo(f"Next batch in {inter_batch_delay}s... (Ctrl+C to stop)")
            time.sleep(inter_batch_delay)

        else:
            # for-loop exhausted without break
            typer.echo(f"\nReached max iterations ({max_iterations}). Stopping.")

    except KeyboardInterrupt:
        typer.echo("\nInterrupted by user.")

    typer.echo(
        f"\nIterations: {iteration} | Applied: {total_applied} | "
        f"Skipped: {total_skipped} | Errors: {total_errors}"
    )
