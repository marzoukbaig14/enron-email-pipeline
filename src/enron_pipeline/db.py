"""SQLite write side of ingest, plus the notification update.

Behavior is fixed by D-018: every run rebuilds the database from schema.sql,
foreign keys are always on, datetimes are stored as ISO 8601 UTC text ending
in "Z", bools as 0/1 and enums as their plain string value.

Transactions: connect() turns off Python's implicit transaction handling
(isolation_level=None), so the only transactions are the ones written out in
SQL. insert_email wraps its work in a SAVEPOINT. Inside a transaction the
caller opened with BEGIN, a failed insert undoes only itself and the caller
commits later. With no transaction open, the savepoint is the transaction and
is committed when insert_email returns.

Also holds the functions dedup uses to read emails and to write its results
(D-021 item 13): load_emails, replace_storage_copies and
replace_duplicate_flags. They only move data; deciding what is a duplicate is
dedup's job.

Does not parse, find duplicates, or send. Imports: models only.
"""
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from enron_pipeline.models import DuplicateFlag, EmailRecord, ParseFailure

# src/enron_pipeline/db.py -> parents[2] is the repo root.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"

# emails columns in the order insert_email writes them. `id` is left out: the
# database assigns it.
_EMAIL_COLUMNS = (
    "message_id", "date", "date_raw", "from_address", "subject", "body",
    "source_file", "mailbox", "x_from", "x_to", "x_cc", "x_bcc", "x_folder",
    "x_origin", "content_type", "has_attachment", "forwarded_content",
    "quoted_content", "headings", "content_hash", "copy_of", "is_duplicate",
    "duplicate_of", "similarity_score", "notification_sent",
    "notification_date",
)
_INSERT_EMAIL = (
    f"INSERT INTO emails ({', '.join(_EMAIL_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_EMAIL_COLUMNS))})"
)
_INSERT_RECIPIENT = (
    "INSERT INTO recipients (email_id, address, kind, position) VALUES (?, ?, ?, ?)"
)
_SELECT_EMAILS = f"SELECT id, {', '.join(_EMAIL_COLUMNS)} FROM emails"
_INSERT_FAILURE = (
    "INSERT INTO parse_failures (source_file, reason, failed_at) VALUES (?, ?, ?)"
)


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection and run PRAGMA foreign_keys = ON.

    SQLite leaves foreign keys off by default, and the setting belongs to the
    connection, so every connection must turn it on.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    """Drop every table, then rebuild from schema.sql (default: repo root).

    This is how a pipeline run starts from a clean database (D-018). It
    commits any transaction the caller had open.
    """
    sql = (schema_path or DEFAULT_SCHEMA_PATH).read_text(encoding="utf-8")
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    conn.commit()  # PRAGMA foreign_keys does nothing inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")  # drop in any order
    try:
        for table in reversed(tables):  # children were created after their parents
            conn.execute(f'DROP TABLE "{table}"')  # also drops the table's indexes
        conn.executescript(sql)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def insert_email(conn: sqlite3.Connection, record: EmailRecord) -> int:
    """Insert the email and its recipients as one all-or-nothing unit.

    Returns the new emails.id. Raises sqlite3.IntegrityError on any constraint
    error (duplicate message_id or source_file, bad copy_of, ...) and
    ValueError for a naive datetime. When it raises, no row of this email is
    left behind, including its recipients.
    """
    values = (
        record.message_id,
        _iso(record.date),
        record.date_raw,
        record.from_address,
        record.subject,
        record.body,
        record.source_file,
        record.mailbox,
        record.x_from,
        record.x_to,
        record.x_cc,
        record.x_bcc,
        record.x_folder,
        record.x_origin,
        record.content_type,
        int(record.has_attachment),
        record.forwarded_content,
        record.quoted_content,
        record.headings,
        record.content_hash,
        record.copy_of,
        int(record.is_duplicate),
        record.duplicate_of,
        record.similarity_score,
        int(record.notification_sent),
        _iso(record.notification_date) if record.notification_date else None,
    )
    conn.execute("SAVEPOINT insert_email")
    try:
        email_id = conn.execute(_INSERT_EMAIL, values).lastrowid
        conn.executemany(
            _INSERT_RECIPIENT,
            [
                (email_id, r.address, r.kind.value, r.position)
                for r in record.recipients
            ],
        )
    except BaseException:
        conn.execute("ROLLBACK TO insert_email")
        conn.execute("RELEASE insert_email")
        raise
    conn.execute("RELEASE insert_email")
    return email_id


def insert_parse_failure(conn: sqlite3.Connection, failure: ParseFailure) -> None:
    """Record one failed file in parse_failures. Does not commit."""
    conn.execute(
        _INSERT_FAILURE,
        (failure.source_file, failure.reason, _iso(failure.failed_at)),
    )


def load_emails(
    conn: sqlite3.Connection, canonical_only: bool = False
) -> list[EmailRecord]:
    """Read emails back as EmailRecord, in insert order (by id).

    `canonical_only=True` skips storage copies (rows whose copy_of is set).
    Recipients are NOT loaded: `recipients` is the empty tuple. Dedup does not
    need them and loading them would cost one extra query per email.
    """
    sql = _SELECT_EMAILS
    if canonical_only:
        sql += " WHERE copy_of IS NULL"
    sql += " ORDER BY id"
    names = ("id", *_EMAIL_COLUMNS)
    return [_record_from_row(dict(zip(names, values))) for values in conn.execute(sql)]


def replace_storage_copies(
    conn: sqlite3.Connection,
    content_hashes: Mapping[str, str],
    copy_of: Mapping[str, str],
) -> None:
    """Store dedup stage 1: content_hash per email, and copy_of for the copies.

    Both mappings are keyed by message_id. `copy_of` maps a copy to its
    canonical row's message_id. Every earlier copy_of is cleared first, so the
    result is exactly what is passed in (a rerun leaves nothing stale). A
    message_id that is not in the table raises ValueError, and so does a
    canonical id that is not (foreign key); either way nothing is changed. Does
    not commit.
    """
    with _savepoint(conn, "replace_storage_copies"):
        conn.execute("UPDATE emails SET copy_of = NULL WHERE copy_of IS NOT NULL")
        _update_each(
            conn,
            "UPDATE emails SET content_hash = ? WHERE message_id = ?",
            ((hash_, message_id) for message_id, hash_ in content_hashes.items()),
        )
        _update_each(
            conn,
            "UPDATE emails SET copy_of = ? WHERE message_id = ?",
            ((canonical, message_id) for message_id, canonical in copy_of.items()),
        )


def replace_duplicate_flags(
    conn: sqlite3.Connection, flags: Iterable[DuplicateFlag]
) -> None:
    """Store dedup stage 2: is_duplicate, duplicate_of, similarity_score.

    Every earlier flag is cleared first, so the result is exactly the flags
    passed in. Touches only those three columns (never copy_of or
    notification_*). An unknown duplicate_message_id raises ValueError, an
    unknown original raises sqlite3.IntegrityError; either way nothing is
    changed. Does not commit.
    """
    with _savepoint(conn, "replace_duplicate_flags"):
        conn.execute(
            "UPDATE emails SET is_duplicate = 0, duplicate_of = NULL,"
            " similarity_score = NULL"
            " WHERE is_duplicate = 1 OR duplicate_of IS NOT NULL"
            " OR similarity_score IS NOT NULL"
        )
        _update_each(
            conn,
            "UPDATE emails SET is_duplicate = 1, duplicate_of = ?,"
            " similarity_score = ? WHERE message_id = ?",
            (
                (f.original_message_id, f.similarity_score, f.duplicate_message_id)
                for f in flags
            ),
        )


def mark_notified(conn: sqlite3.Connection, message_id: str, when: datetime) -> None:
    """Set notification_sent = 1 and notification_date on one email."""
    raise NotImplementedError


def _record_from_row(row: dict) -> EmailRecord:
    """One emails row (column name -> value) as an EmailRecord."""
    row["date"] = _parse_iso(row["date"])
    if row["notification_date"] is not None:
        row["notification_date"] = _parse_iso(row["notification_date"])
    for flag_column in ("has_attachment", "is_duplicate", "notification_sent"):
        row[flag_column] = bool(row[flag_column])
    return EmailRecord(**row)


def _parse_iso(text: str) -> datetime:
    """ISO 8601 UTC text ending in "Z" -> timezone-aware datetime.

    Python 3.10's fromisoformat does not accept a trailing "Z", so it is
    swapped for "+00:00" first.
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """Run a block all-or-nothing, the way insert_email does.

    With no transaction open the savepoint is the transaction and commits when
    the block ends. Inside a caller's BEGIN it only nests, so the caller
    decides when to commit.
    """
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def _update_each(conn: sqlite3.Connection, sql: str, rows: Iterable[tuple]) -> None:
    """Run a one-row UPDATE ... WHERE message_id = ? per tuple.

    The message_id is the last value of each tuple. An UPDATE that matches no
    row would be a silent no-op, so it raises ValueError instead.
    """
    for values in rows:
        if conn.execute(sql, values).rowcount != 1:
            raise ValueError(f"no email with message_id {values[-1]!r}")


def _iso(value: datetime) -> str:
    """A timezone-aware datetime as ISO 8601 UTC text ending in "Z"."""
    if value.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware, got naive {value!r}")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
