"""Folder structure recommender — data collection, LLM integration, and output.

Implements DECISION-004: folder structure recommendations.

Phase 1: Folder inventory (collect_folder_inventory)
Phase 2: Sender/subject profile (collect_sender_profile)
Phase 3: LLM recommendations (get_recommendations)

All aggregation happens in Python. No email bodies are fetched — headers only.
The resulting FolderProfile is a condensed ~5-15KB payload suitable for LLM consumption.
"""

from __future__ import annotations

import email.utils
import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from protonmail_claude.imap_client import ProtonIMAPClient

logger = logging.getLogger(__name__)

# System folder names (case-insensitive). Folders matching these names or
# bearing \Noselect / \NonExistent flags are excluded from overlap analysis
# and flagged as system folders in the inventory.
_SYSTEM_FOLDER_NAMES: frozenset[str] = frozenset({
    "sent",
    "sent mail",
    "sent messages",
    "trash",
    "deleted",
    "deleted messages",
    "drafts",
    "spam",
    "junk",
    "junk mail",
    "junk e-mail",
    "all mail",
    "archive",
})

_SYSTEM_FLAGS: frozenset[bytes] = frozenset({
    b"\\Noselect",
    b"\\NonExistent",
    b"\\Drafts",
    b"\\Sent",
    b"\\Trash",
    b"\\Junk",
    b"\\Spam",
    b"\\All",
    b"\\Archive",
    b"\\Flagged",
})

# Maximum subject samples stored per cluster / pattern
_MAX_SAMPLES = 5

# Folders with more messages than this are reported as large_folders
_LARGE_FOLDER_THRESHOLD = 1000


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FolderInfo:
    """Per-folder metadata collected from IMAP STATUS."""

    name: str
    message_count: int
    unseen_count: int
    is_system: bool  # True if Sent, Trash, Drafts, Spam, etc., or \Noselect
    flags: tuple[bytes, ...] = field(default_factory=tuple)
    recent_count: int = 0


@dataclass
class SenderCluster:
    """Aggregated information about a single email sender."""

    address: str        # full email address — primary key
    display_name: str   # human-readable name from the From header
    domain: str         # domain portion — secondary label only
    count: int
    sample_subjects: list[str] = field(default_factory=list)  # up to 5


@dataclass
class FolderProfile:
    """Aggregated profile ready for LLM consumption.

    Produced by build_profile() and serialized by serialize_profile().
    """

    folder_inventory: list[FolderInfo]
    sender_clusters: list[SenderCluster]       # sorted by count descending
    subject_patterns: list[dict]               # {"pattern": str, "count": int, "examples": list[str]}
    overlap_pairs: list[tuple[str, str]]       # folder name pairs with substring overlap
    empty_folders: list[str]
    large_folders: list[str]                   # folders with > 1000 messages
    sample_size: int                           # messages actually sampled
    total_in_scope: int                        # total messages in the target folder(s)


# ---------------------------------------------------------------------------
# Phase 1: Folder Inventory
# ---------------------------------------------------------------------------


def _is_system_folder(name: str, flags: tuple[bytes, ...]) -> bool:
    """Return True if this folder is a system or non-selectable folder."""
    # Check flags first — most reliable signal
    flag_set = {f.lower() for f in flags}
    for sys_flag in _SYSTEM_FLAGS:
        if sys_flag.lower() in flag_set:
            return True

    # Fall back to well-known name matching (case-insensitive, strip hierarchy)
    leaf = name.lower().rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return leaf in _SYSTEM_FOLDER_NAMES or name.lower() in _SYSTEM_FOLDER_NAMES


def collect_folder_inventory(imap_client: ProtonIMAPClient) -> list[FolderInfo]:
    """Collect per-folder metadata for every accessible folder.

    Calls imap_client.list_folders_with_flags() to get (flags, name) tuples,
    then imap_client.folder_status(name) for message/unseen/recent counts.

    Returns a list of FolderInfo objects, one per accessible folder.
    """
    folders_with_flags: list[tuple[tuple[bytes, ...], str]] = (
        imap_client.list_folders_with_flags()
    )

    inventory: list[FolderInfo] = []
    for flags, name in folders_with_flags:
        try:
            status = imap_client.folder_status(name)
        except Exception:
            logger.warning("Could not get STATUS for folder %r — skipping", name)
            continue

        # folder_status may return byte keys (from IMAPClient) or string keys
        def _get_status(key_str: str) -> int:
            return int(
                status.get(key_str.encode(), status.get(key_str, 0))
            )

        info = FolderInfo(
            name=name,
            flags=flags,
            message_count=_get_status("MESSAGES"),
            unseen_count=_get_status("UNSEEN"),
            recent_count=_get_status("RECENT"),
            is_system=_is_system_folder(name, flags),
        )
        inventory.append(info)
        logger.debug(
            "Folder %r: messages=%d unseen=%d recent=%d system=%s",
            name,
            info.message_count,
            info.unseen_count,
            info.recent_count,
            info.is_system,
        )

    return inventory


# ---------------------------------------------------------------------------
# Phase 2: Sender / Subject Profile
# ---------------------------------------------------------------------------


def _parse_from_header(raw_from: str) -> tuple[str, str, str]:
    """Parse a From header into (display_name, address, domain).

    Returns ("", raw_from, "") on parse failure so callers always get a usable key.
    """
    if not raw_from:
        return ("", "", "")

    try:
        name, addr = email.utils.parseaddr(raw_from)
        addr = addr.lower().strip()
        if not addr:
            return (name, raw_from.lower().strip(), "")
        parts = addr.rsplit("@", 1)
        domain = parts[1] if len(parts) == 2 else ""
        return (name, addr, domain)
    except Exception:
        return ("", raw_from.lower().strip(), "")


def _extract_subject_pattern(subject: str) -> str | None:
    """Extract a repeating structural pattern from a subject line.

    Recognises:
    - List tags: [tag] at the start, e.g. "[GitHub]", "[Jira]"
    - Prefixes up to the first colon or dash separator, e.g. "Re:", "Fwd:",
      "Weekly digest -", "Invoice #"
    - Common newsletter/notification stems

    Returns the normalised pattern string, or None if no pattern found.
    """
    subject = subject.strip()
    if not subject:
        return None

    # List tags: [foo] at the start (case-preserved for readability)
    list_tag_match = re.match(r"^(\[[^\]]{1,40}\])", subject)
    if list_tag_match:
        return list_tag_match.group(1)

    # Prefixes ending with ": " or " - " or " | "
    prefix_match = re.match(r"^([^:\-|]{2,40})(?::\s|\s[-|]\s)", subject)
    if prefix_match:
        token = prefix_match.group(1).strip()
        # Ignore very generic prefixes like "Re", "Fwd", "FW", single words
        # that are too short to be meaningful patterns
        if len(token) >= 3 and token.lower() not in {"re", "fwd", "fw", "aw"}:
            return token

    return None


def _build_subject_patterns(
    subjects: list[str],
) -> list[dict]:
    """Aggregate subjects into pattern groups.

    Returns a list of dicts sorted by count descending:
        {"pattern": str, "count": int, "examples": list[str]}

    Only patterns with count >= 2 are returned.
    """
    pattern_counts: dict[str, int] = defaultdict(int)
    pattern_examples: dict[str, list[str]] = defaultdict(list)

    for subject in subjects:
        pattern = _extract_subject_pattern(subject)
        if pattern:
            pattern_counts[pattern] += 1
            if len(pattern_examples[pattern]) < _MAX_SAMPLES:
                pattern_examples[pattern].append(subject)

    results = [
        {
            "pattern": pattern,
            "count": count,
            "examples": pattern_examples[pattern],
        }
        for pattern, count in pattern_counts.items()
        if count >= 2
    ]
    results.sort(key=lambda d: d["count"], reverse=True)
    return results


def collect_sender_profile(
    imap_client: ProtonIMAPClient,
    folder: str,
    sample_size: int,
    min_count: int,
) -> tuple[list[SenderCluster], list[dict]]:
    """Fetch headers for the most recent messages and build sender clusters.

    Uses fetch_recent() to fetch the sample_size most recent messages from
    folder. The EmailMessage.sender and .subject fields are used directly —
    no body content is accessed.

    Cluster key: (display_name, full_address) — NOT domain (DECISION-004).

    Args:
        imap_client: Connected ProtonIMAPClient.
        folder: Target folder to analyse.
        sample_size: Maximum number of messages to sample.
        min_count: Minimum message count for a cluster to be included.

    Returns:
        A tuple of (sender_clusters, subject_patterns) where sender_clusters
        are sorted by count descending and filtered to count >= min_count.
    """
    logger.debug("Fetching up to %d messages from %r for sender profile", sample_size, folder)
    messages = imap_client.fetch_recent(folder=folder, count=sample_size)
    logger.debug("Fetched %d messages", len(messages))

    # Accumulate per-sender data
    # Key: (display_name, full_address)
    cluster_counts: dict[tuple[str, str], int] = defaultdict(int)
    cluster_domains: dict[tuple[str, str], str] = {}
    cluster_subjects: dict[tuple[str, str], list[str]] = defaultdict(list)

    all_subjects: list[str] = []

    for msg in messages:
        display_name, address, domain = _parse_from_header(msg.sender)
        if not address:
            continue

        key = (display_name, address)
        cluster_counts[key] += 1
        if key not in cluster_domains:
            cluster_domains[key] = domain
        if len(cluster_subjects[key]) < _MAX_SAMPLES and msg.subject:
            cluster_subjects[key].append(msg.subject)
        if msg.subject:
            all_subjects.append(msg.subject)

    # Build SenderCluster objects, filter by min_count, sort by count desc
    clusters: list[SenderCluster] = []
    for (display_name, address), count in cluster_counts.items():
        if count < min_count:
            continue
        clusters.append(
            SenderCluster(
                address=address,
                display_name=display_name,
                domain=cluster_domains.get((display_name, address), ""),
                count=count,
                sample_subjects=cluster_subjects[(display_name, address)],
            )
        )

    clusters.sort(key=lambda c: c.count, reverse=True)

    # Build subject patterns from the full subject list
    subject_patterns = _build_subject_patterns(all_subjects)

    return clusters, subject_patterns


# ---------------------------------------------------------------------------
# Overlap Detection
# ---------------------------------------------------------------------------


def _normalize_folder_name(name: str) -> str:
    """Normalise a folder name for overlap comparison.

    Replaces hierarchy separators (/, ., \\) with a single space, strips
    leading/trailing whitespace, and lowercases.
    """
    normalised = re.sub(r"[/\\.]+", " ", name).strip().lower()
    return normalised


def detect_folder_overlaps(folders: list[FolderInfo]) -> list[tuple[str, str]]:
    """Detect pairs of non-system folders where one name is a substring of the other.

    Uses case-insensitive substring containment (NOT edit distance — per DECISION-004).
    Hierarchy separators are normalised before comparison.

    Returns a list of (folder_a_name, folder_b_name) tuples. Each pair appears
    once (a < b alphabetically).
    """
    # Filter out system folders
    user_folders = [f for f in folders if not f.is_system]

    # Build list of (original_name, normalised_name) pairs
    folder_pairs = [(f.name, _normalize_folder_name(f.name)) for f in user_folders]

    overlaps: list[tuple[str, str]] = []

    for i in range(len(folder_pairs)):
        name_i, norm_i = folder_pairs[i]
        for j in range(i + 1, len(folder_pairs)):
            name_j, norm_j = folder_pairs[j]
            # Check substring containment in both directions
            # Also detect identical-normalized names (same name differing only in case/separators)
            if norm_i in norm_j or norm_j in norm_i:
                # Avoid reporting a folder as overlapping with itself (same original name)
                if name_i.lower() != name_j.lower() or name_i != name_j:
                    # Only skip if names are truly identical (same original string)
                    if name_i != name_j:
                        # Sort so the shorter/earlier name comes first for stable output
                        pair = (name_i, name_j) if name_i <= name_j else (name_j, name_i)
                        overlaps.append(pair)

    return overlaps


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def build_profile(
    imap_client: ProtonIMAPClient,
    folder: str,
    sample_size: int,
    min_count: int,
    all_folders: bool,
) -> FolderProfile:
    """Build a complete FolderProfile by running all data collection phases.

    Phase 1: Collect folder inventory (all folders, always).
    Phase 2: Collect sender/subject profile for target folder(s).

    Args:
        imap_client: Connected ProtonIMAPClient.
        folder: Primary folder to analyse (used when all_folders=False).
        sample_size: Maximum messages to sample per folder.
        min_count: Minimum sender cluster size to include.
        all_folders: If True, analyse every non-system folder.

    Returns:
        A populated FolderProfile ready for LLM serialization.
    """
    # Phase 1
    logger.debug("Phase 1: collecting folder inventory")
    inventory = collect_folder_inventory(imap_client)

    empty_folders = [
        f.name for f in inventory if f.message_count == 0 and not f.is_system
    ]
    large_folders = [
        f.name
        for f in inventory
        if f.message_count > _LARGE_FOLDER_THRESHOLD and not f.is_system
    ]
    overlap_pairs = detect_folder_overlaps(inventory)

    # Phase 2
    logger.debug("Phase 2: collecting sender/subject profile")

    if all_folders:
        target_folders = [f for f in inventory if not f.is_system and f.message_count > 0]
    else:
        # Find the matching FolderInfo so we can report total_in_scope accurately
        target_folders = [f for f in inventory if f.name == folder]
        if not target_folders:
            # Folder not found in inventory — create a stub so we still proceed
            logger.warning("Folder %r not found in inventory; proceeding with fetch", folder)
            target_folders = []

    all_clusters: list[SenderCluster] = []
    all_subject_patterns: list[dict] = []
    total_in_scope = 0
    actual_sampled = 0

    if all_folders:
        # Merge clusters across all folders
        merged_counts: dict[str, int] = defaultdict(int)
        merged_display: dict[str, str] = {}
        merged_domain: dict[str, str] = {}
        merged_subjects: dict[str, list[str]] = defaultdict(list)
        merged_subject_list: list[str] = []

        for fi in target_folders:
            total_in_scope += fi.message_count
            folder_clusters, folder_patterns = collect_sender_profile(
                imap_client, fi.name, sample_size, min_count=1
            )
            for sc in folder_clusters:
                merged_counts[sc.address] += sc.count
                if sc.address not in merged_display:
                    merged_display[sc.address] = sc.display_name
                    merged_domain[sc.address] = sc.domain
                existing = merged_subjects[sc.address]
                for subj in sc.sample_subjects:
                    if len(existing) < _MAX_SAMPLES:
                        existing.append(subj)
            for p in folder_patterns:
                merged_subject_list.extend(p.get("examples", []))
            actual_sampled += min(fi.message_count, sample_size)

        for address, count in merged_counts.items():
            if count < min_count:
                continue
            all_clusters.append(
                SenderCluster(
                    address=address,
                    display_name=merged_display.get(address, ""),
                    domain=merged_domain.get(address, ""),
                    count=count,
                    sample_subjects=merged_subjects[address],
                )
            )
        all_clusters.sort(key=lambda c: c.count, reverse=True)
        all_subject_patterns = _build_subject_patterns(merged_subject_list)

    else:
        if target_folders:
            fi = target_folders[0]
            total_in_scope = fi.message_count
        else:
            # Folder not in inventory — fetch and let IMAP report the total
            total_in_scope = 0

        all_clusters, all_subject_patterns = collect_sender_profile(
            imap_client, folder, sample_size, min_count
        )
        actual_sampled = min(total_in_scope, sample_size) if total_in_scope else sample_size

    return FolderProfile(
        folder_inventory=inventory,
        sender_clusters=all_clusters,
        subject_patterns=all_subject_patterns,
        overlap_pairs=overlap_pairs,
        empty_folders=empty_folders,
        large_folders=large_folders,
        sample_size=actual_sampled,
        total_in_scope=total_in_scope,
    )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_profile(
    profile: FolderProfile,
    user_context: str | None = None,
) -> str:
    """Serialize a FolderProfile to the JSON payload consumed by the LLM.

    Converts dataclass instances to plain dicts, includes user_context if
    provided, and adds a sampling-window caveat so the LLM is aware of
    coverage limits.

    Args:
        profile: Populated FolderProfile from build_profile().
        user_context: Optional free-text context (e.g. "software engineer, client work").

    Returns:
        A JSON string suitable for embedding in the LLM user message.
    """
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    # Convert FolderInfo list — omit raw flag bytes (not JSON-serializable)
    folder_inventory_out = [
        {
            "name": fi.name,
            "flags": [f.decode("utf-8", errors="replace") for f in fi.flags],
            "message_count": fi.message_count,
            "unseen_count": fi.unseen_count,
            "recent_count": fi.recent_count,
            "is_system": fi.is_system,
        }
        for fi in profile.folder_inventory
    ]

    sender_clusters_out = [
        {
            "address": sc.address,
            "display_name": sc.display_name,
            "domain": sc.domain,
            "count": sc.count,
            "sample_subjects": sc.sample_subjects,
        }
        for sc in profile.sender_clusters
    ]

    payload: dict = {
        "generated_at": generated_at,
        "valid_for_minutes": 30,
        "sampling_note": (
            f"Recommendations are based on the {profile.sample_size} most recent "
            f"messages out of {profile.total_in_scope} total in scope. "
            "Patterns in older messages may not be reflected."
        ),
        "folder_inventory": folder_inventory_out,
        "sender_clusters": sender_clusters_out,
        "subject_patterns": profile.subject_patterns,
        "overlap_pairs": [list(pair) for pair in profile.overlap_pairs],
        "empty_folders": profile.empty_folders,
        "large_folders": profile.large_folders,
        "sample_size": profile.sample_size,
        "total_in_scope": profile.total_in_scope,
    }

    if user_context:
        payload["user_context"] = user_context

    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM Integration
# ---------------------------------------------------------------------------

from protonmail_claude.claude_client import call_claude_json

VALID_RECOMMENDATION_TYPES = {"create_folder", "refile_cluster", "delete_empty_folder", "archive_folder"}
VALID_IMPACTS = {"high", "medium", "low"}


@dataclass
class Recommendation:
    """A single folder structure recommendation from the LLM."""
    rank: int = 0
    type: str = ""
    impact: str = ""
    title: str = ""
    description: str = ""
    affected_count: int = 0
    reason: str = ""
    organize_instruction: str = ""
    cli_command: str = ""


@dataclass
class RecommendResult:
    """Complete result of a folder recommendation analysis."""
    generated_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
    valid_for_minutes: int = 30
    scope: str = ""
    sample_size: int = 0
    total_in_scope: int = 0
    recommendations: list[Recommendation] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, default=str)


def _validate_recommendation(raw: dict) -> Recommendation | None:
    """Validate a raw LLM recommendation dict. Returns None if invalid."""
    rec_type = raw.get("type", "")
    if rec_type not in VALID_RECOMMENDATION_TYPES:
        logger.warning("Dropping recommendation with invalid type: %s", rec_type)
        return None
    impact = raw.get("impact", "medium")
    if impact not in VALID_IMPACTS:
        impact = "medium"
    return Recommendation(
        rank=raw.get("rank", 0),
        type=rec_type,
        impact=impact,
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        affected_count=raw.get("affected_count", 0),
        reason=raw.get("reason", ""),
        organize_instruction=raw.get("organize_instruction", ""),
        cli_command=raw.get("cli_command", ""),
    )


def get_recommendations(
    profile: dict,
    model: str | None = None,
) -> list[Recommendation]:
    """Send a serialized profile to the LLM and return validated recommendations."""
    # Skip LLM call for empty profiles
    if not profile.get("sender_clusters") and not profile.get("overlap_candidates") and profile.get("total_in_scope", 0) == 0:
        return []

    profile_json = json.dumps(profile, indent=2, default=str)
    try:
        raw_recs = call_claude_json(
            user_content=profile_json,
            system_prompt_name="folder_recommend_system",
            max_tokens=2048,
            model=model,
        )
    except Exception as e:
        logger.warning("LLM call failed for folder recommendations: %s", e)
        return []

    if not isinstance(raw_recs, list):
        logger.warning("LLM returned non-list for folder recommendations")
        return []

    validated = []
    for raw in raw_recs:
        rec = _validate_recommendation(raw)
        if rec is not None:
            validated.append(rec)

    validated.sort(key=lambda r: r.rank)
    return validated


def build_sender_clusters(
    emails: list[dict],
    min_count: int = 1,
) -> list[SenderCluster]:
    """Build sender clusters from a list of email dicts with 'sender' and 'subject' keys.

    This is a convenience function for testing — the main pipeline uses collect_sender_profile.
    """
    cluster_counts: dict[str, int] = defaultdict(int)
    cluster_display: dict[str, str] = {}
    cluster_domain: dict[str, str] = {}
    cluster_subjects: dict[str, list[str]] = defaultdict(list)

    for e in emails:
        display_name, address, domain = _parse_from_header(e.get("sender", ""))
        if not address:
            continue
        cluster_counts[address] += 1
        if address not in cluster_display:
            cluster_display[address] = display_name
            cluster_domain[address] = domain
        if len(cluster_subjects[address]) < _MAX_SAMPLES and e.get("subject"):
            cluster_subjects[address].append(e["subject"])

    clusters = []
    for address, count in cluster_counts.items():
        if count < min_count:
            continue
        clusters.append(SenderCluster(
            address=address,
            display_name=cluster_display.get(address, ""),
            domain=cluster_domain.get(address, ""),
            count=count,
            sample_subjects=cluster_subjects[address],
        ))
    clusters.sort(key=lambda c: c.count, reverse=True)
    return clusters


def present_recommendations(
    recommendations: list[Recommendation],
    scope: str = "INBOX",
    total_in_scope: int = 0,
    sample_size: int = 0,
) -> None:
    """Print recommendations as a formatted terminal report grouped by impact."""
    import typer

    typer.echo(f"\nFolder Recommendations — {scope} ({total_in_scope:,} messages, sampled: {sample_size})")
    typer.echo(f"NOTE: Recommendations based on {sample_size} most recent messages only.\n")

    if not recommendations:
        typer.echo("No recommendations generated.")
        return

    for impact_level in ["high", "medium", "low"]:
        recs = [r for r in recommendations if r.impact == impact_level]
        if not recs:
            continue
        typer.echo(f"{impact_level.upper()} IMPACT")
        for rec in recs:
            typer.echo(f"  [{rec.rank}] {rec.title}")
            typer.echo(f"      {rec.description}")
            typer.echo(f"      Reason: {rec.reason}")
            if rec.cli_command:
                typer.echo(f"      -> {rec.cli_command}")
            typer.echo("")

    typer.echo(f"{len(recommendations)} recommendation(s) generated.")


def recommend(
    imap_client: ProtonIMAPClient,
    folder: str = "INBOX",
    sample_size: int = 200,
    min_count: int = 10,
    all_folders: bool = False,
    user_context: str | None = None,
    verbose: bool = False,
    model: str | None = None,
) -> RecommendResult:
    """End-to-end folder recommendation pipeline."""
    import typer

    if verbose:
        typer.echo("[1/3] Collecting folder inventory...")

    profile = build_profile(imap_client, folder, sample_size, min_count, all_folders)

    if verbose:
        typer.echo(f"[2/3] Sampled {profile.sample_size} messages from {profile.total_in_scope} total...")

    profile_json = serialize_profile(profile, user_context=user_context)
    profile_dict = json.loads(profile_json)

    if verbose:
        typer.echo("[3/3] Analyzing with LLM...")

    recs = get_recommendations(profile_dict, model=model)

    return RecommendResult(
        scope=folder if not all_folders else "ALL",
        sample_size=profile.sample_size,
        total_in_scope=profile.total_in_scope,
        recommendations=recs,
    )
