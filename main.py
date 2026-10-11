"""CLI entry point: runs the whole pipeline with one command.

Order (docs/plans/module-layout.md): init db -> ingest -> dedup -> notify.
By default notify is a dry run that writes one .eml draft per flagged
duplicate. With --send-live it sends one email per duplicate group, to the
address in the NOTIFY_TO environment variable (D-022); live mode refuses to
start without it. The live sender is still a stub that raises until the Gmail
MCP server is configured.

This file only wires modules together. Parsing, SQL and notification
rendering live in the package.
"""
import argparse
import dataclasses
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from enron_pipeline import db, dedup, ingest, notify
from enron_pipeline.config import Settings, default_settings
from enron_pipeline.models import DuplicateFlag, SendResult, SendStatus
from enron_pipeline.parse_body import parse_body


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enron email extraction, dedup and notification pipeline."
    )
    parser.add_argument("--maildir", type=Path, help="path to the maildir folder")
    parser.add_argument(
        "--db",
        type=Path,
        help="path to the SQLite database (default: enron.db inside --output-dir)",
    )
    parser.add_argument("--output-dir", type=Path, help="folder for generated outputs")
    parser.add_argument(
        "--mailboxes", nargs="+", help="mailbox names to process (default: D-013 set)"
    )
    parser.add_argument(
        "--threshold", type=float, help="duplicate similarity threshold (0-100)"
    )
    parser.add_argument(
        "--send-live",
        action="store_true",
        help="really send notification emails to the NOTIFY_TO address "
        "(default: dry run, .eml drafts only)",
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        help="most emails a live run may send (default: 10); a dry run writes "
        "every draft regardless",
    )
    return parser


def settings_from_args(argv: Sequence[str] | None = None) -> Settings:
    """Default settings with any command-line overrides applied.

    --output-dir moves the database along with it unless --db is also given.
    The error log and duplicates report stay at the repo root either way:
    they are deliverables.
    """
    args = build_parser().parse_args(argv)
    overrides = {}
    if args.maildir is not None:
        overrides["maildir"] = args.maildir
    if args.output_dir is not None:
        overrides["output_dir"] = args.output_dir
        overrides["db_path"] = args.output_dir / default_settings().db_path.name
    if args.db is not None:
        overrides["db_path"] = args.db
    if args.mailboxes is not None:
        overrides["mailboxes"] = tuple(args.mailboxes)
    if args.threshold is not None:
        overrides["similarity_threshold"] = args.threshold
    if args.send_live:
        overrides["send_live"] = True
    if args.limit is not None:
        overrides["limit"] = args.limit
    return dataclasses.replace(default_settings(), **overrides)


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
    group, at most `limit` of them. Every attempt goes to `send_log`. A
    successful live send (status SENT) sets notification_sent and
    notification_date on the flagged email. Dry runs and failures never do
    (D-022 items 7 to 9).
    """
    chosen = notify.select_live_flags(flags, limit) if live else list(flags)
    results = notify.send_notifications(chosen, sender)
    notify.write_send_log(results, send_log)
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
    the maildir or a mailbox folder is missing (main() turns that into a
    one-line error).
    """
    if settings.send_live and sender is None:
        raise ValueError("live mode needs a sender (see notify.McpSender)")

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
        ingest.write_stats(stats, settings.output_dir / "extraction_stats.json")
        print(ingest.format_stats(stats))

        # Stage 3: dedup (D-021). Storage copies first, then the spec's rule.
        storage = dedup.mark_storage_copies(conn)
        flags = dedup.flag_duplicates(conn, settings.similarity_threshold)
        dedup_stats = dedup.summarize(flags, storage)
        dedup.write_report(flags, settings.duplicates_report)
        dedup.write_stats(dedup_stats, settings.output_dir / "dedup_stats.json")
        print("\nDuplicate detection")
        for name, value in dedup_stats.to_dict().items():
            print(f"  {name}: {value}")

        # Stage 4: notify (D-022). Dry run unless --send-live.
        if not settings.send_live:
            sender = notify.DryRunSender(settings.output_dir / "replies")
        results = run_notifications(
            conn,
            flags,
            sender,
            live=settings.send_live,
            limit=settings.limit,
            send_log=settings.output_dir / "send_log.csv",
        )
        _print_notification_summary(results, settings)
    finally:
        conn.close()
    return stats


def _print_notification_summary(results: Sequence[SendResult], settings: Settings) -> None:
    counts = Counter(result.status for result in results)
    log = settings.output_dir / "send_log.csv"
    if not settings.send_live:
        drafts = settings.output_dir / "replies"
        print(f"\nNotifications (dry run): {counts[SendStatus.DRY_RUN]} draft(s) in {drafts}, "
              f"nothing sent; log: {log}")
        return
    print(f"\nNotifications (live, limit {settings.limit}): "
          f"{counts[SendStatus.SENT]} sent, {counts[SendStatus.FAILED]} failed; log: {log}")
    first_error = next((r.error for r in results if r.status is SendStatus.FAILED), None)
    if first_error:
        print(f"  first error: {first_error}")


def main(argv: Sequence[str] | None = None) -> int:
    settings = settings_from_args(argv)

    # Live mode refuses to start without a usable NOTIFY_TO, before any work.
    sender = None
    if settings.send_live:
        try:
            sender = notify.McpSender(os.environ.get("NOTIFY_TO"))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    try:
        run_pipeline(settings, sender=sender)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
