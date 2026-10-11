"""Tests for the db functions that dedup uses (D-021 item 13).

load_emails reads rows back as EmailRecord. replace_storage_copies and
replace_duplicate_flags WRITE dedup's results. "Replace" matters: each one
first clears what an earlier run left, so running dedup twice (or at a
different threshold) never leaves stale marks behind.

Like the other db functions they do not commit: a caller that opened BEGIN can
roll the whole thing back. A failed call undoes only itself.

Lives in its own file so it does not collide with test_db.py.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from enron_pipeline import db
from enron_pipeline.models import DuplicateFlag, EmailRecord, Recipient

T0 = datetime(2001, 5, 14, 23, 39, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    connection = db.connect()
    db.init_schema(connection)
    yield connection
    connection.close()


def make(n: int, **overrides) -> EmailRecord:
    fields = dict(
        message_id=f"<{n}@thyme>",
        date=T0 + timedelta(minutes=n),
        from_address="phillip.allen@enron.com",
        subject=f"Subject {n}",
        body=f"Body {n}",
        source_file=f"maildir/allen-p/inbox/{n}.",
        mailbox="allen-p",
    )
    fields.update(overrides)
    return EmailRecord(**fields)


def seed(conn, *records: EmailRecord) -> None:
    for record in records:
        db.insert_email(conn, record)


def row(conn, message_id: str) -> dict:
    cursor = conn.execute("SELECT * FROM emails WHERE message_id = ?", (message_id,))
    names = [column[0] for column in cursor.description]
    return dict(zip(names, cursor.fetchone()))


def flag(duplicate: str, original: str, score: float = 95.5) -> DuplicateFlag:
    return DuplicateFlag(
        duplicate_message_id=duplicate,
        original_message_id=original,
        subject="s",
        from_address="a@enron.com",
        duplicate_date=T0 + timedelta(days=1),
        original_date=T0,
        similarity_score=score,
    )


# --- load_emails -------------------------------------------------------------------


def test_load_emails_returns_every_row_as_email_record_in_insert_order(conn):
    seed(conn, make(3), make(1), make(2))
    loaded = db.load_emails(conn)
    assert [r.message_id for r in loaded] == ["<3@thyme>", "<1@thyme>", "<2@thyme>"]
    assert all(isinstance(r, EmailRecord) for r in loaded)


def test_load_emails_round_trips_the_fields_with_real_types(conn):
    original = make(
        1,
        date_raw="Mon, 14 May 2001 16:39:00 -0700 (PDT)",
        x_from="Phillip K Allen",
        content_type="text/plain",
        has_attachment=True,
        forwarded_content="fwd text",
        quoted_content="> quoted",
        headings="AGENDA",
        content_hash="abc",
        recipients=[Recipient("tim@enron.com", "to", 0)],
    )
    seed(conn, original)
    (loaded,) = db.load_emails(conn)
    assert loaded.date == original.date
    assert loaded.date.utcoffset() == timedelta(0)  # timezone-aware UTC
    assert loaded.has_attachment is True
    assert loaded.is_duplicate is False and loaded.notification_sent is False
    assert loaded.copy_of is None and loaded.duplicate_of is None
    assert loaded.id is not None
    for name in (
        "message_id", "from_address", "subject", "body", "source_file", "mailbox",
        "date_raw", "x_from", "content_type", "forwarded_content", "quoted_content",
        "headings", "content_hash", "x_to", "x_cc",
    ):
        assert getattr(loaded, name) == getattr(original, name), name


def test_load_emails_does_not_load_recipients(conn):
    seed(conn, make(1, recipients=[Recipient("tim@enron.com", "to", 0)]))
    assert db.load_emails(conn)[0].recipients == ()


def test_load_emails_reads_dedup_columns_back(conn):
    seed(conn, make(1), make(2))
    db.replace_storage_copies(conn, {"<1@thyme>": "h1", "<2@thyme>": "h1"}, {"<2@thyme>": "<1@thyme>"})
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>", 97.25)])
    second = db.load_emails(conn)[1]
    assert second.copy_of == "<1@thyme>"
    assert second.is_duplicate is True
    assert second.duplicate_of == "<1@thyme>"
    assert second.similarity_score == 97.25
    assert second.content_hash == "h1"


def test_load_emails_canonical_only_skips_storage_copies(conn):
    seed(conn, make(1), make(2), make(3))
    db.replace_storage_copies(conn, {}, {"<2@thyme>": "<1@thyme>"})
    assert [r.message_id for r in db.load_emails(conn, canonical_only=True)] == [
        "<1@thyme>",
        "<3@thyme>",
    ]
    assert len(db.load_emails(conn)) == 3


def test_load_emails_on_an_empty_database(conn):
    assert db.load_emails(conn) == []


# --- replace_storage_copies --------------------------------------------------------


def test_replace_storage_copies_sets_hashes_and_copy_of(conn):
    seed(conn, make(1), make(2), make(3))
    db.replace_storage_copies(
        conn,
        {"<1@thyme>": "h-a", "<2@thyme>": "h-a", "<3@thyme>": "h-b"},
        {"<2@thyme>": "<1@thyme>"},
    )
    assert row(conn, "<1@thyme>")["content_hash"] == "h-a"
    assert row(conn, "<3@thyme>")["content_hash"] == "h-b"
    assert row(conn, "<1@thyme>")["copy_of"] is None
    assert row(conn, "<2@thyme>")["copy_of"] == "<1@thyme>"
    assert row(conn, "<3@thyme>")["copy_of"] is None


def test_replace_storage_copies_clears_marks_from_an_earlier_call(conn):
    seed(conn, make(1), make(2), make(3))
    db.replace_storage_copies(conn, {}, {"<2@thyme>": "<1@thyme>"})
    db.replace_storage_copies(conn, {}, {"<3@thyme>": "<1@thyme>"})
    assert row(conn, "<2@thyme>")["copy_of"] is None
    assert row(conn, "<3@thyme>")["copy_of"] == "<1@thyme>"
    db.replace_storage_copies(conn, {}, {})
    assert row(conn, "<3@thyme>")["copy_of"] is None


def test_replace_storage_copies_leaves_other_columns_alone(conn):
    seed(conn, make(1, notification_sent=True, notification_date=T0), make(2))
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>")])
    before = row(conn, "<1@thyme>"), row(conn, "<2@thyme>")
    db.replace_storage_copies(conn, {"<1@thyme>": "h"}, {})
    after = row(conn, "<1@thyme>"), row(conn, "<2@thyme>")
    changed = {k for b, a in zip(before, after) for k in b if b[k] != a[k]}
    assert changed == {"content_hash"}


def test_replace_storage_copies_unknown_canonical_raises_and_changes_nothing(conn):
    seed(conn, make(1), make(2))
    db.replace_storage_copies(conn, {"<1@thyme>": "keep"}, {"<2@thyme>": "<1@thyme>"})
    with pytest.raises(sqlite3.IntegrityError):
        db.replace_storage_copies(
            conn, {"<1@thyme>": "new"}, {"<2@thyme>": "<nobody@thyme>"}
        )
    assert row(conn, "<1@thyme>")["content_hash"] == "keep"
    assert row(conn, "<2@thyme>")["copy_of"] == "<1@thyme>"


def test_replace_storage_copies_does_not_commit_inside_a_caller_transaction(conn):
    seed(conn, make(1), make(2))
    conn.execute("BEGIN")
    db.replace_storage_copies(conn, {"<1@thyme>": "h"}, {"<2@thyme>": "<1@thyme>"})
    conn.execute("ROLLBACK")
    assert row(conn, "<2@thyme>")["copy_of"] is None
    assert row(conn, "<1@thyme>")["content_hash"] is None


# --- replace_duplicate_flags -------------------------------------------------------


def test_replace_duplicate_flags_sets_the_three_columns(conn):
    seed(conn, make(1), make(2), make(3))
    db.replace_duplicate_flags(
        conn, [flag("<2@thyme>", "<1@thyme>", 91.5), flag("<3@thyme>", "<1@thyme>", 100.0)]
    )
    second = row(conn, "<2@thyme>")
    assert (second["is_duplicate"], second["duplicate_of"], second["similarity_score"]) == (
        1, "<1@thyme>", 91.5,
    )
    assert row(conn, "<3@thyme>")["similarity_score"] == 100.0
    original = row(conn, "<1@thyme>")
    assert (original["is_duplicate"], original["duplicate_of"], original["similarity_score"]) == (
        0, None, None,
    )


def test_replace_duplicate_flags_clears_flags_from_an_earlier_call(conn):
    seed(conn, make(1), make(2), make(3))
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>")])
    db.replace_duplicate_flags(conn, [flag("<3@thyme>", "<1@thyme>")])
    cleared = row(conn, "<2@thyme>")
    assert (cleared["is_duplicate"], cleared["duplicate_of"], cleared["similarity_score"]) == (
        0, None, None,
    )
    assert row(conn, "<3@thyme>")["is_duplicate"] == 1
    db.replace_duplicate_flags(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM emails WHERE is_duplicate = 1").fetchone()[0] == 0


def test_replace_duplicate_flags_never_touches_notification_or_copy_columns(conn):
    seed(conn, make(1), make(2, notification_sent=True, notification_date=T0), make(3))
    db.replace_storage_copies(conn, {}, {"<3@thyme>": "<1@thyme>"})
    before = [row(conn, f"<{n}@thyme>") for n in (1, 2, 3)]
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>")])
    after = [row(conn, f"<{n}@thyme>") for n in (1, 2, 3)]
    changed = {k for b, a in zip(before, after) for k in b if b[k] != a[k]}
    assert changed == {"is_duplicate", "duplicate_of", "similarity_score"}
    assert after[1]["notification_sent"] == 1 and after[2]["copy_of"] == "<1@thyme>"


def test_replace_duplicate_flags_unknown_original_raises_and_changes_nothing(conn):
    seed(conn, make(1), make(2))
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>")])
    with pytest.raises(sqlite3.IntegrityError):
        db.replace_duplicate_flags(conn, [flag("<1@thyme>", "<nobody@thyme>")])
    assert row(conn, "<2@thyme>")["is_duplicate"] == 1
    assert row(conn, "<1@thyme>")["is_duplicate"] == 0


def test_replace_duplicate_flags_does_not_commit_inside_a_caller_transaction(conn):
    seed(conn, make(1), make(2))
    conn.execute("BEGIN")
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>")])
    conn.execute("ROLLBACK")
    assert row(conn, "<2@thyme>")["is_duplicate"] == 0


# --- unknown message_ids are an error, not a silent no-op ---------------------------


def test_replace_storage_copies_unknown_message_id_raises_and_changes_nothing(conn):
    seed(conn, make(1))
    with pytest.raises(ValueError, match="nobody"):
        db.replace_storage_copies(conn, {"<1@thyme>": "h", "<nobody@thyme>": "h"}, {})
    assert row(conn, "<1@thyme>")["content_hash"] is None
    with pytest.raises(ValueError, match="nobody"):
        db.replace_storage_copies(conn, {}, {"<nobody@thyme>": "<1@thyme>"})


def test_replace_duplicate_flags_unknown_duplicate_id_raises_and_changes_nothing(conn):
    seed(conn, make(1), make(2))
    db.replace_duplicate_flags(conn, [flag("<2@thyme>", "<1@thyme>")])
    with pytest.raises(ValueError, match="nobody"):
        db.replace_duplicate_flags(conn, [flag("<nobody@thyme>", "<1@thyme>")])
    assert row(conn, "<2@thyme>")["is_duplicate"] == 1
