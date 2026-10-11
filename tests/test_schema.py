"""Tests for schema.sql (D-012).

Every test gets a fresh in-memory SQLite DB built from schema.sql, so tests
cannot leak state into each other.
"""
import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

EMAIL_COLUMNS = {
    "id", "message_id", "date", "date_raw", "from_address", "subject", "body",
    "source_file", "mailbox", "x_from", "x_to", "x_cc", "x_bcc", "x_folder",
    "x_origin", "content_type", "has_attachment", "forwarded_content",
    "quoted_content", "headings", "content_hash", "copy_of", "is_duplicate",
    "duplicate_of", "similarity_score", "notification_sent",
    "notification_date",
}

MANDATORY_COLUMNS = [
    "message_id", "date", "from_address", "subject", "body", "source_file",
    "mailbox",
]


def make_db(foreign_keys: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


@pytest.fixture
def db():
    """Fresh DB with foreign keys ON (how the real db module must run)."""
    conn = make_db(foreign_keys=True)
    yield conn
    conn.close()


@pytest.fixture
def db_fk_off():
    """Fresh DB with foreign keys OFF (SQLite's default)."""
    conn = make_db(foreign_keys=False)
    yield conn
    conn.close()


def insert_email(conn, **overrides) -> int:
    """Insert one email with valid defaults; overrides replace defaults.

    Passing a column as None inserts an explicit NULL.
    """
    row = {
        "message_id": "<1@enron.com>",
        "date": "2001-05-14T16:39:00Z",
        "from_address": "phillip.allen@enron.com",
        "subject": "Hello",
        "body": "Body text",
        "source_file": "maildir/allen-p/inbox/1",
        "mailbox": "allen-p",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT INTO emails ({cols}) VALUES ({marks})", list(row.values())
    )
    return cur.lastrowid


def insert_recipient(conn, email_id, kind="to", address="a@enron.com", position=0):
    conn.execute(
        "INSERT INTO recipients (email_id, address, kind, position) "
        "VALUES (?, ?, ?, ?)",
        (email_id, address, kind, position),
    )


# 1
def test_schema_loads_into_empty_db(db):
    tables = {
        r[0]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"emails", "recipients", "parse_failures"} <= tables


# 2
def test_emails_has_all_columns(db):
    actual = {r[1] for r in db.execute("PRAGMA table_info(emails)")}
    assert EMAIL_COLUMNS <= actual, f"missing: {EMAIL_COLUMNS - actual}"


# 3
def test_duplicate_message_id_rejected(db):
    insert_email(db, source_file="maildir/allen-p/inbox/1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_email(db, source_file="maildir/allen-p/inbox/2")


# 4
def test_duplicate_source_file_rejected(db):
    insert_email(db, message_id="<1@enron.com>")
    with pytest.raises(sqlite3.IntegrityError):
        insert_email(db, message_id="<2@enron.com>")


# 5
@pytest.mark.parametrize("column", MANDATORY_COLUMNS)
def test_null_in_mandatory_column_rejected(db, column):
    with pytest.raises(sqlite3.IntegrityError):
        insert_email(db, **{column: None})


# 6
def test_empty_subject_and_body_accepted(db):
    insert_email(db, subject="", body="")
    row = db.execute("SELECT subject, body FROM emails").fetchone()
    assert row == ("", "")


# 7
def test_recipients_rejects_kind_from(db):
    email_id = insert_email(db)
    with pytest.raises(sqlite3.IntegrityError):
        insert_recipient(db, email_id, kind="from")


# 8
def test_recipient_with_nonexistent_email_id_rejected(db):
    email_id = insert_email(db)
    insert_recipient(db, email_id)  # sanity: a valid parent is accepted
    with pytest.raises(sqlite3.IntegrityError):
        insert_recipient(db, email_id + 999, position=1)


# 9
def test_deleting_email_cascades_to_recipients(db):
    email_id = insert_email(db)
    insert_recipient(db, email_id, kind="to", position=0)
    insert_recipient(db, email_id, kind="cc", position=0)
    assert db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 2

    db.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    assert db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 0


# 10
@pytest.mark.parametrize("column", ["duplicate_of", "copy_of"])
def test_self_reference_to_nonexistent_message_id_rejected(db, column):
    insert_email(db, message_id="<orig@enron.com>")
    # sanity: pointing at a real message_id is accepted
    insert_email(
        db,
        message_id="<2@enron.com>",
        source_file="maildir/allen-p/inbox/2",
        **{column: "<orig@enron.com>"},
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_email(
            db,
            message_id="<3@enron.com>",
            source_file="maildir/allen-p/inbox/3",
            **{column: "<does-not-exist@enron.com>"},
        )


# 11
def test_flag_defaults_are_zero(db):
    insert_email(db)
    row = db.execute(
        "SELECT is_duplicate, notification_sent FROM emails"
    ).fetchone()
    assert row == (0, 0)


# 12
@pytest.mark.parametrize(
    "column", ["has_attachment", "is_duplicate", "notification_sent"]
)
def test_boolean_columns_reject_values_other_than_0_or_1(db, column):
    insert_email(db, **{column: 1})  # sanity: 1 is accepted
    with pytest.raises(sqlite3.IntegrityError):
        insert_email(
            db,
            message_id="<2@enron.com>",
            source_file="maildir/allen-p/inbox/2",
            **{column: 2},
        )


# 13
def test_indexes_exist_on_date_from_address_subject(db):
    index_names = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'emails'"
        )
    ]
    indexed_columns = {
        col[2]
        for name in index_names
        for col in db.execute(f"PRAGMA index_info({name})")
    }
    assert {"date", "from_address", "subject"} <= indexed_columns


# 14
def test_foreign_keys_not_enforced_when_pragma_off(db_fk_off):
    """Documents the SQLite gotcha: FK enforcement is per-connection and OFF
    by default. The db module must run PRAGMA foreign_keys = ON on every
    connection, or the checks in tests 8-10 silently stop applying.
    """
    assert db_fk_off.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    # orphan recipient is silently accepted
    insert_recipient(db_fk_off, email_id=999)
    # dangling duplicate_of is silently accepted
    insert_email(db_fk_off, duplicate_of="<does-not-exist@enron.com>")


# 15
def test_recipients_unique_per_email_kind_position(db):
    email_id = insert_email(db)
    insert_recipient(db, email_id, kind="to", position=0)
    # same position under a different kind, or a different position under
    # the same kind, is fine
    insert_recipient(db, email_id, kind="cc", position=0)
    insert_recipient(db, email_id, kind="to", position=1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_recipient(db, email_id, kind="to", position=0)
    # position is mandatory
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO recipients (email_id, address, kind, position) "
            "VALUES (?, 'x@enron.com', 'bcc', NULL)",
            (email_id,),
        )


# 16
def test_parse_failures_columns_and_not_null(db):
    actual = {r[1] for r in db.execute("PRAGMA table_info(parse_failures)")}
    assert {"id", "source_file", "reason", "failed_at"} <= actual

    valid = {
        "source_file": "maildir/allen-p/inbox/9",
        "reason": "missing To header",
        "failed_at": "2026-10-11T12:00:00Z",
    }
    cols = ", ".join(valid)
    db.execute(
        f"INSERT INTO parse_failures ({cols}) VALUES (?, ?, ?)",
        list(valid.values()),
    )  # sanity: a complete row is accepted
    for column in valid:
        row = {**valid, column: None}
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO parse_failures ({cols}) VALUES (?, ?, ?)",
                list(row.values()),
            )


# 17
@pytest.mark.parametrize(
    "table, column",
    [
        ("emails", "mailbox"),
        ("emails", "content_hash"),
        ("emails", "copy_of"),
        ("emails", "duplicate_of"),
        ("recipients", "email_id"),
        ("recipients", "address"),
    ],
)
def test_remaining_indexes_exist(db, table, column):
    index_names = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ?",
            (table,),
        )
    ]
    indexed_columns = {
        col[2]
        for name in index_names
        for col in db.execute(f"PRAGMA index_info({name})")
    }
    assert column in indexed_columns
