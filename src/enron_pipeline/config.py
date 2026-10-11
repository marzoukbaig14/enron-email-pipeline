"""Default pipeline settings in one place (D-018, D-025).

Every default a run can have lives here: the Settings fields, plus named
constants for the output folder, the file names inside it, the environment
variable that names the live recipient, the duplicate threshold and the
live-send limit. Changing a default means editing this file only.

Only main.py imports this. It reads the values, lets command-line options
override them, and passes the result down as plain arguments; no other module
reads the command line or the environment. The other modules keep their own
defaults so they work in isolation (a test keeps dedup's equal to this one).

Imports: nothing from the package. No I/O, no secrets.
"""
from dataclasses import dataclass
from pathlib import Path


# D-013 (Set 1): one mailbox per size band, smallest to largest.
MAILBOXES: tuple[str, ...] = ("ring-a", "ybarbo-p", "presto-k", "baughman-d", "beck-s")

MAILDIR = Path("data/maildir")

# Where a run writes its outputs, and what the files inside are called.
OUTPUT_DIR = Path("output")
DB_FILE = "enron.db"
EXTRACTION_STATS_FILE = "extraction_stats.json"
DEDUP_STATS_FILE = "dedup_stats.json"
REPLIES_DIR = "replies"  # dry-run .eml drafts
SEND_LOG_FILE = "send_log.csv"  # live attempts only (D-024)
DRY_RUN_LOG_FILE = "send_log_dry_run.csv"
ERROR_LOG = Path("error_log.txt")  # repo root: a spec deliverable
DUPLICATES_REPORT = Path("duplicates_report.csv")  # repo root (D-021)

# The live recipient comes from --notify-to, else from this environment variable.
NOTIFY_TO_ENV = "NOTIFY_TO"

SIMILARITY_THRESHOLD = 90.0  # percent; the spec's value
LIVE_SEND_LIMIT = 10  # most emails a live run may send (D-022 item 4)


@dataclass(frozen=True)
class Settings:
    maildir: Path = MAILDIR
    db_path: Path = OUTPUT_DIR / DB_FILE
    output_dir: Path = OUTPUT_DIR
    error_log: Path = ERROR_LOG
    duplicates_report: Path = DUPLICATES_REPORT
    mailboxes: tuple[str, ...] | None = MAILBOXES  # None would mean every mailbox
    similarity_threshold: float = SIMILARITY_THRESHOLD
    send_live: bool = False
    limit: int = LIVE_SEND_LIMIT
    notify_to: str | None = None  # the live recipient; main fills it from the flag or the environment

    # Paths that follow from output_dir, so no other file has to know the names.

    @property
    def extraction_stats(self) -> Path:
        return self.output_dir / EXTRACTION_STATS_FILE

    @property
    def dedup_stats(self) -> Path:
        return self.output_dir / DEDUP_STATS_FILE

    @property
    def replies_dir(self) -> Path:
        return self.output_dir / REPLIES_DIR

    @property
    def send_log(self) -> Path:
        """The log for this run's mode: live attempts and dry-run attempts are kept apart (D-024)."""
        return self.output_dir / (SEND_LOG_FILE if self.send_live else DRY_RUN_LOG_FILE)


def default_settings() -> Settings:
    """The settings used when no command-line overrides are given."""
    return Settings()
