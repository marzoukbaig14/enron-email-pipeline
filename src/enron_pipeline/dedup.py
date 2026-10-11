"""Find storage copies and fuzzy duplicates, flag them in the DB, write the report.

Receives a DB connection and runs its own SQL against schema.sql. Does not
send anything or set notification_* columns. Imports: models only.
"""
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from enron_pipeline.models import DuplicateFlag


@dataclass(frozen=True, slots=True)
class DedupStats:
    groups: int
    flagged: int
    average_group_size: float


def normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/Fw: prefixes so replies group with their original."""
    raise NotImplementedError


def default_similarity(a: str, b: str) -> float:
    """Body similarity on a 0-100 scale (fuzzy library chosen at build time)."""
    raise NotImplementedError


def mark_storage_copies(conn: sqlite3.Connection) -> int:
    """Set copy_of on storage copies found via content_hash.

    Returns the number of rows marked.
    """
    raise NotImplementedError


def flag_duplicates(
    conn: sqlite3.Connection,
    threshold: float = 90.0,
    similarity: Callable[[str, str], float] = default_similarity,
) -> list[DuplicateFlag]:
    """Flag fuzzy duplicates per the spec and return the flags it set.

    Groups on from_address plus normalized subject, compares bodies, treats
    the earliest as the original, and sets is_duplicate, duplicate_of and
    similarity_score on every later one.
    """
    raise NotImplementedError


def summarize(flags: Sequence[DuplicateFlag]) -> DedupStats:
    """Groups found, emails flagged, average group size."""
    raise NotImplementedError


def write_report(flags: Sequence[DuplicateFlag], path: Path) -> None:
    """Write duplicates_report.csv."""
    raise NotImplementedError
