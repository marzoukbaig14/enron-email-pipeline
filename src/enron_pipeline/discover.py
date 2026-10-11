"""Find email files under the maildir and name them.

Does not open files, parse, or touch the DB. Imports: nothing from the package.
"""
from collections.abc import Iterator, Sequence
from pathlib import Path


def discover_email_files(
    maildir: Path, mailboxes: Sequence[str] | None = None
) -> Iterator[Path]:
    """Yield every email file under `maildir`, in sorted order.

    `mailboxes=None` means all mailboxes; otherwise only the named ones.
    """
    raise NotImplementedError


def source_file_for(path: Path, maildir: Path) -> str:
    """Relative traceability path, e.g. "maildir/lay-k/inbox/45"."""
    raise NotImplementedError


def mailbox_for(source_file: str) -> str:
    """Mailbox name from a source_file path, e.g. "lay-k"."""
    raise NotImplementedError
