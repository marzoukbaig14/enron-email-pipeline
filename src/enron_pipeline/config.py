"""Default pipeline settings in one place.

Only main.py imports this. It reads values and passes them down as plain
arguments; the other modules keep their own defaults so they work in
isolation. Imports: nothing from the package. No I/O, no secrets.
"""
from dataclasses import dataclass
from pathlib import Path


# D-013 (Set 1): one mailbox per size band, smallest to largest.
MAILBOXES: tuple[str, ...] = ("ring-a", "ybarbo-p", "presto-k", "baughman-d", "beck-s")


@dataclass(frozen=True)
class Settings:
    maildir: Path = Path("data/maildir")
    db_path: Path = Path("output/enron.db")
    output_dir: Path = Path("output")
    mailboxes: tuple[str, ...] | None = MAILBOXES  # None would mean every mailbox
    similarity_threshold: float = 90.0
    send_live: bool = False


def default_settings() -> Settings:
    """The settings used when no command-line overrides are given."""
    return Settings()
