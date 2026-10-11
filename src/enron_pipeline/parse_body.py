"""Raw body text -> body plus the optional content fields.

Pure: no file, DB or config access. Imports: nothing from the package.

Behavior is fixed by D-019. In order, parse_body():
  1. normalizes line endings to "\\n";
  2. decodes quoted-printable, but only if Content-Transfer-Encoding says so;
  3. finds the first reply or forward marker; the text before it is the body,
     and everything from it on goes into ONE bucket (forwarded_content or
     quoted_content, chosen by what kind of marker came first);
  4. looks for headings in the body only, and for attachment hints.
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
# The date line of a Lotus Notes reply header: "04/11/2001 05:26 PM".
_NOTES_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M", re.IGNORECASE)

# --- Quoted-printable ---------------------------------------------------------
# "=" at the end of a line (optionally followed by spaces) is a soft line break.
_QP_SOFT_BREAK = re.compile(r"=[ \t]*\n")
# One or more consecutive "=XX" escapes. A run is decoded together because a
# multi-byte character (UTF-8 "é" is "=C3=A9") spans several escapes.
_QP_RUN = re.compile(r"(?:=[0-9A-Fa-f]{2})+")
_CHARSET = re.compile(r"""charset\s*=\s*["']?([^\s"';]+)""", re.IGNORECASE)

# --- Attachments and headings --------------------------------------------------
_FILE_REFERENCE = re.compile(r"<<([^<>\n]+)>>")  # "<< File: a.xls >>", "<<a.ppt>>"
_ATTACHMENT_WORD = re.compile(r"\b(?:attached|attachment)\b", re.IGNORECASE)
_UNDERLINE = re.compile(r"-{3,}|={3,}")
_WORD_OF_3_LETTERS = re.compile(r"[^\W\d_]{3,}")  # letters in any script
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
    """Is lines[index] the name line of a Lotus Notes inline reply header?

        Susan J Mara                  <- name line (index)
        04/11/2001 05:26 PM           <- date line
                                      <- blank lines are allowed here
        To: Jeff Dasovich/NA/Enron    <- To: line

    All three are needed, so a stray date line in ordinary text is not a marker.
    """
    if not lines[index].strip() or index + 1 >= len(lines):
        return False
    if not _NOTES_DATE.fullmatch(lines[index + 1].strip()):
        return False
    after_date = index + 2
    while after_date < len(lines) and not lines[after_date].strip():
        after_date += 1
    return after_date < len(lines) and lines[after_date].strip().lower().startswith("to:")


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
      - ALL CAPS with at least one word of 3+ letters;
      - the next line is a run of "---" or "===";
      - it ends in ":" with 5 words or fewer, and is not the first non-empty
        line (that is usually a salutation like "Mark:").
    """
    if len(line) > _MAX_HEADING_LENGTH or line.endswith("."):
        return False
    if not any(char.isalnum() for char in line):
        return False
    if line.isupper() and _WORD_OF_3_LETTERS.search(line):
        return True
    if _UNDERLINE.fullmatch(next_line):
        return True
    return (
        line.endswith(":")
        and len(line.split()) <= _MAX_COLON_HEADING_WORDS
        and not is_first_line
    )
