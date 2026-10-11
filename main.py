"""CLI entry point: runs the whole pipeline with one command.

Order (docs/plans/module-layout.md): init db -> ingest -> dedup -> notify ->
write outputs. Only the first stage and the ingest outputs are wired so far;
dedup and notify are marked TODO below. Without --send-live nothing is ever
sent, and for now --send-live changes nothing at all.

This file only wires modules together. Parsing, SQL and notification
rendering live in the package.
"""
import argparse
import dataclasses
import sys
from collections.abc import Sequence
from pathlib import Path

from enron_pipeline import db, ingest
from enron_pipeline.config import Settings, default_settings
from enron_pipeline.parse_body import parse_body


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
        help="really send notification emails (default: dry run, .eml files only)",
    )
    return parser


def settings_from_args(argv: Sequence[str] | None = None) -> Settings:
    """Default settings with any command-line overrides applied.

    --output-dir moves the database along with it unless --db is also given.
    The error log stays at the repo root either way: it is a deliverable.
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
    return dataclasses.replace(default_settings(), **overrides)


def run_pipeline(
    settings: Settings, body_parser=parse_body
) -> ingest.ExtractionStats:
    """Run every wired stage and return the extraction stats.

    Raises FileNotFoundError if the maildir or a mailbox folder is missing
    (main() turns that into a one-line error).
    """
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
    finally:
        conn.close()
    ingest.write_stats(stats, settings.output_dir / "extraction_stats.json")
    print(ingest.format_stats(stats))

    # Stage 3: TODO dedup. mark_storage_copies, flag_duplicates, summarize and
    # write_report (duplicates_report.csv) once dedup.py exists.
    print("\nTODO: dedup is not implemented yet; skipped.")

    # Stage 4: TODO notify. DryRunSender writes output/replies/*.eml; with
    # --send-live the live sender and send_log.csv, once notify.py exists.
    print("TODO: notify is not implemented yet; skipped.")
    if settings.send_live:
        print("Note: --send-live has no effect yet; nothing was sent.")

    return stats


def main(argv: Sequence[str] | None = None) -> int:
    settings = settings_from_args(argv)
    try:
        run_pipeline(settings)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
