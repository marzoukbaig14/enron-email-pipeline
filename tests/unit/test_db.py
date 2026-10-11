"""Tests for db.py (behavior fixed in D-018, schema in D-012).

Everything runs against an in-memory SQLite database built from the real
schema.sql, so these tests also catch drift between db.py and the schema.

Conventions:
- make_record() builds a minimal valid EmailRecord; pass keyword overrides.
- A stored datetime reads back as ISO 8601 UTC text ending in "Z". Python 3.10
  cannot parse "Z" in fromisoformat, so the round-trip test swaps it for
  "+00:00" first.
- insert_email raises sqlite3.IntegrityError on a constraint error and must
  leave nothing behind (all-or-nothing, D-018 item 1).
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from enron_pipeline import db
from enron_pipeline.models import (
    EmailRecord,
    ParseFailure,
    Recipient,
    RecipientKind,
)

SENT = datetime(2001, 5, 14, 23, 39, 0, tzinfo=timezone.utc)
TABLES = ("emails", "recipients", "parse_failures")


@pytest.fixture
def conn():
    connection = db.connect()
    db.init_schema(connection)
    yield connection
    connection.close()


def make_record(**overrides) -> EmailRecord:
    fields = dict(
        message_id="<1@thyme>",
        date=SENT,
        from_address="phillip.allen@enron.com",
        subject="Q2 numbers",
        body="Hello.",
        source_file="maildir/allen-p/inbox/1.",
        mailbox="allen-p",
    )
    fields.update(overrides)
    return EmailRecord(**fields)


def count(conn, table) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def counts(conn) -> dict:
    return {table: count(conn, table) for table in TABLES}


# --- connect -----------------------------------------------------------------


def test_connect_turns_foreign_keys_on():
    connection = db.connect()
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_foreign_keys_really_enforced(conn):
    # A recipient pointing at a missing email must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recipients (email_id, address, kind, position)"
            " VALUES (999, 'a@x.com', 'to', 0)"
        )


def test_connect_to_a_file_path_also_turns_foreign_keys_on(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.close()


# --- init_schema --------------------------------------------------------------


def test_init_creates_all_tables_and_indexes(conn):
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    assert set(TABLES) <= names
    assert {"idx_emails_date", "idx_emails_from_address", "idx_emails_subject"} <= names


def test_init_rebuilds_so_data_from_a_previous_run_is_gone(conn):
    db.insert_email(
        conn,
        make_record(recipients=[Recipient("tim.belden@enron.com", "to", 0)]),
    )
    db.insert_parse_failure(conn, ParseFailure("maildir/x/1.", "missing to", SENT))
    assert counts(conn) == {"emails": 1, "recipients": 1, "parse_failures": 1}

    db.init_schema(conn)

    assert counts(conn) == {"emails": 0, "recipients": 0, "parse_failures": 0}


def test_init_rebuild_keeps_foreign_keys_on(conn):
    db.init_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_rebuild_works_when_emails_reference_each_other(conn):
    # copy_of is a foreign key from emails to emails; dropping must not choke on it.
    db.insert_email(conn, make_record())
    db.insert_email(
        conn,
        make_record(
            message_id="<2@thyme>",
            source_file="maildir/allen-p/sent/1.",
            copy_of="<1@thyme>",
        ),
    )
    db.init_schema(conn)
    assert count(conn, "emails") == 0


def test_init_removes_tables_the_schema_does_not_know_about(conn):
    conn.execute("CREATE TABLE leftover (x)")
    db.init_schema(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "leftover" not in tables


def test_init_accepts_a_custom_schema_path(tmp_path):
    schema = tmp_path / "tiny.sql"
    schema.write_text("CREATE TABLE tiny (x INTEGER);")
    connection = db.connect()
    db.init_schema(connection, schema)
    assert connection.execute("SELECT COUNT(*) FROM tiny").fetchone()[0] == 0


# --- insert_email -------------------------------------------------------------


def test_insert_stores_every_email_field(conn):
    original = make_record()
    db.insert_email(conn, original)
    record = make_record(
        message_id="<2@thyme>",
        date_raw="Mon, 14 May 2001 16:39:00 -0700 (PDT)",
        source_file="maildir/allen-p/sent/7.",
        subject="Re: Q2 numbers",
        body="Thanks.",
        x_from="Phillip K Allen",
        x_to="Tim Belden",
        x_cc="Mike Grigsby",
        x_bcc="Mike Grigsby",
        x_folder="\\Phillip_Allen_Jan2002_1\\Allen, Phillip K.\\Sent",
        x_origin="Allen-P",
        content_type="text/plain; charset=us-ascii",
        has_attachment=True,
        forwarded_content="---- Forwarded by ----",
        quoted_content="> old text",
        headings="Q2 NUMBERS",
        content_hash="abc123",
        copy_of="<1@thyme>",
        is_duplicate=True,
        duplicate_of="<1@thyme>",
        similarity_score=97.5,
        notification_sent=True,
        notification_date=datetime(2001, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    email_id = db.insert_email(conn, record)

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    assert dict(row) == {
        "id": email_id,
        "message_id": "<2@thyme>",
        "date": "2001-05-14T23:39:00Z",
        "date_raw": "Mon, 14 May 2001 16:39:00 -0700 (PDT)",
        "from_address": "phillip.allen@enron.com",
        "subject": "Re: Q2 numbers",
        "body": "Thanks.",
        "source_file": "maildir/allen-p/sent/7.",
        "mailbox": "allen-p",
        "x_from": "Phillip K Allen",
        "x_to": "Tim Belden",
        "x_cc": "Mike Grigsby",
        "x_bcc": "Mike Grigsby",
        "x_folder": "\\Phillip_Allen_Jan2002_1\\Allen, Phillip K.\\Sent",
        "x_origin": "Allen-P",
        "content_type": "text/plain; charset=us-ascii",
        "has_attachment": 1,
        "forwarded_content": "---- Forwarded by ----",
        "quoted_content": "> old text",
        "headings": "Q2 NUMBERS",
        "content_hash": "abc123",
        "copy_of": "<1@thyme>",
        "is_duplicate": 1,
        "duplicate_of": "<1@thyme>",
        "similarity_score": 97.5,
        "notification_sent": 1,
        "notification_date": "2001-06-01T12:00:00Z",
    }


def test_insert_stores_defaults_and_nulls_for_a_minimal_record(conn):
    db.insert_email(conn, make_record())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM emails").fetchone()
    assert row["has_attachment"] == 0
    assert row["is_duplicate"] == 0
    assert row["notification_sent"] == 0
    for column in ("x_from", "content_type", "quoted_content", "copy_of",
                   "duplicate_of", "similarity_score", "notification_date"):
        assert row[column] is None, column


def test_insert_returns_the_new_row_id(conn):
    first = db.insert_email(conn, make_record())
    second = db.insert_email(
        conn, make_record(message_id="<2@thyme>", source_file="maildir/a/2.")
    )
    assert isinstance(first, int)
    assert second != first
    assert conn.execute(
        "SELECT message_id FROM emails WHERE id = ?", (second,)
    ).fetchone()[0] == "<2@thyme>"


def test_insert_stores_all_recipients_with_kind_and_position(conn):
    recipients = [
        Recipient("tim.belden@enron.com", RecipientKind.TO, 0),
        Recipient("mike.grigsby@enron.com", RecipientKind.TO, 1),
        Recipient("keith.holst@enron.com", "cc", 0),
        Recipient("frank.ermis@enron.com", "bcc", 0),
        # The same address twice in one header is kept; position tells them apart.
        Recipient("tim.belden@enron.com", "cc", 1),
    ]
    email_id = db.insert_email(conn, make_record(recipients=recipients))

    rows = conn.execute(
        "SELECT address, kind, position FROM recipients WHERE email_id = ?"
        " ORDER BY kind, position",
        (email_id,),
    ).fetchall()
    assert rows == [
        ("frank.ermis@enron.com", "bcc", 0),
        ("keith.holst@enron.com", "cc", 0),
        ("tim.belden@enron.com", "cc", 1),
        ("tim.belden@enron.com", "to", 0),
        ("mike.grigsby@enron.com", "to", 1),
    ]


def test_insert_with_no_recipients_stores_the_email_only(conn):
    db.insert_email(conn, make_record())
    assert counts(conn) == {"emails": 1, "recipients": 0, "parse_failures": 0}


def test_datetime_is_stored_as_iso_utc_text_and_round_trips(conn):
    db.insert_email(conn, make_record())
    stored = conn.execute("SELECT date FROM emails").fetchone()[0]
    assert stored == "2001-05-14T23:39:00Z"
    assert datetime.fromisoformat(stored.replace("Z", "+00:00")) == SENT


def test_non_utc_datetime_is_converted_to_utc(conn):
    pacific = timezone(timedelta(hours=-7))
    sent = datetime(2001, 5, 14, 16, 39, 0, tzinfo=pacific)  # same instant as SENT
    db.insert_email(conn, make_record(date=sent))
    assert conn.execute("SELECT date FROM emails").fetchone()[0] == "2001-05-14T23:39:00Z"


def test_naive_datetime_is_rejected_and_nothing_is_stored(conn):
    with pytest.raises(ValueError):
        db.insert_email(conn, make_record(date=datetime(2001, 5, 14, 23, 39)))
    assert count(conn, "emails") == 0


def test_enums_and_bools_are_stored_as_plain_text_and_integers(conn):
    db.insert_email(
        conn,
        make_record(
            has_attachment=True,
            recipients=[Recipient("a@x.com", RecipientKind.CC, 0)],
        ),
    )
    assert conn.execute("SELECT typeof(kind), kind FROM recipients").fetchone() == (
        "text",
        "cc",
    )
    assert conn.execute("SELECT typeof(has_attachment), has_attachment FROM emails").fetchone() == (
        "integer",
        1,
    )


# --- constraint errors raise, and leave nothing behind --------------------------


def test_same_message_id_twice_raises(conn):
    db.insert_email(conn, make_record())
    with pytest.raises(sqlite3.IntegrityError, match="message_id"):
        db.insert_email(conn, make_record(source_file="maildir/allen-p/inbox/2."))
    assert count(conn, "emails") == 1


def test_same_source_file_twice_raises(conn):
    db.insert_email(conn, make_record())
    with pytest.raises(sqlite3.IntegrityError, match="source_file"):
        db.insert_email(conn, make_record(message_id="<2@thyme>"))
    assert count(conn, "emails") == 1


def test_copy_of_must_point_at_an_existing_message(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_email(conn, make_record(copy_of="<nobody@thyme>"))
    assert count(conn, "emails") == 0


def test_failed_insert_leaves_no_partial_rows(conn):
    # The email row and the first recipient insert fine; the second recipient
    # repeats (kind, position) and violates the UNIQUE constraint. The email
    # and the first recipient must be rolled back with it.
    clashing = [
        Recipient("a@x.com", "to", 0),
        Recipient("b@x.com", "to", 0),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_email(conn, make_record(recipients=clashing))
    assert counts(conn) == {"emails": 0, "recipients": 0, "parse_failures": 0}


def test_failed_insert_inside_a_caller_transaction_keeps_the_earlier_work(conn):
    # Ingest wraps a whole run in one transaction. One bad insert must undo
    # only itself, not the good emails inserted before it in that transaction.
    conn.execute("BEGIN")
    db.insert_email(conn, make_record(recipients=[Recipient("a@x.com", "to", 0)]))
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_email(
            conn,
            make_record(
                message_id="<2@thyme>",
                source_file="maildir/allen-p/inbox/2.",
                recipients=[Recipient("a@x.com", "to", 0), Recipient("b@x.com", "to", 0)],
            ),
        )
    conn.commit()
    assert counts(conn) == {"emails": 1, "recipients": 1, "parse_failures": 0}


def test_insert_does_not_commit_inside_a_caller_transaction(conn):
    conn.execute("BEGIN")
    db.insert_email(conn, make_record())
    conn.rollback()
    assert count(conn, "emails") == 0


def test_insert_is_durable_without_a_caller_transaction(tmp_path):
    # With no transaction open, insert_email is its own transaction, so another
    # connection to the same file sees the row straight away.
    path = tmp_path / "test.db"
    writer = db.connect(path)
    db.init_schema(writer)
    db.insert_email(writer, make_record())
    reader = db.connect(path)
    assert reader.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
    writer.close()
    reader.close()


# --- insert_parse_failure ---------------------------------------------------------


def test_insert_parse_failure_stores_a_row(conn):
    failure = ParseFailure(
        source_file="maildir/beck-s/inbox/9.",
        reason="missing to",
        failed_at=datetime(2026, 10, 11, 17, 30, 5, tzinfo=timezone.utc),
    )
    db.insert_parse_failure(conn, failure)
    assert conn.execute(
        "SELECT source_file, reason, failed_at FROM parse_failures"
    ).fetchall() == [("maildir/beck-s/inbox/9.", "missing to", "2026-10-11T17:30:05Z")]


def test_the_same_file_can_fail_more_than_once(conn):
    # parse_failures has no UNIQUE on source_file: a failure log, not a catalogue.
    failure = ParseFailure("maildir/beck-s/inbox/9.", "missing to", SENT)
    db.insert_parse_failure(conn, failure)
    db.insert_parse_failure(conn, failure)
    assert count(conn, "parse_failures") == 2


# --- mark_notified (D-022 item 8) -----------------------------------------------------


def test_mark_notified_sets_the_flag_and_the_send_time(conn):
    db.insert_email(conn, make_record())
    when = datetime(2026, 10, 11, 18, 30, 5, tzinfo=timezone.utc)

    db.mark_notified(conn, "<1@thyme>", when)

    assert conn.execute(
        "SELECT notification_sent, notification_date FROM emails"
    ).fetchall() == [(1, "2026-10-11T18:30:05Z")]


def test_mark_notified_changes_only_the_named_email(conn):
    db.insert_email(conn, make_record())
    db.insert_email(
        conn, make_record(message_id="<2@thyme>", source_file="maildir/allen-p/inbox/2.")
    )
    db.mark_notified(conn, "<2@thyme>", SENT)
    assert conn.execute(
        "SELECT message_id, notification_sent, notification_date FROM emails ORDER BY id"
    ).fetchall() == [("<1@thyme>", 0, None), ("<2@thyme>", 1, "2001-05-14T23:39:00Z")]


def test_mark_notified_converts_to_utc(conn):
    db.insert_email(conn, make_record())
    pacific = timezone(timedelta(hours=-7))
    db.mark_notified(conn, "<1@thyme>", datetime(2026, 10, 11, 11, 30, tzinfo=pacific))
    assert conn.execute("SELECT notification_date FROM emails").fetchone()[0] == (
        "2026-10-11T18:30:00Z"
    )


def test_mark_notified_for_an_unknown_message_raises(conn):
    with pytest.raises(ValueError, match="nobody"):
        db.mark_notified(conn, "<nobody@thyme>", SENT)


def test_mark_notified_rejects_a_naive_datetime(conn):
    db.insert_email(conn, make_record())
    with pytest.raises(ValueError):
        db.mark_notified(conn, "<1@thyme>", datetime(2026, 10, 11, 18, 30))
    assert conn.execute("SELECT notification_sent FROM emails").fetchone()[0] == 0


def test_mark_notified_does_not_commit_inside_a_caller_transaction(conn):
    db.insert_email(conn, make_record())
    conn.execute("BEGIN")
    db.mark_notified(conn, "<1@thyme>", SENT)
    conn.rollback()
    assert conn.execute("SELECT notification_sent FROM emails").fetchone()[0] == 0
