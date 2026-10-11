"""Raw bytes of one email file -> parsed headers plus the raw body text.

Pure: no file, DB or config access. This is the only module that sees bytes,
so it owns decoding. Imports: models only.

Behavior is fixed by D-016 and D-017. Failures raise ParseError with one of
three fixed reason formats so they can be grouped in the stats:
    "missing <field>"            e.g. "missing to", "missing message_id"
    "invalid <field>: <detail>"  e.g. "invalid date: no timezone"
    "empty input"
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.parser import Parser
from email.utils import parsedate_to_datetime

from enron_pipeline.models import ParseError


@dataclass(frozen=True, slots=True)
class ParsedHeaders:
    message_id: str
    date: datetime  # timezone-aware, UTC
    date_raw: str | None
    from_address: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str  # "" if blank
    x_from: str | None
    x_to: str | None
    x_cc: str | None
    x_bcc: str | None
    x_folder: str | None
    x_origin: str | None
    content_type: str | None
    raw_body: str  # everything after the header block, decoded


# Hours from UTC for the timezone abbreviations D-016 allows. Anything else
# (including real ones like "Z" or "AST") is rejected rather than guessed.
_ABBREVIATION_HOURS = {
    "PST": -8, "PDT": -7,
    "MST": -7, "MDT": -6,
    "CST": -6, "CDT": -5,
    "EST": -5, "EDT": -4,
    "GMT": 0, "UT": 0, "UTC": 0,
}

_BLANK_LINE = re.compile(r"\r?\n\r?\n")  # ends the header block
_FOLD = re.compile(r"[ \t]*\r?\n[ \t]*")  # a line break plus its indentation
_COMMENT = re.compile(r"\([^)]*\)")  # "(PDT)" after a date
_NUMERIC_TZ = re.compile(r"\s([+-])(\d{2})(\d{2})$")
_ABBREVIATION_TZ = re.compile(r"\s([A-Za-z]{1,4})$")
_ANGLE_ADDRESS = re.compile(r"<([^<>]*)>")
# Starts with a letter; then letters (any script), spaces, hyphens, apostrophes.
_SURNAME = re.compile(r"[^\W\d_](?:[^\W\d_]|[ '\-])*")


def parse_headers(raw: bytes) -> ParsedHeaders:
    """Parse one raw RFC 2822 message.

    Raises ParseError when a mandatory header (message_id, date, from, to,
    subject) is missing or unusable. Mandatory headers are checked in that
    order, so a file with several problems reports the first one.
    """
    text = _decode(raw)
    if not text.strip():
        raise ParseError("empty input")

    # The header block ends at the first blank line. With no blank line the
    # whole input is headers and the body is empty (D-017).
    blank = _BLANK_LINE.search(text)
    if blank:
        header_text, raw_body = text[: blank.start()], text[blank.end() :]
    else:
        header_text, raw_body = text, ""

    # The stdlib parser handles folding and case-insensitive names for us.
    # headersonly=True stops it from trying to interpret the body.
    msg = Parser().parsestr(header_text, headersonly=True)

    message_id = _header(msg, "Message-ID")
    if not message_id:
        raise ParseError("missing message_id")

    date_raw = _header(msg, "Date")
    if not date_raw:
        raise ParseError("missing date")
    date = parse_date(date_raw)

    from_value = _header(msg, "From")
    if not from_value:
        raise ParseError("missing from")
    from_addresses = parse_address_list(from_value)
    if not from_addresses:
        raise ParseError("invalid from: no address")
    if "@" not in from_addresses[0]:
        raise ParseError("invalid from: no @ in address")

    to = parse_address_list(_header(msg, "To") or "")
    if not to:  # header absent, blank, or only commas
        raise ParseError("missing to")

    subject = _header(msg, "Subject")
    if subject is None:
        raise ParseError("missing subject")

    return ParsedHeaders(
        message_id=message_id,
        date=date,
        date_raw=date_raw,
        from_address=from_addresses[0],
        to=to,
        cc=parse_address_list(_header(msg, "Cc") or ""),
        bcc=parse_address_list(_header(msg, "Bcc") or ""),
        subject=subject,
        x_from=_header(msg, "X-From") or None,
        x_to=_header(msg, "X-To") or None,
        x_cc=_header(msg, "X-cc") or None,
        x_bcc=_header(msg, "X-bcc") or None,
        x_folder=_header(msg, "X-Folder") or None,
        x_origin=_header(msg, "X-Origin") or None,
        content_type=_header(msg, "Content-Type") or None,
        raw_body=raw_body,
    )


def parse_date(value: str) -> datetime:
    """Parse a Date header into a timezone-aware UTC datetime.

    Uses the numeric offset if there is one, else maps the timezone
    abbreviation (only those in _ABBREVIATION_HOURS), else fails. Raises
    ParseError("invalid date: <detail>") if the value cannot be parsed.
    """
    text = _COMMENT.sub("", value).strip()
    if not text:
        raise ParseError("invalid date: empty value")

    # Split the timezone off the end of the string, if there is one.
    offset: timedelta | None = None
    abbreviation: str | None = None
    numeric = _NUMERIC_TZ.search(text)
    if numeric:
        sign, hours, minutes = numeric.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        if sign == "-":
            offset = -offset
        text = text[: numeric.start()]
    else:
        named = _ABBREVIATION_TZ.search(text)
        if named:
            abbreviation = named.group(1).upper()
            text = text[: named.start()]

    # Parse the date and time on their own, labelled UTC for now. This tells
    # a garbled date apart from a good date that has no timezone.
    try:
        local = parsedate_to_datetime(text + " +0000")
    except (TypeError, ValueError, IndexError, OverflowError):
        raise ParseError("invalid date: unparseable") from None

    if abbreviation is not None:
        if abbreviation not in _ABBREVIATION_HOURS:
            raise ParseError(f"invalid date: unknown timezone {abbreviation}")
        offset = timedelta(hours=_ABBREVIATION_HOURS[abbreviation])
    if offset is None:
        raise ParseError("invalid date: no timezone")

    # `local` is the wall-clock time from the header. Attach its real offset,
    # then convert to UTC.
    try:
        return local.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ParseError("invalid date: offset out of range") from None


def parse_address_list(value: str) -> list[str]:
    """Split a To/Cc/Bcc header into bare, lowercased addresses.

    Unfolds continuation lines, splits on commas, drops display names and
    keeps entries with no "@" as they are (lowercased). Repeated addresses are
    kept so their position stays meaningful. Empty entries are dropped.

    A comma inside a display name does not split the recipient, quoted or
    not: '"Lay, Kenneth" <k@x.com>', 'Lay, Kenneth <k@x.com>' and the LDAP
    style 'Jordan, Mike </O=ENRON/OU=EU/cn=Recipients/cn=mjordan2>' are each
    one recipient. For LDAP entries the text inside <...> is the address.
    Only a surname-like segment (letters, spaces, hyphens, apostrophes) is
    glued to the entry after it, and unbalanced double quotes are ignored.
    """
    segments = [s.strip() for s in _split_commas(_unfold(value))]
    segments = [s for s in segments if s]

    # An unquoted "Last, First <addr>" arrives as the two segments "Last" and
    # "First <addr>". A segment that looks like a surname (_SURNAME: letters,
    # spaces, hyphens, apostrophes; so no "@" and no dot) and is followed by a
    # segment containing "<" is the front of a display name, so glue them
    # back together. Anything else, like "tim.belden" in "tim.belden,
    # Mike <m@x.com>", stays its own recipient.
    entries: list[str] = []
    pending = ""
    for index, segment in enumerate(segments):
        candidate = f"{pending}, {segment}" if pending else segment
        looks_like_surname = _SURNAME.fullmatch(segment) is not None
        next_has_bracket = index + 1 < len(segments) and "<" in segments[index + 1]
        if looks_like_surname and next_has_bracket:
            pending = candidate
        else:
            entries.append(candidate)
            pending = ""

    addresses = (_address_of(entry) for entry in entries)
    return [address for address in addresses if address]


def _decode(raw: bytes) -> str:
    """Bytes -> text without ever failing: UTF-8, else Latin-1 (accepts any byte)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _unfold(value: str) -> str:
    """Join a folded header value into one line; each fold becomes one space."""
    return _FOLD.sub(" ", value).strip()


def _header(msg, name: str) -> str | None:
    """Unfolded, stripped value of a header, or None if it is absent.

    A header that is present but blank gives "", so callers can tell the two
    apart (a blank Subject is stored, a missing one fails).
    """
    value = msg.get(name)
    return None if value is None else _unfold(str(value))


def _split_commas(value: str) -> list[str]:
    """Split on commas that are outside double quotes and outside <...>.

    If the double quotes do not pair up, quotes are ignored and only <...>
    protects a comma. Otherwise one stray quote would turn the rest of the
    header into a single entry and silently drop every later recipient.
    """
    parts: list[str] = []
    current: list[str] = []
    unescaped_quotes = len(re.findall(r'(?<!\\)"', value))
    honor_quotes = unescaped_quotes % 2 == 0
    in_quotes = False
    in_angle = False
    previous = ""
    for char in value:
        if char == '"' and previous != "\\":
            if honor_quotes:
                in_quotes = not in_quotes
        elif not in_quotes:
            if char == "<":
                in_angle = True
            elif char == ">":
                in_angle = False
            elif char == "," and not in_angle:
                parts.append("".join(current))
                current = []
                previous = char
                continue
        current.append(char)
        previous = char
    parts.append("".join(current))
    return parts


def _address_of(entry: str) -> str:
    """The address in one recipient entry, lowercased.

    Text inside <...> wins (that also covers LDAP names). Otherwise the entry
    is taken as a bare address with any "(comment)" and surrounding quotes
    removed.
    """
    bracketed = _ANGLE_ADDRESS.findall(entry)
    address = bracketed[-1] if bracketed else _COMMENT.sub("", entry)
    return address.strip().strip("'\"").strip().lower()
