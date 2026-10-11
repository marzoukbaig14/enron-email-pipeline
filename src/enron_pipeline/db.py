"""SQLite write side of ingest, plus the notification update.

Functions do not commit; the caller controls transactions. Does not parse,
find duplicates, or send. Imports: models only.
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from enron_pipeline.models import EmailRecord, ParseFailure

InsertStatus = Literal["inserted", "duplicate_message_id", "duplicate_source_file"]


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection and run PRAGMA foreign_keys = ON.

    SQLite leaves foreign keys off by default, so every connection must set it.
    """
    raise NotImplementedError


def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    """Create the tables and indexes. Default is the repo-root schema.sql."""
    raise NotImplementedError


def insert_email(conn: sqlite3.Connection, record: EmailRecord) -> InsertStatus:
    """Insert the email and its recipients.

    Unique-constraint conflicts are reported as a status, not raised.
    Converts datetimes to ISO 8601 UTC text and bools to 0/1.
    """
    raise NotImplementedError


def insert_parse_failure(conn: sqlite3.Connection, failure: ParseFailure) -> None:
    """Record one failed file in parse_failures."""
    raise NotImplementedError


def mark_notified(conn: sqlite3.Connection, message_id: str, when: datetime) -> None:
    """Set notification_sent = 1 and notification_date on one email."""
    raise NotImplementedError
