"""Tests for models.py: records build with valid fields and reject bad ones."""
import dataclasses
from datetime import datetime, timezone

import pytest

from enron_pipeline import models
from enron_pipeline.models import (
    DuplicateFlag,
    EmailRecord,
    ParseError,
    ParseFailure,
    Recipient,
    SendResult,
)

NOW = datetime(2001, 5, 14, 16, 39, tzinfo=timezone.utc)

MANDATORY = dict(
    message_id="<1@enron.com>",
    date=NOW,
    from_address="phillip.allen@enron.com",
    subject="Hello",
    body="Body text",
    source_file="maildir/allen-p/inbox/1",
    mailbox="allen-p",
)


def test_email_record_builds_with_mandatory_fields_only():
    record = EmailRecord(**MANDATORY)
    assert record.message_id == "<1@enron.com>"
    # optional and DB-managed fields take their defaults
    assert record.recipients == ()
    assert record.x_from is None
    assert record.has_attachment is False
    assert record.is_duplicate is False
    assert record.notification_sent is False
    assert record.id is None


def test_email_record_builds_with_all_fields():
    record = EmailRecord(
        **MANDATORY,
        recipients=[Recipient("a@enron.com", "to", 0)],
        date_raw="Mon, 14 May 2001 09:39:00 -0700 (PDT)",
        x_from="Phillip Allen",
        x_to="A",
        x_cc="B",
        x_bcc="C",
        x_folder="\\Inbox",
        x_origin="ALLEN-P",
        content_type="text/plain",
        has_attachment=True,
        forwarded_content="fwd",
        quoted_content="> quoted",
        headings="Heading",
        content_hash="abc123",
        id=7,
        copy_of="<0@enron.com>",
        is_duplicate=True,
        duplicate_of="<0@enron.com>",
        similarity_score=95.5,
        notification_sent=True,
        notification_date=NOW,
    )
    assert record.recipients[0].address == "a@enron.com"
    assert record.similarity_score == 95.5


def test_email_record_rejects_unknown_field():
    with pytest.raises(TypeError):
        EmailRecord(**MANDATORY, from_adress="typo@enron.com")


@pytest.mark.parametrize("missing", list(MANDATORY))
def test_email_record_requires_mandatory_fields(missing):
    fields = {k: v for k, v in MANDATORY.items() if k != missing}
    with pytest.raises(TypeError):
        EmailRecord(**fields)


def test_email_record_is_frozen_and_rejects_new_attributes():
    record = EmailRecord(**MANDATORY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.subject = "changed"
    # A frozen dataclass with slots=True raises TypeError (not
    # FrozenInstanceError) for an unknown attribute name, a CPython quirk.
    # Either way the typo fails loudly, which is what matters here.
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        record.subjct = "typo"


def test_recipient_builds_and_rejects_unknown_field():
    r = Recipient(address="a@enron.com", kind="cc", position=2)
    assert (r.address, r.kind, r.position) == ("a@enron.com", "cc", 2)
    with pytest.raises(TypeError):
        Recipient(address="a@enron.com", kind="cc", position=2, extra=1)


def test_parse_failure_builds_and_rejects_unknown_field():
    f = ParseFailure(source_file="maildir/x/1", reason="missing To header", failed_at=NOW)
    assert f.reason == "missing To header"
    with pytest.raises(TypeError):
        ParseFailure(source_file="maildir/x/1", reason="r", failed_at=NOW, extra=1)


def test_duplicate_flag_builds_and_rejects_unknown_field():
    fields = dict(
        duplicate_message_id="<2@enron.com>",
        original_message_id="<1@enron.com>",
        subject="Hello",
        from_address="phillip.allen@enron.com",
        duplicate_date=NOW,
        original_date=NOW,
        similarity_score=97.0,
    )
    assert DuplicateFlag(**fields).similarity_score == 97.0
    with pytest.raises(TypeError):
        DuplicateFlag(**fields, extra=1)


def test_send_result_builds_and_rejects_unknown_field():
    fields = dict(
        timestamp=NOW,
        recipient="phillip.allen@enron.com",
        subject="[Duplicate Notice] Re: Hello",
        status="dry_run",
        error=None,
        duplicate_message_id="<2@enron.com>",
    )
    assert SendResult(**fields).status == "dry_run"
    with pytest.raises(TypeError):
        SendResult(**fields, extra=1)


def test_parse_error_carries_reason():
    err = ParseError("missing To header")
    assert err.reason == "missing To header"
    assert str(err) == "missing To header"
    assert isinstance(err, Exception)


# --- Stricter models (D-015) ---


# 1
@pytest.mark.parametrize("text, member", [("to", "TO"), ("cc", "CC"), ("bcc", "BCC")])
def test_recipient_kind_string_becomes_matching_enum(text, member):
    r = Recipient("a@enron.com", text, 0)
    assert r.kind is getattr(models.RecipientKind, member)
    assert isinstance(r.kind, models.RecipientKind)


def test_recipient_accepts_an_enum_member_directly():
    r = Recipient("a@enron.com", models.RecipientKind.CC, 0)
    assert r.kind is models.RecipientKind.CC


# 2
@pytest.mark.parametrize("bad_kind", ["To", "TO", "from", ""])
def test_recipient_rejects_invalid_or_wrong_case_kind(bad_kind):
    with pytest.raises(ValueError):
        Recipient("a@enron.com", bad_kind, 0)


# 3
def test_recipient_kind_compares_equal_to_plain_text():
    assert Recipient("a@enron.com", "to", 0).kind == "to"


# 4
SEND_RESULT_FIELDS = dict(
    timestamp=NOW,
    recipient="phillip.allen@enron.com",
    subject="[Duplicate Notice] Re: Hello",
    error=None,
    duplicate_message_id="<2@enron.com>",
)


def test_send_result_rejects_invalid_status():
    with pytest.raises(ValueError):
        SendResult(**SEND_RESULT_FIELDS, status="sucess")


@pytest.mark.parametrize(
    "text, member", [("sent", "SENT"), ("dry_run", "DRY_RUN"), ("failed", "FAILED")]
)
def test_send_result_status_string_becomes_matching_enum(text, member):
    result = SendResult(**SEND_RESULT_FIELDS, status=text)
    assert result.status is getattr(models.SendStatus, member)
    assert result.status == text


# 5
def test_email_record_converts_recipient_list_to_tuple():
    recipients = [Recipient("a@enron.com", "to", 0), Recipient("b@enron.com", "cc", 0)]
    record = EmailRecord(**MANDATORY, recipients=recipients)
    assert isinstance(record.recipients, tuple)
    assert record.recipients == tuple(recipients)


# 6
def test_recipients_cannot_be_mutated_in_place():
    record = EmailRecord(**MANDATORY, recipients=[Recipient("a@enron.com", "to", 0)])
    with pytest.raises(AttributeError):
        record.recipients.append(Recipient("b@enron.com", "to", 1))


# 7
@pytest.mark.parametrize("bad_item", ["a@enron.com", ("a@enron.com", "to", 0), None])
def test_email_record_rejects_non_recipient_items(bad_item):
    good = Recipient("a@enron.com", "to", 0)
    with pytest.raises(TypeError):
        EmailRecord(**MANDATORY, recipients=[good, bad_item])


# 8
def test_default_recipients_is_an_empty_tuple():
    assert EmailRecord(**MANDATORY).recipients == ()
    assert isinstance(EmailRecord(**MANDATORY).recipients, tuple)
