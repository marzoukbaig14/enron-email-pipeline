"""Tests for parse_headers.py (behavior fixed in D-016, amended in D-017).

Synthetic inputs only: every message is built as bytes by build() below, so
no test touches the dataset. Test numbers in the comments match the numbered
list in the task.

Failure reasons have fixed formats (D-017), checked exactly: "missing <field>"
(e.g. "missing to", "missing message_id"), "invalid <field>: <detail>"
(only the prefix is checked) and "empty input".

Two conventions to keep in mind:
- ParsedHeaders.date is a UTC datetime; iso() turns it into the
  "2001-05-14T23:39:00Z" form the decision describes, so expected values read
  as plain strings.
- ParsedHeaders.to/cc/bcc are plain lists. A recipient's "position" is its
  index in that list, and its "kind" is the field it sits in. ingest builds
  the Recipient objects later.
"""
import dataclasses
from datetime import datetime, timedelta

import pytest

from enron_pipeline.models import ParseError
from enron_pipeline.parse_headers import (
    parse_address_list,
    parse_date,
    parse_headers,
)

DEFAULT_HEADERS = {
    "Message-ID": "<1234.5678.JavaMail.evans@thyme>",
    "Date": "Mon, 14 May 2001 16:39:00 -0700 (PDT)",
    "From": "phillip.allen@enron.com",
    "To": "tim.belden@enron.com",
    "Subject": "Q2 numbers",
}



def build(overrides=None, extra=(), body="Hello.\n", eol="\n", case=None) -> bytes:
    """Build one raw message.

    overrides: replaces a default header's value; None removes the header.
    extra: more (name, value) headers appended after the defaults.
    Values may be str or bytes; a "\\n" inside a value is a fold point and is
    written with the chosen line ending (so "a,\\n\\tb" is a folded header).
    case: optional function applied to every header name (e.g. str.lower).
    """
    headers = {**DEFAULT_HEADERS, **(overrides or {})}
    items = [(k, v) for k, v in headers.items() if v is not None] + list(extra)
    end = eol.encode()
    out = b""
    for name, value in items:
        if case:
            name = case(name)
        if isinstance(value, str):
            value = value.encode()
        out += name.encode() + b": " + value.replace(b"\n", end) + end
    return out + end + body.encode()


def iso(dt: datetime) -> str:
    """Format a UTC datetime like the stored value, and check it really is UTC."""
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_reason(error: ParseError, expected: str) -> None:
    """Check a reason against the fixed formats from D-017.

    "missing <field>" and "empty input" must match exactly. An expected value
    ending in ":" (e.g. "invalid date:") checks the prefix and requires some
    detail after it.
    """
    reason = error.reason
    assert isinstance(reason, str)
    if expected.endswith(":"):
        assert reason.startswith(expected + " "), reason
        assert reason[len(expected) :].strip(), reason
    else:
        assert reason == expected


# --- Message-ID ------------------------------------------------------------


def test_message_id_extracted_as_written_with_whitespace_stripped():  # 1
    raw = build({"Message-ID": "   <Abc.123.JavaMail.evans@thyme>  "})
    assert parse_headers(raw).message_id == "<Abc.123.JavaMail.evans@thyme>"


def test_missing_message_id_raises_missing_message_id():  # 2
    with pytest.raises(ParseError) as info:
        parse_headers(build({"Message-ID": None}))
    assert_reason(info.value, "missing message_id")


# --- Date ------------------------------------------------------------------


def test_date_with_offset_and_abbreviation_comment():  # 3
    value = "Mon, 14 May 2001 16:39:00 -0700 (PDT)"
    assert iso(parse_date(value)) == "2001-05-14T23:39:00Z"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Mon, 14 May 2001 16:39:00 +0000", "2001-05-14T16:39:00Z"),  # 4
        ("Mon, 14 May 2001 16:39:00 +0000 (GMT)", "2001-05-14T16:39:00Z"),
        ("Tue, 15 May 2001 04:09:00 +0530", "2001-05-14T22:39:00Z"),  # positive offset
    ],
)
def test_date_numeric_offsets(value, expected):
    assert iso(parse_date(value)) == expected


def test_date_abbreviation_only():  # 5
    assert iso(parse_date("Mon, 14 May 2001 16:39:00 PDT")) == "2001-05-14T23:39:00Z"


# Hours to add to local time to reach UTC, for every abbreviation in D-016.
ABBREVIATIONS = [
    ("PST", 8), ("PDT", 7),
    ("MST", 7), ("MDT", 6),
    ("CST", 6), ("CDT", 5),
    ("EST", 5), ("EDT", 4),
    ("GMT", 0), ("UT", 0), ("UTC", 0),
]


@pytest.mark.parametrize("abbr, hours", ABBREVIATIONS)
def test_every_listed_abbreviation_maps_to_utc(abbr, hours):  # 6
    result = parse_date(f"Mon, 14 May 2001 12:00:00 {abbr}")
    expected = datetime(2001, 5, 14, 12) + timedelta(hours=hours)
    assert iso(result) == expected.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_date_without_offset_or_abbreviation_raises_no_timezone():  # 7
    with pytest.raises(ParseError) as info:
        parse_date("Mon, 14 May 2001 16:39:00")
    assert info.value.reason == "invalid date: no timezone"


@pytest.mark.parametrize(
    "value",
    [
        "Mon, 14 May 2001 16:39:00 XYZ",  # 7: abbreviation not in the list
        "Mon, 14 May 2001 16:39:00 Z",  # 7: real abbreviation, but not in the list
        "not a date",  # 8
        "",  # 8
        "Mon, 99 Foo 2001 16:39:00 -0700",  # 8: date-shaped garbage
    ],
)
def test_unusable_date_raises_invalid_date(value):  # 7, 8
    with pytest.raises(ParseError) as info:
        parse_date(value)
    assert_reason(info.value, "invalid date:")


def test_date_raw_keeps_exact_original_header_value():  # 9
    value = "Mon, 14 May 2001 16:39:00 -0700 (PDT)"
    parsed = parse_headers(build({"Date": value}))
    assert parsed.date_raw == value
    assert iso(parsed.date) == "2001-05-14T23:39:00Z"


def test_date_rolls_over_day_and_year():  # 10
    assert iso(parse_date("Mon, 31 Dec 2001 20:00:00 -0800")) == "2002-01-01T04:00:00Z"


# --- From ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "kenneth.lay@enron.com",  # 11
        "Kenneth Lay <kenneth.lay@enron.com>",  # 12
        '"Lay, Kenneth" <kenneth.lay@enron.com>',  # 12
        "Kenneth.LAY@Enron.COM",  # 13
        "  Kenneth Lay <Kenneth.Lay@ENRON.com>  ",  # 12, 13
    ],
)
def test_from_address_only_lowercased(value):  # 11, 12, 13
    assert parse_headers(build({"From": value})).from_address == "kenneth.lay@enron.com"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_from_raises_missing_from(value):  # 14
    with pytest.raises(ParseError) as info:
        parse_headers(build({"From": value}))
    assert_reason(info.value, "missing from")


@pytest.mark.parametrize("value", ["kenneth lay", "kenneth.lay", "Kenneth Lay <kenneth.lay>"])
def test_from_without_at_sign_raises_invalid_from(value):  # 14
    with pytest.raises(ParseError) as info:
        parse_headers(build({"From": value}))
    assert_reason(info.value, "invalid from:")


# --- To --------------------------------------------------------------------


def test_to_single_address():  # 15
    assert parse_headers(build({"To": "a@enron.com"})).to == ["a@enron.com"]


def test_to_comma_list_keeps_order():  # 15
    parsed = parse_headers(build({"To": "a@enron.com, b@enron.com,c@enron.com"}))
    assert parsed.to == ["a@enron.com", "b@enron.com", "c@enron.com"]


def test_to_folded_with_tab_continuation_keeps_every_address_in_order():  # 15
    value = "a@enron.com, b@enron.com,\n\tc@enron.com,\n\td@enron.com"
    parsed = parse_headers(build({"To": value}))
    assert parsed.to == ["a@enron.com", "b@enron.com", "c@enron.com", "d@enron.com"]
    # a recipient's position is its index, so these are positions 0..3
    assert list(enumerate(parsed.to))[-1] == (3, "d@enron.com")


def test_to_display_name_containing_a_comma_is_one_address():  # 16
    value = '"Lay, Kenneth" <k.lay@enron.com>, j.s@enron.com'
    assert parse_headers(build({"To": value})).to == ["k.lay@enron.com", "j.s@enron.com"]


def test_missing_to_raises_missing_to():  # 17
    with pytest.raises(ParseError) as info:
        parse_headers(build({"To": None}))
    assert_reason(info.value, "missing to")


@pytest.mark.parametrize("value", ["", "   ", "\n\t", " , ,"])
def test_blank_to_raises_missing_to(value):  # 17
    with pytest.raises(ParseError) as info:
        parse_headers(build({"To": value}))
    assert_reason(info.value, "missing to")


def test_same_address_twice_is_kept_at_different_positions():  # 18
    parsed = parse_headers(build({"To": "a@enron.com, b@enron.com, a@enron.com"}))
    assert parsed.to == ["a@enron.com", "b@enron.com", "a@enron.com"]


def test_recipient_without_at_sign_is_kept_and_lowercased():  # 19 (D-017: lowercase)
    parsed = parse_headers(build({"To": "Tim.Belden, a@enron.com"}))
    assert parsed.to == ["tim.belden", "a@enron.com"]


def test_placeholder_address_is_kept():  # 20
    parsed = parse_headers(build({"To": "no_address@enron.com, a@enron.com"}))
    assert parsed.to == ["no_address@enron.com", "a@enron.com"]


def test_five_hundred_addresses_across_folded_lines_are_all_extracted():  # 21
    expected = [f"user{i}@enron.com" for i in range(500)]
    lines = [", ".join(expected[i : i + 5]) for i in range(0, 500, 5)]
    value = ",\n\t".join(lines)
    assert parse_headers(build({"To": value})).to == expected


@pytest.mark.parametrize(
    "value",
    [
        "a@enron.com, b@enron.com,",  # trailing comma
        "  a@enron.com ,   b@enron.com  ,  ",  # extra whitespace
        "a@enron.com,, b@enron.com,,",  # empty entries between commas
    ],
)
def test_trailing_comma_and_whitespace_make_no_empty_entries(value):  # 22
    assert parse_headers(build({"To": value})).to == ["a@enron.com", "b@enron.com"]


def test_parse_address_list_unfolds_and_lowercases():  # 15, 22 (direct)
    value = "A@Enron.com,\n\t Kenneth Lay <K.Lay@Enron.com> ,\n\tc@enron.com,"
    assert parse_address_list(value) == ["a@enron.com", "k.lay@enron.com", "c@enron.com"]


# --- Cc / Bcc --------------------------------------------------------------


def test_cc_follows_the_same_rules_as_to():  # 23
    value = '"Lay, Kenneth" <K.Lay@enron.com>,\n\tb@enron.com, b@enron.com,'
    parsed = parse_headers(build(extra=[("Cc", value)]))
    assert parsed.cc == ["k.lay@enron.com", "b@enron.com", "b@enron.com"]
    assert parsed.to == ["tim.belden@enron.com"]  # cc did not leak into to


@pytest.mark.parametrize("extra", [[], [("Cc", "")], [("Cc", "   ")]])
def test_missing_or_blank_cc_means_no_cc_and_no_error(extra):  # 23
    assert parse_headers(build(extra=extra)).cc == []


def test_missing_bcc_means_no_bcc_and_no_error():  # 23
    assert parse_headers(build()).bcc == []


def test_bcc_identical_to_cc_is_stored_in_both():  # 24
    value = "a@enron.com, b@enron.com"
    parsed = parse_headers(build(extra=[("Cc", value), ("Bcc", value)]))
    assert parsed.cc == ["a@enron.com", "b@enron.com"]
    assert parsed.bcc == ["a@enron.com", "b@enron.com"]


# --- Subject ---------------------------------------------------------------


def test_re_and_fwd_prefixes_preserved_exactly():  # 25
    parsed = parse_headers(build({"Subject": "Re: Fwd: Q2 numbers"}))
    assert parsed.subject == "Re: Fwd: Q2 numbers"


def test_folded_subject_is_unfolded_into_one_line():  # 26
    # Assumption to confirm: a fold becomes a single space, even when the
    # continuation starts with a tab, and the result has no newline in it.
    parsed = parse_headers(build({"Subject": "Re: Fwd: Q2 numbers for\n\tthe west desk"}))
    assert parsed.subject == "Re: Fwd: Q2 numbers for the west desk"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_subject_is_empty_string(value):  # 27
    assert parse_headers(build({"Subject": value})).subject == ""


def test_blank_subject_written_without_a_space_is_empty_string():  # 27
    raw = build({"Subject": ""}).replace(b"Subject: \n", b"Subject:\n")
    assert b"Subject:\n" in raw
    assert parse_headers(raw).subject == ""


def test_missing_subject_raises_missing_subject():  # 28
    with pytest.raises(ParseError) as info:
        parse_headers(build({"Subject": None}))
    assert_reason(info.value, "missing subject")


# --- Optional headers ------------------------------------------------------

OPTIONAL_HEADERS = [
    ("X-From", "x_from", "Phillip K Allen"),
    ("X-To", "x_to", "Tim Belden, Jeff Skilling"),
    ("X-cc", "x_cc", "Kenneth Lay"),
    ("X-bcc", "x_bcc", "Andrew Fastow"),
    ("X-Folder", "x_folder", r"\Phillip_Allen_Jan2002\Allen, Phillip K.\'Sent Mail"),
    ("X-Origin", "x_origin", "Allen-P"),
    ("Content-Type", "content_type", "text/plain; charset=us-ascii"),
]


@pytest.mark.parametrize("header, attr, value", OPTIONAL_HEADERS)
def test_optional_header_extracted_when_present(header, attr, value):  # 29
    parsed = parse_headers(build(extra=[(header, f"  {value}  ")]))
    assert getattr(parsed, attr) == value


@pytest.mark.parametrize("header, attr, value", OPTIONAL_HEADERS)
@pytest.mark.parametrize("extra_for", ["missing", "empty", "spaces"])
def test_optional_header_missing_or_blank_is_none(header, attr, value, extra_for):  # 30
    extra = {"missing": [], "empty": [(header, "")], "spaces": [(header, "   ")]}[extra_for]
    assert getattr(parse_headers(build(extra=extra)), attr) is None


def test_content_transfer_encoding_extracted_stripped_and_none_when_absent_or_blank():  # D-019
    # parse_body needs this to know whether to decode quoted-printable. It is
    # not stored in the DB, so it is not in OPTIONAL_HEADERS above.
    header = "Content-Transfer-Encoding"
    present = parse_headers(build(extra=[(header, "  quoted-printable  ")]))
    assert present.content_transfer_encoding == "quoted-printable"
    assert parse_headers(build()).content_transfer_encoding is None
    assert parse_headers(build(extra=[(header, "   ")])).content_transfer_encoding is None


# --- Robustness ------------------------------------------------------------


@pytest.mark.parametrize(
    "header, value, attr, expected",
    [
        ("Subject", b"Caf\xe9 meeting", None, None),
        ("X-From", b"Jos\xe9 Mu\xf1oz", None, None),
        ("X-Folder", b"\\Caf\xe9\\Inbox", None, None),
        ("From", b'"Jos\xe9" <jose@enron.com>', "from_address", "jose@enron.com"),
        (
            "To",
            b'"Mu\xf1oz, Jos\xe9" <jose@enron.com>, b@enron.com',
            "to",
            ["jose@enron.com", "b@enron.com"],
        ),
    ],
)
def test_non_ascii_bytes_in_a_header_do_not_crash(header, value, attr, expected):  # 31
    overrides = {header: value} if header in DEFAULT_HEADERS else None
    extra = [] if overrides else [(header, value)]
    parsed = parse_headers(build(overrides, extra=extra))
    assert parsed.message_id == DEFAULT_HEADERS["Message-ID"]
    assert iso(parsed.date) == "2001-05-14T23:39:00Z"
    if attr:
        assert getattr(parsed, attr) == expected
    if header != "From":
        assert parsed.from_address == "phillip.allen@enron.com"
    if header != "To":
        assert parsed.to == ["tim.belden@enron.com"]


def test_non_ascii_bytes_in_the_body_do_not_crash():  # 31
    parsed = parse_headers(build() + b"price was caf\xe9 \xff\xfe each\n")
    assert isinstance(parsed.raw_body, str)
    assert "price was caf" in parsed.raw_body


def test_crlf_line_endings_parse_the_same_as_lf():  # 32
    overrides = {
        "To": "a@enron.com,\n\tb@enron.com",
        "Subject": "Re: Q2\n\tnumbers",
    }
    extra = [("Cc", "c@enron.com"), ("X-From", "Phillip K Allen")]
    body = "Line one.\n\nLine two.\n"
    lf = parse_headers(build(overrides, extra, body=body, eol="\n"))
    crlf = parse_headers(build(overrides, extra, body=body.replace("\n", "\r\n"), eol="\r\n"))
    assert lf.to == ["a@enron.com", "b@enron.com"]
    # Body line-ending handling belongs to parse_body, so compare the bodies by
    # lines and everything else exactly.
    assert dataclasses.replace(crlf, raw_body="") == dataclasses.replace(lf, raw_body="")
    assert crlf.raw_body.splitlines() == lf.raw_body.splitlines()


@pytest.mark.parametrize("name", ["X-cc", "X-CC", "x-cc"])
def test_header_names_are_case_insensitive(name):  # 33
    assert parse_headers(build(extra=[(name, "Kenneth Lay")])).x_cc == "Kenneth Lay"


@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_mandatory_header_names_are_case_insensitive(case):  # 33
    parsed = parse_headers(build(case=case))
    assert parsed.message_id == DEFAULT_HEADERS["Message-ID"]
    assert parsed.from_address == "phillip.allen@enron.com"
    assert parsed.to == ["tim.belden@enron.com"]
    assert parsed.subject == "Q2 numbers"
    assert iso(parsed.date) == "2001-05-14T23:39:00Z"


@pytest.mark.parametrize("raw", [b"", b"   \n", b"\r\n\r\n"], ids=["empty", "spaces", "blank-lines"])
def test_empty_input_raises_empty_input(raw):  # 34 (D-017)
    with pytest.raises(ParseError) as info:
        parse_headers(raw)
    assert_reason(info.value, "empty input")


@pytest.mark.parametrize(
    "raw",
    [
        b"just some text, no headers at all",
        b"\x00\x01\xff\xfe",
        b"\n\nbody only, blank line first\n",
    ],
    ids=["no-headers", "binary", "blank-first"],
)
def test_input_without_any_headers_raises_parse_error_only(raw):  # 34
    with pytest.raises(ParseError) as info:
        parse_headers(raw)
    assert_reason(info.value, "missing message_id")  # first mandatory header checked


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_input_with_no_blank_line_is_all_headers_and_empty_body(eol):  # 34 (D-017)
    raw = build(body="", eol=eol)[: -len(eol)]  # drop the blank line after the headers
    assert not raw.endswith((eol * 2).encode())
    parsed = parse_headers(raw)
    assert parsed.raw_body == ""
    assert parsed.message_id == DEFAULT_HEADERS["Message-ID"]
    assert parsed.subject == "Q2 numbers"


def test_input_with_no_blank_line_still_fails_if_a_mandatory_header_is_missing():  # 34
    raw = build({"To": None}, body="")[:-1]
    with pytest.raises(ParseError) as info:
        parse_headers(raw)
    assert_reason(info.value, "missing to")


# 35: one case per way a mandatory field can fail, with its exact reason.
@pytest.mark.parametrize(
    "raw, expected",
    [
        (build({"Message-ID": None}), "missing message_id"),
        (build({"Message-ID": ""}), "missing message_id"),
        (build({"Date": None}), "missing date"),
        (build({"Date": ""}), "missing date"),
        (build({"Date": "not a date"}), "invalid date:"),
        (build({"Date": "Mon, 14 May 2001 16:39:00"}), "invalid date:"),
        (build({"From": None}), "missing from"),
        (build({"From": "kenneth lay"}), "invalid from:"),
        (build({"To": None}), "missing to"),
        (build({"To": ""}), "missing to"),
        (build({"Subject": None}), "missing subject"),
        (b"", "empty input"),
    ],
    ids=[
        "no-message-id", "blank-message-id", "no-date", "blank-date", "garbage-date",
        "no-offset-date", "no-from", "from-without-at", "no-to", "blank-to",
        "no-subject", "empty-input",
    ],
)
def test_every_parse_error_has_a_fixed_format_reason(raw, expected):  # 35
    with pytest.raises(ParseError) as info:
        parse_headers(raw)
    assert_reason(info.value, expected)


# --- LDAP-style recipients with unquoted commas in the display name (D-017) --

JORDAN_DN = "/o=enron/ou=eu/cn=recipients/cn=mjordan2"
JORDAN_LINE = "Jordan, Mike </O=ENRON/OU=EU/cn=Recipients/cn=mjordan2>"


def recipients_of(parsed, header):
    return {"To": parsed.to, "Cc": parsed.cc}[header]


def header_for(header, value):
    """Put value in To or Cc (To keeps its default when testing Cc)."""
    return build({"To": value}) if header == "To" else build(extra=[("Cc", value)])


@pytest.mark.parametrize("header", ["To", "Cc"])
def test_ldap_recipient_with_unquoted_comma_is_one_recipient(header):  # 8a
    parsed = parse_headers(header_for(header, JORDAN_LINE))
    assert recipients_of(parsed, header) == [JORDAN_DN]


@pytest.mark.parametrize("header", ["To", "Cc"])
def test_ldap_recipient_followed_by_normal_address_gives_two_in_order(header):  # 8b
    parsed = parse_headers(header_for(header, JORDAN_LINE + ", a.b@enron.com"))
    assert recipients_of(parsed, header) == [JORDAN_DN, "a.b@enron.com"]


@pytest.mark.parametrize("header", ["To", "Cc"])
def test_unquoted_comma_with_normal_address_is_one_recipient(header):  # 8c
    parsed = parse_headers(header_for(header, "Lay, Kenneth <kenneth.lay@enron.com>"))
    assert recipients_of(parsed, header) == ["kenneth.lay@enron.com"]


@pytest.mark.parametrize("header", ["To", "Cc"])
def test_folded_header_mixing_ldap_and_normal_addresses_keeps_all(header):  # 8d
    value = (
        "Jordan, Mike </O=ENRON/OU=EU/cn=Recipients/cn=mjordan2>,\n"
        "\ta.b@enron.com,\n"
        "\tLay, Kenneth <Kenneth.Lay@enron.com>,\n"
        "\tSmith, Pat </O=ENRON/OU=NA/cn=Recipients/cn=psmith>,\n"
        "\tc@enron.com"
    )
    parsed = parse_headers(header_for(header, value))
    assert recipients_of(parsed, header) == [
        JORDAN_DN,
        "a.b@enron.com",
        "kenneth.lay@enron.com",
        "/o=enron/ou=na/cn=recipients/cn=psmith",
        "c@enron.com",
    ]


def test_bare_name_before_an_address_still_splits_but_before_a_bracket_it_does_not():  # 8, 19
    # "tim.belden" is followed by a plain address, so it is its own recipient.
    parsed = parse_headers(build({"To": "tim.belden, a@enron.com, " + JORDAN_LINE}))
    assert parsed.to == ["tim.belden", "a@enron.com", JORDAN_DN]


# A segment before a "First <addr>" segment is only glued on as a surname if
# it is letters, spaces, hyphens and apostrophes: no "@", no dot.
@pytest.mark.parametrize("header", ["To", "Cc"])
@pytest.mark.parametrize(
    "value, expected",
    [
        ("tim.belden, Mike <m@x.com>", ["tim.belden", "m@x.com"]),  # dot: not a surname
        (JORDAN_LINE, [JORDAN_DN]),  # plain surname: still one
        ("O'Neal, Shaq <s@x.com>", ["s@x.com"]),  # apostrophe
        ("Smith-Jones, Ann <a@x.com>", ["a@x.com"]),  # hyphen
        ("Van Der Berg, Ann <a@x.com>", ["a@x.com"]),  # spaces
        ("a@enron.com, Mike <m@x.com>", ["a@enron.com", "m@x.com"]),  # "@": not a surname
        ("tim.belden, Smith, Ann <a@x.com>", ["tim.belden", "a@x.com"]),  # mixed
    ],
)
def test_only_a_surname_like_segment_is_glued_to_the_next_entry(header, value, expected):
    parsed = parse_headers(header_for(header, value))
    assert recipients_of(parsed, header) == expected


# --- Unbalanced quotes (silent data loss otherwise) --------------------------


@pytest.mark.parametrize("header", ["To", "Cc"])
def test_unbalanced_quote_does_not_swallow_later_recipients(header):
    parsed = parse_headers(header_for(header, '"unterminated <a@x.com>, b@x.com'))
    assert recipients_of(parsed, header) == ["a@x.com", "b@x.com"]


def test_unbalanced_quote_in_a_folded_header_keeps_every_recipient():
    value = 'Jim "Bud <a@x.com>,\n\tb@x.com,\n\tLay, Kenneth <k@x.com>'
    assert parse_address_list(value) == ["a@x.com", "b@x.com", "k@x.com"]


def test_balanced_quotes_still_protect_a_comma_in_a_display_name():
    value = '"Lay, Kenneth" <k@x.com>, "Doe, Jane" <d@x.com>'
    assert parse_address_list(value) == ["k@x.com", "d@x.com"]


# --- Extra: body split and result shape (not in the numbered list) ----------


def test_raw_body_is_everything_after_the_first_blank_line():
    body = "First line.\nSubject: not a header\n\nAfter a blank line.\n"
    parsed = parse_headers(build(body=body))
    assert parsed.raw_body == body


def test_headers_then_blank_line_with_no_body_gives_empty_raw_body():
    assert parse_headers(build(body="")).raw_body == ""


def test_result_lists_are_lists_and_date_is_utc():
    parsed = parse_headers(build())
    assert isinstance(parsed.to, list) and isinstance(parsed.cc, list)
    assert isinstance(parsed.bcc, list)
    assert iso(parsed.date) == "2001-05-14T23:39:00Z"
