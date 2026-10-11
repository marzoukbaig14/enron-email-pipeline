"""CLI entry point: runs the whole pipeline with one command.

Order (docs/plans/module-layout.md): check inputs -> init db -> ingest -> dedup
-> notify. Every run setting has a default in config.py and an option here
(D-025); run `python main.py --help` for the list. By default notify is a dry
run that writes one .eml draft per flagged duplicate. With --send-live it
sends one email per duplicate group to the --notify-to address (or the
NOTIFY_TO environment variable); live mode refuses to start without one. The
live sender launches the Gmail MCP server through the official mcp client and
is closed when the run ends (D-024).

This file is the only place that reads the command line or the environment,
and it only wires modules together. Parsing, SQL and notification rendering
live in the package, which takes plain arguments.
"""
import argparse
import dataclasses
import math
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from enron_pipeline import db, dedup, discover, ingest, notify
from enron_pipeline.config import DB_FILE, NOTIFY_TO_ENV, Settings, default_settings
from enron_pipeline.models import DuplicateFlag, SendResult, SendStatus
from enron_pipeline.parse_body import parse_body


# --- argument types: each rejects a bad value with a usage error (exit 2) ---------------------


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return value


def _percentage(text: str) -> float:
    """0 to 100 inclusive. nan and inf fail the comparison, so they are rejected too."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0.0 <= value <= 100.0 or math.isnan(value):
        raise argparse.ArgumentTypeError(f"must be between 0 and 100, got {text}")
    return value


def _mailbox_name(text: str) -> str:
    """A mailbox is a folder directly under the maildir, so only a plain name will do."""
    if text in ("", ".", "..") or "/" in text or "\\" in text:
        raise argparse.ArgumentTypeError(
            f"not a mailbox folder name: {text!r} (use the folder's name, like beck-s)"
        )
    return text


def build_parser() -> argparse.ArgumentParser:
    """The command line. Every default shown in --help comes from config.py."""
    defaults = default_settings()
    parser = argparse.ArgumentParser(
        description="Enron email extraction, dedup and notification pipeline."
    )
    parser.add_argument(
        "--maildir", type=Path, help=f"path to the maildir folder (default: {defaults.maildir})"
    )
    parser.add_argument(
        "--mailboxes",
        nargs="+",
        type=_mailbox_name,
        metavar="NAME",
        help="mailbox folders to process, one or more; each must exist under "
        f"--maildir (default: {', '.join(defaults.mailboxes)})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=f"folder for generated outputs (default: {defaults.output_dir})",
    )
    parser.add_argument(
        "--db-path",
        "--db",  # the older name, kept
        dest="db_path",
        type=Path,
        help=f"SQLite database file (default: {defaults.db_path}, or {DB_FILE} inside --output-dir)",
    )
    parser.add_argument(
        "--threshold",
        type=_percentage,
        help="duplicate similarity threshold, 0 to 100 "
        f"(default: {defaults.similarity_threshold:g}, the spec value)",
    )
    parser.add_argument(
        "--send-live",
        action="store_true",
        help="really send notification emails (default: dry run, .eml drafts only)",
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        help=f"most emails a live run may send (default: {defaults.limit}); "
        "a dry run writes every draft regardless",
    )
    parser.add_argument(
        "--notify-to",
        metavar="ADDRESS",
        help="where live notifications are delivered (default: the "
        f"{NOTIFY_TO_ENV} environment variable); live mode needs one of the two",
    )
    return parser


def settings_from_args(argv: Sequence[str] | None = None) -> Settings:
    """Default settings with any command-line overrides applied.

    --output-dir moves the database along with it unless --db-path is also
    given. The error log and duplicates report stay at the repo root either
    way: they are deliverables (D-025 item 6). Repeated mailbox names are
    dropped. The live recipient is --notify-to, else the NOTIFY_TO environment
    variable (read here, and nowhere in the package).
    """
    args = build_parser().parse_args(argv)
    overrides = {}
    if args.maildir is not None:
        overrides["maildir"] = args.maildir
    if args.output_dir is not None:
        overrides["output_dir"] = args.output_dir
        overrides["db_path"] = args.output_dir / DB_FILE
    if args.db_path is not None:
        overrides["db_path"] = args.db_path
    if args.mailboxes is not None:
        overrides["mailboxes"] = tuple(dict.fromkeys(args.mailboxes))  # drops repeats, keeps order
    if args.threshold is not None:
        overrides["similarity_threshold"] = args.threshold
    if args.send_live:
        overrides["send_live"] = True
    if args.limit is not None:
        overrides["limit"] = args.limit
    overrides["notify_to"] = (
        args.notify_to if args.notify_to is not None else os.environ.get(NOTIFY_TO_ENV)
    )
    return dataclasses.replace(default_settings(), **overrides)


def check_inputs(settings: Settings) -> None:
    """Fail fast (D-025): the maildir and every named mailbox folder must exist.

    Runs before anything is created, wiped or written, so a typo in
    --mailboxes cannot destroy the previous run's database. Raises
    FileNotFoundError naming everything that is missing. discover checks when
    it is called, before it yields anything, so the iterator is dropped unread.
    """
    discover.discover_email_files(settings.maildir, settings.mailboxes)


def run_notifications(
    conn: sqlite3.Connection,
    flags: Sequence[DuplicateFlag],
    sender: notify.Sender,
    *,
    live: bool,
    limit: int,
    send_log: Path,
) -> list[SendResult]:
    """The notify stage: choose flags, send, log every attempt, record successes.

    Dry run: every flag gets a draft. Live: only the latest duplicate of each
    group, at most `limit` of them. Every attempt goes to `send_log`: a live run
    appends to it, a dry run rewrites it (D-024 item 7; the caller gives the two
    modes different files). A successful live send (status SENT) sets
    notification_sent and notification_date on the flagged email. Dry runs and
    failures never do (D-022 items 8 and 9).
    """
    chosen = notify.select_live_flags(flags, limit) if live else list(flags)
    results = notify.send_notifications(chosen, sender)
    notify.write_send_log(results, send_log, append=live)
    for result in results:
        if result.status is SendStatus.SENT:
            db.mark_notified(conn, result.duplicate_message_id, result.timestamp)
    return results


def run_pipeline(
    settings: Settings, body_parser=parse_body, sender: notify.Sender | None = None
) -> ingest.ExtractionStats:
    """Run every stage and return the extraction stats.

    `sender` is only needed in live mode (main() builds it before any work
    starts); a dry run makes its own DryRunSender. Raises FileNotFoundError if
    the maildir or a mailbox folder is missing, before anything is touched
    (main() turns that into a one-line error).
    """
    if settings.send_live and sender is None:
        raise ValueError("live mode needs a sender (see notify.McpSender)")
    check_inputs(settings)

    # Stage 1: rebuild the database from schema.sql (D-018).
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)

        # Stage 2: ingest. Failures go to the error log and parse_failures.
        stats = ingest.ingest_mailboxes(
            conn,
            settings.maildir,
            settings.mailboxes,
            body_parser,
            error_log=settings.error_log,
        )
        ingest.write_stats(stats, settings.extraction_stats)
        print(ingest.format_stats(stats))

        # Stage 3: dedup (D-021). Storage copies first, then the spec's rule.
        storage = dedup.mark_storage_copies(conn)
        flags = dedup.flag_duplicates(conn, settings.similarity_threshold)
        dedup_stats = dedup.summarize(flags, storage)
        dedup.write_report(flags, settings.duplicates_report)
        dedup.write_stats(dedup_stats, settings.dedup_stats)
        print("\nDuplicate detection")
        for name, value in dedup_stats.to_dict().items():
            print(f"  {name}: {value}")

        # Stage 4: notify (D-022). Dry run unless --send-live.
        if not settings.send_live:
            sender = notify.DryRunSender(settings.replies_dir)
        results = run_notifications(
            conn,
            flags,
            sender,
            live=settings.send_live,
            limit=settings.limit,
            send_log=settings.send_log,
        )
        _print_notification_summary(results, settings)
    finally:
        conn.close()
    return stats


def _print_notification_summary(results: Sequence[SendResult], settings: Settings) -> None:
    counts = Counter(result.status for result in results)
    log = settings.send_log
    if not settings.send_live:
        print(f"\nNotifications (dry run): {counts[SendStatus.DRY_RUN]} draft(s) in "
              f"{settings.replies_dir}, nothing sent; log: {log}")
        return
    recipients = ", ".join(sorted({result.recipient for result in results}))
    print(f"\nNotifications (live, limit {settings.limit}): "
          f"{counts[SendStatus.SENT]} sent, {counts[SendStatus.FAILED]} failed"
          f"{f', delivered to {recipients}' if recipients else ''}; log: {log}")
    first_error = next((r.error for r in results if r.status is SendStatus.FAILED), None)
    if first_error:
        print(f"  first error: {first_error}")


def main(argv: Sequence[str] | None = None) -> int:
    settings = settings_from_args(argv)

    # Live mode refuses to start without a usable recipient, before any work.
    sender = None
    if settings.send_live:
        try:
            sender = notify.McpSender(settings.notify_to)
        except ValueError as error:
            print(
                f"error: {error}. Give the recipient with --notify-to ADDRESS "
                f"or the {NOTIFY_TO_ENV} environment variable.",
                file=sys.stderr,
            )
            return 1

    try:
        run_pipeline(settings, sender=sender)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if sender is not None:
            sender.close()  # stops the MCP server and its background thread
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
