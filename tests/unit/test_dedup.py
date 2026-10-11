"""Tests for dedup.py (behavior fixed in D-021).

Every test runs on an in-memory database seeded with hand-built EmailRecords.

Two stages, tested separately and together:
- Stage 1 (mark_storage_copies): the same email filed in several folders or
  mailboxes is ONE logical email. Rows with the same content_hash are grouped;
  the row with the smallest source_file is canonical, the rest get copy_of.
- Stage 2 (flag_duplicates): on canonical rows only, the spec's rule: same
  from_address, same normalized subject, main bodies >= 90% similar. The
  earliest is the original and later ones are flagged against it. A flagged
  row is only ever compared to ORIGINALS (no chaining).

Helpers:
- email(n, **overrides): a valid record. Each n gets its own message_id,
  source_file and date (n days after T0), so a test only spells out what it
  cares about. For storage copies, pass the same date and body and a different
  source_file.
- ratio_pair(k): two strings whose fuzz.ratio is exactly k (the helper asserts
  it, so a wrong construction fails loudly).
"""
import csv
import json
from datetime import datetime, timedelta, timezone

import pytest
from rapidfuzz import fuzz

from enron_pipeline import db, dedup
from enron_pipeline.models import DuplicateFlag, EmailRecord

T0 = datetime(2001, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
SENDER = "phil@enron.com"
BODY = "Hello team, the Q2 numbers are ready for review on Friday."

REPORT_COLUMNS = [
    "duplicate_message_id",
    "original_message_id",
    "subject",
    "from_address",
    "duplicate_date",
    "original_date",
    "similarity_score",
]


@pytest.fixture
def conn():
    connection = db.connect()
    db.init_schema(connection)
    yield connection
    connection.close()


def email(n: int, **overrides) -> EmailRecord:
    fields = dict(
        message_id=f"<{n}@thyme>",
        date=T0 + timedelta(days=n),
        from_address=SENDER,
        subject="Q2 numbers",
        body=BODY,
        source_file=f"maildir/allen-p/inbox/{n}.",
        mailbox="allen-p",
    )
    fields.update(overrides)
    return EmailRecord(**fields)


def seed(conn, *records: EmailRecord) -> None:
    for record in records:
        db.insert_email(conn, record)


def run_both(conn, threshold: float = 90.0) -> list[DuplicateFlag]:
    dedup.mark_storage_copies(conn)
    return dedup.flag_duplicates(conn, threshold)


def row(conn, message_id: str) -> dict:
    cursor = conn.execute("SELECT * FROM emails WHERE message_id = ?", (message_id,))
    names = [column[0] for column in cursor.description]
    return dict(zip(names, cursor.fetchone()))


def snapshot(conn) -> list[tuple]:
    return conn.execute(
        "SELECT message_id, content_hash, copy_of, is_duplicate, duplicate_of,"
        " similarity_score FROM emails ORDER BY message_id"
    ).fetchall()


def ratio_pair(k: int) -> tuple[str, str]:
    """Two 100-character strings with fuzz.ratio exactly k."""
    a = "x" * k + "a" * (100 - k)
    b = "x" * k + "b" * (100 - k)
    assert fuzz.ratio(a, b) == float(k)
    return a, b


def flagged_ids(flags) -> set[str]:
    return {f.duplicate_message_id for f in flags}


# --- normalize_subject ---------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    ["Re: X", "RE: FW: Re: X", "Fwd:X", "re:  X  ", "X", "FW: fwd: RE: x", "Re :X", "FWD: Fw: X"],
)
def test_prefixed_and_plain_subjects_normalize_the_same(subject):
    assert dedup.normalize_subject(subject) == "x"


def test_regarding_does_not_lose_its_re():
    assert dedup.normalize_subject("Regarding X") == "regarding x"
    assert dedup.normalize_subject("Re: Regarding X") == "regarding x"
    assert dedup.normalize_subject("Forward: X") == "forward: x"
    assert dedup.normalize_subject("Fwdx: X") == "fwdx: x"


def test_only_leading_prefixes_are_stripped():
    assert dedup.normalize_subject("Update re: budget") == "update re: budget"
    assert dedup.normalize_subject("Budget FW: draft") == "budget fw: draft"


def test_whitespace_collapses_and_case_is_lowered():
    assert dedup.normalize_subject("  Q2   NUMBERS \t update ") == "q2 numbers update"
    assert dedup.normalize_subject("Re:\tQ2\nnumbers") == "q2 numbers"


@pytest.mark.parametrize("subject", ["", "   ", "Re:", "Re: Fwd: ", "RE:RE:RE:"])
def test_empty_and_prefix_only_subjects_normalize_to_empty(subject):
    assert dedup.normalize_subject(subject) == ""


# --- compute hash --------------------------------------------------------------------


def test_content_hash_is_a_sha256_hex_digest_and_deterministic():
    first = dedup.content_hash_for(email(1))
    assert len(first) == 64 and int(first, 16) >= 0
    assert dedup.content_hash_for(email(1)) == first


@pytest.mark.parametrize(
    "field, value",
    [
        ("from_address", "other@enron.com"),
        ("date", T0 + timedelta(days=99)),
        ("subject", "Re: Q2 numbers"),
        ("body", BODY + "!"),
        ("quoted_content", "> quoted"),
        ("forwarded_content", "forwarded text"),
    ],
)
def test_each_hashed_field_changes_the_hash(field, value):
    assert dedup.content_hash_for(email(1, **{field: value})) != dedup.content_hash_for(email(1))


@pytest.mark.parametrize(
    "field, value",
    [
        ("message_id", "<other@thyme>"),
        ("source_file", "maildir/other-b/sent/7."),
        ("mailbox", "other-b"),
        ("x_folder", "\\Sent"),
        ("headings", "AGENDA"),
    ],
)
def test_other_fields_do_not_change_the_hash(field, value):
    assert dedup.content_hash_for(email(1, **{field: value})) == dedup.content_hash_for(email(1))


def test_same_instant_in_another_timezone_hashes_the_same():
    plus_two = timezone(timedelta(hours=2))
    a = email(1, date=T0)
    b = email(1, date=T0.astimezone(plus_two))
    assert dedup.content_hash_for(a) == dedup.content_hash_for(b)


def test_missing_and_empty_optional_text_hash_the_same():
    assert dedup.content_hash_for(email(1, quoted_content=None)) == dedup.content_hash_for(
        email(1, quoted_content="")
    )


def test_field_boundaries_matter():
    # "ab" + "c" must not collide with "a" + "bc".
    a = email(1, subject="ab", body="c")
    b = email(1, subject="a", body="bc")
    assert dedup.content_hash_for(a) != dedup.content_hash_for(b)


# --- Stage 1: storage copies ---------------------------------------------------------


def test_same_email_in_two_folders_is_one_canonical_and_one_copy(conn):
    seed(
        conn,
        email(1, date=T0, source_file="maildir/allen-p/inbox/1."),
        email(2, date=T0, source_file="maildir/allen-p/sent/9."),
    )
    result = dedup.mark_storage_copies(conn)
    assert (result.groups, result.rows) == (1, 1)
    assert row(conn, "<1@thyme>")["copy_of"] is None
    assert row(conn, "<2@thyme>")["copy_of"] == "<1@thyme>"


def test_same_email_in_two_mailboxes_is_collapsed(conn):
    seed(
        conn,
        email(1, date=T0, mailbox="allen-p", source_file="maildir/allen-p/inbox/1."),
        email(2, date=T0, mailbox="beck-s", source_file="maildir/beck-s/inbox/1."),
    )
    result = dedup.mark_storage_copies(conn)
    assert (result.groups, result.rows) == (1, 1)
    assert row(conn, "<2@thyme>")["copy_of"] == "<1@thyme>"


def test_same_body_on_a_different_date_is_not_collapsed(conn):
    seed(conn, email(1, date=T0), email(2, date=T0 + timedelta(days=1)))
    result = dedup.mark_storage_copies(conn)
    assert (result.groups, result.rows) == (0, 0)
    assert row(conn, "<1@thyme>")["copy_of"] is None
    assert row(conn, "<2@thyme>")["copy_of"] is None
    assert row(conn, "<1@thyme>")["content_hash"] != row(conn, "<2@thyme>")["content_hash"]


def test_same_body_from_a_different_sender_is_not_collapsed(conn):
    seed(conn, email(1, date=T0), email(2, date=T0, from_address="tim@enron.com"))
    assert dedup.mark_storage_copies(conn).rows == 0


@pytest.mark.parametrize(
    "field, value",
    [
        ("subject", "Re: Q2 numbers"),  # "as written": a Re: prefix is a different email
        ("quoted_content", "> quoted"),
        ("forwarded_content", "forwarded text"),
        ("body", BODY + " Thanks."),
    ],
)
def test_a_difference_in_any_hashed_field_is_not_collapsed(conn, field, value):
    seed(conn, email(1, date=T0), email(2, date=T0, **{field: value}))
    assert dedup.mark_storage_copies(conn).rows == 0


def test_canonical_is_the_smallest_source_file_even_when_inserted_last(conn):
    seed(
        conn,
        email(1, date=T0, source_file="maildir/beck-s/sent/5."),
        email(2, date=T0, source_file="maildir/allen-p/inbox/9."),
        email(3, date=T0, source_file="maildir/allen-p/inbox/10."),
    )
    result = dedup.mark_storage_copies(conn)
    # String order: "…/inbox/10." < "…/inbox/9." < "maildir/beck-s/…".
    assert row(conn, "<3@thyme>")["copy_of"] is None
    assert row(conn, "<2@thyme>")["copy_of"] == "<3@thyme>"
    assert row(conn, "<1@thyme>")["copy_of"] == "<3@thyme>"
    assert (result.groups, result.rows) == (1, 2)


def test_copies_point_at_the_canonical_row_not_at_each_other(conn):
    seed(conn, *(email(n, date=T0, source_file=f"maildir/allen-p/f{n}/1.") for n in (1, 2, 3, 4)))
    dedup.mark_storage_copies(conn)
    assert [row(conn, f"<{n}@thyme>")["copy_of"] for n in (1, 2, 3, 4)] == [
        None, "<1@thyme>", "<1@thyme>", "<1@thyme>",
    ]


def test_mark_storage_copies_fills_content_hash_on_every_row(conn):
    seed(conn, email(1), email(2))
    dedup.mark_storage_copies(conn)
    for n in (1, 2):
        stored = row(conn, f"<{n}@thyme>")["content_hash"]
        assert stored == dedup.content_hash_for(email(n))


def test_mark_storage_copies_with_no_emails(conn):
    result = dedup.mark_storage_copies(conn)
    assert (result.groups, result.rows) == (0, 0)


# --- Stage 2: the spec rule ------------------------------------------------------------


def test_identical_bodies_on_different_dates_later_is_flagged_with_score_100(conn):
    seed(conn, email(1), email(2))
    flags = run_both(conn)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.duplicate_message_id == "<2@thyme>"
    assert flag.original_message_id == "<1@thyme>"
    assert flag.similarity_score == 100.0
    assert flag.from_address == SENDER
    assert flag.subject == "Q2 numbers"
    assert flag.duplicate_date == T0 + timedelta(days=2)
    assert flag.original_date == T0 + timedelta(days=1)
    flagged = row(conn, "<2@thyme>")
    assert (flagged["is_duplicate"], flagged["duplicate_of"], flagged["similarity_score"]) == (
        1, "<1@thyme>", 100.0,
    )
    original = row(conn, "<1@thyme>")
    assert (original["is_duplicate"], original["duplicate_of"], original["similarity_score"]) == (
        0, None, None,
    )


def test_95_percent_similar_is_flagged(conn):
    a, b = ratio_pair(95)
    seed(conn, email(1, body=a), email(2, body=b))
    flags = run_both(conn)
    assert [f.similarity_score for f in flags] == [95.0]


def test_89_percent_similar_is_not_flagged(conn):
    a, b = ratio_pair(89)
    seed(conn, email(1, body=a), email(2, body=b))
    assert run_both(conn) == []
    assert row(conn, "<2@thyme>")["is_duplicate"] == 0


def test_exactly_90_is_flagged(conn):
    a, b = ratio_pair(90)
    seed(conn, email(1, body=a), email(2, body=b))
    flags = run_both(conn)
    assert [f.similarity_score for f in flags] == [90.0]


def test_threshold_is_an_argument(conn):
    a, b = ratio_pair(95)
    seed(conn, email(1, body=a), email(2, body=b))
    dedup.mark_storage_copies(conn)
    assert dedup.flag_duplicates(conn, threshold=96.0) == []
    assert len(dedup.flag_duplicates(conn, threshold=95.0)) == 1
    assert len(dedup.flag_duplicates(conn, threshold=50.0)) == 1


def test_similarity_function_can_be_swapped(conn):
    seen = []

    def always(a, b):
        seen.append((a, b))
        return 100.0

    seed(conn, email(1, body="alpha"), email(2, body="completely different"))
    dedup.mark_storage_copies(conn)
    flags = dedup.flag_duplicates(conn, similarity=always)
    assert [f.similarity_score for f in flags] == [100.0]
    assert seen == [("alpha", "completely different")]
    assert dedup.flag_duplicates(conn, similarity=lambda a, b: 0.0) == []


def test_default_similarity_is_the_rapidfuzz_ratio_on_a_0_to_100_scale():
    assert dedup.default_similarity("abc", "abc") == 100.0
    assert dedup.default_similarity("abc", "xyz") == 0.0
    a, b = ratio_pair(93)
    assert dedup.default_similarity(a, b) == fuzz.ratio(a, b) == 93.0


def test_re_prefix_and_plain_subject_from_the_same_sender_share_a_block(conn):
    seed(conn, email(1, subject="Q2 numbers"), email(2, subject="Re: Q2 numbers"))
    flags = run_both(conn)
    assert flagged_ids(flags) == {"<2@thyme>"}
    # The report shows the flagged email's own subject, prefix intact.
    assert flags[0].subject == "Re: Q2 numbers"


def test_prefix_stacks_and_case_do_not_split_a_block(conn):
    seed(conn, email(1, subject="Q2 Numbers"), email(2, subject="RE: FW: re: q2   numbers"))
    assert flagged_ids(run_both(conn)) == {"<2@thyme>"}


def test_different_senders_with_the_same_body_and_subject_are_not_flagged(conn):
    seed(conn, email(1), email(2, from_address="tim@enron.com"))
    assert run_both(conn) == []


def test_different_subjects_are_not_flagged(conn):
    seed(conn, email(1, subject="Q2 numbers"), email(2, subject="Q3 numbers"))
    assert run_both(conn) == []


def test_recipients_are_not_part_of_the_rule(conn):
    from enron_pipeline.models import Recipient

    seed(
        conn,
        email(1, recipients=[Recipient("a@enron.com", "to", 0)]),
        email(2, recipients=[Recipient("totally.different@enron.com", "to", 0)]),
    )
    assert flagged_ids(run_both(conn)) == {"<2@thyme>"}


def test_group_of_three_flags_two_both_pointing_to_the_earliest(conn):
    seed(conn, email(1), email(2), email(3))
    flags = run_both(conn)
    assert flagged_ids(flags) == {"<2@thyme>", "<3@thyme>"}
    assert {f.original_message_id for f in flags} == {"<1@thyme>"}
    assert row(conn, "<1@thyme>")["is_duplicate"] == 0


def test_chain_case_b_is_flagged_and_c_is_not_a_duplicate_of_a(conn):
    # A~B 92, B~C 92, A~C 85. B is flagged against A. C is compared to the
    # original A only (never to the duplicate B), scores 85, and stays unflagged.
    a = "x" * 100
    b = "b" * 8 + "x" * 92
    c = "b" * 7 + "c" + "d" * 7 + "x" * 85
    assert (fuzz.ratio(a, b), fuzz.ratio(b, c), fuzz.ratio(a, c)) == (92.0, 92.0, 85.0)
    seed(conn, email(1, body=a), email(2, body=b), email(3, body=c))

    seen = []

    def spy(left, right):
        seen.append((left, right))
        return fuzz.ratio(left, right)

    dedup.mark_storage_copies(conn)
    flags = dedup.flag_duplicates(conn, similarity=spy)
    assert [(f.duplicate_message_id, f.original_message_id, f.similarity_score) for f in flags] == [
        ("<2@thyme>", "<1@thyme>", 92.0)
    ]
    assert row(conn, "<3@thyme>")["is_duplicate"] == 0
    assert row(conn, "<3@thyme>")["duplicate_of"] is None
    assert (b, c) not in seen  # a duplicate is never an "original" to compare against
    assert seen == [(a, b), (a, c)]


def test_a_row_left_unassigned_becomes_the_next_original(conn):
    # A, then X (unrelated to A), then Y (a copy of X). X is a new original and
    # Y is flagged against X, not against A.
    seed(conn, email(1, body="a" * 100), email(2, body="q" * 100), email(3, body="q" * 100))
    flags = run_both(conn)
    assert [(f.duplicate_message_id, f.original_message_id) for f in flags] == [
        ("<3@thyme>", "<2@thyme>")
    ]


def test_storage_copies_of_an_email_are_never_flagged_against_each_other(conn):
    seed(
        conn,
        email(1, date=T0, source_file="maildir/allen-p/inbox/1."),
        email(2, date=T0, source_file="maildir/allen-p/sent/1."),
        email(3, date=T0, source_file="maildir/beck-s/inbox/1.", mailbox="beck-s"),
    )
    assert run_both(conn) == []
    assert row(conn, "<1@thyme>")["copy_of"] is None
    assert [row(conn, f"<{n}@thyme>")["is_duplicate"] for n in (1, 2, 3)] == [0, 0, 0]


def test_resend_is_flagged_against_the_canonical_copy_and_copies_stay_unflagged(conn):
    seed(
        conn,
        # The original, filed twice.
        email(1, date=T0, source_file="maildir/allen-p/inbox/1."),
        email(2, date=T0, source_file="maildir/allen-p/sent/1."),
        # A re-send a week later, also filed twice.
        email(3, date=T0 + timedelta(days=7), source_file="maildir/allen-p/inbox/2."),
        email(4, date=T0 + timedelta(days=7), source_file="maildir/allen-p/sent/2."),
    )
    flags = run_both(conn)
    assert [(f.duplicate_message_id, f.original_message_id) for f in flags] == [
        ("<3@thyme>", "<1@thyme>")
    ]
    # Storage copies of a flagged row are not flagged; they are reachable via copy_of.
    assert row(conn, "<4@thyme>")["is_duplicate"] == 0
    assert row(conn, "<4@thyme>")["copy_of"] == "<3@thyme>"
    assert row(conn, "<2@thyme>")["is_duplicate"] == 0
    assert row(conn, "<2@thyme>")["copy_of"] == "<1@thyme>"


def test_two_empty_main_bodies_with_different_forwarded_content_are_not_flagged(conn):
    seed(
        conn,
        email(1, body="", forwarded_content="A" * 80),
        email(2, body="", forwarded_content="B" * 80),
    )
    assert run_both(conn) == []


def test_two_empty_main_bodies_are_compared_on_the_full_text(conn):
    seen = []

    def spy(left, right):
        seen.append((left, right))
        return fuzz.ratio(left, right)

    seed(
        conn,
        email(1, body="", forwarded_content="fwd one", quoted_content="quote one"),
        email(2, body="", forwarded_content="fwd one", quoted_content="quote one"),
    )
    dedup.mark_storage_copies(conn)  # the dates differ, so these are two logical emails
    flags = dedup.flag_duplicates(conn, similarity=spy)
    assert seen == [("fwd one quote one", "fwd one quote one")]
    assert [f.similarity_score for f in flags] == [100.0]


def test_two_emails_with_nothing_at_all_are_flagged_as_identical(conn):
    # Body, forwarded and quoted are all empty in both, so the full text is ""
    # for both and they really are identical. Pinned so the fallback is known
    # not to crash on empty text; the D-021 item 6 guard only covers emails
    # that DO have forwarded or quoted content.
    seed(conn, email(1, body=""), email(2, body=""))
    flags = run_both(conn)
    assert [f.similarity_score for f in flags] == [100.0]


def test_one_empty_main_body_is_compared_on_main_bodies_and_scores_zero(conn):
    seed(conn, email(1, body=""), email(2, body=BODY))
    assert run_both(conn) == []


def test_main_body_is_compared_not_the_quoted_chain(conn):
    seen = []

    def spy(left, right):
        seen.append((left, right))
        return fuzz.ratio(left, right)

    seed(
        conn,
        email(1, quoted_content="> something quite different"),
        email(2, quoted_content="> and now a very much longer quoted reply chain here"),
    )
    dedup.mark_storage_copies(conn)
    flags = dedup.flag_duplicates(conn, similarity=spy)
    assert [f.similarity_score for f in flags] == [100.0]
    assert seen == [(BODY, BODY)]


def test_whitespace_is_collapsed_before_comparing(conn):
    seen = []

    def spy(left, right):
        seen.append((left, right))
        return fuzz.ratio(left, right)

    seed(conn, email(1, body="Hello\n\nteam,\n  the  numbers"), email(2, body="Hello team, the numbers\n"))
    dedup.mark_storage_copies(conn)
    flags = dedup.flag_duplicates(conn, similarity=spy)
    assert seen == [("Hello team, the numbers", "Hello team, the numbers")]
    assert flags[0].similarity_score == 100.0


def test_same_date_two_different_emails_smaller_source_file_is_the_original(conn):
    a, b = ratio_pair(95)
    seed(
        conn,
        # Inserted first but has the LARGER source_file.
        email(1, date=T0, body=b, source_file="maildir/allen-p/sent/2."),
        email(2, date=T0, body=a, source_file="maildir/allen-p/inbox/1."),
    )
    flags = run_both(conn)
    assert [(f.duplicate_message_id, f.original_message_id) for f in flags] == [
        ("<1@thyme>", "<2@thyme>")
    ]


def test_earliest_by_date_is_the_original_regardless_of_insert_order(conn):
    seed(conn, email(3), email(1), email(2))
    flags = run_both(conn)
    assert {f.original_message_id for f in flags} == {"<1@thyme>"}
    assert flagged_ids(flags) == {"<2@thyme>", "<3@thyme>"}


def test_automated_mail_is_flagged_like_any_other(conn):
    # D-021 item 10: no exemption for repeated automated mail.
    seed(conn, *(email(n, subject="Start Date: report failed", body=f"Run {n} failed, retry") for n in range(1, 5)))
    assert len(run_both(conn)) == 3


def test_blank_subjects_from_one_sender_share_a_block(conn):
    seed(conn, email(1, subject=""), email(2, subject="Re:"))
    assert flagged_ids(run_both(conn)) == {"<2@thyme>"}


def test_flag_duplicates_does_not_touch_notification_columns(conn):
    seed(conn, email(1), email(2, notification_sent=True, notification_date=T0))
    run_both(conn)
    flagged = row(conn, "<2@thyme>")
    assert flagged["notification_sent"] == 1
    assert flagged["notification_date"] == "2001-05-14T12:00:00Z"
    assert row(conn, "<1@thyme>")["notification_sent"] == 0


def test_flag_duplicates_with_no_emails_or_no_duplicates(conn):
    assert dedup.flag_duplicates(conn) == []
    seed(conn, email(1, subject="a"), email(2, subject="b"))
    assert run_both(conn) == []


def test_returned_flags_match_the_database(conn):
    a, b = ratio_pair(93)
    seed(conn, email(1, body=a), email(2, body=b), email(3, subject="Other"), email(4, subject="Other"))
    flags = run_both(conn)
    in_db = conn.execute(
        "SELECT message_id, duplicate_of, similarity_score FROM emails WHERE is_duplicate = 1"
        " ORDER BY message_id"
    ).fetchall()
    assert in_db == sorted(
        (f.duplicate_message_id, f.original_message_id, f.similarity_score) for f in flags
    )


# --- Running twice -------------------------------------------------------------------------


def test_running_dedup_twice_gives_the_same_result(conn):
    a, b = ratio_pair(93)
    seed(
        conn,
        email(1, date=T0, source_file="maildir/allen-p/inbox/1."),
        email(2, date=T0, source_file="maildir/allen-p/sent/1."),
        email(3, body=a),
        email(4, body=b),
        email(5, body=BODY + " plus"),
    )
    first_flags = run_both(conn)
    first_state = snapshot(conn)
    second_flags = run_both(conn)
    assert second_flags == first_flags
    assert snapshot(conn) == first_state
    assert first_flags  # the scenario really did flag something


def test_a_second_run_at_a_stricter_threshold_clears_the_old_flags(conn):
    a, b = ratio_pair(93)
    seed(conn, email(1, body=a), email(2, body=b))
    assert len(run_both(conn, threshold=90.0)) == 1
    assert run_both(conn, threshold=95.0) == []
    assert row(conn, "<2@thyme>")["is_duplicate"] == 0
    assert row(conn, "<2@thyme>")["similarity_score"] is None


def test_a_second_run_after_new_emails_picks_them_up(conn):
    seed(conn, email(1), email(2))
    run_both(conn)
    seed(conn, email(3))
    assert flagged_ids(run_both(conn)) == {"<2@thyme>", "<3@thyme>"}


# --- summarize ---------------------------------------------------------------------------------


def make_flag(duplicate: str, original: str, day: int = 1, score: float = 95.0) -> DuplicateFlag:
    return DuplicateFlag(
        duplicate_message_id=duplicate,
        original_message_id=original,
        subject="s",
        from_address="a@enron.com",
        duplicate_date=T0 + timedelta(days=day),
        original_date=T0,
        similarity_score=score,
    )


def test_summarize_counts_groups_flagged_average_and_largest():
    flags = [
        make_flag("<d1>", "<o1>"),
        make_flag("<d2>", "<o1>"),
        make_flag("<d3>", "<o2>"),
    ]
    stats = dedup.summarize(flags)
    assert stats.duplicate_groups == 2
    assert stats.emails_flagged == 3
    # Group sizes count the original too: o1 + 2 duplicates = 3, o2 + 1 = 2.
    assert stats.average_group_size == 2.5
    assert stats.largest_group_size == 3


def test_summarize_includes_the_storage_copy_numbers():
    storage = dedup.StorageCopyStats(groups=4, rows=9)
    stats = dedup.summarize([make_flag("<d>", "<o>")], storage)
    assert (stats.storage_copy_groups, stats.storage_copy_rows) == (4, 9)
    assert dedup.summarize([make_flag("<d>", "<o>")]).storage_copy_groups == 0


def test_summarize_with_no_flags_is_all_zero():
    stats = dedup.summarize([])
    assert (stats.duplicate_groups, stats.emails_flagged, stats.average_group_size, stats.largest_group_size) == (
        0, 0, 0.0, 0,
    )


def test_stats_numbers_on_a_small_example(conn):
    seed(
        conn,
        # Storage-copy group 1: one email filed in 2 places -> 1 row collapsed.
        email(1, subject="S1", date=T0, source_file="maildir/allen-p/inbox/1."),
        email(2, subject="S1", date=T0, source_file="maildir/allen-p/sent/1."),
        # Storage-copy group 2: one email filed in 3 places -> 2 rows collapsed.
        email(3, subject="S2", date=T0, source_file="maildir/allen-p/inbox/2."),
        email(4, subject="S2", date=T0, source_file="maildir/allen-p/sent/2."),
        email(5, subject="S2", date=T0, source_file="maildir/beck-s/inbox/2.", mailbox="beck-s"),
        # Duplicate group G1: an original and 2 re-sends (size 3).
        email(10, subject="G1", date=T0 + timedelta(days=1)),
        email(11, subject="G1", date=T0 + timedelta(days=2)),
        email(12, subject="G1", date=T0 + timedelta(days=3)),
        # Duplicate group G2: an original and 1 re-send (size 2).
        email(20, subject="G2", date=T0 + timedelta(days=1)),
        email(21, subject="G2", date=T0 + timedelta(days=2)),
    )
    storage = dedup.mark_storage_copies(conn)
    flags = dedup.flag_duplicates(conn)
    stats = dedup.summarize(flags, storage)
    assert (stats.storage_copy_groups, stats.storage_copy_rows) == (2, 3)
    assert stats.duplicate_groups == 2
    assert stats.emails_flagged == 3
    assert stats.average_group_size == 2.5
    assert stats.largest_group_size == 3


def test_write_stats_writes_the_documented_json(tmp_path):
    stats = dedup.summarize(
        [make_flag("<d1>", "<o1>"), make_flag("<d2>", "<o1>"), make_flag("<d3>", "<o2>")],
        dedup.StorageCopyStats(groups=2, rows=3),
    )
    path = tmp_path / "output" / "dedup_stats.json"  # parent folder does not exist yet
    dedup.write_stats(stats, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "storage_copy_groups": 2,
        "storage_copy_rows_collapsed": 3,
        "duplicate_groups": 2,
        "emails_flagged": 3,
        "average_group_size": 2.5,
        "largest_group_size": 3,
    }


# --- write_report ---------------------------------------------------------------------------------


def read_report(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_report_has_the_exact_spec_columns_and_one_row_per_flag(tmp_path):
    flags = [make_flag("<d1>", "<o1>", day=3, score=91.234), make_flag("<d2>", "<o1>", day=1, score=100.0)]
    path = tmp_path / "duplicates_report.csv"
    dedup.write_report(flags, path)
    rows = read_report(path)
    assert rows[0] == REPORT_COLUMNS
    assert len(rows) == 3
    first = dict(zip(REPORT_COLUMNS, rows[1]))
    assert first["duplicate_message_id"] == "<d2>"  # earlier duplicate date first
    assert first["original_message_id"] == "<o1>"
    assert first["subject"] == "s"
    assert first["from_address"] == "a@enron.com"
    assert first["duplicate_date"] == "2001-05-15T12:00:00Z"
    assert first["original_date"] == "2001-05-14T12:00:00Z"
    assert first["similarity_score"] == "100.00"
    assert dict(zip(REPORT_COLUMNS, rows[2]))["similarity_score"] == "91.23"


def test_report_is_sorted_by_original_then_duplicate_date(tmp_path):
    early = T0
    late = T0 + timedelta(days=10)

    def f(dup, orig, orig_date, dup_day):
        return DuplicateFlag(dup, orig, "s", "a@enron.com", T0 + timedelta(days=dup_day), orig_date, 95.0)

    flags = [
        f("<d-b2>", "<o-b>", late, 30),
        f("<d-a2>", "<o-a>", early, 22),
        f("<d-b1>", "<o-b>", late, 20),
        f("<d-a1>", "<o-a>", early, 21),
    ]
    path = tmp_path / "r.csv"
    dedup.write_report(flags, path)
    assert [r[0] for r in read_report(path)[1:]] == ["<d-a1>", "<d-a2>", "<d-b1>", "<d-b2>"]


def test_report_keeps_a_group_together_when_originals_share_a_date(tmp_path):
    def f(dup, orig, dup_day):
        return DuplicateFlag(dup, orig, "s", "a@enron.com", T0 + timedelta(days=dup_day), T0, 95.0)

    flags = [f("<d1>", "<o-b>", 1), f("<d2>", "<o-a>", 2), f("<d3>", "<o-b>", 3), f("<d4>", "<o-a>", 4)]
    path = tmp_path / "r.csv"
    dedup.write_report(flags, path)
    assert [r[1] for r in read_report(path)[1:]] == ["<o-a>", "<o-a>", "<o-b>", "<o-b>"]


def test_report_survives_commas_quotes_and_newlines_in_the_subject(tmp_path):
    awkward = 'Budget, "final"\nversion'
    flag = DuplicateFlag("<d>", "<o>", awkward, "a@enron.com", T0 + timedelta(days=1), T0, 95.0)
    path = tmp_path / "r.csv"
    dedup.write_report([flag], path)
    assert read_report(path)[1][2] == awkward


def test_report_with_no_flags_is_just_the_header(tmp_path):
    path = tmp_path / "sub" / "r.csv"  # parent folder does not exist yet
    dedup.write_report([], path)
    assert read_report(path) == [REPORT_COLUMNS]


def test_report_from_a_real_run_has_one_row_per_flagged_email(conn, tmp_path):
    seed(conn, email(1), email(2), email(3), email(4, subject="other"))
    flags = run_both(conn)
    path = tmp_path / "r.csv"
    dedup.write_report(flags, path)
    rows = read_report(path)[1:]
    assert len(rows) == len(flags) == 2
    assert {r[0] for r in rows} == {"<2@thyme>", "<3@thyme>"}


def test_report_scores_are_never_below_the_threshold(conn, tmp_path):
    a, b = ratio_pair(90)
    seed(conn, email(1, body=a), email(2, body=b), email(3, subject="x", body="z" * 50), email(4, subject="x", body="y" * 50))
    flags = run_both(conn)
    path = tmp_path / "r.csv"
    dedup.write_report(flags, path)
    assert all(float(r[6]) >= 90.0 for r in read_report(path)[1:])
