"""Tests for parse_body.py (behavior fixed in D-019).

Synthetic inputs only; nothing here touches the dataset. Each test builds a
body string and calls parse_body() through the small helper below.

Terms used in the tests:
- "marker": the first line that starts a reply or forward (an Original Message
  line, a "Forwarded by" banner, a Lotus Notes inline reply header, or a line
  starting with ">"). Everything from the marker on goes into ONE bucket,
  chosen by the kind of that first marker.
- "bucket": forwarded_content or quoted_content.
"""
import pytest

from enron_pipeline.parse_body import ParsedBody, parse_body


def parse(body, content_type=None, cte=None) -> ParsedBody:
    return parse_body(body, content_type, cte)


# --- Plain bodies ------------------------------------------------------------


def test_plain_body_without_markers_is_body_only():
    result = parse("Hi Tim,\n\nLet's meet at 3.\n\nPhillip\n")
    assert result.body == "Hi Tim,\n\nLet's meet at 3.\n\nPhillip"
    assert result.forwarded_content is None
    assert result.quoted_content is None
    assert result.headings is None
    assert result.has_attachment is False


def test_trailing_whitespace_is_stripped_from_body_and_bucket_but_not_leading():
    result = parse("Hello.  \n\n\n-----Original Message-----\nFrom: Tim\n\n  \n")
    assert result.body == "Hello."
    assert result.quoted_content == "-----Original Message-----\nFrom: Tim"
    # Leading whitespace is not mentioned in D-019, so it is left alone.
    assert parse("\n\n  Hello.").body == "\n\n  Hello."


def test_signatures_and_disclaimers_stay_in_the_body():
    text = "Thanks,\nPhil\n\nThis e-mail is confidential and privileged.\n"
    assert parse(text).body == text.rstrip()


# --- Quoted-printable (decode only when declared) ----------------------------


def test_qp_declared_decodes_escapes():
    result = parse("Hello=20world, 1+1=3D2", cte="quoted-printable")
    assert result.body == "Hello world, 1+1=2"


def test_qp_declared_joins_soft_line_breaks():
    result = parse("This was fine=\nfor today=\r\nand tomorrow", cte="quoted-printable")
    assert result.body == "This was finefor todayand tomorrow"


def test_qp_soft_break_followed_by_an_escape_line_like_the_real_data():
    # Real shape from mcconnell-m/all_documents/410: "fine=" then "=20".
    result = parse("This was fine=\n=20\nfor today", cte="quoted-printable")
    assert result.body == "This was fine \nfor today"


@pytest.mark.parametrize("cte", ["Quoted-Printable", "  QUOTED-PRINTABLE  "])
def test_qp_header_value_is_case_and_space_insensitive(cte):
    assert parse("a=20b", cte=cte).body == "a b"


def test_qp_multibyte_run_is_decoded_with_the_declared_charset():
    utf8 = parse("caf=C3=A9", "text/plain; charset=utf-8", "quoted-printable")
    latin1 = parse("caf=E9", 'text/plain; charset="iso-8859-1"', "quoted-printable")
    assert utf8.body == "café"
    assert latin1.body == "café"


def test_qp_run_with_wrong_or_missing_charset_still_decodes_something_sensible():
    # No charset: try UTF-8, then fall back to Latin-1 for a lone byte.
    assert parse("caf=C3=A9", cte="quoted-printable").body == "café"
    assert parse("caf=E9", cte="quoted-printable").body == "café"
    # An unknown charset name must not raise.
    assert parse("caf=E9", "text/plain; charset=nonsense-9", "quoted-printable").body == "café"


def test_qp_invalid_escapes_are_left_as_they_are():
    result = parse("a =ZZ b =4 c", cte="quoted-printable")
    assert result.body == "a =ZZ b =4 c"


def test_qp_is_decoded_inside_buckets_too_and_before_markers_are_found():
    text = "Hi=20there\n-----Original Message-----\nFrom: T=\nim"
    result = parse(text, cte="quoted-printable")
    assert result.body == "Hi there"
    assert result.quoted_content == "-----Original Message-----\nFrom: Tim"


@pytest.mark.parametrize("cte", [None, "7bit", "8bit", "base64", ""])
def test_qp_not_declared_leaves_artifacts_alone(cte):
    text = "Hello=20world=\nnext =3D"
    assert parse(text, cte=cte).body == text


# --- Line endings --------------------------------------------------------------


def test_crlf_becomes_lf():
    assert parse("one\r\ntwo\r\n\r\nthree\r\n").body == "one\ntwo\n\nthree"


def test_mixed_line_endings_all_become_lf():
    result = parse("a\r\nb\rc\nd\r\n-----Original Message-----\r\nFrom: X\rTo: Y")
    assert result.body == "a\nb\nc\nd"
    assert result.quoted_content == "-----Original Message-----\nFrom: X\nTo: Y"
    assert "\r" not in result.body + result.quoted_content


def test_qp_decoded_crlf_is_normalized_too():
    assert parse("a=0D=0Ab", cte="quoted-printable").body == "a\nb"


# --- Outlook replies ---------------------------------------------------------


def test_outlook_original_message_splits_body_from_quoted_content():
    text = (
        "Sounds good, see you then.\n"
        "\n"
        "-----Original Message-----\n"
        "From: Tim Belden\n"
        "Sent: Monday, May 14, 2001 4:39 PM\n"
        "To: Phillip Allen\n"
        "Subject: Lunch\n"
        "\n"
        "Are you free?\n"
    )
    result = parse(text)
    assert result.body == "Sounds good, see you then."
    assert result.quoted_content == (
        "-----Original Message-----\n"
        "From: Tim Belden\n"
        "Sent: Monday, May 14, 2001 4:39 PM\n"
        "To: Phillip Allen\n"
        "Subject: Lunch\n"
        "\n"
        "Are you free?"
    )
    assert result.forwarded_content is None


@pytest.mark.parametrize(
    "marker",
    [
        "----- Original Message -----",  # spaced variant, 4.5k lines in the corpus
        " -----Original Message-----",  # leading space, seen in real data
        "-----ORIGINAL MESSAGE-----",
    ],
)
def test_original_message_variants_are_markers(marker):
    result = parse(f"Reply.\n{marker}\nFrom: Tim\n")
    assert result.body == "Reply."
    assert result.quoted_content == f"{marker}\nFrom: Tim"


def test_original_message_in_prose_is_not_a_marker():
    text = "Please see the Original Message from Tim below the line.\n"
    assert parse(text).body == text.rstrip()


# --- Lotus Notes forwards ----------------------------------------------------


def test_lotus_forwarded_by_goes_to_forwarded_content():
    text = (
        "FYI - see below.\n"
        "---------------------- Forwarded by Rick Buy/HOU/ECT on 01/30/2001 09:45 AM ----------\n"
        "\n"
        "\tSheila Walton\n"
        "\t01/30/2001 09:05 AM\n"
        "Some forwarded text.\n"
    )
    result = parse(text)
    assert result.body == "FYI - see below."
    assert result.forwarded_content.startswith("---------------------- Forwarded by Rick Buy")
    assert result.forwarded_content.endswith("Some forwarded text.")
    assert result.quoted_content is None


@pytest.mark.parametrize(
    "marker",
    [
        "----- Forwarded by Jeff Dasovich/NA/Enron on 07/11/2001 07:08 PM -----",
        "----- Forwarded Message -----",
        "-----Forwarded Message-----",
        "---------- Forwarded message ----------",
    ],
)
def test_forward_marker_styles(marker):
    result = parse(f"fyi\n{marker}\nstuff\n")
    assert result.body == "fyi"
    assert result.forwarded_content == f"{marker}\nstuff"
    assert result.quoted_content is None


# --- Lotus Notes inline reply headers ----------------------------------------


def test_lotus_inline_reply_header_goes_to_quoted_content():
    # Shape from dasovich-j/all_documents/10997: name line, date line, blank
    # indented lines, then an indented To:.
    text = (
        "If you need me to, I can do it.\n"
        "\n"
        "\n"
        "\tSusan J Mara\n"
        "\t04/11/2001 05:26 PM\n"
        "\t\t \n"
        "\t\t To: Jeff Dasovich/NA/Enron@Enron\n"
        "\t\t cc: \n"
        "\t\t Subject: IEP Board Conference Call\n"
        "\n"
        "I can't be on this. Can you cover?\n"
    )
    result = parse(text)
    assert result.body == "If you need me to, I can do it."
    assert result.quoted_content.startswith("\tSusan J Mara\n\t04/11/2001 05:26 PM")
    assert result.quoted_content.endswith("I can't be on this. Can you cover?")
    assert result.forwarded_content is None


def test_name_and_date_lines_without_a_to_line_are_not_a_marker():
    text = "Notes from the call.\nSusan J Mara\n04/11/2001 05:26 PM\nNothing else here.\n"
    result = parse(text)
    assert result.body == text.rstrip()
    assert result.quoted_content is None


def test_a_date_line_with_no_name_line_above_it_is_not_a_marker():
    # The header needs all three: a name line, a date line, then To:.
    assert parse("04/11/2001 05:26 PM\nTo: Tim\n").quoted_content is None
    assert parse("Hello\n\n04/11/2001 05:26 PM\nTo: Tim\n").quoted_content is None


# --- ">" quoted lines ----------------------------------------------------------


def test_gt_quoted_lines_after_the_reply_go_to_quoted_content():
    text = "I agree.\n\n> Are you free Tuesday?\n> Let me know.\n>> older line\n"
    result = parse(text)
    assert result.body == "I agree."
    assert result.quoted_content == "> Are you free Tuesday?\n> Let me know.\n>> older line"


def test_gt_quoted_lines_after_an_original_message_marker_stay_in_that_bucket():
    text = "Reply.\n-----Original Message-----\nFrom: Tim\n> quoted\n"
    result = parse(text)
    assert result.quoted_content == "-----Original Message-----\nFrom: Tim\n> quoted"


def test_indented_gt_is_not_a_marker():
    # D-019 says "lines starting with >", read literally: column 0 only.
    text = "a\n  > indented\n"
    assert parse(text).quoted_content is None


def test_gt_marker_wins_over_a_later_forward_marker():
    text = "x\n> quoted\n----- Forwarded by A/B/C on 01/01/2001 -----\ny\n"
    result = parse(text)
    assert result.body == "x"
    assert result.quoted_content == "> quoted\n----- Forwarded by A/B/C on 01/01/2001 -----\ny"
    assert result.forwarded_content is None


# --- Nested chains and marker-first bodies -----------------------------------


def test_nested_chain_stays_in_one_bucket():
    text = (
        "Top reply.\n"
        "-----Original Message-----\n"
        "From: B\n"
        "Middle reply.\n"
        "-----Original Message-----\n"
        "From: A\n"
        "First message.\n"
    )
    result = parse(text)
    assert result.body == "Top reply."
    assert result.quoted_content.count("-----Original Message-----") == 2
    assert result.quoted_content.endswith("First message.")
    assert result.forwarded_content is None


def test_first_marker_decides_the_bucket_for_a_mixed_chain():
    forward_first = "a\n----- Forwarded by X/Y on 01/01/2001 -----\nb\n-----Original Message-----\nc\n"
    result = parse(forward_first)
    assert result.forwarded_content.count("-----") >= 3 and "Original Message" in result.forwarded_content
    assert result.quoted_content is None

    quote_first = "a\n-----Original Message-----\nb\n----- Forwarded by X/Y on 01/01/2001 -----\nc\n"
    result = parse(quote_first)
    assert "Forwarded by" in result.quoted_content
    assert result.forwarded_content is None


def test_body_that_starts_with_a_marker_is_empty_and_bucket_is_filled():
    result = parse("-----Original Message-----\nFrom: Tim\nHello\n")
    assert result.body == ""
    assert result.quoted_content == "-----Original Message-----\nFrom: Tim\nHello"

    result = parse("---- Forwarded by A/B/C on 01/01/2001 ----\nHello\n")
    assert result.body == ""
    assert result.forwarded_content is not None

    result = parse("> just a quote\n")
    assert result.body == ""
    assert result.quoted_content == "> just a quote"


def test_markers_with_crlf_line_endings_are_found():
    result = parse("Reply.\r\n-----Original Message-----\r\nFrom: Tim\r\n> q\r\n")
    assert result.body == "Reply."
    assert result.quoted_content == "-----Original Message-----\nFrom: Tim\n> q"


# --- has_attachment ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "See the file.\n<< File: a.xls >>\n",
        "See the file.\n<<a.ppt>>\n",
        "Subject line is fine.\n<<Concur Expense Document>>\n",
        "Please see the attached report.",
        "Attachment: report.doc",
        "ATTACHED is the file",
        "Attached.",
        "See the attachments below.",  # plural counts since D-020
        "ATTACHMENTS: a.doc, b.doc",
    ],
)
def test_has_attachment_true_cases(text):
    assert parse(text).has_attachment is True


def test_has_attachment_true_for_multipart_content_type():
    assert parse("Plain text.", "multipart/mixed; boundary=xyz").has_attachment is True
    assert parse("Plain text.", "Multipart/Alternative; boundary=xyz").has_attachment is True


def test_has_attachment_false_for_plain_content_type():
    assert parse("Plain text.", "text/plain; charset=us-ascii").has_attachment is False


def test_has_attachment_true_for_file_reference_inside_a_bucket():
    # "any part" in D-019: the <<...>> reference counts wherever it appears.
    text = "Thanks.\n-----Original Message-----\n<<budget.xls>>\n"
    assert parse(text).has_attachment is True


@pytest.mark.parametrize(
    "text",
    [
        "Nothing here about files.",
        "A word like attachedness does not count.",
        "The word attachmentsx is not a whole word either.",
        "Reattached is not a whole word.",
    ],
)
def test_has_attachment_false_cases(text):
    assert parse(text).has_attachment is False


def test_attached_only_inside_the_quoted_bucket_does_not_count():
    text = "Thanks.\n-----Original Message-----\nPlease see the attached file.\n"
    assert parse(text).has_attachment is False


def test_attached_only_inside_the_forwarded_bucket_does_not_count():
    text = "FYI\n----- Forwarded by A/B/C on 01/01/2001 -----\nattachment: x\n"
    assert parse(text).has_attachment is False


def test_double_angle_email_address_is_not_a_file_reference():
    # "To: <<jeff@enron.com>>" is common in quoted headers (7.8k lines in the
    # corpus) and is not an attachment.
    text = "Thanks.\n-----Original Message-----\nTo: <<jeff@enron.com>>\n"
    assert parse(text).has_attachment is False


# --- headings (main body only, heuristic) ------------------------------------


def test_all_caps_line_is_a_heading():
    assert parse("Intro line\nAGENDA\nitem one\n").headings == "AGENDA"


def test_colon_line_with_five_words_or_fewer_is_a_heading():
    assert parse("Hello all\nAction Items:\n- do it\n").headings == "Action Items:"


def test_colon_line_as_the_first_non_empty_line_is_not_a_heading():
    assert parse("Mark:\nPlease call me.\n").headings is None
    assert parse("\n\n  Mark:\nPlease call me.\n").headings is None


def test_colon_line_that_is_not_first_is_a_heading_even_if_a_name():
    assert parse("Hi\nMark:\nPlease call me.\n").headings == "Mark:"


def test_all_caps_word_under_four_letters_is_not_a_heading():
    # D-020: the all-caps rule needs a word of 4+ letters (was 3 in D-019).
    for text in ("OK", "A B", "FYI", "NDA"):
        assert parse(f"Hello\n{text}\nbye\n").headings is None, text


def test_all_caps_word_of_exactly_four_letters_is_a_heading():
    assert parse("Hello\nITEM\nbye\n").headings == "ITEM"
    assert parse("Hello\nFYI ITEM\nbye\n").headings == "FYI ITEM"


def test_all_caps_needs_a_long_enough_word_not_just_digits_or_symbols():
    assert parse("Hello\n713-853-7751\nbye\n").headings is None
    assert parse("Hello\n12 A 3\nbye\n").headings is None


def test_long_sentence_ending_in_colon_is_not_a_heading():
    text = "Hello\nPlease find the numbers for the second quarter below:\nx\n"
    assert parse(text).headings is None  # 9 words


def test_underlined_line_is_a_heading_and_the_underline_is_not():
    result = parse("Hello\nProject status\n---------------\nall good\n")
    assert result.headings == "Project status"
    result = parse("Hello\nBudget notes\n=====\nall good\n")
    assert result.headings == "Budget notes"


def test_underline_of_fewer_than_three_chars_does_not_count():
    assert parse("Hello\nProject status\n--\nall good\n").headings is None


def test_separator_line_followed_by_separator_is_not_a_heading():
    assert parse("Hi there.\n\n=====\n=====\nbye\n").headings is None


def test_line_ending_in_a_period_is_never_a_heading():
    assert parse("Hello\nTHE END.\nbye\n").headings is None
    assert parse("Hello\nNotes.\n-----\nbye\n").headings is None


def test_sixty_characters_is_the_limit():
    ok = "A" * 59 + ":"  # 60 chars, all caps, one word
    too_long = "A" * 60 + ":"  # 61
    assert len(ok) == 60 and len(too_long) == 61
    assert parse(f"Hello\n{ok}\nbye\n").headings == ok
    assert parse(f"Hello\n{too_long}\nbye\n").headings is None


def test_multiple_headings_are_joined_by_newline_in_order_and_stripped():
    text = "Hello\n  AGENDA  \nstuff\nAction Items:\nmore\nNotes\n=====\n"
    assert parse(text).headings == "AGENDA\nAction Items:\nNotes"


def test_headings_are_taken_from_the_main_body_only():
    text = (
        "Plain reply.\n"
        "-----Original Message-----\n"
        "AGENDA\n"
        "Action Items:\n"
    )
    assert parse(text).headings is None
    text = "Plain reply.\n----- Forwarded by A/B/C on 01/01/2001 -----\nAGENDA\n"
    assert parse(text).headings is None


def test_heading_detection_runs_on_the_decoded_body():
    # "=\n" would otherwise cut the heading line in two.
    result = parse("Hello\nAGEN=\nDA\nbye\n", cte="quoted-printable")
    assert result.headings == "AGENDA"


# --- Empty input ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n", " \t\r\n \r\n"])
def test_empty_and_whitespace_only_body(text):
    result = parse(text)
    assert result == ParsedBody(
        body="",
        forwarded_content=None,
        quoted_content=None,
        headings=None,
        has_attachment=False,
    )


# --- Non-ASCII text ------------------------------------------------------------


def test_non_ascii_text_survives_in_body_and_buckets():
    text = "Café résumé — naïve 日本語\n-----Original Message-----\nÜber: señor\n"
    result = parse(text)
    assert result.body == "Café résumé — naïve 日本語"
    assert result.quoted_content == "-----Original Message-----\nÜber: señor"


def test_non_ascii_text_survives_alongside_qp_decoding():
    result = parse("Café=20menu", cte="quoted-printable")
    assert result.body == "Café menu"


# --- Robustness ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("\x00\x01", id="control-chars"),
        pytest.param("=", id="lone-equals"),
        pytest.param("=\n", id="soft-break-only"),
        pytest.param("<<", id="open-double-angle"),
        pytest.param(">", id="lone-gt"),
        pytest.param("-" * 5000, id="long-dashes"),
        pytest.param("A" * 100000, id="long-caps-line"),
    ],
)
def test_odd_input_never_raises(text):
    for cte in (None, "quoted-printable"):
        assert isinstance(parse(text, cte=cte), ParsedBody)


# --- More Lotus Notes reply header shapes (D-020 item 1) -------------------------
# Every shape: the marker is the FIRST line of the header, text above it stays
# in the body, and a "To:" line must follow.

NOTES_TAIL = "To: Sally Beck/HOU/ECT@ECT\ncc:\nSubject: Re: Summer Cookout\n\nThe 30th works for me.\n"


def assert_quoted_from(result, first_line, body="Thanks."):
    assert result.body == body
    assert result.quoted_content is not None
    assert result.quoted_content.split("\n")[0] == first_line
    assert result.forwarded_content is None


def test_shape_a_24_hour_date_without_am_pm():
    result = parse("Thanks.\n\nTim Poullain-Patterson\n09/11/2000 15:27\n\n" + NOTES_TAIL)
    assert_quoted_from(result, "Tim Poullain-Patterson")


def test_shape_a_sent_by_line_between_date_and_to():
    text = "Thanks.\n\nKristin Albrecht\n08/03/2000 10:05 AM\nSent by: Kristin Albrecht\n\n" + NOTES_TAIL
    assert_quoted_from(parse(text), "Kristin Albrecht")


def test_shape_a_please_respond_to_line_between_date_and_to():
    text = "Thanks.\n\nCarol Hudson\n04/11/2001 03:03 PM\nPlease respond to carol@iepa.com\n\n" + NOTES_TAIL
    assert_quoted_from(parse(text), "Carol Hudson")


def test_shape_b_from_name_date_on_one_line_24_hour():
    text = "Thanks.\n\nFrom:  Sally Beck                           27/10/2000 21:34\n\n" + NOTES_TAIL
    assert_quoted_from(parse(text), "From:  Sally Beck                           27/10/2000 21:34")


def test_shape_b_from_name_at_company_with_am_pm():
    text = "Thanks.\nFrom:  Margarite Cunningham @ ENRON      09/19/2000 09:21 AM\n" + NOTES_TAIL
    assert_quoted_from(parse(text), "From:  Margarite Cunningham @ ENRON      09/19/2000 09:21 AM")


def test_shape_b_with_a_sent_by_line():
    text = "Thanks.\nFrom: Kim Rizzi @ ENRON    11/27/2000 02:11 PM\nSent by: Jennifer Jordan@ENRON\n" + NOTES_TAIL
    assert parse(text).body == "Thanks."


def test_shape_c_name_on_date_on_one_line():
    line = "Beverly Gibson <bgibson@yahoo.com> on 08/23/2000 10:31:41 AM"
    text = f"Thanks.\n\n{line}\nTo: Sally Beck <sally.beck@enron.com>\ncc:\nSubject: fashion show\n"
    assert_quoted_from(parse(text), line)


def test_shape_c_with_please_respond_to_line():
    line = '"Brian\'s Mail" <bk@mail.utexas.edu> on 10/10/2000 06:16:33 PM'
    text = f"Thanks.\n{line}\nPlease respond to \"Brian's Mail\" <bk@mail.utexas.edu>\nTo: Sally\ncc:\nSubject: x\n"
    assert_quoted_from(parse(text), line)


def test_shape_d_bare_to_block_after_a_blank_line():
    text = "Thanks,\nPatti\n\n\nTo: Shona Wilson/NA/Enron@Enron\ncc:  \n\nSubject: Chris Abel\n\nbody text\n"
    result = parse(text)
    assert result.body == "Thanks,\nPatti"
    assert result.quoted_content.startswith("To: Shona Wilson")
    assert result.quoted_content.endswith("body text")


def test_shape_d_to_list_wrapped_onto_continuation_lines():
    text = "Thanks.\n\nTo: a@x.com, b@x.com,\nc@x.com, d@x.com\ncc: e@x.com\nSubject: x\n"
    assert_quoted_from(parse(text), "To: a@x.com, b@x.com,")


def test_shape_d_subject_without_cc_also_counts():
    assert_quoted_from(parse("Thanks.\n\nTo: Tim\nSubject: Lunch\n"), "To: Tim")


def test_shape_d_to_block_at_the_start_of_the_body():
    result = parse("To: Tim\ncc: Bob\nSubject: x\n\nHello\n")
    assert result.body == ""
    assert result.quoted_content == "To: Tim\ncc: Bob\nSubject: x\n\nHello"


def test_shape_d_is_case_insensitive():
    assert parse("Thanks.\n\nTO: Tim\nCC:\nSUBJECT: x\n").quoted_content.startswith("TO: Tim")


@pytest.mark.parametrize(
    "text",
    [
        # To: directly under a text line (no blank above) is not shape d.
        "Reply below\nTo: Tim\ncc: Bob\nSubject: x\n",
        # Blank above, but no cc:/Subject: line in the same block.
        "Hi\n\nTo: be sure to call me\nThanks\n",
        # The block is cut by a blank line before any cc:/Subject:.
        "Hi\n\nTo: Tim\n\nsome text\ncc: Bob\n",
        # A From:+date line with no To: after it is just text.
        "Hi\nFrom: Tim    04/11/2001 05:26 PM\nNothing else.\n",
        # "on <date>" in ordinary prose with no To: after it.
        "I will call you on 04/11/2001 05:26 PM\nbye\n",
    ],
)
def test_near_misses_are_not_markers(text):
    result = parse(text)
    assert result.quoted_content is None
    assert result.forwarded_content is None
    assert result.body == text.rstrip()


def test_known_gap_outlook_header_block_without_original_message_line_stays_in_body():
    # Pinned on purpose (D-020 item 2, known gap): fewer than 20 files.
    text = "Thanks.\nFrom: Tim Belden\nSent: Monday, May 14, 2001 4:39 PM\nTo: Phillip Allen\nSubject: Lunch\n"
    assert parse(text).quoted_content is None


def test_known_gap_inline_attachment_header_stays_in_body():
    text = "Hi.\n--------- Inline attachment follows ---------\nFrom: </O=ENRON/CN=SBECK>\nTo: x\ncc:\nSubject: y\n"
    # "To: x" has a text line above it, so shape d does not apply either.
    assert parse(text).quoted_content is None


def test_earliest_marker_wins_among_the_new_shapes():
    text = (
        "Top.\n\nFrom: A B   01/02/2001 10:00 AM\nTo: x\ncc:\nSubject: s\n\n"
        "Middle.\n\nC D\n01/01/2001 09:00 AM\nTo: y\ncc:\nSubject: s\n"
    )
    result = parse(text)
    assert result.body == "Top."
    assert result.quoted_content.count("To:") == 2


def test_headings_inside_a_new_shape_bucket_are_not_extracted():
    result = parse("Thanks.\n\nTo: Tim\ncc:\nSubject: x\n\nAGENDA\nAction Items:\n")
    assert result.headings is None


# --- Control characters (D-020 item 5) ---------------------------------------------


def test_control_characters_are_stripped_but_newline_and_tab_stay():
    result = parse("a\x01b\x07c\x00d\x7fe\x0cf\tg\nh")
    assert result.body == "abcdef\tg\nh"


def test_control_characters_are_stripped_from_buckets_too():
    result = parse("Hi\n-----Original Message-----\nFrom: T\x01im\n")
    assert result.quoted_content == "-----Original Message-----\nFrom: Tim"


def test_qp_escape_that_decodes_to_a_control_character_is_stripped():
    # Real shape from beck-s/_sent_mail/236: "Int=01,l" (an apostrophe in the source).
    assert parse("Arrive Int=01,l", cte="quoted-printable").body == "Arrive Int,l"
    assert parse("a=07b", cte="quoted-printable").body == "ab"


def test_c1_range_and_normal_unicode_are_left_alone():
    assert parse("a\x93b\x85c").body == "a\x93b\x85c"
    assert parse("naïve “quote” — ok").body == "naïve “quote” — ok"


def test_stripping_happens_before_markers_and_headings_are_found():
    # A control character in the middle of a marker or heading must not hide it.
    assert parse("Hi\n-----Original\x01 Message-----\nx\n").quoted_content is not None
    assert parse("Hello\nAGEN\x01DA\nbye\n").headings == "AGENDA"


def test_cr_is_a_line_ending_not_a_stripped_control_character():
    assert parse("a\rb").body == "a\nb"
