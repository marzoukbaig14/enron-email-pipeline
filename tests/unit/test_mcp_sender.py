"""Tests for notify.McpSender, the live sender (behavior: D-024 items 3 to 6).

Nothing here starts a server or touches the network. McpSender takes a
`connect` function that returns an async context manager yielding the MCP
session; the tests pass a fake one. That exercises the real code paths (the
background event loop, the single shared session, timeouts, error handling)
with a session whose replies the test controls.

The fake session mimics the two things the sender reads from a real
`ClientSession.call_tool` result: `is_error` and `content[*].text`. The send_email
argument names come from the server's own schema (scripts/list_mcp_tools.py):
required `to` (a list), `subject`, `body`; optional `mimeType`, `inReplyTo`, ...;
nothing else is allowed.
"""
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email import utils as email_utils
from types import SimpleNamespace

import anyio
import pytest
from mcp import MCPError

if sys.version_info < (3, 11):  # exception groups are built in from Python 3.11
    from exceptiongroup import ExceptionGroup

from enron_pipeline import notify
from enron_pipeline.models import DuplicateFlag, SendStatus
from enron_pipeline.notify import McpSender, send_notifications

NOW = datetime(2026, 10, 11, 18, 30, 5, tzinfo=timezone.utc)
NOTIFY_TO = "me@example.com"
SCHEMA_PROPERTIES = {
    "to", "subject", "body", "htmlBody", "mimeType", "cc", "bcc", "threadId", "inReplyTo", "attachments",
}


def make_flag(n=1, subject="Q2 numbers", original_subject=None, sender="phillip.allen@enron.com"):
    return DuplicateFlag(
        duplicate_message_id=f"<dup{n}@thyme>",
        original_message_id="<orig@thyme>",
        subject=subject,
        original_subject=subject if original_subject is None else original_subject,
        from_address=sender,
        duplicate_date=datetime(2001, 5, 14, 23, 39, tzinfo=timezone.utc),
        original_date=datetime(2001, 5, 10, 9, 0, tzinfo=timezone.utc),
        similarity_score=97.5,
    )


def ok(text="Email sent successfully with ID: 18f2a"):
    return SimpleNamespace(is_error=False, content=[SimpleNamespace(text=text)])


def tool_error(text="Invalid email address"):
    return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=text)])


class FakeSession:
    """Answers call_tool from a script. An exception in the script is raised, not returned."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments))
        reply = self.replies.pop(0) if self.replies else ok()
        if callable(reply):
            reply = await reply()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeConnect:
    """The `connect` argument: counts how often a session was opened and closed."""

    def __init__(self, session=None, start_error=None, exit_error=None):
        self.session = session or FakeSession([])
        self.start_error = start_error
        self.exit_error = exit_error
        self.attempts = 0
        self.opened = 0
        self.closed = 0

    def __call__(self):
        return self._session_manager()

    @asynccontextmanager
    async def _session_manager(self):
        self.attempts += 1
        if self.start_error:
            raise self.start_error
        self.opened += 1
        try:
            yield self.session
        finally:
            self.closed += 1
            if self.exit_error:
                raise self.exit_error


@pytest.fixture
def make_sender():
    senders = []

    def build(connect, **options):
        sender = McpSender(NOTIFY_TO, connect=connect, **options)
        senders.append(sender)
        return sender

    yield build
    for sender in senders:
        sender.close()


def message_for(flag=None, now=NOW):
    return notify.render_notification(flag or make_flag(), now)


# --- a successful send --------------------------------------------------------------------


def test_a_successful_send_reports_sent_and_where_it_went(make_sender):
    connect = FakeConnect(FakeSession([ok()]))
    message = message_for()
    result = make_sender(connect).send(message)
    assert result.status is SendStatus.SENT
    assert result.error is None
    assert result.recipient == NOTIFY_TO  # the log shows where the mail really went
    assert result.subject == "[Duplicate Notice] Re: Q2 numbers"
    assert result.duplicate_message_id == "<dup1@thyme>"
    assert result.timestamp == NOW  # the message's own Date (D-024 item 6)


def test_send_email_gets_exactly_the_schema_arguments(make_sender):
    connect = FakeConnect()
    make_sender(connect).send(message_for())
    [(name, arguments)] = connect.session.calls
    assert name == "send_email"
    assert set(arguments) <= SCHEMA_PROPERTIES  # the server rejects anything else
    assert {"to", "subject", "body"} <= set(arguments)  # and these are required
    assert arguments["to"] == [NOTIFY_TO]  # a list, and the real recipient, not the Enron sender
    assert arguments["subject"] == "[Duplicate Notice] Re: Q2 numbers"
    assert arguments["mimeType"] == "text/plain"
    assert arguments["inReplyTo"] == "<dup1@thyme>"  # the flagged email's message id


def test_the_body_starts_with_the_template_header_block_then_the_notification(make_sender):
    connect = FakeConnect()
    message = message_for(make_flag(sender="jane.doe@enron.com"))
    make_sender(connect).send(message)
    body = connect.session.calls[0][1]["body"]
    header, _, rest = body.partition("\n\n")
    assert header.split("\n") == [
        "To: jane.doe@enron.com",  # the template's To keeps the real Enron sender
        "Subject: [Duplicate Notice] Re: Q2 numbers",
        f"Date: {email_utils.format_datetime(NOW)}",
        "References: <dup1@thyme>",  # the spec's References line stays in the body
    ]
    assert rest == message.body  # then the notification text, unchanged


def test_the_real_enron_sender_never_becomes_the_recipient(make_sender):
    connect = FakeConnect()
    make_sender(connect).send(message_for(make_flag(sender="jane.doe@enron.com")))
    assert "jane.doe@enron.com" not in connect.session.calls[0][1]["to"]


def test_the_notice_subject_with_an_existing_re_is_sent_as_is(make_sender):
    connect = FakeConnect()
    make_sender(connect).send(message_for(make_flag(original_subject="Re: Q2 numbers")))
    assert connect.session.calls[0][1]["subject"] == "[Duplicate Notice] Re: Q2 numbers"


# --- one session per run ---------------------------------------------------------------------


def test_nothing_connects_until_the_first_send(make_sender):
    connect = FakeConnect()
    sender = make_sender(connect)
    assert connect.attempts == 0
    sender.close()
    assert connect.attempts == 0  # closing an unused sender does not connect either


def test_many_sends_share_one_session(make_sender):
    connect = FakeConnect(FakeSession([ok(), ok(), ok(), ok()]))
    sender = make_sender(connect)
    results = [sender.send(message_for(make_flag(n))) for n in range(1, 5)]
    assert [r.status for r in results] == [SendStatus.SENT] * 4
    assert connect.attempts == 1 and connect.opened == 1
    assert len(connect.session.calls) == 4
    assert connect.closed == 0  # still open until the run ends


def test_close_ends_the_session_once_and_is_safe_to_repeat(make_sender):
    connect = FakeConnect()
    sender = make_sender(connect)
    sender.send(message_for())
    sender.close()
    sender.close()
    assert connect.closed == 1


def test_the_sender_works_as_a_context_manager(make_sender):
    connect = FakeConnect()
    with McpSender(NOTIFY_TO, connect=connect) as sender:
        sender.send(message_for())
        assert connect.closed == 0
    assert connect.closed == 1


def test_a_closed_sender_does_not_reconnect_by_surprise(make_sender):
    connect = FakeConnect()
    sender = make_sender(connect)
    sender.send(message_for())
    sender.close()
    result = sender.send(message_for(make_flag(2)))
    assert result.status is SendStatus.FAILED
    assert "closed" in result.error
    assert connect.attempts == 1


def test_the_background_thread_is_gone_after_close(make_sender):
    before = threading.active_count()
    sender = make_sender(FakeConnect())
    sender.send(message_for())
    assert threading.active_count() > before
    sender.close()
    assert threading.active_count() == before


# --- failures that cost one message -----------------------------------------------------------


def test_a_tool_error_fails_that_message_and_the_session_carries_on(make_sender):
    connect = FakeConnect(FakeSession([tool_error("Invalid email address"), ok()]))
    sender = make_sender(connect)
    first = sender.send(message_for(make_flag(1)))
    second = sender.send(message_for(make_flag(2)))
    assert first.status is SendStatus.FAILED
    assert first.error == "send_email failed: Invalid email address"
    assert second.status is SendStatus.SENT
    assert connect.attempts == 1 and len(connect.session.calls) == 2


def test_a_tool_error_without_text_still_has_a_reason(make_sender):
    connect = FakeConnect(FakeSession([SimpleNamespace(is_error=True, content=[])]))
    result = make_sender(connect).send(message_for())
    assert result.status is SendStatus.FAILED
    assert result.error.startswith("send_email failed")


def test_an_mcp_error_fails_that_message_and_the_session_carries_on(make_sender):
    connect = FakeConnect(FakeSession([MCPError(-32602, "Invalid params"), ok()]))
    sender = make_sender(connect)
    first = sender.send(message_for(make_flag(1)))
    second = sender.send(message_for(make_flag(2)))
    assert first.status is SendStatus.FAILED
    assert first.error.startswith("MCP error:") and "Invalid params" in first.error
    assert second.status is SendStatus.SENT


def test_a_timeout_fails_that_message_and_the_session_carries_on(make_sender):
    async def hang():
        await anyio.sleep(30)

    connect = FakeConnect(FakeSession([hang, ok()]))
    sender = make_sender(connect, call_timeout_seconds=0.05)
    first = sender.send(message_for(make_flag(1)))
    second = sender.send(message_for(make_flag(2)))
    assert first.status is SendStatus.FAILED
    assert "timed out" in first.error and "may or may not" in first.error
    assert second.status is SendStatus.SENT
    assert connect.attempts == 1


def test_an_unexpected_result_type_fails_that_message_only(make_sender):
    connect = FakeConnect(FakeSession([SimpleNamespace(result_type="input_required"), ok()]))
    sender = make_sender(connect)
    first = sender.send(message_for(make_flag(1)))
    assert first.status is SendStatus.FAILED and "unexpected" in first.error
    assert sender.send(message_for(make_flag(2))).status is SendStatus.SENT


# --- the session is lost -------------------------------------------------------------------------


def test_a_session_crash_mid_run_fails_that_message_and_every_later_one(make_sender):
    connect = FakeConnect(FakeSession([ok(), anyio.ClosedResourceError(), ok(), ok()]))
    sender = make_sender(connect)
    results = [sender.send(message_for(make_flag(n))) for n in range(1, 5)]
    assert [r.status for r in results] == [
        SendStatus.SENT, SendStatus.FAILED, SendStatus.FAILED, SendStatus.FAILED,
    ]
    assert results[1].error.startswith("MCP session lost: ClosedResourceError")
    assert all(r.error.startswith("MCP session lost") for r in results[2:])
    assert len(connect.session.calls) == 2  # nothing is sent after the crash
    assert connect.attempts == 1  # and there is no reconnect
    assert connect.closed == 1  # the dead session was shut down


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError("server exited"),
        ConnectionResetError("reset"),
        RuntimeError("something odd"),
        ValueError("bad"),
        ExceptionGroup("task group", [OSError("pipe closed")]),
    ],
)
def test_any_other_failure_is_a_lost_session_and_never_raises(make_sender, error):
    connect = FakeConnect(FakeSession([error]))
    result = make_sender(connect).send(message_for())
    assert result.status is SendStatus.FAILED
    assert result.error.startswith("MCP session lost:")


def test_a_server_that_cannot_start_fails_the_first_send_and_no_retry_follows(make_sender):
    connect = FakeConnect(start_error=FileNotFoundError("npx: not found"))
    sender = make_sender(connect)
    first = sender.send(message_for(make_flag(1)))
    second = sender.send(message_for(make_flag(2)))
    assert first.status is second.status is SendStatus.FAILED
    assert first.error == "could not start MCP server: FileNotFoundError: npx: not found"
    assert second.error.startswith("could not start MCP server")
    assert connect.attempts == 1  # one failed start, not one subprocess per email


def test_an_error_while_closing_a_dead_session_does_not_escape(make_sender):
    connect = FakeConnect(FakeSession([anyio.ClosedResourceError()]), exit_error=OSError("already gone"))
    sender = make_sender(connect)
    assert sender.send(message_for()).status is SendStatus.FAILED
    sender.close()  # must not raise


def test_close_swallows_an_error_from_the_session(make_sender):
    connect = FakeConnect(exit_error=OSError("already gone"))
    sender = make_sender(connect)
    sender.send(message_for())
    sender.close()  # must not raise


def test_an_unrenderable_message_does_not_raise(make_sender):
    connect = FakeConnect()
    sender = make_sender(connect)
    broken = SimpleNamespace(to="a@enron.com", subject="s", date=None, references="<r>", body="b")
    result = sender.send(broken)
    assert result.status is SendStatus.FAILED
    assert connect.session.calls == []


# --- through send_notifications --------------------------------------------------------------------


def test_a_crash_during_a_run_is_logged_per_notice_and_the_run_finishes(make_sender):
    connect = FakeConnect(FakeSession([ok(), BrokenPipeError("gone")]))
    flags = [make_flag(n) for n in (1, 2, 3)]
    results = send_notifications(flags, make_sender(connect), now=NOW)
    assert [r.status for r in results] == [SendStatus.SENT, SendStatus.FAILED, SendStatus.FAILED]
    assert [r.duplicate_message_id for r in results] == ["<dup1@thyme>", "<dup2@thyme>", "<dup3@thyme>"]
    assert len(connect.session.calls) == 2


# --- the real connection (still no network) ----------------------------------------------------------


def test_defaults_launch_the_gongrzhe_gmail_server_through_npx():
    sender = McpSender(NOTIFY_TO)
    assert sender.command == "npx"
    assert sender.args == ("-y", "@gongrzhe/server-gmail-autoauth-mcp")
    sender.close()


def test_the_stdio_connection_runs_the_mcp_handshake_before_the_first_call(monkeypatch):
    events = []

    class FakeClientSession:
        def __init__(self, read, write):
            events.append(("session", read, write))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            events.append("session closed")

        async def initialize(self):
            events.append("initialize")

        async def call_tool(self, name, arguments=None, **kwargs):
            events.append(("call", name))
            return ok()

    @asynccontextmanager
    async def fake_stdio_client(params):
        events.append(("spawn", params.command, list(params.args)))
        yield "READ", "WRITE"
        events.append("server stopped")

    monkeypatch.setattr(notify, "ClientSession", FakeClientSession)
    monkeypatch.setattr(notify, "stdio_client", fake_stdio_client)
    with McpSender(NOTIFY_TO, command="npx", args=("-y", "pkg")) as sender:
        assert sender.send(message_for()).status is SendStatus.SENT
    assert events == [
        ("spawn", "npx", ["-y", "pkg"]),
        ("session", "READ", "WRITE"),
        "initialize",
        ("call", "send_email"),
        "session closed",
        "server stopped",
    ]


def test_a_handshake_that_never_finishes_is_a_start_failure(monkeypatch):
    class HangingSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def initialize(self):
            await anyio.sleep(30)

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield "READ", "WRITE"

    monkeypatch.setattr(notify, "ClientSession", HangingSession)
    monkeypatch.setattr(notify, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(notify, "CONNECT_TIMEOUT_SECONDS", 0.05)
    with McpSender(NOTIFY_TO) as sender:
        result = sender.send(message_for())
    assert result.status is SendStatus.FAILED
    assert result.error.startswith("could not start MCP server: TimeoutError")
