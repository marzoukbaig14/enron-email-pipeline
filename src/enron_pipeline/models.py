"""Shared record types (D-014).

This is the only module that every other module may import. It holds plain
dataclasses, two enums and one exception, and no logic beyond checking values
on construction. All records are frozen and use slots, so a misspelled field
name fails loudly instead of silently creating a new attribute. Python does
not check dataclass types at runtime, so __post_init__ validates the enum
fields and the recipients tuple (D-015).

Datetimes are timezone-aware UTC. The db module converts them to and from the
ISO 8601 UTC text stored in schema.sql.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class RecipientKind(str, Enum):
    """Which header a recipient came from. Stored as plain text in SQLite."""

    TO = "to"
    CC = "cc"
    BCC = "bcc"


class SendStatus(str, Enum):
    """Outcome of a send attempt. Stored as plain text in send_log.csv."""

    SENT = "sent"
    DRY_RUN = "dry_run"
    FAILED = "failed"


class ParseError(Exception):
    """A file could not be turned into a valid record.

    `reason` is a short, stable string (e.g. "missing To header") so failures
    can be grouped in the stats.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Recipient:
    """One To/Cc/Bcc address; one row of the `recipients` table."""

    address: str
    kind: RecipientKind  # a plain "to"/"cc"/"bcc" string is converted
    position: int  # index within that header

    def __post_init__(self) -> None:
        # Frozen, so assign through object.__setattr__. RecipientKind("To")
        # raises ValueError: matching is case-sensitive.
        object.__setattr__(self, "kind", RecipientKind(self.kind))


@dataclass(frozen=True, slots=True)
class EmailRecord:
    """One row of `emails` plus its recipients (maps 1:1 to schema.sql).

    The fields marked DB-managed are set by dedup/db after ingest. Parsers
    leave them at their defaults. `recipients` may be given as any iterable of
    Recipient; it is stored as a tuple so it cannot be changed in place.
    """

    # Mandatory (schema NOT NULL)
    message_id: str
    date: datetime
    from_address: str
    subject: str
    body: str
    source_file: str
    mailbox: str

    recipients: tuple[Recipient, ...] = ()

    # Optional, extracted if present
    date_raw: str | None = None
    x_from: str | None = None
    x_to: str | None = None
    x_cc: str | None = None
    x_bcc: str | None = None
    x_folder: str | None = None
    x_origin: str | None = None
    content_type: str | None = None
    has_attachment: bool = False
    forwarded_content: str | None = None
    quoted_content: str | None = None
    headings: str | None = None
    content_hash: str | None = None

    # DB-managed
    id: int | None = None
    copy_of: str | None = None
    is_duplicate: bool = False
    duplicate_of: str | None = None
    similarity_score: float | None = None
    notification_sent: bool = False
    notification_date: datetime | None = None

    def __post_init__(self) -> None:
        recipients = tuple(self.recipients)
        for item in recipients:
            if not isinstance(item, Recipient):
                raise TypeError(
                    f"recipients must contain Recipient objects, got {item!r}"
                )
        object.__setattr__(self, "recipients", recipients)


@dataclass(frozen=True, slots=True)
class ParseFailure:
    """One row of `parse_failures` (and one line of error_log.txt)."""

    source_file: str
    reason: str
    failed_at: datetime


@dataclass(frozen=True, slots=True)
class DuplicateFlag:
    """One flagged duplicate. Same fields as a duplicates_report.csv row.

    Produced by dedup, consumed by notify.
    """

    duplicate_message_id: str
    original_message_id: str
    subject: str
    from_address: str
    duplicate_date: datetime
    original_date: datetime
    similarity_score: float  # 0-100


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of one send attempt.

    The first five fields are the send_log.csv columns;
    duplicate_message_id links the result back to the flagged email.
    """

    timestamp: datetime
    recipient: str
    subject: str
    status: SendStatus  # a plain "sent"/"dry_run"/"failed" string is converted
    error: str | None
    duplicate_message_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SendStatus(self.status))
