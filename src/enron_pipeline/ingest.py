"""Wire discover -> parse_headers -> parse_body -> EmailRecord -> db, per file.

Must survive any bad input: one file never stops the run. A ParseError is
logged with its reason as is. Any other Exception is logged as
"unexpected <ExceptionType>: <message>" so it can be told apart and grouped.
Settings arrive as arguments (no config import).

Imports: models, discover, parse_headers, parse_body, db.
"""
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from enron_pipeline.models import EmailRecord, ParseFailure


@dataclass
class ExtractionStats:
    total_found: int
    parsed: int
    failed: int
    inserted: int
    skipped_existing: int
    field_counts: dict[str, int]  # non-empty count per field

    def completeness(self) -> dict[str, float]:
        """Fraction of parsed emails with a non-empty value, per field."""
        raise NotImplementedError


def process_file(path: Path, maildir: Path) -> EmailRecord:
    """Read and parse one file into an EmailRecord.

    Raises ParseError for known parse failures. Other exceptions propagate to
    ingest_mailboxes, which catches them.
    """
    raise NotImplementedError


def ingest_mailboxes(
    conn: sqlite3.Connection,
    maildir: Path,
    mailboxes: Sequence[str] | None = None,
    error_log: Path | None = None,
) -> ExtractionStats:
    """Ingest every file in the chosen mailboxes.

    Per file: catches ParseError and any other Exception, records a
    ParseFailure in the DB and the error log, and continues. Safe to re-run:
    existing source_files count as skipped_existing.
    """
    raise NotImplementedError


def write_error_log(failures: Sequence[ParseFailure], path: Path) -> None:
    """Write error_log.txt: one line per failure with file path and reason."""
    raise NotImplementedError
