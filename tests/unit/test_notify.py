"""Tests for notify.py and the notification stage in main.py (D-022, amended by D-024).

Layout of this file:
- Rendering: the body is checked character for character against the template
  in docs/assignment.md (section 4.2). One test reads the quoted template out
  of the spec file and fills in the placeholders; another compares against a
  hand-written literal, so the two guard each other.
- Dry run: DryRunSender writes real RFC 822 drafts that email.parser reads back.
- Live: which flags are chosen (latest per group, capped by --limit), who the
  real recipient is, and that live mode refuses to start without NOTIFY_TO.
- Stage: main.run_notifications ties notify to the database. notify itself
  never touches the DB, so the "notification_sent" tests go through that
  function, with an in-memory DB seeded with flagged rows and a fake sender
  that records its calls.

main.py lives at the repo root, outside the package, so it is loaded by path.
Nothing here sends a real email or touches the dataset.
"""
import csv
import email
import importlib.util
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from enron_pipeline import db, notify
from enron_pipeline.models import (
    DuplicateFlag,
    EmailRecord,
    SendResult,
    SendStatus,
)
from enron_pipeline.notify import (
    DryRunSender,
    McpSender,
    OutgoingEmail,
    Sender,
    render_notification,
    select_live_flags,
    send_notifications,
    write_send_log,
)

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("pipeline_main", ROOT / "main.py")
main_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main_module)

NOW = datetime(2026, 10, 11, 18, 30, 5, tzinfo=timezone.utc)
ORIGINAL_DATE = datetime(2001, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
DUPLICATE_DATE = datetime(2001, 5, 14, 23, 39, 0, tzinfo=timezone.utc)
SENDER = "phillip.allen@enron.com"
LOG_COLUMNS = ["timestamp", "recipient", "subject", "status", "error"]


def make_flag(
    n=1,
    original="<orig@thyme>",
    sender=SENDER,
    subject="Q2 numbers",
    score=97.5,
    duplicate_date=DUPLICATE_DATE,
    original_date=ORIGINAL_DATE,
    original_subject=None,
) -> DuplicateFlag:
    """A flag. `subject` is the flagged email's; `original_subject` defaults to the same."""
    return DuplicateFlag(
        duplicate_message_id=f"<dup{n}@thyme>",
        original_message_id=original,
        subject=subject,
        original_subject=subject if original_subject is None else original_subject,
        from_address=sender,
        duplicate_date=duplicate_date,
        original_date=original_date,
        similarity_score=score,
    )


# --- the spec template -----------------------------------------------------------------


def spec_template() -> tuple[list[str], list[str]]:
    """The notification template from docs/assignment.md, section 4.2.

    Returns (header_lines, body_lines) with the markdown escapes removed:
    "\\<x\\>" -> "<x>", "\\[x\\]" -> "[x]". The spec shows a blank line between
    every template line; blank lines are kept in the body and dropped from the
    four header lines (blank lines cannot separate email headers).
    """
    text = (ROOT / "docs" / "assignment.md").read_text()
    after = text[text.index("4.2 Notification Email Logic"):]
    quoted, started = [], False
    for line in after.splitlines():
        if line.startswith(">"):
            started = True
            quoted.append("" if line == ">" else re.sub(r"\\(.)", r"\1", line[2:]))
        elif started:
            break
    headers = [l for l in quoted[:7] if l]  # To, Subject, Date, References
    body = quoted[quoted.index(headers[3]) + 2:]  # skip the References line and its blank
    assert [h.split(":")[0] for h in headers] == ["To", "Subject", "Date", "References"]
    return headers, body


def fill(template_lines: list[str], flag: DuplicateFlag) -> list[str]:
    """Replace the spec's <placeholders> with the values for `flag`."""
    values = {
        "<sender_of_latest_duplicate>": flag.from_address,
        "<original_subject>": flag.original_subject,
        "<message_id_of_latest_duplicate>": flag.duplicate_message_id,
        "<date_of_latest_duplicate>": "2001-05-14T23:39:00Z",
        "<subject>": flag.subject,
        "<message_id_of_earliest_original>": flag.original_message_id,
        "<date_of_earliest_original>": "2001-05-10T09:00:00Z",
        "<similarity_percentage>": f"{flag.similarity_score:.2f}",
    }
    filled = []
    for line in template_lines:
        for placeholder, value in values.items():
            line = line.replace(placeholder, value)
        filled.append(line)
    return filled


def test_the_spec_template_is_found_and_has_the_expected_shape():
    headers, body = spec_template()
    assert headers[0] == "To: <sender_of_latest_duplicate>"
    assert headers[1] == "Subject: [Duplicate Notice] Re: <original_subject>"
    assert body[0] == "This is an automated notification from the Email Deduplication System."
    assert body[-1] == "No action is required if this is indeed a duplicate."
    assert len([l for l in body if l]) == 13


def test_body_matches_the_spec_template_character_for_character():
    flag = make_flag()
    _, template_body = spec_template()

    message = render_notification(flag, NOW)

    assert message.body == "\n".join(fill(template_body, flag)) + "\n"


def test_headers_match_the_spec_template():
    flag = make_flag()
    template_headers, _ = spec_template()
    filled = fill(template_headers, flag)

    message = render_notification(flag, NOW)

    assert filled[0] == f"To: {message.to}"
    assert filled[1] == f"Subject: {message.subject}"
    assert filled[3] == f"References: {message.references}"


def test_body_matches_a_hand_written_literal():
    flag = make_flag(subject="Q2 numbers", score=97.5)
    expected = (
        "This is an automated notification from the Email Deduplication System.\n"
        "\n"
        "Your email has been identified as a potential duplicate:\n"
        "\n"
        "Your Email (Flagged):\n"
        "\n"
        "Message-ID: <dup1@thyme>\n"
        "\n"
        "Date Sent: 2001-05-14T23:39:00Z\n"
        "\n"
        "Subject: Q2 numbers\n"
        "\n"
        "Original Email on Record:\n"
        "\n"
        "Message-ID: <orig@thyme>\n"
        "\n"
        "Date Sent: 2001-05-10T09:00:00Z\n"
        "\n"
        "Similarity Score: 97.50%\n"
        "\n"
        "If this was NOT a duplicate and you intended to send this email,\n"
        "\n"
        "please reply with CONFIRM to restore it to active status.\n"
        "\n"
        "No action is required if this is indeed a duplicate.\n"
    )
    assert render_notification(flag, NOW).body == expected


# --- rendered fields --------------------------------------------------------------------


def test_subject_has_the_notice_prefix_then_re_then_the_subject():
    message = render_notification(make_flag(subject="Q2 numbers"), NOW)
    assert message.subject == "[Duplicate Notice] Re: Q2 numbers"


@pytest.mark.parametrize(
    "original, expected",
    [
        ("X", "[Duplicate Notice] Re: X"),
        ("Re: X", "[Duplicate Notice] Re: X"),  # no second "Re:" (D-024 item 1)
        ("RE: X", "[Duplicate Notice] RE: X"),  # any case, kept as written
        ("re: X", "[Duplicate Notice] re: X"),
        ("  Re: X", "[Duplicate Notice] Re: X"),  # leading spaces ignored
        ("Re:X", "[Duplicate Notice] Re:X"),
        ("Re: Re: X", "[Duplicate Notice] Re: Re: X"),  # nothing is added or removed
        ("Regarding X", "[Duplicate Notice] Re: Regarding X"),  # starts with "re", not "re:"
        ("Real estate", "[Duplicate Notice] Re: Real estate"),
        ("Fwd: X", "[Duplicate Notice] Re: Fwd: X"),
        ("", "[Duplicate Notice] Re: "),
    ],
)
def test_subject_is_the_original_subject_with_one_re(original, expected):
    assert render_notification(make_flag(original_subject=original), NOW).subject == expected


def test_subject_comes_from_the_original_not_from_the_flagged_email():
    # The flagged email is a "Re: ..." reply of the original; the notice must not say "Re: Re:".
    flag = make_flag(subject="Re: Q2 numbers", original_subject="Q2 numbers")
    assert render_notification(flag, NOW).subject == "[Duplicate Notice] Re: Q2 numbers"
    other = make_flag(subject="Q2 numbers", original_subject="Budget draft")
    assert render_notification(other, NOW).subject == "[Duplicate Notice] Re: Budget draft"


def test_the_body_subject_line_stays_the_flagged_emails_own_subject():
    flag = make_flag(subject="Re: Q2 numbers", original_subject="Q2 numbers")
    body = render_notification(flag, NOW).body
    assert "Your Email (Flagged):\n\nMessage-ID: <dup1@thyme>\n\nDate Sent: 2001-05-14T23:39:00Z\n\nSubject: Re: Q2 numbers\n\n" in body
    assert "Subject: Q2 numbers" not in body.replace("Subject: Re: Q2 numbers", "")


def test_a_draft_subject_uses_the_original_subject(tmp_path):
    flag = make_flag(subject="Re: Q2 numbers", original_subject="Q2 numbers")
    send_notifications([flag], DryRunSender(tmp_path), now=NOW)
    [draft] = tmp_path.glob("*.eml")
    assert read_draft(draft)["Subject"] == "[Duplicate Notice] Re: Q2 numbers"


def test_blank_subject_still_renders():
    message = render_notification(make_flag(subject=""), NOW)
    assert message.subject == "[Duplicate Notice] Re: "


def test_references_is_the_flagged_message_id():
    message = render_notification(make_flag(n=7), NOW)
    assert message.references == "<dup7@thyme>"


def test_to_is_the_sender_of_the_flagged_duplicate():
    message = render_notification(make_flag(sender="jane.doe@enron.com"), NOW)
    assert message.to == "jane.doe@enron.com"


def test_date_is_the_send_time_passed_in():
    assert render_notification(make_flag(), NOW).date == NOW


@pytest.mark.parametrize(
    "score, shown",
    [(97.5, "97.50%"), (90.0, "90.00%"), (100.0, "100.00%"), (97.456, "97.46%"), (91, "91.00%")],
)
def test_similarity_has_two_decimals_and_a_percent_sign(score, shown):
    body = render_notification(make_flag(score=score), NOW).body
    assert f"Similarity Score: {shown}\n" in body


def test_non_utc_dates_are_shown_as_utc():
    pacific = timezone(timedelta(hours=-7))
    flag = make_flag(duplicate_date=datetime(2001, 5, 14, 16, 39, tzinfo=pacific))
    assert "Date Sent: 2001-05-14T23:39:00Z" in render_notification(flag, NOW).body


def test_a_newline_in_the_subject_cannot_break_the_header():
    message = render_notification(make_flag(original_subject="two\r\nlines"), NOW)
    assert "\n" not in message.subject and "\r" not in message.subject


# --- dry run: .eml drafts ------------------------------------------------------------------


def read_draft(path: Path):
    return email.message_from_bytes(path.read_bytes(), policy=policy.default)


def test_dry_run_writes_one_eml_per_flagged_duplicate(tmp_path):
    flags = [make_flag(n) for n in (1, 2, 3)]
    results = send_notifications(flags, DryRunSender(tmp_path / "replies"), now=NOW)
    drafts = sorted((tmp_path / "replies").glob("*.eml"))
    assert len(drafts) == 3
    assert len(results) == 3


def test_drafts_are_named_n_underscore_sanitized_message_id(tmp_path):
    flags = [
        make_flag(1),
        DuplicateFlag(
            duplicate_message_id="<1398971.1075845390651.JavaMail.evans@thyme>",
            original_message_id="<orig@thyme>",
            subject="s",
            original_subject="s",
            from_address=SENDER,
            duplicate_date=DUPLICATE_DATE,
            original_date=ORIGINAL_DATE,
            similarity_score=95.0,
        ),
    ]
    send_notifications(flags, DryRunSender(tmp_path), now=NOW)
    assert sorted(p.name for p in tmp_path.glob("*.eml")) == [
        "0001_dup1_thyme.eml",
        "0002_1398971.1075845390651.JavaMail.evans_thyme.eml",
    ]


def test_unsafe_characters_in_a_message_id_never_reach_the_file_name(tmp_path):
    flag = DuplicateFlag(
        duplicate_message_id="<a b/c\\d:e*f?g\"h|i@x>",
        original_message_id="<o@x>",
        subject="s",
        original_subject="s",
        from_address=SENDER,
        duplicate_date=DUPLICATE_DATE,
        original_date=ORIGINAL_DATE,
        similarity_score=95.0,
    )
    send_notifications([flag], DryRunSender(tmp_path), now=NOW)
    [draft] = tmp_path.glob("*.eml")  # exactly one file, directly in tmp_path
    assert re.fullmatch(r"0001_[A-Za-z0-9._-]+\.eml", draft.name)
    assert draft.parent == tmp_path


def test_two_flags_with_the_same_message_id_get_different_files(tmp_path):
    send_notifications([make_flag(1), make_flag(1)], DryRunSender(tmp_path), now=NOW)
    assert len(list(tmp_path.glob("*.eml"))) == 2


def test_a_draft_reads_back_with_the_same_to_subject_and_references(tmp_path):
    flag = make_flag(n=4, sender="jane.doe@enron.com", subject="Budget review")
    send_notifications([flag], DryRunSender(tmp_path), now=NOW)

    [draft] = tmp_path.glob("*.eml")
    parsed = read_draft(draft)

    assert parsed["To"] == "jane.doe@enron.com"
    assert parsed["Subject"] == "[Duplicate Notice] Re: Budget review"
    assert parsed["References"] == "<dup4@thyme>"
    assert parsedate_to_datetime(parsed["Date"]) == NOW


def test_a_draft_body_is_the_rendered_body(tmp_path):
    flag = make_flag()
    send_notifications([flag], DryRunSender(tmp_path), now=NOW)
    [draft] = tmp_path.glob("*.eml")
    assert read_draft(draft).get_content() == render_notification(flag, NOW).body


def test_a_draft_is_a_single_plain_text_message(tmp_path):
    send_notifications([make_flag()], DryRunSender(tmp_path), now=NOW)
    [draft] = tmp_path.glob("*.eml")
    parsed = read_draft(draft)
    assert not parsed.is_multipart()
    assert parsed.get_content_type() == "text/plain"
    assert parsed.defects == []


@pytest.mark.parametrize(
    "sender", ["m..presto@enron.com", "paul.y'barbo@enron.com", "a+b@enron.com"]
)
def test_awkward_enron_addresses_still_make_a_valid_draft(tmp_path, sender):
    send_notifications([make_flag(sender=sender)], DryRunSender(tmp_path), now=NOW)
    [draft] = tmp_path.glob("*.eml")
    assert read_draft(draft)["To"] == sender


def test_a_non_ascii_subject_survives_the_round_trip(tmp_path):
    flag = make_flag(subject="Réunion budget \u2013 Q2")
    send_notifications([flag], DryRunSender(tmp_path), now=NOW)
    [draft] = tmp_path.glob("*.eml")
    parsed = read_draft(draft)
    assert parsed["Subject"] == "[Duplicate Notice] Re: Réunion budget \u2013 Q2"
    assert "Subject: Réunion budget \u2013 Q2" in parsed.get_content()


def test_dry_run_result_says_dry_run_and_names_the_enron_sender(tmp_path):
    [result] = send_notifications([make_flag(n=2)], DryRunSender(tmp_path), now=NOW)
    assert result == SendResult(
        timestamp=NOW,
        recipient=SENDER,
        subject="[Duplicate Notice] Re: Q2 numbers",
        status=SendStatus.DRY_RUN,
        error=None,
        duplicate_message_id="<dup2@thyme>",
    )


def test_dry_run_sender_creates_its_folder(tmp_path):
    folder = tmp_path / "output" / "replies"
    DryRunSender(folder)
    assert folder.is_dir()


def test_dry_run_sender_clears_old_drafts_but_nothing_else(tmp_path):
    (tmp_path / "0007_old.eml").write_text("stale draft from an earlier run")
    (tmp_path / "12_older.eml").write_text("stale draft")
    (tmp_path / "notes.eml").write_text("not one of ours")
    (tmp_path / "README.md").write_text("keep")

    DryRunSender(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["README.md", "notes.eml"]


def test_dry_run_with_no_flags_leaves_an_empty_folder(tmp_path):
    (tmp_path / "0001_old.eml").write_text("stale")
    results = send_notifications([], DryRunSender(tmp_path), now=NOW)
    assert results == []
    assert list(tmp_path.iterdir()) == []


# --- live: which flags, and to whom ------------------------------------------------------------


def group_of(original, dup_numbers, base_day=14):
    """Flags for one group: duplicate k is dated base_day + k, so a higher k is later."""
    return [
        make_flag(
            n,
            original=original,
            duplicate_date=datetime(2001, 5, base_day + n % 10, tzinfo=timezone.utc),
        )
        for n in dup_numbers
    ]


def test_live_picks_only_the_latest_duplicate_of_each_group():
    flags = group_of("<a@thyme>", [1, 2, 3]) + group_of("<b@thyme>", [4]) + group_of("<c@thyme>", [5, 6])
    chosen = select_live_flags(flags, limit=10)
    assert sorted(f.duplicate_message_id for f in chosen) == ["<dup3@thyme>", "<dup4@thyme>", "<dup6@thyme>"]


def test_the_latest_is_decided_by_date_not_by_list_order():
    early = make_flag(1, original="<a@thyme>", duplicate_date=datetime(2001, 5, 20, tzinfo=timezone.utc))
    late = make_flag(2, original="<a@thyme>", duplicate_date=datetime(2001, 6, 20, tzinfo=timezone.utc))
    assert select_live_flags([late, early], 10) == [late]
    assert select_live_flags([early, late], 10) == [late]


def test_a_date_tie_is_broken_by_message_id_so_the_choice_is_stable():
    same = datetime(2001, 5, 20, tzinfo=timezone.utc)
    a = make_flag(1, original="<g@thyme>", duplicate_date=same)
    b = make_flag(2, original="<g@thyme>", duplicate_date=same)
    assert select_live_flags([a, b], 10) == [b]
    assert select_live_flags([b, a], 10) == [b]


def test_a_group_of_three_gives_one_live_send():
    # An original plus two flagged duplicates: 2 drafts in dry run, 1 live send.
    flags = group_of("<a@thyme>", [1, 2])
    assert len(select_live_flags(flags, 10)) == 1


def test_limit_caps_the_number_of_live_sends():
    flags = [f for g in range(5) for f in group_of(f"<g{g}@thyme>", [g * 10 + 1])]
    assert len(select_live_flags(flags, limit=2)) == 2
    assert len(select_live_flags(flags, limit=5)) == 5
    assert len(select_live_flags(flags, limit=99)) == 5
    assert select_live_flags(flags, limit=0) == []


def test_the_limit_counts_groups_not_flags():
    flags = group_of("<a@thyme>", [1, 2, 3]) + group_of("<b@thyme>", [4, 5, 6])
    assert len(select_live_flags(flags, limit=2)) == 2  # both groups, one flag each


def test_selection_follows_the_report_order_of_the_groups():
    # Groups are taken in order of their original's date, like duplicates_report.csv,
    # so a small --limit always means "the first N groups of the report".
    late_group = make_flag(1, original="<late@thyme>", original_date=datetime(2001, 3, 1, tzinfo=timezone.utc))
    early_group = make_flag(2, original="<early@thyme>", original_date=datetime(2001, 1, 1, tzinfo=timezone.utc))
    assert select_live_flags([late_group, early_group], limit=1) == [early_group]


def test_a_negative_limit_is_rejected():
    with pytest.raises(ValueError, match="limit"):
        select_live_flags([make_flag()], limit=-1)


def test_selecting_does_not_change_the_input():
    flags = group_of("<a@thyme>", [1, 2, 3])
    before = list(flags)
    select_live_flags(flags, 10)
    assert flags == before


@pytest.mark.parametrize("value", [None, "", "   ", "not-an-address"])
def test_live_mode_refuses_to_start_without_a_usable_notify_to(value):
    with pytest.raises(ValueError, match="NOTIFY_TO"):
        McpSender(value)


def test_the_mcp_sender_keeps_the_notify_to_it_was_given():
    assert McpSender(" me@gmail.com ").notify_to == "me@gmail.com"


def test_creating_the_mcp_sender_starts_nothing():
    # The connection opens on the first send, so building the sender (which
    # main does before any work) neither spawns a server nor needs Node.
    sender = McpSender("me@gmail.com")
    sender.close()  # closing a sender that never connected is a no-op


class FakeSender:
    """Records every message. Fails for the message ids in `fail_for`.

    `deliver_to` stands in for the live sender's NOTIFY_TO: the message keeps
    the Enron sender in `to`, and the result reports where it really went.
    """

    def __init__(self, fail_for=(), deliver_to=None, raise_for=()):
        self.messages: list[OutgoingEmail] = []
        self.fail_for = set(fail_for)
        self.raise_for = set(raise_for)
        self.deliver_to = deliver_to

    def send(self, message: OutgoingEmail) -> SendResult:
        self.messages.append(message)
        if message.references in self.raise_for:
            raise RuntimeError("boom")
        failed = message.references in self.fail_for
        return SendResult(
            timestamp=message.date,
            recipient=self.deliver_to or message.to,
            subject=message.subject,
            status=SendStatus.FAILED if failed else SendStatus.SENT,
            error="smtp refused" if failed else None,
            duplicate_message_id=message.references,
        )


# --- send_notifications -------------------------------------------------------------------------


def test_every_flag_is_rendered_and_handed_to_the_sender_in_order():
    sender = FakeSender()
    flags = [make_flag(n) for n in (3, 1, 2)]
    results = send_notifications(flags, sender, now=NOW)
    assert [m.references for m in sender.messages] == ["<dup3@thyme>", "<dup1@thyme>", "<dup2@thyme>"]
    assert [r.duplicate_message_id for r in results] == ["<dup3@thyme>", "<dup1@thyme>", "<dup2@thyme>"]
    assert sender.messages[0] == render_notification(flags[0], NOW)


def test_a_sender_that_raises_becomes_a_failed_result_and_the_run_continues():
    sender = FakeSender(raise_for={"<dup2@thyme>"})
    results = send_notifications([make_flag(1), make_flag(2), make_flag(3)], sender, now=NOW)
    assert [r.status for r in results] == [SendStatus.SENT, SendStatus.FAILED, SendStatus.SENT]
    failed = results[1]
    assert failed.error == "RuntimeError: boom"
    assert failed.duplicate_message_id == "<dup2@thyme>"
    assert failed.recipient == SENDER
    assert failed.subject == "[Duplicate Notice] Re: Q2 numbers"
    assert len(sender.messages) == 3


def test_a_flag_that_cannot_be_rendered_is_a_failed_result_and_the_run_continues():
    broken = make_flag(1, duplicate_date=datetime(2001, 5, 14, 23, 39))  # naive datetime
    sender = FakeSender()
    results = send_notifications([broken, make_flag(2)], sender, now=NOW)
    assert [r.status for r in results] == [SendStatus.FAILED, SendStatus.SENT]
    assert results[0].duplicate_message_id == "<dup1@thyme>"
    assert results[0].error
    assert len(sender.messages) == 1


def test_a_failed_result_returned_by_the_sender_is_kept_as_is():
    sender = FakeSender(fail_for={"<dup1@thyme>"})
    [result] = send_notifications([make_flag(1)], sender, now=NOW)
    assert result.status is SendStatus.FAILED
    assert result.error == "smtp refused"


def test_send_notifications_never_raises():
    class Hopeless:
        def send(self, message):
            raise ValueError("always")

    results = send_notifications([make_flag(n) for n in range(5)], Hopeless(), now=NOW)
    assert [r.status for r in results] == [SendStatus.FAILED] * 5


def test_without_now_the_send_time_is_the_current_time_in_whole_seconds():
    # Whole seconds: the Date header has no fractions, and the database and
    # send_log.csv store times as plain "...:05Z" text.
    before = datetime.now(timezone.utc).replace(microsecond=0)
    sender = FakeSender()
    send_notifications([make_flag()], sender)
    after = datetime.now(timezone.utc)
    sent_at = sender.messages[0].date
    assert before <= sent_at <= after
    assert sent_at.microsecond == 0
    assert sent_at.utcoffset() == timedelta(0)


def test_no_flags_means_no_sends():
    sender = FakeSender()
    assert send_notifications([], sender, now=NOW) == []
    assert sender.messages == []


def test_the_sender_interface_is_a_protocol_any_object_with_send_fits():
    # Structural typing: FakeSender does not inherit from notify.Sender, yet is
    # accepted by send_notifications (every test above relies on that).
    assert Protocol in notify.Sender.__mro__
    assert Sender not in FakeSender.__mro__


# --- send_log.csv ---------------------------------------------------------------------------------


def read_log(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames), list(reader)


def test_send_log_has_the_exact_columns_and_one_row_per_attempt(tmp_path):
    sender = FakeSender(fail_for={"<dup2@thyme>"})
    results = send_notifications([make_flag(1), make_flag(2), make_flag(3)], sender, now=NOW)

    write_send_log(results, tmp_path / "output" / "send_log.csv")  # folder does not exist yet

    columns, rows = read_log(tmp_path / "output" / "send_log.csv")
    assert columns == LOG_COLUMNS
    assert len(rows) == 3
    assert [r["status"] for r in rows] == ["sent", "failed", "sent"]
    assert rows[0] == {
        "timestamp": "2026-10-11T18:30:05Z",
        "recipient": SENDER,
        "subject": "[Duplicate Notice] Re: Q2 numbers",
        "status": "sent",
        "error": "",
    }
    assert rows[1]["error"] == "smtp refused"


def test_send_log_with_no_attempts_is_just_the_header(tmp_path):
    write_send_log([], tmp_path / "send_log.csv")
    assert (tmp_path / "send_log.csv").read_text().strip() == ",".join(LOG_COLUMNS)


def test_send_log_quotes_commas_and_quotes_in_a_subject_and_error(tmp_path):
    flag = make_flag(subject='Q2, "final" numbers')
    sender = FakeSender(raise_for={"<dup1@thyme>"})
    results = send_notifications([flag], sender, now=NOW)
    write_send_log(results, tmp_path / "send_log.csv")
    _, [row] = read_log(tmp_path / "send_log.csv")
    assert row["subject"] == '[Duplicate Notice] Re: Q2, "final" numbers'
    assert row["error"] == "RuntimeError: boom"


def test_send_log_is_rewritten_not_appended(tmp_path):
    path = tmp_path / "send_log.csv"
    write_send_log(send_notifications([make_flag(1), make_flag(2)], FakeSender(), now=NOW), path)
    write_send_log(send_notifications([make_flag(3)], FakeSender(), now=NOW), path)
    _, rows = read_log(path)
    assert len(rows) == 1


def test_append_adds_rows_after_the_existing_ones_with_one_header(tmp_path):
    path = tmp_path / "send_log.csv"
    write_send_log(send_notifications([make_flag(1), make_flag(2)], FakeSender(), now=NOW), path, append=True)
    first_bytes = path.read_bytes()
    write_send_log(send_notifications([make_flag(3)], FakeSender(fail_for={"<dup3@thyme>"}), now=NOW), path, append=True)
    columns, rows = read_log(path)
    assert columns == LOG_COLUMNS
    assert [r["status"] for r in rows] == ["sent", "sent", "failed"]
    assert path.read_bytes().startswith(first_bytes)  # earlier rows are untouched
    assert path.read_text().count("timestamp,recipient") == 1


def test_append_to_a_missing_file_creates_it_with_the_header(tmp_path):
    path = tmp_path / "output" / "send_log.csv"  # folder does not exist yet
    write_send_log([], path, append=True)
    assert path.read_text().strip() == ",".join(LOG_COLUMNS)


def test_append_to_an_empty_existing_file_still_writes_the_header(tmp_path):
    path = tmp_path / "send_log.csv"
    path.write_text("")
    write_send_log(send_notifications([make_flag(1)], FakeSender(), now=NOW), path, append=True)
    columns, rows = read_log(path)
    assert columns == LOG_COLUMNS and len(rows) == 1


def test_appending_nothing_leaves_an_existing_log_unchanged(tmp_path):
    path = tmp_path / "send_log.csv"
    write_send_log(send_notifications([make_flag(1)], FakeSender(), now=NOW), path, append=True)
    before = path.read_bytes()
    write_send_log([], path, append=True)
    assert path.read_bytes() == before


def test_send_log_timestamps_are_utc_iso_text(tmp_path):
    pacific = timezone(timedelta(hours=-7))
    result = SendResult(
        timestamp=datetime(2026, 10, 11, 11, 30, 5, tzinfo=pacific),
        recipient="me@gmail.com",
        subject="s",
        status="sent",
        error=None,
        duplicate_message_id="<d@x>",
    )
    write_send_log([result], tmp_path / "send_log.csv")
    _, [row] = read_log(tmp_path / "send_log.csv")
    assert row["timestamp"] == "2026-10-11T18:30:05Z"


# --- the stage: notify + database (main.run_notifications) ------------------------------------------


def seed(conn, flags):
    """Insert an in-memory DB's rows for these flags: each original once, each flagged duplicate flagged."""
    seen = set()
    n = 0
    for flag in flags:
        if flag.original_message_id not in seen:
            seen.add(flag.original_message_id)
            n += 1
            db.insert_email(
                conn,
                EmailRecord(
                    message_id=flag.original_message_id,
                    date=flag.original_date,
                    from_address=flag.from_address,
                    subject=flag.subject,
                    body="body",
                    source_file=f"maildir/x/original{n}",
                    mailbox="x",
                ),
            )
    for flag in flags:
        n += 1
        db.insert_email(
            conn,
            EmailRecord(
                message_id=flag.duplicate_message_id,
                date=flag.duplicate_date,
                from_address=flag.from_address,
                subject=flag.subject,
                body="body",
                source_file=f"maildir/x/dup{n}",
                mailbox="x",
                is_duplicate=True,
                duplicate_of=flag.original_message_id,
                similarity_score=flag.similarity_score,
            ),
        )


@pytest.fixture
def conn():
    connection = db.connect()
    db.init_schema(connection)
    yield connection
    connection.close()


def notification_state(conn) -> dict:
    return {
        message_id: (sent, date)
        for message_id, sent, date in conn.execute(
            "SELECT message_id, notification_sent, notification_date FROM emails"
        )
    }


def stage(conn, flags, sender, tmp_path, live=False, limit=10):
    return main_module.run_notifications(
        conn, flags, sender, live=live, limit=limit, send_log=tmp_path / "send_log.csv"
    )


def test_dry_run_flags_every_duplicate_and_marks_nothing_as_notified(conn, tmp_path):
    flags = group_of("<a@thyme>", [1, 2, 3]) + group_of("<b@thyme>", [4])
    seed(conn, flags)

    results = stage(conn, flags, DryRunSender(tmp_path / "replies"), tmp_path)

    assert len(list((tmp_path / "replies").glob("*.eml"))) == 4
    assert [r.status for r in results] == [SendStatus.DRY_RUN] * 4
    assert all(sent == 0 and date is None for sent, date in notification_state(conn).values())


def test_a_group_of_three_gives_two_drafts_and_one_live_send(conn, tmp_path):
    # One original with two flagged duplicates.
    flags = group_of("<a@thyme>", [1, 2])
    seed(conn, flags)

    dry = stage(conn, flags, DryRunSender(tmp_path / "replies"), tmp_path)
    assert len(dry) == 2
    assert len(list((tmp_path / "replies").glob("*.eml"))) == 2

    live_sender = FakeSender()
    live = stage(conn, flags, live_sender, tmp_path, live=True)
    assert len(live) == 1
    assert len(live_sender.messages) == 1
    assert live_sender.messages[0].references == "<dup2@thyme>"  # the later of the two


def test_live_sends_the_template_to_the_enron_sender_but_delivers_to_notify_to(conn, tmp_path):
    flags = [make_flag(1, sender="jane.doe@enron.com")]
    seed(conn, flags)
    sender = FakeSender(deliver_to="me@gmail.com")

    [result] = stage(conn, flags, sender, tmp_path, live=True)

    assert sender.messages[0].to == "jane.doe@enron.com"  # the template's To line
    assert result.recipient == "me@gmail.com"  # where it really went
    _, [row] = read_log(tmp_path / "send_log.csv")
    assert row["recipient"] == "me@gmail.com"


def test_live_limit_caps_sends_at_the_stage_level(conn, tmp_path):
    flags = [f for g in range(5) for f in group_of(f"<g{g}@thyme>", [g * 10 + 1])]
    seed(conn, flags)
    sender = FakeSender()

    results = stage(conn, flags, sender, tmp_path, live=True, limit=3)

    assert len(results) == 3
    assert len(sender.messages) == 3
    assert sum(sent for sent, _ in notification_state(conn).values()) == 3


def test_limit_does_not_cap_dry_run_drafts(conn, tmp_path):
    flags = [f for g in range(5) for f in group_of(f"<g{g}@thyme>", [g * 10 + 1])]
    seed(conn, flags)
    stage(conn, flags, DryRunSender(tmp_path / "replies"), tmp_path, live=False, limit=1)
    assert len(list((tmp_path / "replies").glob("*.eml"))) == 5


def test_success_sets_notification_sent_and_the_send_time_on_the_duplicate_row(conn, tmp_path):
    flags = group_of("<a@thyme>", [1, 2])  # dup2 is the later one
    seed(conn, flags)

    stage(conn, flags, FakeSender(), tmp_path, live=True)

    state = notification_state(conn)
    sent_at = [(mid, date) for mid, (sent, date) in state.items() if sent == 1]
    [(message_id, date)] = sent_at
    assert message_id == "<dup2@thyme>"
    # The fake sender stamps results with the message's Date header = the real send time.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", date)
    assert state["<a@thyme>"] == (0, None)  # the original is untouched
    assert state["<dup1@thyme>"] == (0, None)  # the earlier duplicate is untouched


def test_the_notification_date_is_the_time_the_result_reports(conn, tmp_path):
    flags = [make_flag(1)]
    seed(conn, flags)

    class FixedClockSender(FakeSender):
        def send(self, message):
            result = super().send(message)
            return SendResult(
                timestamp=NOW, recipient=result.recipient, subject=result.subject,
                status=result.status, error=None, duplicate_message_id=result.duplicate_message_id,
            )

    stage(conn, flags, FixedClockSender(), tmp_path, live=True)

    assert notification_state(conn)["<dup1@thyme>"] == (1, "2026-10-11T18:30:05Z")


def test_a_failed_send_is_logged_the_run_continues_and_the_row_stays_unnotified(conn, tmp_path):
    flags = [f for g in range(3) for f in group_of(f"<g{g}@thyme>", [g + 1])]  # dup1, dup2, dup3
    seed(conn, flags)
    sender = FakeSender(fail_for={"<dup2@thyme>"})

    results = stage(conn, flags, sender, tmp_path, live=True)

    assert [r.status for r in results] == [SendStatus.SENT, SendStatus.FAILED, SendStatus.SENT]
    assert len(sender.messages) == 3  # the run went on past the failure
    state = notification_state(conn)
    assert state["<dup1@thyme>"][0] == 1
    assert state["<dup2@thyme>"] == (0, None)
    assert state["<dup3@thyme>"][0] == 1
    _, rows = read_log(tmp_path / "send_log.csv")
    assert [r["status"] for r in rows] == ["sent", "failed", "sent"]
    assert rows[1]["error"] == "smtp refused"


def test_a_sender_that_raises_leaves_the_row_unnotified(conn, tmp_path):
    flags = [make_flag(1)]
    seed(conn, flags)
    [result] = stage(conn, flags, FakeSender(raise_for={"<dup1@thyme>"}), tmp_path, live=True)
    assert result.status is SendStatus.FAILED
    assert notification_state(conn)["<dup1@thyme>"] == (0, None)


def test_the_stage_logs_every_attempt_for_a_dry_run_too(conn, tmp_path):
    flags = group_of("<a@thyme>", [1, 2, 3])
    seed(conn, flags)
    stage(conn, flags, DryRunSender(tmp_path / "replies"), tmp_path)
    columns, rows = read_log(tmp_path / "send_log.csv")
    assert columns == LOG_COLUMNS
    assert [r["status"] for r in rows] == ["dry_run"] * 3
    assert all(r["recipient"] == SENDER for r in rows)


def test_the_stage_with_no_flags_writes_an_empty_log_and_sends_nothing(conn, tmp_path):
    sender = FakeSender()
    assert stage(conn, [], sender, tmp_path, live=True) == []
    assert sender.messages == []
    assert (tmp_path / "send_log.csv").read_text().strip() == ",".join(LOG_COLUMNS)


# --- the stage and the two logs (D-024 item 7) -----------------------------------------------------


def test_a_live_stage_appends_to_its_log_across_runs(conn, tmp_path):
    flags = group_of("<a@thyme>", [1, 2])
    seed(conn, flags)
    stage(conn, flags, FakeSender(deliver_to="me@gmail.com"), tmp_path, live=True)
    stage(conn, flags, FakeSender(deliver_to="me@gmail.com", fail_for={"<dup2@thyme>"}), tmp_path, live=True)
    columns, rows = read_log(tmp_path / "send_log.csv")
    assert columns == LOG_COLUMNS
    # One send per run (the group's latest duplicate), and the second run adds to the first.
    assert [r["status"] for r in rows] == ["sent", "failed"]
    assert (tmp_path / "send_log.csv").read_text().count("timestamp,recipient") == 1


def test_a_dry_stage_rewrites_its_log_each_run(conn, tmp_path):
    flags = group_of("<a@thyme>", [1, 2, 3])
    seed(conn, flags)
    stage(conn, flags, DryRunSender(tmp_path / "replies"), tmp_path)
    stage(conn, flags[:1], DryRunSender(tmp_path / "replies"), tmp_path)
    _, rows = read_log(tmp_path / "send_log.csv")
    assert [r["status"] for r in rows] == ["dry_run"]
