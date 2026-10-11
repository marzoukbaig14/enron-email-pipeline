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

Does not parse, find duplicates, or send. Imports: models only.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from enron_pipeline.models import EmailRecord, ParseFailure

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


def mark_notified(conn: sqlite3.Connection, message_id: str, when: datetime) -> None:
    """Set notification_sent = 1 and notification_date on one email."""
    raise NotImplementedError


def _iso(value: datetime) -> str:
    """A timezone-aware datetime as ISO 8601 UTC text ending in "Z"."""
    if value.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware, got naive {value!r}")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
