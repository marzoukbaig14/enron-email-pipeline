"""Tests for ingest.py (behavior fixed in D-018).

Each test builds a fake maildir in tmp_path and ingests it into a temporary
SQLite file, using a fake body parser so parse_body is not needed. The real
discover, parse_headers and db modules are used: ingest is the wiring between
them, so that wiring is what these tests check.

The fake body parser returns the raw body unchanged and fills the other
fields from markers in the body text:
    starts with "FWD"  -> forwarded_content = "fwd"
    starts with ">"    -> quoted_content = "q"
    contains "HEAD"    -> headings = "h"
    contains "ATTACH"  -> has_attachment = True
    contains "EXPLODE" -> raises RuntimeError("boom")
That lets a test choose which optional fields an email ends up with.

Failure reasons are checked exactly: "missing to" (a ParseError reason passed
through as is) and "unexpected <ExceptionType>: <message>" for anything else.
"""
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from enron_pipeline import db
from enron_pipeline.ingest import (
    STATS_FIELDS,
    ExtractionStats,
    format_stats,
    ingest_mailboxes,
    process_file,
    write_error_log,
    write_stats,
)
from enron_pipeline.models import ParseFailure, Recipient

DEFAULT_HEADERS = {
    "Message-ID": "<1@thyme>",
    "Date": "Mon, 14 May 2001 16:39:00 -0700 (PDT)",
    "From": "phillip.allen@enron.com",
    "To": "tim.belden@enron.com",
    "Subject": "Q2 numbers",
}

# Section 3 of the spec: 7 mandatory + 13 optional fields.
SPEC_FIELDS = {
    "message_id", "date", "from_address", "to_addresses", "subject", "body",
    "source_file", "cc_addresses", "bcc_addresses", "x_from", "x_to", "x_cc",
    "x_bcc", "x_folder", "x_origin", "content_type", "has_attachment",
    "forwarded_content", "quoted_content", "headings",
}


@dataclass
class FakeParsed:
    body: str
    forwarded_content: str | None = None
    quoted_content: str | None = None
    headings: str | None = None
    has_attachment: bool = False


class FakeBodyParser:
    """Records every call so tests can check the arguments ingest passes."""

    def __init__(self):
        self.calls = []

    def __call__(self, raw_body, content_type, content_transfer_encoding):
        self.calls.append((raw_body, content_type, content_transfer_encoding))
        if "EXPLODE" in raw_body:
            raise RuntimeError("boom")
        return FakeParsed(
            body=raw_body,
            forwarded_content="fwd" if raw_body.startswith("FWD") else None,
            quoted_content="q" if raw_body.startswith(">") else None,
            headings="h" if "HEAD" in raw_body else None,
            has_attachment="ATTACH" in raw_body,
        )


def build(overrides=None, extra=(), body="Hello.\n") -> bytes:
    """One raw message. overrides replace default headers; None removes one."""
    headers = {**DEFAULT_HEADERS, **(overrides or {})}
    items = [(k, v) for k, v in headers.items() if v is not None] + list(extra)
    text = "".join(f"{name}: {value}\n" for name, value in items)
    return (text + "\n" + body).encode()


def write_email(maildir, relative, raw: bytes):
    path = maildir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


@pytest.fixture
def maildir(tmp_path):
    root = tmp_path / "maildir"
    root.mkdir()
    return root


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def parser():
    return FakeBodyParser()


def run(conn, maildir, parser, mailboxes=("beck-s",), error_log=None):
    return ingest_mailboxes(conn, maildir, mailboxes, parser, error_log=error_log)


def rows(conn, sql):
    return conn.execute(sql).fetchall()


# --- one good file ----------------------------------------------------------------


def test_good_file_becomes_one_row_with_mailbox_and_source_file(conn, maildir, parser):
    write_email(maildir, "beck-s/inbox/1.", build(body="Hello there.\n"))

    stats = run(conn, maildir, parser)

    assert (stats.total_found, stats.parsed, stats.failed) == (1, 1, 0)
    assert rows(
        conn, "SELECT message_id, mailbox, source_file, from_address, subject, body, date FROM emails"
    ) == [
        (
            "<1@thyme>",
            "beck-s",
            "maildir/beck-s/inbox/1.",
            "phillip.allen@enron.com",
            "Q2 numbers",
            "Hello there.\n",
            "2001-05-14T23:39:00Z",
        )
    ]
    assert rows(conn, "SELECT address, kind, position FROM recipients") == [
        ("tim.belden@enron.com", "to", 0)
    ]


def test_body_parser_gets_the_raw_body_and_the_two_content_headers(conn, maildir, parser):
    # Same arguments, in the same order, as parse_body(raw_body, content_type,
    # content_transfer_encoding) in module-layout.md (D-019).
    write_email(
        maildir,
        "beck-s/inbox/1.",
        build(
            extra=[
                ("Content-Type", "text/plain; charset=us-ascii"),
                ("Content-Transfer-Encoding", "quoted-printable"),
            ],
            body="Raw body\n",
        ),
    )
    write_email(maildir, "beck-s/inbox/2.", build({"Message-ID": "<2@thyme>"}))

    run(conn, maildir, parser)

    assert parser.calls == [
        ("Raw body\n", "text/plain; charset=us-ascii", "quoted-printable"),
        ("Hello.\n", None, None),
    ]


def test_body_parser_output_is_stored(conn, maildir, parser):
    write_email(maildir, "beck-s/inbox/1.", build(body="FWD ATTACH HEAD text\n"))
    run(conn, maildir, parser)
    assert rows(
        conn,
        "SELECT forwarded_content, quoted_content, headings, has_attachment FROM emails",
    ) == [("fwd", None, "h", 1)]


def test_process_file_builds_the_full_record(maildir, parser):
    write_email(
        maildir,
        "beck-s/inbox/1.",
        build(
            {"To": "a@x.com, b@x.com", "Cc": "c@x.com", "Bcc": "d@x.com, e@x.com"},
            extra=[("X-From", "Phillip Allen"), ("X-Folder", "\\Inbox")],
        ),
    )

    record = process_file(maildir, "maildir/beck-s/inbox/1.", parser)

    assert record.source_file == "maildir/beck-s/inbox/1."
    assert record.mailbox == "beck-s"
    assert record.x_from == "Phillip Allen"
    assert record.x_folder == "\\Inbox"
    assert record.recipients == (
        Recipient("a@x.com", "to", 0),
        Recipient("b@x.com", "to", 1),
        Recipient("c@x.com", "cc", 0),
        Recipient("d@x.com", "bcc", 0),
        Recipient("e@x.com", "bcc", 1),
    )
    # The hash is not ingest's job yet (open question 4 in module-layout.md).
    assert record.content_hash is None


# --- failures ----------------------------------------------------------------------


def test_missing_to_is_a_failure_in_the_error_log_and_in_parse_failures(
    conn, maildir, parser, tmp_path
):
    write_email(maildir, "beck-s/inbox/1.", build({"To": None}))
    log = tmp_path / "out" / "error_log.txt"

    stats = run(conn, maildir, parser, error_log=log)

    assert (stats.total_found, stats.parsed, stats.failed) == (1, 0, 1)
    assert log.read_text() == "maildir/beck-s/inbox/1. | missing to\n"
    assert rows(conn, "SELECT source_file, reason FROM parse_failures") == [
        ("maildir/beck-s/inbox/1.", "missing to")
    ]
    assert rows(conn, "SELECT COUNT(*) FROM emails") == [(0,)]
    assert parser.calls == []  # a header failure stops the file before the body parser


def test_failure_gets_a_utc_timestamp(conn, maildir, parser):
    write_email(maildir, "beck-s/inbox/1.", build({"To": None}))
    before = datetime.now(timezone.utc).replace(microsecond=0)

    run(conn, maildir, parser)

    [(failed_at,)] = rows(conn, "SELECT failed_at FROM parse_failures")
    assert failed_at.endswith("Z")
    stamp = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
    assert before <= stamp <= datetime.now(timezone.utc)


def test_body_parser_crash_is_logged_as_unexpected_and_the_run_continues(
    conn, maildir, parser, tmp_path
):
    write_email(maildir, "beck-s/inbox/1.", build(body="EXPLODE\n"))
    write_email(maildir, "beck-s/inbox/2.", build({"Message-ID": "<2@thyme>"}))
    log = tmp_path / "error_log.txt"

    stats = run(conn, maildir, parser, error_log=log)

    assert (stats.total_found, stats.parsed, stats.failed) == (2, 1, 1)
    assert log.read_text() == (
        "maildir/beck-s/inbox/1. | unexpected RuntimeError: boom\n"
    )
    assert rows(conn, "SELECT reason FROM parse_failures") == [
        ("unexpected RuntimeError: boom",)
    ]
    assert rows(conn, "SELECT message_id FROM emails") == [("<2@thyme>",)]


def test_a_multi_line_error_message_stays_on_one_log_line(conn, maildir, tmp_path):
    def parser(raw_body, content_type, content_transfer_encoding):
        raise ValueError("line one\nline two")

    write_email(maildir, "beck-s/inbox/1.", build())
    log = tmp_path / "error_log.txt"

    run(conn, maildir, parser, error_log=log)

    assert log.read_text() == (
        "maildir/beck-s/inbox/1. | unexpected ValueError: line one line two\n"
    )


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root can read any file, so chmod 000 proves nothing",
)
def test_unreadable_file_is_a_failure_and_the_run_continues(conn, maildir, parser):
    locked = write_email(maildir, "beck-s/inbox/1.", build())
    locked.chmod(0o000)
    write_email(maildir, "beck-s/inbox/2.", build({"Message-ID": "<2@thyme>"}))
    try:
        stats = run(conn, maildir, parser)
    finally:
        locked.chmod(0o644)  # let pytest clean up tmp_path

    assert (stats.parsed, stats.failed) == (1, 1)
    [(source_file, reason)] = rows(conn, "SELECT source_file, reason FROM parse_failures")
    assert source_file == "maildir/beck-s/inbox/1."
    assert reason.startswith("unexpected PermissionError: ")


def test_a_database_error_on_one_file_is_a_failure_and_the_run_continues(
    conn, maildir, parser
):
    # Two files with the same Message-ID: the second insert violates UNIQUE.
    write_email(maildir, "beck-s/inbox/1.", build({"Cc": "c@x.com"}))
    write_email(maildir, "beck-s/inbox/2.", build({"Cc": "c@x.com"}))
    write_email(maildir, "beck-s/inbox/3.", build({"Message-ID": "<3@thyme>", "Cc": "c@x.com"}))

    stats = run(conn, maildir, parser)

    assert (stats.parsed, stats.failed) == (2, 1)
    assert rows(conn, "SELECT source_file, reason FROM parse_failures") == [
        (
            "maildir/beck-s/inbox/2.",
            "unexpected IntegrityError: UNIQUE constraint failed: emails.message_id",
        )
    ]
    assert rows(conn, "SELECT source_file FROM emails ORDER BY source_file") == [
        ("maildir/beck-s/inbox/1.",),
        ("maildir/beck-s/inbox/3.",),
    ]
    # The rejected email left no recipient rows behind.
    assert rows(conn, "SELECT COUNT(*) FROM recipients") == [(4,)]  # 2 emails x (to + cc)


def test_non_utf8_bytes_do_not_fail_the_file(conn, maildir, parser):
    # A lone 0xE9 byte is invalid UTF-8; parse_headers falls back to Latin-1.
    write_email(maildir, "beck-s/inbox/1.", build().replace(b"Hello.", b"caf\xe9"))
    stats = run(conn, maildir, parser)
    assert (stats.parsed, stats.failed) == (1, 0)


# --- a missing mailbox is a setup error, not a bad file ----------------------------------


def test_missing_mailbox_raises_before_anything_is_ingested(conn, maildir, parser):
    write_email(maildir, "beck-s/inbox/1.", build())
    with pytest.raises(FileNotFoundError, match="ghost-x"):
        run(conn, maildir, parser, mailboxes=("beck-s", "ghost-x"))
    assert rows(conn, "SELECT COUNT(*) FROM emails") == [(0,)]
    assert rows(conn, "SELECT COUNT(*) FROM parse_failures") == [(0,)]


# --- batches and counts --------------------------------------------------------------------


def mixed_batch(maildir):
    """7 files: 3 good, 2 missing To, 1 missing Message-ID, 1 body-parser crash."""
    write_email(maildir, "beck-s/inbox/1.", build({"Message-ID": "<1@thyme>"}))
    write_email(maildir, "beck-s/inbox/2.", build({"Message-ID": "<2@thyme>", "To": None}))
    write_email(maildir, "beck-s/inbox/3.", build({"Message-ID": "<3@thyme>"}))
    write_email(maildir, "beck-s/sent/1.", build({"Message-ID": "<4@thyme>", "To": None}))
    write_email(maildir, "beck-s/sent/2.", build({"Message-ID": None}))
    write_email(maildir, "ring-a/inbox/1.", build({"Message-ID": "<6@thyme>"}, body="EXPLODE\n"))
    write_email(maildir, "ring-a/inbox/2.", build({"Message-ID": "<7@thyme>"}))


def test_mixed_batch_gives_correct_counts(conn, maildir, parser, tmp_path):
    mixed_batch(maildir)
    log = tmp_path / "error_log.txt"

    stats = run(conn, maildir, parser, mailboxes=("beck-s", "ring-a"), error_log=log)

    assert stats.total_found == 7
    assert stats.parsed == 3
    assert stats.failed == 4
    assert stats.parsed + stats.failed == stats.total_found
    assert stats.failures_by_reason == {
        "missing to": 2,
        "missing message_id": 1,
        "unexpected RuntimeError: boom": 1,
    }
    assert rows(conn, "SELECT COUNT(*) FROM emails") == [(3,)]
    assert rows(conn, "SELECT COUNT(*) FROM parse_failures") == [(4,)]
    assert len(log.read_text().splitlines()) == 4


def test_failures_are_grouped_by_reason_most_common_first(conn, maildir, parser):
    mixed_batch(maildir)
    stats = run(conn, maildir, parser, mailboxes=("beck-s", "ring-a"))
    assert list(stats.to_dict()["failures_by_reason"].items()) == [
        ("missing to", 2),
        ("missing message_id", 1),
        ("unexpected RuntimeError: boom", 1),
    ]


def test_empty_mailbox_gives_zero_stats_and_an_empty_error_log(
    conn, maildir, parser, tmp_path
):
    (maildir / "beck-s").mkdir()
    log = tmp_path / "error_log.txt"

    stats = run(conn, maildir, parser, error_log=log)

    assert (stats.total_found, stats.parsed, stats.failed) == (0, 0, 0)
    assert stats.failures_by_reason == {}
    assert set(stats.completeness()) == SPEC_FIELDS
    assert set(stats.completeness().values()) == {0.0}  # no division by zero
    assert log.read_text() == ""


def test_no_error_log_is_written_when_no_path_is_given(conn, maildir, parser, tmp_path):
    write_email(maildir, "beck-s/inbox/1.", build({"To": None}))
    run(conn, maildir, parser)
    assert list(tmp_path.glob("*.txt")) == []


def test_mailboxes_none_ingests_every_mailbox(conn, maildir, parser):
    write_email(maildir, "beck-s/inbox/1.", build({"Message-ID": "<1@thyme>"}))
    write_email(maildir, "ring-a/inbox/1.", build({"Message-ID": "<2@thyme>"}))
    stats = ingest_mailboxes(conn, maildir, None, parser)
    assert stats.parsed == 2


# --- transactions -------------------------------------------------------------------------


def test_ingest_commits_its_work(conn, maildir, parser, tmp_path):
    write_email(maildir, "beck-s/inbox/1.", build())
    write_email(maildir, "beck-s/inbox/2.", build({"To": None}))

    run(conn, maildir, parser)

    assert not conn.in_transaction
    other = sqlite3.connect(tmp_path / "test.db")
    assert other.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
    assert other.execute("SELECT COUNT(*) FROM parse_failures").fetchone()[0] == 1
    other.close()


def test_ingest_leaves_the_commit_to_a_caller_who_opened_a_transaction(
    conn, maildir, parser
):
    write_email(maildir, "beck-s/inbox/1.", build())
    conn.execute("BEGIN")
    run(conn, maildir, parser)
    assert conn.in_transaction
    conn.rollback()
    assert rows(conn, "SELECT COUNT(*) FROM emails") == [(0,)]


# --- completeness stats ----------------------------------------------------------------------


def completeness_example(maildir):
    """Four parsed emails whose optional fields are filled on purpose.

    n = 4, so every count converts to a percentage by hand: 1 -> 25, 2 -> 50,
    3 -> 75, 4 -> 100.

    Every email has a To, so to_addresses is 100%. The rest:

    email  subject  body          cc  bcc  x_from  x_bcc  x_folder  x_origin  content_type  from the body parser
    1      "S1"     "hello HEAD"  c   -    A       -      \\Inbox    -         text/plain    headings
    2      ""       "   " (blank) -   -    B       -      -         -         -             -
    3      "S3"     "FWD ATTACH"  -   d    -       D      -         -         -             forwarded, attachment
    4      "S4"     "> q HEAD"    -   -    -       -      -         O         -             quoted, headings
    """
    write_email(
        maildir,
        "beck-s/inbox/1.",
        build(
            {"Message-ID": "<1@thyme>", "Subject": "S1", "Cc": "c@x.com"},
            extra=[("X-From", "A"), ("X-Folder", "\\Inbox"),
                   ("Content-Type", "text/plain")],
            body="hello HEAD\n",
        ),
    )
    write_email(
        maildir,
        "beck-s/inbox/2.",
        build({"Message-ID": "<2@thyme>", "Subject": ""}, extra=[("X-From", "B")],
              body="   \n"),
    )
    write_email(
        maildir,
        "beck-s/inbox/3.",
        build(
            {"Message-ID": "<3@thyme>", "Subject": "S3", "To": "a@x.com, b@x.com",
             "Bcc": "d@x.com"},
            extra=[("X-bcc", "D")],
            body="FWD ATTACH\n",
        ),
    )
    write_email(
        maildir,
        "beck-s/inbox/4.",
        build({"Message-ID": "<4@thyme>", "Subject": "S4"}, extra=[("X-Origin", "O")],
              body="> q HEAD\n"),
    )


EXPECTED_PERCENT = {
    "message_id": 100.0, "date": 100.0, "from_address": 100.0,
    "to_addresses": 100.0, "source_file": 100.0,
    "subject": 75.0,           # email 2 has a blank subject
    "body": 75.0,              # email 2's body is only whitespace
    "cc_addresses": 25.0,      # email 1
    "bcc_addresses": 25.0,     # email 3
    "x_from": 50.0,            # emails 1, 2
    "x_to": 0.0,
    "x_cc": 0.0,
    "x_bcc": 25.0,             # email 3
    "x_folder": 25.0,          # email 1
    "x_origin": 25.0,          # email 4
    "content_type": 25.0,      # email 1
    "has_attachment": 100.0,   # a boolean is never null, so it always counts
    "forwarded_content": 25.0,  # email 3
    "quoted_content": 25.0,    # email 4
    "headings": 50.0,          # emails 1, 4
}


def test_completeness_matches_a_hand_checked_example(conn, maildir, parser):
    completeness_example(maildir)

    stats = run(conn, maildir, parser)

    assert (stats.total_found, stats.parsed, stats.failed) == (4, 4, 0)
    assert stats.completeness() == EXPECTED_PERCENT


def test_completeness_covers_exactly_the_spec_fields(conn, maildir, parser):
    completeness_example(maildir)
    stats = run(conn, maildir, parser)
    assert set(stats.completeness()) == SPEC_FIELDS
    assert set(STATS_FIELDS) == SPEC_FIELDS
    assert len(STATS_FIELDS) == len(SPEC_FIELDS)  # no repeats


def test_completeness_counts_only_parsed_emails(conn, maildir, parser):
    # Two failures must not dilute the percentages.
    completeness_example(maildir)
    write_email(maildir, "beck-s/inbox/5.", build({"Message-ID": "<5@thyme>", "To": None}))
    write_email(maildir, "beck-s/inbox/6.", build({"Message-ID": None}))

    stats = run(conn, maildir, parser)

    assert (stats.total_found, stats.parsed, stats.failed) == (6, 4, 2)
    assert stats.completeness() == EXPECTED_PERCENT


def test_field_counts_are_raw_counts(conn, maildir, parser):
    completeness_example(maildir)
    stats = run(conn, maildir, parser)
    assert stats.field_counts["subject"] == 3
    assert stats.field_counts["x_from"] == 2
    assert stats.field_counts["message_id"] == 4


def test_has_attachment_true_percent_counts_only_true_values(conn, maildir, parser):
    # completeness_example: only email 3 has "ATTACH" in its body, so 1 of 4.
    # has_attachment completeness stays 100%: a boolean is never null.
    completeness_example(maildir)

    stats = run(conn, maildir, parser)

    assert stats.has_attachment_true == 1
    assert stats.has_attachment_true_percent() == 25.0
    assert stats.completeness()["has_attachment"] == 100.0


def test_has_attachment_true_percent_ignores_failed_files(conn, maildir, parser):
    write_email(maildir, "beck-s/inbox/1.", build({"Message-ID": "<1@thyme>"}, body="ATTACH\n"))
    write_email(maildir, "beck-s/inbox/2.", build({"Message-ID": "<2@thyme>"}))
    write_email(maildir, "beck-s/inbox/3.", build({"Message-ID": "<3@thyme>", "To": None}, body="ATTACH\n"))

    stats = run(conn, maildir, parser)

    assert (stats.parsed, stats.failed) == (2, 1)
    assert stats.has_attachment_true_percent() == 50.0  # 1 of 2 parsed, not 2 of 3 found


def test_has_attachment_true_percent_is_zero_when_nothing_parsed():
    assert ExtractionStats().has_attachment_true_percent() == 0.0


def test_has_attachment_true_percent_in_a_bare_stats_object():
    stats = ExtractionStats(parsed=8, has_attachment_true=2)
    assert stats.has_attachment_true_percent() == 25.0
    assert stats.to_dict()["has_attachment_true_percent"] == 25.0


def test_has_attachment_true_percent_is_rounded_in_the_dict():
    stats = ExtractionStats(parsed=3, has_attachment_true=1)
    assert stats.to_dict()["has_attachment_true_percent"] == 33.33


def test_completeness_percent_is_hand_computable_for_a_bare_stats_object():
    stats = ExtractionStats(parsed=8, field_counts={"subject": 6, "body": 8})
    assert stats.completeness() == {"subject": 75.0, "body": 100.0}


# --- written outputs ------------------------------------------------------------------------


def test_extraction_stats_json_matches_the_stats(conn, maildir, parser, tmp_path):
    mixed_batch(maildir)
    stats = run(conn, maildir, parser, mailboxes=("beck-s", "ring-a"))
    path = tmp_path / "output" / "extraction_stats.json"  # "output" does not exist yet

    write_stats(stats, path)

    data = json.loads(path.read_text())
    assert data == stats.to_dict()
    assert data["total_found"] == 7
    assert data["parsed"] == 3
    assert data["failed"] == 4
    assert data["failures_by_reason"]["missing to"] == 2
    assert set(data["completeness_percent"]) == SPEC_FIELDS
    assert data["completeness_percent"]["message_id"] == 100.0
    assert data["field_counts"]["message_id"] == 3
    assert data["has_attachment_true_percent"] == 0.0  # no ATTACH marker in this batch


def test_stats_json_has_the_documented_keys(conn, maildir, parser):
    stats = run(conn, maildir, parser, mailboxes=())
    assert set(stats.to_dict()) == {
        "total_found", "parsed", "failed", "failures_by_reason",
        "field_counts", "completeness_percent", "has_attachment_true_percent",
    }


def test_stats_to_dict_rounds_percentages_to_two_places():
    stats = ExtractionStats(parsed=3, field_counts={"subject": 1})
    assert stats.to_dict()["completeness_percent"] == {"subject": 33.33}


def test_format_stats_prints_the_numbers_reasons_and_every_field(
    conn, maildir, parser
):
    completeness_example(maildir)
    write_email(maildir, "beck-s/inbox/5.", build({"Message-ID": "<5@thyme>", "To": None}))

    text = format_stats(run(conn, maildir, parser))

    assert "Total files found: 5" in text
    assert "Parsed: 4" in text
    assert "Failed: 1" in text
    assert re.search(r"1\s+missing to", text)
    assert "has_attachment true: 25.0%" in text  # 1 of 4 parsed emails
    for name, percent in EXPECTED_PERCENT.items():
        assert re.search(rf"{name}\s+{percent:.1f}%", text), name


def test_write_error_log_format(tmp_path):
    when = datetime(2026, 10, 11, tzinfo=timezone.utc)
    path = tmp_path / "error_log.txt"
    write_error_log(
        [
            ParseFailure("maildir/a/inbox/1.", "missing to", when),
            ParseFailure("maildir/a/inbox/2.", "invalid date: no timezone", when),
        ],
        path,
    )
    assert path.read_text() == (
        "maildir/a/inbox/1. | missing to\n"
        "maildir/a/inbox/2. | invalid date: no timezone\n"
    )
