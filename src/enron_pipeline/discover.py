"""Find email files under the maildir and name them.

Behavior is fixed by D-018. Files are named by their "source_file" string, for
example "maildir/beck-s/inbox/1.": relative, with forward slashes, starting
with the maildir folder's own name. The same string is what ends up in
emails.source_file.

Does not open files, parse, or touch the DB. Imports: nothing from the package.
"""
import os
from collections.abc import Iterator, Sequence
from pathlib import Path


def discover_email_files(
    maildir: Path, mailboxes: Sequence[str] | None = None
) -> Iterator[str]:
    """Return an iterator of source_file strings, sorted.

    `mailboxes=None` means every mailbox folder under `maildir`; otherwise only
    the named ones are walked. Files only; hidden files and hidden folders
    (names starting with ".") are skipped.

    This is a plain function that returns a generator, not a generator
    itself. That is deliberate: the checks below run when the function is
    called, so a missing mailbox raises FileNotFoundError before anything has
    been yielded (and even if nobody iterates the result).
    """
    maildir = Path(maildir)
    if not maildir.is_dir():
        raise FileNotFoundError(f"maildir not found: {maildir}")

    if mailboxes is None:
        names = [
            entry.name
            for entry in maildir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        ]
    else:
        names = sorted(set(mailboxes))  # a mailbox listed twice is walked once
        for name in names:
            # "", ".", ".." or "a/b" would resolve outside the one mailbox folder.
            if name in ("", ".", "..") or "/" in name or "\\" in name:
                raise ValueError(f"not a mailbox folder name: {name!r}")
        missing = [name for name in names if not (maildir / name).is_dir()]
        if missing:
            raise FileNotFoundError(
                f"mailbox folder(s) not found under {maildir}: {', '.join(missing)}"
            )
    return _walk(maildir, names)


def source_path(maildir: Path, source_file: str) -> Path:
    """The real file for a source_file string.

    source_file starts with the maildir folder's own name, so it is relative to
    the maildir's parent: "maildir/beck-s/inbox/1." -> <parent>/maildir/beck-s/inbox/1.
    """
    return Path(maildir).parent / source_file


def mailbox_for(source_file: str) -> str:
    """Mailbox name from a source_file path, e.g. "lay-k"."""
    parts = source_file.split("/")
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"source_file has no mailbox part: {source_file!r}")
    return parts[1]


def _walk(maildir: Path, mailboxes: Sequence[str]) -> Iterator[str]:
    found: list[str] = []
    for mailbox in mailboxes:
        for folder, subfolders, files in os.walk(maildir / mailbox):
            # Editing the list in place stops os.walk from entering hidden folders.
            subfolders[:] = [name for name in subfolders if not name.startswith(".")]
            relative = Path(folder).relative_to(maildir).as_posix()
            for name in files:
                if not name.startswith("."):
                    found.append(f"{maildir.name}/{relative}/{name}")
    # Sorting the whole list (not per folder) makes the order identical on every run.
    yield from sorted(found)
