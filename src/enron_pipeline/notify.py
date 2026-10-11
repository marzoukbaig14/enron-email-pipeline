"""Render the spec notification and hand it to a sender.

Receives DuplicateFlags and a Sender; never a DB connection or settings. The
caller decides dry-run vs live by choosing the sender. Imports: models only.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from enron_pipeline.models import DuplicateFlag, SendResult, SendStatus


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    date: datetime
    references: str  # message_id of the flagged email
    body: str


class Sender(Protocol):
    def send(self, message: OutgoingEmail) -> SendResult: ...


class DryRunSender:
    """Writes one .eml per message into output_dir; sends nothing.

    Returns SendResult(status=SendStatus.DRY_RUN).
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def send(self, message: OutgoingEmail) -> SendResult:
        raise NotImplementedError


class McpSender:
    """Sends through the Gmail MCP server's send_email tool.

    How it reaches MCP is decided at the notifier (D-014 item 6).
    """

    def send(self, message: OutgoingEmail) -> SendResult:
        raise NotImplementedError


def render_notification(flag: DuplicateFlag, now: datetime) -> OutgoingEmail:
    """Fill the spec's notification template for one flagged duplicate."""
    raise NotImplementedError


def send_notifications(
    flags: Sequence[DuplicateFlag],
    sender: Sender,
    now: datetime | None = None,
) -> list[SendResult]:
    """Render and send one notification per flag.

    Catches exceptions per message and records them as SendStatus.FAILED; never
    raises. `now` can be injected for tests.
    """
    raise NotImplementedError


def write_send_log(results: Sequence[SendResult], path: Path) -> None:
    """Write send_log.csv: timestamp, recipient, subject, status, error."""
    raise NotImplementedError
