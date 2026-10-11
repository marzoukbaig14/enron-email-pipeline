"""Wire discover -> parse_headers -> body parser -> EmailRecord -> db, per file.

Behavior is fixed by D-018. One file never stops the run: a ParseError is
logged with its reason as is, and any other Exception is logged as
"unexpected <ExceptionType>: <message>" so it can be told apart and grouped.
That covers unreadable files and database errors (such as a duplicate
Message-ID) as well. Each failure goes to the parse_failures table and to the
error log, and the run moves on to the next file.

The body parser is passed in as a function with the same signature as
parse_body(raw_body, content_type, content_transfer_encoding) (module-layout.md,
D-019), so ingest can be built and tested before parse_body exists. main.py
passes parse_body in. ingest never imports it.

Settings arrive as arguments (no config import).
Imports: models, discover, parse_headers, db.
"""
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enron_pipeline import db
from enron_pipeline.discover import discover_email_files, mailbox_for, source_path
from enron_pipeline.models import (
    EmailRecord,
    ParseError,
    ParseFailure,
    Recipient,
    RecipientKind,
)
from enron_pipeline.parse_headers import parse_headers

if TYPE_CHECKING:  # for the type hint only; no runtime dependency on parse_body
    from enron_pipeline.parse_body import ParsedBody

# (raw_body, content_type, content_transfer_encoding) -> object with the
# ParsedBody attributes: body, forwarded_content, quoted_content, headings,
# has_attachment.
BodyParser = Callable[[str, "str | None", "str | None"], "ParsedBody | Any"]

# Every field in Section 3 of the spec (mandatory, then optional). The names
# of the three recipient lists follow the spec: to_addresses, cc_addresses,
# bcc_addresses.
STATS_FIELDS: tuple[str, ...] = (
    "message_id", "date", "from_address", "to_addresses", "subject", "body",
    "source_file",
    "cc_addresses", "bcc_addresses", "x_from", "x_to", "x_cc", "x_bcc",
    "x_folder", "x_origin", "content_type", "has_attachment",
    "forwarded_content", "quoted_content", "headings",
)


@dataclass
class ExtractionStats:
    """Counts for one ingest run (spec Task 1, item 5).

    parsed means "stored in the emails table", so total_found = parsed + failed.
    """

    total_found: int = 0
    parsed: int = 0
    failed: int = 0
    failures_by_reason: dict[str, int] = field(default_factory=dict)
    field_counts: dict[str, int] = field(default_factory=dict)  # populated count per field
    has_attachment_true: int = 0  # parsed emails whose has_attachment is True

    def completeness(self) -> dict[str, float]:
        """Percent (0-100) of parsed emails with a populated value, per field.

        Populated means non-null and non-empty: a string with only whitespace
        and an empty list both count as empty. A boolean is never null, so
        has_attachment always counts. Failed files have no record, so only
        parsed emails are in the denominator. With nothing parsed every field
        is 0.0.
        """
        if self.parsed == 0:
            return {name: 0.0 for name in self.field_counts}
        return {
            name: 100.0 * count / self.parsed
            for name, count in self.field_counts.items()
        }

    def has_attachment_true_percent(self) -> float:
        """Percent (0-100) of parsed emails with has_attachment True.

        Separate from completeness()["has_attachment"], which is always 100%
        because a boolean is never null. 0.0 when nothing was parsed.
        """
        if self.parsed == 0:
            return 0.0
        return 100.0 * self.has_attachment_true / self.parsed

    def to_dict(self) -> dict:
        """The shape saved as extraction_stats.json."""
        by_reason = sorted(self.failures_by_reason.items(), key=lambda item: (-item[1], item[0]))
        return {
            "total_found": self.total_found,
            "parsed": self.parsed,
            "failed": self.failed,
            "failures_by_reason": dict(by_reason),
            "field_counts": dict(self.field_counts),
            "completeness_percent": {
                name: round(percent, 2) for name, percent in self.completeness().items()
            },
            "has_attachment_true_percent": round(self.has_attachment_true_percent(), 2),
        }

    def _record_parsed(self, record: EmailRecord) -> None:
        self.parsed += 1
        if record.has_attachment:
            self.has_attachment_true += 1
        for name, value in _field_values(record).items():
            if _is_populated(value):
                self.field_counts[name] += 1

    def _record_failed(self, reason: str) -> None:
        self.failed += 1
        self.failures_by_reason[reason] = self.failures_by_reason.get(reason, 0) + 1


def process_file(maildir: Path, source_file: str, body_parser: BodyParser) -> EmailRecord:
    """Read and parse one file into an EmailRecord (not yet stored).

    `source_file` is the string discover yields, e.g.
    "maildir/beck-s/inbox/1.". Raises ParseError for known parse failures.
    Anything else (read errors, a crashing body parser) propagates to
    ingest_mailboxes, which catches it.
    """
    raw = source_path(maildir, source_file).read_bytes()
    headers = parse_headers(raw)
    parsed = body_parser(
        headers.raw_body, headers.content_type, headers.content_transfer_encoding
    )

    # A recipient's position is its index within its own header.
    recipients = [
        Recipient(address, kind, position)
        for kind, addresses in (
            (RecipientKind.TO, headers.to),
            (RecipientKind.CC, headers.cc),
            (RecipientKind.BCC, headers.bcc),
        )
        for position, address in enumerate(addresses)
    ]
    return EmailRecord(
        message_id=headers.message_id,
        date=headers.date,
        from_address=headers.from_address,
        subject=headers.subject,
        body=parsed.body,
        source_file=source_file,
        mailbox=mailbox_for(source_file),
        recipients=recipients,
        date_raw=headers.date_raw,
        x_from=headers.x_from,
        x_to=headers.x_to,
        x_cc=headers.x_cc,
        x_bcc=headers.x_bcc,
        x_folder=headers.x_folder,
        x_origin=headers.x_origin,
        content_type=headers.content_type,
        has_attachment=parsed.has_attachment,
        forwarded_content=parsed.forwarded_content,
        quoted_content=parsed.quoted_content,
        headings=parsed.headings,
        # content_hash stays None: what goes into it is still an open point
        # (module-layout.md, open question 4).
    )


def ingest_mailboxes(
    conn: sqlite3.Connection,
    maildir: Path,
    mailboxes: Sequence[str] | None,
    body_parser: BodyParser,
    error_log: Path | None = None,
) -> ExtractionStats:
    """Ingest every file in the chosen mailboxes and return the stats.

    A missing mailbox raises FileNotFoundError before any file is touched:
    that is a setup mistake, not a bad file. After that, every per-file
    problem becomes a ParseFailure (stored in parse_failures and written to
    `error_log` if a path is given) and the loop continues.

    The run is one transaction, so it is fast on a file database. If the
    caller already opened a transaction, the commit is left to the caller.
    """
    maildir = Path(maildir)
    source_files = list(discover_email_files(maildir, mailboxes))
    stats = ExtractionStats(
        total_found=len(source_files), field_counts={name: 0 for name in STATS_FIELDS}
    )
    failures: list[ParseFailure] = []

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        for source_file in source_files:
            try:
                record = process_file(maildir, source_file, body_parser)
                db.insert_email(conn, record)
            except Exception as error:  # one bad file must never stop the run
                failure = ParseFailure(
                    source_file=source_file,
                    reason=_reason_for(error),
                    failed_at=datetime.now(timezone.utc),
                )
                failures.append(failure)
                db.insert_parse_failure(conn, failure)
                stats._record_failed(failure.reason)
            else:
                stats._record_parsed(record)
    finally:
        if owns_transaction:
            conn.commit()

    if error_log is not None:
        write_error_log(failures, error_log)
    return stats


def write_error_log(failures: Sequence[ParseFailure], path: Path) -> None:
    """Write error_log.txt: one "<source_file> | <reason>" line per failure.

    Creates the file (empty if there are no failures) and its folder.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for failure in failures:
            handle.write(f"{failure.source_file} | {failure.reason}\n")


def format_stats(stats: ExtractionStats) -> str:
    """The console report. main.py prints it; ingest itself never prints."""
    lines = [
        "Extraction statistics",
        f"  Total files found: {stats.total_found}",
        f"  Parsed: {stats.parsed}",
        f"  Failed: {stats.failed}",
        "",
        "Failures by reason",
    ]
    data = stats.to_dict()
    if data["failures_by_reason"]:
        lines += [f"  {count:>6}  {reason}" for reason, count in data["failures_by_reason"].items()]
    else:
        lines.append("  none")
    lines += ["", "Field completeness (% of parsed emails, non-null and non-empty)"]
    lines += [
        f"  {name:<18} {percent:6.1f}%"
        for name, percent in stats.completeness().items()
    ]
    lines += ["", f"  has_attachment true: {stats.has_attachment_true_percent():.1f}%"]
    return "\n".join(lines)


def write_stats(stats: ExtractionStats, path: Path) -> None:
    """Save the stats as JSON (output/extraction_stats.json). Creates the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats.to_dict(), indent=2) + "\n", encoding="utf-8")


def _reason_for(error: Exception) -> str:
    """A ParseError's reason as is; anything else as "unexpected Type: message".

    Whitespace in the message is collapsed so a failure is always one log line.
    """
    if isinstance(error, ParseError):
        return error.reason
    message = " ".join(str(error).split())
    return f"unexpected {type(error).__name__}: {message}"


def _field_values(record: EmailRecord) -> dict[str, Any]:
    """The STATS_FIELDS values of one record."""

    def addresses(kind: RecipientKind) -> list[str]:
        return [r.address for r in record.recipients if r.kind == kind]

    return {
        "message_id": record.message_id,
        "date": record.date,
        "from_address": record.from_address,
        "to_addresses": addresses(RecipientKind.TO),
        "subject": record.subject,
        "body": record.body,
        "source_file": record.source_file,
        "cc_addresses": addresses(RecipientKind.CC),
        "bcc_addresses": addresses(RecipientKind.BCC),
        "x_from": record.x_from,
        "x_to": record.x_to,
        "x_cc": record.x_cc,
        "x_bcc": record.x_bcc,
        "x_folder": record.x_folder,
        "x_origin": record.x_origin,
        "content_type": record.content_type,
        "has_attachment": record.has_attachment,
        "forwarded_content": record.forwarded_content,
        "quoted_content": record.quoted_content,
        "headings": record.headings,
    }


def _is_populated(value: Any) -> bool:
    """Non-null and non-empty. Whitespace-only text and empty lists are empty."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return len(value) > 0
    return True  # datetime, bool: present means populated
