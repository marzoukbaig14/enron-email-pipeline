"""Find storage copies and fuzzy duplicates, flag them in the DB, write the report.

Behavior is fixed by D-021. Two stages:

  Stage 1, mark_storage_copies: one email filed in several folders or mailboxes
  shows up as several rows with the same content. Those rows are ONE logical
  email, not re-sends. They are grouped by a content hash; the row with the
  smallest source_file is canonical and the others get copy_of.

  Stage 2, flag_duplicates: the spec's rule, on canonical rows only. Same
  from_address, same subject once Re:/Fwd: prefixes are stripped, and main
  bodies at least 90% similar. The earliest is the original; later ones are
  flagged against it.

Receives a DB connection and reads and writes through the db module (D-021
item 13). Does not send anything or set notification_* columns. Imports:
models and db. The fuzzy-matching library (rapidfuzz) is used only here.
"""
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from enron_pipeline import db
from enron_pipeline.models import DuplicateFlag, EmailRecord

REPORT_COLUMNS = (
    "duplicate_message_id",
    "original_message_id",
    "subject",
    "from_address",
    "duplicate_date",
    "original_date",
    "similarity_score",
)

# Separates the fields that go into the content hash. U+001F cannot appear in a
# body (the body parser strips control characters, D-020), so two different
# field splits can never produce the same joined text.
_HASH_SEPARATOR = "\x1f"

# One or more leading "Re:", "Fw:" or "Fwd:" (any case, spaces allowed around
# the colon). "Regarding X" is untouched: after "re" comes "g", not a colon.
_SUBJECT_PREFIXES = re.compile(r"^(?:\s*(?:re|fwd?)\s*:)+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StorageCopyStats:
    """Stage 1 result: how many groups of copies, and how many rows collapsed."""

    groups: int  # logical emails that exist in more than one row
    rows: int  # rows marked copy_of (the canonical row of each group is not counted)


@dataclass(frozen=True, slots=True)
class DedupStats:
    """What dedup found, for the console and output/dedup_stats.json."""

    storage_copy_groups: int
    storage_copy_rows: int
    duplicate_groups: int  # an original plus its flagged duplicates
    emails_flagged: int
    average_group_size: float  # the original counts toward its group's size
    largest_group_size: int

    def to_dict(self) -> dict:
        """The dedup_stats.json shape."""
        return {
            "storage_copy_groups": self.storage_copy_groups,
            "storage_copy_rows_collapsed": self.storage_copy_rows,
            "duplicate_groups": self.duplicate_groups,
            "emails_flagged": self.emails_flagged,
            "average_group_size": round(self.average_group_size, 2),
            "largest_group_size": self.largest_group_size,
        }


def normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fw:/Fwd: prefixes, collapse whitespace, lowercase.

    "RE: FW: re:  Q2   numbers" and "Q2 numbers" both give "q2 numbers", so a
    reply and its original land in the same block. Only LEADING prefixes go:
    "Update re: budget" is unchanged.
    """
    without_prefixes = _SUBJECT_PREFIXES.sub("", subject)
    return " ".join(without_prefixes.split()).lower()


def default_similarity(a: str, b: str) -> float:
    """Similarity on a 0-100 scale: rapidfuzz.fuzz.ratio.

    It is 100 * (1 - edit distance / total length), where an edit is one
    character inserted or deleted. Two identical strings score 100.
    """
    return fuzz.ratio(a, b)


def content_hash_for(email: EmailRecord) -> str:
    """SHA-256 (hex) of the fields that make two rows "the same email".

    from_address, date (UTC ISO), subject as written, body, quoted_content and
    forwarded_content. Not in it: message_id, source_file, mailbox and the
    X-headers, which are what differ between storage copies. A missing
    optional text counts the same as an empty one.
    """
    parts = (
        email.from_address,
        email.date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        email.subject,
        email.body,
        email.quoted_content or "",
        email.forwarded_content or "",
    )
    return hashlib.sha256(_HASH_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


def mark_storage_copies(conn: sqlite3.Connection) -> StorageCopyStats:
    """Stage 1: compute every row's content_hash and mark the storage copies.

    Rows sharing a hash are one logical email. The one with the smallest
    source_file stays canonical (copy_of NULL); every other row gets copy_of =
    the canonical row's message_id. Earlier marks are replaced, so a rerun
    gives the same result.
    """
    hashes: dict[str, str] = {}
    by_hash: dict[str, list[EmailRecord]] = defaultdict(list)
    for email in db.load_emails(conn):
        digest = content_hash_for(email)
        hashes[email.message_id] = digest
        by_hash[digest].append(email)

    copy_of: dict[str, str] = {}
    groups = 0
    for members in by_hash.values():
        if len(members) < 2:
            continue
        groups += 1
        canonical, *copies = sorted(members, key=lambda e: e.source_file)
        for copy in copies:
            copy_of[copy.message_id] = canonical.message_id

    db.replace_storage_copies(conn, hashes, copy_of)
    return StorageCopyStats(groups=groups, rows=len(copy_of))


def flag_duplicates(
    conn: sqlite3.Connection,
    threshold: float = 90.0,
    similarity: Callable[[str, str], float] = default_similarity,
) -> list[DuplicateFlag]:
    """Stage 2: flag fuzzy duplicates among canonical rows and return the flags.

    Rows are blocked on (from_address, normalized subject); only rows in the
    same block are ever compared. Within a block, sorted by (date,
    source_file): the earliest unassigned row is an original, and every later
    unassigned row scoring >= threshold against it is flagged. The next
    unassigned row then becomes a new original, and so on. A row is compared
    only to originals, never to a duplicate, so there is no chaining and every
    score is against the original.

    Flags are written to the database (replacing any earlier ones) and returned
    in report order. `similarity` is a parameter so the metric can be swapped
    without touching anything else.
    """
    blocks: dict[tuple[str, str], list[EmailRecord]] = defaultdict(list)
    for email in db.load_emails(conn, canonical_only=True):
        blocks[(email.from_address, normalize_subject(email.subject))].append(email)

    flags: list[DuplicateFlag] = []
    for block in blocks.values():
        if len(block) > 1:
            block.sort(key=lambda e: (e.date, e.source_file))
            flags.extend(_flag_block(block, threshold, similarity))

    flags.sort(key=_report_order)
    db.replace_duplicate_flags(conn, flags)
    return flags


def summarize(
    flags: Sequence[DuplicateFlag], storage: StorageCopyStats | None = None
) -> DedupStats:
    """Group and flag counts. `storage` is what mark_storage_copies returned."""
    storage = storage or StorageCopyStats(groups=0, rows=0)
    # Each original with n flagged duplicates is a group of n + 1 emails.
    group_sizes = [n + 1 for n in Counter(f.original_message_id for f in flags).values()]
    return DedupStats(
        storage_copy_groups=storage.groups,
        storage_copy_rows=storage.rows,
        duplicate_groups=len(group_sizes),
        emails_flagged=len(flags),
        average_group_size=sum(group_sizes) / len(group_sizes) if group_sizes else 0.0,
        largest_group_size=max(group_sizes, default=0),
    )


def write_report(flags: Sequence[DuplicateFlag], path: Path) -> None:
    """Write duplicates_report.csv: one row per flagged email.

    Sorted by original (its date, then message_id, so a group stays together)
    and then by duplicate date. Dates are ISO 8601 UTC like the database;
    scores have two decimals.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for flag in sorted(flags, key=_report_order):
            writer.writerow(
                [
                    flag.duplicate_message_id,
                    flag.original_message_id,
                    flag.subject,
                    flag.from_address,
                    _iso(flag.duplicate_date),
                    _iso(flag.original_date),
                    f"{flag.similarity_score:.2f}",
                ]
            )


def write_stats(stats: DedupStats, path: Path) -> None:
    """Write output/dedup_stats.json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats.to_dict(), indent=2) + "\n", encoding="utf-8")


# --- Helpers ---------------------------------------------------------------------------


def _flag_block(
    block: list[EmailRecord],
    threshold: float,
    similarity: Callable[[str, str], float],
) -> list[DuplicateFlag]:
    """Apply the no-chaining grouping to one block (already sorted)."""
    flags: list[DuplicateFlag] = []
    remaining = block
    while remaining:
        original, *candidates = remaining
        remaining = []  # candidates that did not match this original
        for candidate in candidates:
            score = similarity(*_compared_texts(original, candidate))
            if score >= threshold:
                flags.append(
                    DuplicateFlag(
                        duplicate_message_id=candidate.message_id,
                        original_message_id=original.message_id,
                        subject=candidate.subject,
                        from_address=candidate.from_address,
                        duplicate_date=candidate.date,
                        original_date=original.date,
                        similarity_score=float(score),
                    )
                )
            else:
                remaining.append(candidate)
    return flags


def _compared_texts(a: EmailRecord, b: EmailRecord) -> tuple[str, str]:
    """The two texts to score: main bodies with whitespace collapsed.

    If BOTH main bodies are empty (a bare forward, say), compare body +
    forwarded_content + quoted_content instead. Otherwise two content-free
    forwards would be "100% similar" just because both are empty.
    """
    main_a, main_b = _collapse(a.body), _collapse(b.body)
    if main_a or main_b:
        return main_a, main_b
    return _full_text(a), _full_text(b)


def _full_text(email: EmailRecord) -> str:
    return _collapse(
        " ".join(part for part in (email.body, email.forwarded_content, email.quoted_content) if part)
    )


def _collapse(text: str) -> str:
    """Any run of whitespace (newlines, tabs, repeated spaces) becomes one space."""
    return " ".join(text.split())


def _report_order(flag: DuplicateFlag) -> tuple:
    return (
        flag.original_date,
        flag.original_message_id,
        flag.duplicate_date,
        flag.duplicate_message_id,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
