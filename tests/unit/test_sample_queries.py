"""Tests for sample_queries.sql at the repo root.

Every query must run as written against the real schema.sql, and the
"-- Columns:" line above it must match the columns it really returns, so the
documented output shape cannot drift from the SQL. A small seeded database
also checks the results the comments promise (top 10 limit, date boundaries,
CC-only counting, ordering, and the flagged-duplicates join).
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from enron_pipeline import db, dedup
from enron_pipeline.models import EmailRecord, ParseFailure, Recipient

SQL_FILE = Path(__file__).resolve().parents[2] / "sample_queries.sql"


def load_queries() -> list[dict]:
    """Split the file into queries: {title, columns, sql}.

    A statement ends where sqlite3.complete_statement says so. The comment
    lines collected before it supply the title (the "N. Title" line) and the
    "Columns:" list. A trailing comment block with no statement produces no
    query.
    """
    queries, buffer = [], []
    for line in SQL_FILE.read_text().splitlines():
        buffer.append(line)
        text = "\n".join(buffer)
        if sqlite3.complete_statement(text) and not all(
            not l.strip() or l.lstrip().startswith("--") for l in buffer
        ):
            comments = [l.lstrip("- ").strip() for l in buffer if l.lstrip().startswith("--")]
            columns = next(
                (c[len("Columns:"):].split(",") for c in comments if c.startswith("Columns:")),
                None,
            )
            title = next((c for c in comments if c[:1].isdigit() and ". " in c[:4]), None)
            sql = "\n".join(l for l in buffer if not l.lstrip().startswith("--")).strip()
            queries.append(
                {
                    "title": title,
                    "columns": [c.strip() for c in columns] if columns else None,
                    "comments": "\n".join(comments),
                    "sql": sql,
                }
            )
            buffer = []
    return queries


QUERIES = load_queries()


def utc(year, month, day, hour=12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def record(n, sender="a@enron.com", mailbox="beck-s", date=None, recipients=()):
    return EmailRecord(
        message_id=f"<{n}@thyme>",
        date=date or utc(2001, 5, 14),
        from_address=sender,
        subject=f"Subject {n}",
        body="body",
        source_file=f"maildir/{mailbox}/inbox/{n}.",
        mailbox=mailbox,
        recipients=list(recipients),
    )


@pytest.fixture
def seeded():
    """An empty schema plus helper to run query N."""
    conn = db.connect()
    db.init_schema(conn)
    yield conn
    conn.close()


def run(conn, number):
    query = QUERIES[number - 1]
    cursor = conn.execute(query["sql"])
    return [d[0] for d in cursor.description], cursor.fetchall()


# --- the file itself ---------------------------------------------------------------


def test_there_are_at_least_five_queries():
    assert len(QUERIES) >= 5


def test_the_five_required_queries_are_present_in_order():
    titles = [q["title"].lower() for q in QUERIES[:5]]
    assert "sender" in titles[0] and "10" in titles[0]
    assert "date range" in titles[1]
    assert "cc" in titles[2]
    assert "failure" in titles[3]
    assert "mailbox" in titles[4]


def test_every_query_describes_what_it_shows_and_the_expected_output():
    for query in QUERIES:
        assert "Shows:" in query["comments"], query["title"]
        assert "Expected output:" in query["comments"], query["title"]
        assert query["columns"], query["title"]


def test_there_are_six_queries_and_the_sixth_is_the_duplicate_query():
    assert len(QUERIES) == 6
    assert "duplicate" in QUERIES[5]["title"].lower()


def test_no_todo_or_placeholder_is_left_in_the_file():
    text = SQL_FILE.read_text()
    assert "TODO" not in text
    assert "placeholder" not in text.lower()


def test_the_duplicate_query_returns_the_columns_of_duplicates_report_csv():
    # Same seven columns, in the same order, as the spec's report (D-021 item 11).
    assert QUERIES[5]["columns"] == list(dedup.REPORT_COLUMNS)


@pytest.mark.parametrize("number", range(1, 7))
def test_each_query_runs_on_an_empty_database_with_the_documented_columns(seeded, number):
    columns, rows = run(seeded, number)
    assert columns == QUERIES[number - 1]["columns"]
    assert rows == []


# --- results on seeded data -------------------------------------------------------------


def test_top_senders_is_capped_at_10_busiest_first_ties_by_address(seeded):
    n = 0
    for sender_index in range(12):  # 12 senders; sender 0 sends 12 emails, sender 11 sends 1
        for _ in range(12 - sender_index):
            n += 1
            db.insert_email(seeded, record(n, sender=f"s{sender_index:02d}@enron.com"))
    # Two senders tied at one email each would be ordered by address; make a tie:
    n += 1
    db.insert_email(seeded, record(n, sender="aa-tie@enron.com"))

    _, rows = run(seeded, 1)

    assert len(rows) == 10
    assert rows[0] == ("s00@enron.com", 12)
    counts = [count for _, count in rows]
    assert counts == sorted(counts, reverse=True)
    assert [r[0] for r in rows] == [f"s{i:02d}@enron.com" for i in range(10)]


def test_ties_in_top_senders_are_ordered_by_address(seeded):
    db.insert_email(seeded, record(1, sender="zed@enron.com"))
    db.insert_email(seeded, record(2, sender="amy@enron.com"))
    _, rows = run(seeded, 1)
    assert rows == [("amy@enron.com", 1), ("zed@enron.com", 1)]


def test_date_range_includes_its_start_excludes_its_end_and_is_oldest_first(seeded):
    dates = {
        1: datetime(2001, 4, 30, 23, 59, 59, tzinfo=timezone.utc),  # just before: out
        2: datetime(2001, 5, 1, 0, 0, 0, tzinfo=timezone.utc),      # exactly the start: in
        3: datetime(2001, 5, 20, 8, 0, 0, tzinfo=timezone.utc),
        4: datetime(2001, 5, 31, 23, 59, 59, tzinfo=timezone.utc),  # last second: in
        5: datetime(2001, 6, 1, 0, 0, 0, tzinfo=timezone.utc),      # exactly the end: out
    }
    for n in (4, 3, 5, 1, 2):  # inserted out of order on purpose
        db.insert_email(seeded, record(n, date=dates[n]))

    _, rows = run(seeded, 2)

    assert [r[0] for r in rows] == [
        "2001-05-01T00:00:00Z",
        "2001-05-20T08:00:00Z",
        "2001-05-31T23:59:59Z",
    ]
    assert [r[2] for r in rows] == ["Subject 2", "Subject 3", "Subject 4"]


def test_cc_query_counts_only_cc_and_only_emails_that_have_one(seeded):
    db.insert_email(seeded, record(1, recipients=[Recipient("t@x.com", "to", 0)]))
    db.insert_email(
        seeded,
        record(
            2,
            recipients=[
                Recipient("t@x.com", "to", 0),
                Recipient("c1@x.com", "cc", 0),
                Recipient("b1@x.com", "bcc", 0),  # not counted
            ],
        ),
    )
    db.insert_email(
        seeded,
        record(
            3,
            recipients=[
                Recipient("c1@x.com", "cc", 0),
                Recipient("c2@x.com", "cc", 1),
                Recipient("c3@x.com", "cc", 2),
            ],
        ),
    )

    columns, rows = run(seeded, 3)

    assert columns == ["message_id", "from_address", "subject", "cc_count"]
    assert [(r[0], r[3]) for r in rows] == [("<3@thyme>", 3), ("<2@thyme>", 1)]


def test_failures_by_reason_groups_and_orders_most_common_first(seeded):
    when = utc(2026, 10, 11)
    for n, reason in enumerate(["missing to", "missing to", "missing message_id",
                                "missing to", "invalid date: no timezone"]):
        db.insert_parse_failure(seeded, ParseFailure(f"maildir/x/{n}.", reason, when))

    _, rows = run(seeded, 4)

    assert rows == [
        ("missing to", 3),
        ("invalid date: no timezone", 1),
        ("missing message_id", 1),
    ]


def test_emails_per_mailbox_adds_up_to_the_total(seeded):
    for n, mailbox in enumerate(["beck-s"] * 3 + ["ring-a"] * 2 + ["presto-k"], start=1):
        db.insert_email(seeded, record(n, mailbox=mailbox))

    _, rows = run(seeded, 5)

    assert rows == [("beck-s", 3), ("ring-a", 2), ("presto-k", 1)]
    assert sum(count for _, count in rows) == seeded.execute(
        "SELECT COUNT(*) FROM emails"
    ).fetchone()[0]


def flagged(n, original, score, date, sender="a@enron.com", subject=None):
    """An email that dedup flagged as a duplicate of <original>."""
    return EmailRecord(
        message_id=f"<{n}@thyme>",
        date=date,
        from_address=sender,
        subject=subject or f"Subject {n}",
        body="body",
        source_file=f"maildir/beck-s/inbox/{n}.",
        mailbox="beck-s",
        is_duplicate=True,
        duplicate_of=f"<{original}@thyme>",
        similarity_score=score,
    )


def test_flagged_duplicates_are_listed_with_their_originals_most_similar_first(seeded):
    db.insert_email(seeded, record(1, date=utc(2001, 5, 1)))  # an original
    db.insert_email(seeded, record(10, sender="b@enron.com", date=utc(2001, 5, 2)))  # another
    db.insert_email(seeded, flagged(2, 1, 95.5, utc(2001, 5, 5), subject="Re: Subject 1"))
    db.insert_email(seeded, flagged(3, 1, 100.0, utc(2001, 5, 9)))
    db.insert_email(seeded, flagged(4, 1, 100.0, utc(2001, 5, 7)))  # ties with 3: earlier date first
    db.insert_email(seeded, flagged(11, 10, 91.25, utc(2001, 5, 8), sender="b@enron.com"))
    db.insert_email(seeded, record(20, date=utc(2001, 5, 3)))  # not a duplicate: absent
    db.insert_email(
        seeded,
        EmailRecord(  # a storage copy of email 1 (copy_of set, but not flagged): absent
            message_id="<21@thyme>", date=utc(2001, 5, 1), from_address="a@enron.com",
            subject="Subject 1", body="body", source_file="maildir/ring-a/inbox/21.",
            mailbox="ring-a", copy_of="<1@thyme>",
        ),
    )

    columns, rows = run(seeded, 6)

    assert columns == [
        "duplicate_message_id", "original_message_id", "subject", "from_address",
        "duplicate_date", "original_date", "similarity_score",
    ]
    assert rows == [
        ("<4@thyme>", "<1@thyme>", "Subject 4", "a@enron.com", "2001-05-07T12:00:00Z", "2001-05-01T12:00:00Z", 100.0),
        ("<3@thyme>", "<1@thyme>", "Subject 3", "a@enron.com", "2001-05-09T12:00:00Z", "2001-05-01T12:00:00Z", 100.0),
        ("<2@thyme>", "<1@thyme>", "Re: Subject 1", "a@enron.com", "2001-05-05T12:00:00Z", "2001-05-01T12:00:00Z", 95.5),
        ("<11@thyme>", "<10@thyme>", "Subject 11", "b@enron.com", "2001-05-08T12:00:00Z", "2001-05-02T12:00:00Z", 91.25),
    ]


def test_the_duplicate_query_has_one_row_per_flagged_email(seeded):
    db.insert_email(seeded, record(1, date=utc(2001, 5, 1)))
    for n in (2, 3, 4):
        db.insert_email(seeded, flagged(n, 1, 90.0 + n, utc(2001, 5, n)))
    _, rows = run(seeded, 6)
    assert len(rows) == seeded.execute("SELECT COUNT(*) FROM emails WHERE is_duplicate = 1").fetchone()[0] == 3


def test_the_duplicate_query_agrees_with_what_dedup_really_flags(seeded):
    # Not hand-written flags: let the real dedup stages decide, then read the result back.
    for n in (1, 2, 3):
        db.insert_email(seeded, EmailRecord(
            message_id=f"<{n}@thyme>", date=utc(2001, 5, n), from_address="a@enron.com",
            subject="Q2 numbers" if n != 2 else "Re: Q2 numbers",
            body="Hello team, the Q2 numbers are ready for review on Friday.",
            source_file=f"maildir/beck-s/inbox/{n}.", mailbox="beck-s",
        ))
    dedup.mark_storage_copies(seeded)
    flags = dedup.flag_duplicates(seeded)
    assert len(flags) == 2

    _, rows = run(seeded, 6)

    assert {(r[0], r[1], r[6]) for r in rows} == {
        (f.duplicate_message_id, f.original_message_id, f.similarity_score) for f in flags
    }
    assert {r[2] for r in rows} == {"Re: Q2 numbers", "Q2 numbers"}  # each flagged email's own subject
