"""Raw body text -> body plus the optional content fields.

Pure: no file, DB or config access. Imports: nothing from the package.

Behavior is fixed by D-019, amended by D-020. In order, parse_body():
  1. normalizes line endings to "\\n";
  2. decodes quoted-printable, but only if Content-Transfer-Encoding says so;
  3. strips control characters (except newline and tab);
  4. finds the first reply or forward marker; the text before it is the body,
     and everything from it on goes into ONE bucket (forwarded_content or
     quoted_content, chosen by what kind of marker came first);
  5. looks for headings in the body only, and for attachment hints.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedBody:
    body: str
    forwarded_content: str | None
    quoted_content: str | None
    headings: str | None
    has_attachment: bool


# --- Marker patterns ----------------------------------------------------------
# Each is matched against ONE line. "-{2,}" lets the dash run vary in length
# ("-----Original Message-----" and "----- Original Message -----" both occur).
_ORIGINAL_MESSAGE = re.compile(r"\s*-{2,}\s*original message\s*-{2,}\s*", re.IGNORECASE)
# "---- Forwarded by Jo/HOU/ECT on ...", "----- Forwarded Message -----".
_FORWARDED = re.compile(r"\s*-{2,}\s*forwarded\s+(?:by|message)\b", re.IGNORECASE)
# A Lotus Notes date: "04/11/2001 05:26 PM", "08/23/2000 10:31:41 AM" or the
# 24-hour "27/10/2000 21:34". Only the digit shape is checked, so dd/mm and
# mm/dd both match.
_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?"
_NOTES_DATE_LINE = re.compile(_DATE, re.IGNORECASE)
# Shape b, "From:  Sally Beck      27/10/2000 21:34": the date ends the line.
_FROM_DATE_LINE = re.compile(r"\s*from:.*\s" + _DATE + r"\s*", re.IGNORECASE)
# Shape c, "Jo Smith <jo@x.com> on 08/23/2000 10:31:41 AM": something, then "on <date>".
_ON_DATE_LINE = re.compile(r"\s*\S.*?\s+on\s+" + _DATE + r"\s*", re.IGNORECASE)
# Lines that may sit between the date and the To: line.
_BETWEEN_DATE_AND_TO = re.compile(r"\s*(?:sent by:|please respond to)", re.IGNORECASE)
# How far a wrapped To: list is followed when looking for its cc:/Subject: line.
_MAX_TO_BLOCK_LINES = 40

# --- Quoted-printable ---------------------------------------------------------
# "=" at the end of a line (optionally followed by spaces) is a soft line break.
_QP_SOFT_BREAK = re.compile(r"=[ \t]*\n")
# One or more consecutive "=XX" escapes. A run is decoded together because a
# multi-byte character (UTF-8 "é" is "=C3=A9") spans several escapes.
_QP_RUN = re.compile(r"(?:=[0-9A-Fa-f]{2})+")
_CHARSET = re.compile(r"""charset\s*=\s*["']?([^\s"';]+)""", re.IGNORECASE)

# --- Attachments and headings --------------------------------------------------
_FILE_REFERENCE = re.compile(r"<<([^<>\n]+)>>")  # "<< File: a.xls >>", "<<a.ppt>>"
_ATTACHMENT_WORD = re.compile(r"\b(?:attached|attachments?)\b", re.IGNORECASE)
_UNDERLINE = re.compile(r"-{3,}|={3,}")
_WORD_OF_4_LETTERS = re.compile(r"[^\W\d_]{4,}")  # letters in any script
# C0 controls and DEL, except tab (\x09) and newline (\x0a). The C1 range
# (\x80-\x9f) is left alone on purpose (D-020).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MAX_HEADING_LENGTH = 60
_MAX_COLON_HEADING_WORDS = 5


def parse_body(
    raw_body: str,
    content_type: str | None = None,
    content_transfer_encoding: str | None = None,
) -> ParsedBody:
    """Split a decoded body into body, forwarded and quoted parts, headings.

    `content_type` is used for the multipart attachment hint and for the
    charset when decoding quoted-printable. `content_transfer_encoding`
    decides whether quoted-printable is decoded at all. Never raises on odd
    input; an empty body is valid.
    """
    text = _normalize_line_endings(raw_body)
    if (content_transfer_encoding or "").strip().lower() == "quoted-printable":
        text = _decode_quoted_printable(text, content_type)
        text = _normalize_line_endings(text)  # "=0D=0A" decodes to "\r\n"
    text = _CONTROL_CHARS.sub("", text)

    lines = text.split("\n")
    marker = _find_first_marker(lines)
    if marker is None:
        body, bucket, kind = text, "", ""
    else:
        index, kind = marker
        body, bucket = "\n".join(lines[:index]), "\n".join(lines[index:])

    # Trailing whitespace goes; leading whitespace is left alone (D-019 item 8).
    body = body.rstrip()
    bucket = bucket.rstrip() or None

    return ParsedBody(
        body=body,
        forwarded_content=bucket if kind == "forwarded" else None,
        quoted_content=bucket if kind == "quoted" else None,
        headings=_find_headings(body),
        has_attachment=_has_attachment(text, body, content_type),
    )


def compute_content_hash(body: str) -> str:
    """Hash used to find storage copies (emails.content_hash).

    What goes into the hash (body only vs body plus sender and subject) is not
    decided yet; it belongs to the dedup plan.
    """
    raise NotImplementedError


# --- Line endings and quoted-printable -----------------------------------------


def _normalize_line_endings(text: str) -> str:
    """CRLF and lone CR both become LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_quoted_printable(text: str, content_type: str | None) -> str:
    """Undo quoted-printable: join soft line breaks, turn "=XX" into characters.

    Works on text, not bytes: only the "=XX" runs are converted, so any
    literal non-ASCII characters already in the text are left untouched.
    Malformed escapes ("=ZZ", a lone "=") do not match and stay as written.
    """
    charset = _declared_charset(content_type)
    text = _QP_SOFT_BREAK.sub("", text)
    return _QP_RUN.sub(lambda run: _decode_escape_run(run.group(), charset), text)


def _declared_charset(content_type: str | None) -> str | None:
    match = _CHARSET.search(content_type or "")
    return match.group(1) if match else None


def _decode_escape_run(run: str, charset: str | None) -> str:
    """"=C3=A9" -> "é". Tries the declared charset, then UTF-8, then Latin-1.

    Latin-1 accepts every byte, so this never fails. An unknown charset name
    (LookupError) or bytes that do not fit the charset just move on to the
    next candidate.
    """
    data = bytes.fromhex(run.replace("=", ""))
    for encoding in (charset, "utf-8"):
        if encoding is None:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1")


# --- Markers -----------------------------------------------------------------------


def _find_first_marker(lines: list[str]) -> tuple[int, str] | None:
    """Index and bucket ("quoted" or "forwarded") of the first marker line."""
    for index, line in enumerate(lines):
        if line.startswith(">") or _ORIGINAL_MESSAGE.fullmatch(line):
            return index, "quoted"
        if _FORWARDED.match(line):
            return index, "forwarded"
        if _starts_notes_reply_header(lines, index):
            return index, "quoted"
    return None


def _starts_notes_reply_header(lines: list[str], index: int) -> bool:
    """Does a Lotus Notes inline reply header start at lines[index]? (D-019, D-020)

    The marker is the FIRST line of the header, and a "To:" line must follow.
    Four shapes:

      a. name line, then a date line     Susan J Mara
                                         04/11/2001 05:26 PM
      b. "From:" line ending in a date   From:  Sally Beck      27/10/2000 21:34
      c. line ending in "on <date>"     Jo Smith <jo@x.com> on 08/23/2000 10:31:41 AM
      d. a bare To:/cc:/Subject: block   (the name and date lines are missing)

    For a, b and c, blank lines and "Sent by:" / "Please respond to" lines may
    sit between the date and the To: line. Requiring the To: line is what
    keeps an ordinary line that happens to end in a date from being a marker.
    """
    line = lines[index]
    if not line.strip():
        return False
    if _FROM_DATE_LINE.fullmatch(line) or _ON_DATE_LINE.fullmatch(line):
        return _to_line_follows(lines, index + 1)
    if index + 1 < len(lines) and _NOTES_DATE_LINE.fullmatch(lines[index + 1].strip()):
        return _to_line_follows(lines, index + 2)
    return _starts_bare_to_block(lines, index)


def _to_line_follows(lines: list[str], start: int) -> bool:
    """Is the next line from `start` that is not blank (or a Sent by / Please
    respond to line) a "To:" line?"""
    for line in lines[start:]:
        if not line.strip() or _BETWEEN_DATE_AND_TO.match(line):
            continue
        return line.strip().lower().startswith("to:")
    return False


def _starts_bare_to_block(lines: list[str], index: int) -> bool:
    """Shape d: a "To:" line with a blank line (or the body start) above it,
    whose block reaches a "cc:" or "Subject:" line.

    The To: list may wrap onto continuation lines, so the block is followed
    until it hits cc:/Subject: (a match) or a blank line (not a match).
    """
    if not lines[index].strip().lower().startswith("to:"):
        return False
    if index > 0 and lines[index - 1].strip():
        return False  # text directly above: part of a normal paragraph
    for line in lines[index + 1 : index + 1 + _MAX_TO_BLOCK_LINES]:
        stripped = line.strip().lower()
        if not stripped:
            return False
        if stripped.startswith(("cc:", "subject:")):
            return True
    return False


# --- Attachments -------------------------------------------------------------------


def _has_attachment(text: str, body: str, content_type: str | None) -> bool:
    """Any of: multipart Content-Type; a "<<...>>" reference anywhere in the
    text (buckets included); "attached"/"attachment" as a whole word in the
    main body only."""
    if (content_type or "").strip().lower().startswith("multipart"):
        return True
    # "To: <<jeff@enron.com>>" is common in quoted headers and is an address,
    # not a file, so a reference containing "@" does not count.
    if any("@" not in ref for ref in _FILE_REFERENCE.findall(text)):
        return True
    return _ATTACHMENT_WORD.search(body) is not None


# --- Headings ----------------------------------------------------------------------


def _find_headings(body: str) -> str | None:
    """Heading lines of the main body, in order, joined by "\\n" (or None).

    A heuristic, not a parser. See _is_heading for the rule.
    """
    lines = [line.strip() for line in body.split("\n")]
    headings = []
    seen_text = False
    for index, line in enumerate(lines):
        if not line:
            continue
        is_first_line = not seen_text
        seen_text = True
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if _is_heading(line, next_line, is_first_line):
            headings.append(line)
    return "\n".join(headings) or None


def _is_heading(line: str, next_line: str, is_first_line: bool) -> bool:
    """D-019 item 6. The line is already stripped and non-empty.

    It must be at most 60 characters, not end in a period, and contain a
    letter or digit (so a row of "=====" is never a heading). Then any one of:
      - ALL CAPS with at least one word of 4+ letters;
      - the next line is a run of "---" or "===";
      - it ends in ":" with 5 words or fewer, and is not the first non-empty
        line (that is usually a salutation like "Mark:").
    """
    if len(line) > _MAX_HEADING_LENGTH or line.endswith("."):
        return False
    if not any(char.isalnum() for char in line):
        return False
    if line.isupper() and _WORD_OF_4_LETTERS.search(line):
        return True
    if _UNDERLINE.fullmatch(next_line):
        return True
    return (
        line.endswith(":")
        and len(line.split()) <= _MAX_COLON_HEADING_WORDS
        and not is_first_line
    )
