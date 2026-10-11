"""CLI entry point: runs the whole pipeline with one command.

Skeleton: arguments are parsed but nothing runs yet. Planned order (see
docs/plans/module-layout.md): init db -> ingest -> dedup -> notify -> write
outputs. Without --send-live nothing is ever sent.
"""
import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enron email extraction, dedup and notification pipeline."
    )
    parser.add_argument("--maildir", type=Path, help="path to the maildir folder")
    parser.add_argument("--db", type=Path, help="path to the SQLite database")
    parser.add_argument("--output-dir", type=Path, help="folder for generated outputs")
    parser.add_argument("--mailboxes", nargs="+", help="mailbox names to process")
    parser.add_argument(
        "--threshold", type=float, help="duplicate similarity threshold (0-100)"
    )
    parser.add_argument(
        "--send-live",
        action="store_true",
        help="really send notification emails (default: dry run, .eml files only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
