"""Raw bytes of one email file -> parsed headers plus the raw body text.

Pure: no file, DB or config access. This is the only module that sees bytes,
so it owns decoding. Imports: models only.
"""
from dataclasses import dataclass
from datetime import datetime

from enron_pipeline.models import ParseError  # noqa: F401  (raised by parse_headers)


@dataclass(frozen=True, slots=True)
class ParsedHeaders:
    message_id: str
    date: datetime  # timezone-aware, UTC
    date_raw: str | None
    from_address: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str  # "" if absent
    x_from: str | None
    x_to: str | None
    x_cc: str | None
    x_bcc: str | None
    x_folder: str | None
    x_origin: str | None
    content_type: str | None
    raw_body: str  # everything after the header block, decoded


def parse_headers(raw: bytes) -> ParsedHeaders:
    """Parse one raw RFC 2822 message.

    Raises ParseError when a mandatory header (message_id, date, from, to) is
    missing or unparseable; the reason names the header, e.g.
    "missing To header".
    """
    raise NotImplementedError


def parse_date(value: str) -> datetime:
    """Parse a Date header into a timezone-aware UTC datetime.

    Handles abbreviations (PST, EST, CDT, ...) and numeric offsets.
    Raises ParseError if the value cannot be parsed.
    """
    raise NotImplementedError


def parse_address_list(value: str) -> list[str]:
    """Split a To/Cc/Bcc header into bare addresses.

    Unfolds continuation lines, splits on commas, and drops display names.
    """
    raise NotImplementedError
