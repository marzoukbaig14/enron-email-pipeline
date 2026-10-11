"""Render the spec notification and hand it to a sender.

Behavior is fixed by D-022. Receives DuplicateFlags and a Sender; never a DB
connection or settings. The caller decides dry-run vs live by choosing the
sender, and does the database update after a successful live send. Imports:
models only.

The message follows the template in the assignment, section 4.2. Its four
header lines (To, Subject, Date, References) are OutgoingEmail fields, and the
rest is the body. The spec shows a blank line between every template line, and
the body keeps that spacing.
"""
import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Protocol

from enron_pipeline.models import DuplicateFlag, SendResult, SendStatus

SUBJECT_PREFIX = "[Duplicate Notice] Re: "
SEND_LOG_COLUMNS = ("timestamp", "recipient", "subject", "status", "error")

# A draft written by DryRunSender: "<n>_<sanitized message id>.eml". Only files
# with this shape are ever deleted from the drafts folder.
_DRAFT_NAME = re.compile(r"\d+_.*\.eml")
_UNSAFE_FILE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_ID_IN_NAME = 120


@dataclass(frozen=True)
class OutgoingEmail:
    to: str  # the template's To line: the sender of the flagged duplicate
    subject: str
    date: datetime  # send time, timezone-aware
    references: str  # message_id of the flagged email
    body: str


class Sender(Protocol):
    """Anything with a send(message) method. Structural: no inheritance needed."""

    def send(self, message: OutgoingEmail) -> SendResult: ...


class DryRunSender:
    """Writes one .eml draft per message into output_dir; sends nothing.

    Drafts are named "<n>_<sanitized message id>.eml", n counting up from 1
    (zero-padded to 4 digits so they sort). Creating the sender creates the
    folder and deletes drafts left by an earlier run, so the folder always
    matches the current run. Only files shaped like a draft are deleted.

    Each result is SendResult(status=SendStatus.DRY_RUN), with the Enron
    sender as recipient and the message's Date as timestamp.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for old in self.output_dir.iterdir():
            if old.is_file() and _DRAFT_NAME.fullmatch(old.name):
                old.unlink()
        self._count = 0

    def send(self, message: OutgoingEmail) -> SendResult:
        self._count += 1
        name = f"{self._count:04d}_{_safe_id(message.references)}.eml"
        (self.output_dir / name).write_bytes(_to_eml(message))
        return SendResult(
            timestamp=message.date,
            recipient=message.to,
            subject=message.subject,
            status=SendStatus.DRY_RUN,
            error=None,
            duplicate_message_id=message.references,
        )


class McpSender:
    """Live sender: delivers through the Gmail MCP server's send_email tool.

    `notify_to` is the NOTIFY_TO address (D-022 item 3). Enron addresses are
    dead, so every live email is delivered there while the message keeps the
    real Enron sender in its To line. Creating the sender without a usable
    address raises ValueError: that is how live mode refuses to start.

    The MCP connection itself is not built yet: send() raises until it is.
    """

    def __init__(self, notify_to: str | None) -> None:
        address = (notify_to or "").strip()
        if "@" not in address:
            raise ValueError(
                "NOTIFY_TO must be set to the email address that receives live "
                f"notifications (got {notify_to!r})"
            )
        self.notify_to = address

    def send(self, message: OutgoingEmail) -> SendResult:
        raise NotImplementedError("configure MCP first")


def render_notification(flag: DuplicateFlag, now: datetime) -> OutgoingEmail:
    """Fill the spec's notification template for one flagged duplicate.

    `now` is the send time (the Date header). Dates inside the body are ISO
    8601 UTC like the database, and the score has two decimals and a "%".
    The subject is the flagged email's own, as written. Newlines are removed
    from the header subject so it cannot break the header.
    """
    lines = [
        "This is an automated notification from the Email Deduplication System.",
        "Your email has been identified as a potential duplicate:",
        "Your Email (Flagged):",
        f"Message-ID: {flag.duplicate_message_id}",
        f"Date Sent: {_iso(flag.duplicate_date)}",
        f"Subject: {flag.subject}",
        "Original Email on Record:",
        f"Message-ID: {flag.original_message_id}",
        f"Date Sent: {_iso(flag.original_date)}",
        f"Similarity Score: {flag.similarity_score:.2f}%",
        "If this was NOT a duplicate and you intended to send this email,",
        "please reply with CONFIRM to restore it to active status.",
        "No action is required if this is indeed a duplicate.",
    ]
    return OutgoingEmail(
        to=flag.from_address,
        subject=_one_line(SUBJECT_PREFIX + flag.subject),
        date=now,
        references=flag.duplicate_message_id,
        body="\n\n".join(lines) + "\n",
    )


def select_live_flags(flags: Sequence[DuplicateFlag], limit: int) -> list[DuplicateFlag]:
    """The flags a live run sends: the latest duplicate of each group, capped.

    A group is an original plus its flagged duplicates, so flags that share
    original_message_id are one group. Its latest duplicate is the one with
    the greatest duplicate_date (a tie goes to the greater message id, so the
    choice never depends on input order). Groups come in the order of
    duplicates_report.csv (original date, then original message id) and the
    first `limit` are kept, so a small --limit means "the first N groups of
    the report". Raises ValueError for a negative limit.
    """
    if limit < 0:
        raise ValueError(f"limit must not be negative, got {limit}")
    latest: dict[str, DuplicateFlag] = {}
    for flag in flags:
        current = latest.get(flag.original_message_id)
        if current is None or _recency(flag) > _recency(current):
            latest[flag.original_message_id] = flag
    chosen = sorted(latest.values(), key=lambda f: (f.original_date, f.original_message_id))
    return chosen[:limit]


def send_notifications(
    flags: Sequence[DuplicateFlag],
    sender: Sender,
    now: datetime | None = None,
) -> list[SendResult]:
    """Render and send one notification per flag, in order.

    Never raises: an exception while rendering or sending becomes a result
    with status FAILED and the error "<ExceptionType>: <message>", and the run
    moves on to the next flag. Without `now`, each message is stamped with the
    current UTC time in whole seconds.
    """
    results: list[SendResult] = []
    for flag in flags:
        sent_at = now if now is not None else datetime.now(timezone.utc).replace(microsecond=0)
        recipient = flag.from_address
        subject = _one_line(SUBJECT_PREFIX + flag.subject)
        try:
            message = render_notification(flag, sent_at)
            recipient, subject = message.to, message.subject
            results.append(sender.send(message))
        except Exception as error:  # one bad message must never stop the run
            results.append(
                SendResult(
                    timestamp=sent_at,
                    recipient=recipient,
                    subject=subject,
                    status=SendStatus.FAILED,
                    error=f"{type(error).__name__}: {' '.join(str(error).split())}",
                    duplicate_message_id=flag.duplicate_message_id,
                )
            )
    return results


def write_send_log(results: Sequence[SendResult], path: Path) -> None:
    """Write send_log.csv: timestamp, recipient, subject, status, error.

    One row per attempt, dry run or live. The file is rewritten each run and
    its folder is created if needed. Timestamps are ISO 8601 UTC text; error
    is empty when there was none.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SEND_LOG_COLUMNS)
        for result in results:
            writer.writerow(
                [
                    _iso(result.timestamp),
                    result.recipient,
                    result.subject,
                    result.status.value,
                    result.error or "",
                ]
            )


def _to_eml(message: OutgoingEmail) -> bytes:
    """The message as RFC 822 bytes: the four template headers plus a plain-text body."""
    eml = EmailMessage()
    eml["To"] = message.to
    eml["Subject"] = message.subject
    eml["Date"] = format_datetime(message.date)
    eml["References"] = message.references
    eml.set_content(message.body)
    return eml.as_bytes()


def _safe_id(message_id: str) -> str:
    """A message id as a safe file-name part: no brackets, only [A-Za-z0-9._-]."""
    return _UNSAFE_FILE_CHARS.sub("_", message_id.strip("<>"))[:_MAX_ID_IN_NAME]


def _one_line(text: str) -> str:
    return re.sub(r"[\r\n]+", " ", text)


def _recency(flag: DuplicateFlag) -> tuple:
    return (flag.duplicate_date, flag.duplicate_message_id)


def _iso(value: datetime) -> str:
    """A timezone-aware datetime as ISO 8601 UTC text ending in "Z"."""
    if value.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware, got naive {value!r}")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
