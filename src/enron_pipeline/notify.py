"""Render the spec notification and hand it to a sender.

Behavior is fixed by D-022, amended by D-024. Receives DuplicateFlags and a
Sender; never a DB connection or settings. The caller decides dry-run vs live
by choosing the sender, and does the database update after a successful live
send. Imports: models. The MCP client libraries (mcp, anyio) are used only
here, by McpSender.

The message follows the template in the assignment, section 4.2. Its four
header lines (To, Subject, Date, References) are OutgoingEmail fields, and the
rest is the body. The spec shows a blank line between every template line, and
the body keeps that spacing.
"""
import csv
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Protocol

import anyio
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, MCPError
from mcp.client.stdio import StdioServerParameters, stdio_client

from enron_pipeline.models import DuplicateFlag, SendResult, SendStatus

NOTICE_TAG = "[Duplicate Notice] "
SUBJECT_PREFIX = NOTICE_TAG + "Re: "
SEND_LOG_COLUMNS = ("timestamp", "recipient", "subject", "status", "error")

# The Gmail MCP server the live sender launches (D-024 item 3). "-y" stops npx
# from stopping to ask whether it may install the package.
MCP_SERVER_COMMAND = "npx"
MCP_SERVER_ARGS = ("-y", "@gongrzhe/server-gmail-autoauth-mcp")
CONNECT_TIMEOUT_SECONDS = 120.0  # the first npx run may have to download the package
SEND_TIMEOUT_SECONDS = 60.0

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
    """Live sender: delivers through the Gmail MCP server's send_email tool (D-024).

    Python is the MCP client. The server is launched over stdio ("npx -y
    @gongrzhe/server-gmail-autoauth-mcp"), and one session is used for the
    whole run: it opens on the first send and closes in close() (or when a
    `with` block ends). A server that is never needed is never started.

    `notify_to` is the NOTIFY_TO address (D-022 item 3). Enron addresses are
    dead, so every live email is delivered there while the body keeps the real
    Enron sender in its To line. Creating the sender without a usable address
    raises ValueError: that is how live mode refuses to start.

    send() never raises. It returns a SendResult:
      - SENT when the server accepted the email;
      - FAILED, with the session still usable, for a tool error, an MCP error
        or a timeout (that one message is lost);
      - FAILED for every message from the moment the session is lost (the
        server crashed, the pipe closed, it could not start). There is no
        reconnect or retry, so a broken setup cannot spawn a process per
        email, and the reason is repeated in each message's error.

    The MCP SDK is async and Sender.send is not, so the session lives in an
    event loop on a background thread (anyio's blocking portal) and send()
    waits for each call. `connect` is a test hook: a function returning an async
    context manager that yields the session. The default launches the server.
    """

    def __init__(
        self,
        notify_to: str | None,
        *,
        connect: Callable[[], AbstractAsyncContextManager] | None = None,
        command: str = MCP_SERVER_COMMAND,
        args: Sequence[str] = MCP_SERVER_ARGS,
        call_timeout_seconds: float = SEND_TIMEOUT_SECONDS,
    ) -> None:
        address = (notify_to or "").strip()
        if "@" not in address:
            raise ValueError(
                "NOTIFY_TO must be set to the email address that receives live "
                f"notifications (got {notify_to!r})"
            )
        self.notify_to = address
        self.command = command
        self.args = tuple(args)
        self._connect = connect or (lambda: _stdio_session(self.command, self.args))
        self._timeout = call_timeout_seconds
        self._closed = False
        self._lost: str | None = None  # why the session is unusable, once it is
        self._portal_manager = None
        self._portal = None
        self._session_manager = None
        self._session = None

    def __enter__(self) -> "McpSender":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def send(self, message: OutgoingEmail) -> SendResult:
        try:
            return self._send(message)
        except Exception as error:  # nothing may escape into the pipeline
            return self._result(message, f"unexpected {_describe(error)}")

    def close(self) -> None:
        """End the session and stop the server. Safe to call twice or when unused."""
        self._closed = True
        self._shutdown()

    # -- internals ------------------------------------------------------------------

    def _send(self, message: OutgoingEmail) -> SendResult:
        arguments = self._send_email_arguments(message)  # a bad message fails before any connection
        if self._closed:
            return self._result(message, "MCP sender is closed")
        if self._lost:
            return self._result(message, self._lost)
        try:
            self._ensure_open()
        except Exception as error:
            return self._result(message, self._lose(f"could not start MCP server: {_describe(error)}"))

        try:
            reply = self._portal.call(self._call_send_email, arguments)
        except TimeoutError:
            return self._result(
                message,
                f"timed out after {self._timeout:g}s waiting for send_email "
                "(the email may or may not have been sent)",
            )
        except MCPError as error:
            return self._result(message, f"MCP error: {_describe(error, with_type=False)}")
        except Exception as error:
            return self._result(message, self._lose(f"MCP session lost: {_describe(error)}"))

        if not hasattr(reply, "is_error"):
            return self._result(message, f"unexpected result from send_email: {type(reply).__name__}")
        if reply.is_error:
            return self._result(message, f"send_email failed: {_reply_text(reply) or 'no details given'}")
        return self._result(message, None)

    def _send_email_arguments(self, message: OutgoingEmail) -> dict[str, Any]:
        """The arguments for the server's send_email tool.

        The names come from the server's own schema (scripts/list_mcp_tools.py)
        and it rejects any other. The email goes to NOTIFY_TO. inReplyTo carries
        the flagged email's message id, the closest thing the tool has to the
        spec's References header, which stays in the body as well.
        """
        return {
            "to": [self.notify_to],
            "subject": message.subject,
            "body": live_body(message),
            "mimeType": "text/plain",
            "inReplyTo": message.references,
        }

    async def _call_send_email(self, arguments: dict[str, Any]):
        with anyio.fail_after(self._timeout):
            return await self._session.call_tool("send_email", arguments)

    def _ensure_open(self) -> None:
        if self._session is not None:
            return
        portal_manager = start_blocking_portal()
        portal = portal_manager.__enter__()
        try:
            session_manager = portal.wrap_async_context_manager(self._connect())
            session = session_manager.__enter__()
        except BaseException:
            portal_manager.__exit__(None, None, None)  # stop the background loop again
            raise
        self._portal_manager, self._portal = portal_manager, portal
        self._session_manager, self._session = session_manager, session

    def _lose(self, reason: str) -> str:
        """Record that the session is unusable, shut it down, and return the reason."""
        self._lost = reason
        self._shutdown()
        return reason

    def _shutdown(self) -> None:
        session_manager, portal_manager = self._session_manager, self._portal_manager
        self._session = self._session_manager = self._portal = self._portal_manager = None
        for manager in (session_manager, portal_manager):
            if manager is None:
                continue
            try:
                manager.__exit__(None, None, None)
            except (Exception, BaseExceptionGroup):
                pass  # a dead session may not close cleanly; there is nothing left to do

    def _result(self, message: OutgoingEmail, error: str | None) -> SendResult:
        stamp = message.date if isinstance(message.date, datetime) else datetime.now(timezone.utc)
        return SendResult(
            timestamp=stamp,
            recipient=self.notify_to,
            subject=message.subject,
            status=SendStatus.SENT if error is None else SendStatus.FAILED,
            error=error,
            duplicate_message_id=message.references,
        )


@asynccontextmanager
async def _stdio_session(command: str, args: Sequence[str]) -> AsyncIterator[ClientSession]:
    """Launch the MCP server over stdio and yield an initialized session.

    Entering starts the subprocess and runs the MCP handshake (bounded by
    CONNECT_TIMEOUT_SECONDS); leaving stops the subprocess.
    """
    params = StdioServerParameters(command=command, args=list(args))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            with anyio.fail_after(CONNECT_TIMEOUT_SECONDS):
                await session.initialize()
            yield session


def live_body(message: OutgoingEmail) -> str:
    """The live email's body: the template's header block, a blank line, the notice.

    send_email has no free-form headers, so the spec template's four header
    lines go at the top of the body. To is the real Enron sender; the email
    itself is delivered to NOTIFY_TO.
    """
    header = [
        f"To: {message.to}",
        f"Subject: {message.subject}",
        f"Date: {format_datetime(message.date)}",
        f"References: {message.references}",
    ]
    return "\n".join(header) + "\n\n" + message.body


def _reply_text(reply) -> str:
    """The text parts of an MCP tool reply, joined."""
    parts = (getattr(item, "text", "") for item in getattr(reply, "content", None) or [])
    return " ".join(" ".join(part.split()) for part in parts if part)


def _describe(error: BaseException, with_type: bool = True) -> str:
    """"<ExceptionType>: <message>" on one line; an exception group is shown as its first leaf."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    text = " ".join(str(error).split())
    if not with_type:
        return text or type(error).__name__
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def render_notification(flag: DuplicateFlag, now: datetime) -> OutgoingEmail:
    """Fill the spec's notification template for one flagged duplicate.

    `now` is the send time (the Date header). Dates inside the body are ISO
    8601 UTC like the database, and the score has two decimals and a "%".
    The subject is built from the ORIGINAL email's subject (notice_subject);
    the body's "Subject:" line under "Your Email (Flagged)" is the flagged
    email's own. Newlines are removed from the header subject so it cannot
    break the header.
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
        subject=notice_subject(flag.original_subject),
        date=now,
        references=flag.duplicate_message_id,
        body="\n\n".join(lines) + "\n",
    )


def notice_subject(original_subject: str) -> str:
    """"[Duplicate Notice] Re: <original subject>", with no second "Re:" (D-024 item 1).

    If the subject already starts with "Re:" (any case, leading spaces ignored)
    it is kept as written: "Re: X" gives "[Duplicate Notice] Re: X".
    """
    subject = _one_line(original_subject)
    if subject.lstrip().lower().startswith("re:"):
        return NOTICE_TAG + subject.lstrip()
    return SUBJECT_PREFIX + subject


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
        subject = notice_subject(flag.original_subject)
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


def write_send_log(
    results: Sequence[SendResult], path: Path, *, append: bool = False
) -> None:
    """Write a send log: timestamp, recipient, subject, status, error.

    One row per attempt. By default the file is rewritten. With `append=True`
    the rows go after what is already there and the header is written only if
    the file is new or empty (D-024: live runs append, so nothing later can
    wipe them). The folder is created if needed. Timestamps are ISO 8601 UTC
    text; error is empty when there was none.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    has_rows_already = append and path.exists() and path.stat().st_size > 0
    with open(path, "a" if append else "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not has_rows_already:
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
