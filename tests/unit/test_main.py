"""Tests for main.py: argument handling and the wired-up ingest stage.

main.py lives at the repo root, outside the package, so it is loaded here by
file path. Every run uses a fake maildir and tmp_path outputs, and the body
parser is injected, so these tests never touch the dataset and do not depend
on parse_body's behavior. One test runs the real parse_body, only to prove it
is the default.

Dedup and notify are not built yet. main prints a clearly marked "TODO"
notice for each, and --send-live is accepted but changes nothing.
"""
import importlib.util
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from enron_pipeline.config import MAILBOXES, Settings

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("pipeline_main", ROOT / "main.py")
main_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main_module)

HEADERS = {
    "Message-ID": "<1@thyme>",
    "Date": "Mon, 14 May 2001 16:39:00 -0700 (PDT)",
    "From": "phillip.allen@enron.com",
    "To": "tim.belden@enron.com",
    "Subject": "Q2 numbers",
}


@dataclass
class FakeParsed:
    body: str
    forwarded_content: str | None = None
    quoted_content: str | None = None
    headings: str | None = None
    has_attachment: bool = False


class FakeBodyParser:
    def __init__(self):
        self.calls = []

    def __call__(self, raw_body, content_type, content_transfer_encoding):
        self.calls.append((raw_body, content_type, content_transfer_encoding))
        return FakeParsed(body=raw_body)


def write_email(maildir, relative, overrides=None, body="Hello.\n"):
    headers = {**HEADERS, **(overrides or {})}
    text = "".join(f"{k}: {v}\n" for k, v in headers.items() if v is not None)
    path = maildir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((text + "\n" + body).encode())


@pytest.fixture
def maildir(tmp_path):
    root = tmp_path / "maildir"
    write_email(root, "beck-s/inbox/1.", {"Message-ID": "<1@thyme>"})
    write_email(root, "beck-s/inbox/2.", {"Message-ID": "<2@thyme>"})
    write_email(root, "beck-s/inbox/3.", {"Message-ID": "<3@thyme>", "To": None})
    write_email(root, "ring-a/inbox/1.", {"Message-ID": "<4@thyme>"})
    return root


@pytest.fixture
def settings(tmp_path, maildir):
    return Settings(
        maildir=maildir,
        db_path=tmp_path / "out" / "enron.db",  # "out" does not exist yet
        output_dir=tmp_path / "out",
        error_log=tmp_path / "error_log.txt",
        mailboxes=("beck-s",),
    )


def count(db_path, table) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# --- settings_from_args ----------------------------------------------------------


def test_no_arguments_gives_the_default_settings():
    settings = main_module.settings_from_args([])
    assert settings.maildir == Path("data/maildir")
    assert settings.db_path == Path("output/enron.db")
    assert settings.output_dir == Path("output")
    assert settings.error_log == Path("error_log.txt")
    assert settings.mailboxes == MAILBOXES
    assert settings.similarity_threshold == 90.0
    assert settings.send_live is False


def test_arguments_override_the_defaults(tmp_path):
    settings = main_module.settings_from_args(
        [
            "--maildir", str(tmp_path / "m"),
            "--db", str(tmp_path / "x.db"),
            "--output-dir", str(tmp_path / "o"),
            "--mailboxes", "a-mail", "b-mail",
            "--threshold", "80",
            "--send-live",
        ]
    )
    assert settings.maildir == tmp_path / "m"
    assert settings.db_path == tmp_path / "x.db"
    assert settings.output_dir == tmp_path / "o"
    assert settings.mailboxes == ("a-mail", "b-mail")
    assert settings.similarity_threshold == 80.0
    assert settings.send_live is True


def test_output_dir_alone_moves_the_database_with_it(tmp_path):
    settings = main_module.settings_from_args(["--output-dir", str(tmp_path / "o")])
    assert settings.db_path == tmp_path / "o" / "enron.db"


def test_explicit_db_wins_over_output_dir(tmp_path):
    settings = main_module.settings_from_args(
        ["--output-dir", str(tmp_path / "o"), "--db", str(tmp_path / "x.db")]
    )
    assert settings.db_path == tmp_path / "x.db"


def test_error_log_stays_at_the_repo_root_when_output_dir_changes(tmp_path):
    settings = main_module.settings_from_args(["--output-dir", str(tmp_path / "o")])
    assert settings.error_log == Path("error_log.txt")


# --- run_pipeline: the ingest stage --------------------------------------------------


def test_run_rebuilds_the_db_ingests_and_writes_both_outputs(settings, capsys):
    parser = FakeBodyParser()

    stats = main_module.run_pipeline(settings, body_parser=parser)

    assert (stats.total_found, stats.parsed, stats.failed) == (3, 2, 1)
    assert count(settings.db_path, "emails") == 2
    assert count(settings.db_path, "parse_failures") == 1
    assert settings.error_log.read_text() == "maildir/beck-s/inbox/3. | missing to\n"
    saved = json.loads((settings.output_dir / "extraction_stats.json").read_text())
    assert saved == stats.to_dict()
    assert len(parser.calls) == 2  # the injected parser is the one used


def test_run_prints_the_extraction_report(settings, capsys):
    main_module.run_pipeline(settings, body_parser=FakeBodyParser())
    out = capsys.readouterr().out
    assert "Total files found: 3" in out
    assert "Parsed: 2" in out
    assert "missing to" in out
    assert "has_attachment true:" in out


def test_run_only_ingests_the_configured_mailboxes(settings):
    main_module.run_pipeline(settings, body_parser=FakeBodyParser())
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT DISTINCT mailbox FROM emails").fetchall() == [("beck-s",)]
    conn.close()


def test_running_twice_rebuilds_instead_of_doubling(settings):
    main_module.run_pipeline(settings, body_parser=FakeBodyParser())
    main_module.run_pipeline(settings, body_parser=FakeBodyParser())
    assert count(settings.db_path, "emails") == 2
    assert count(settings.db_path, "parse_failures") == 1
    # The error log is rewritten, not appended to.
    assert len(settings.error_log.read_text().splitlines()) == 1


def test_a_stale_database_is_replaced(settings):
    settings.db_path.parent.mkdir(parents=True)
    old = sqlite3.connect(settings.db_path)
    old.execute("CREATE TABLE emails (leftover TEXT)")
    old.execute("INSERT INTO emails VALUES ('from an old run')")
    old.commit()
    old.close()

    main_module.run_pipeline(settings, body_parser=FakeBodyParser())

    assert count(settings.db_path, "emails") == 2


def test_the_real_parse_body_is_the_default_body_parser(settings):
    # Only the wiring is checked, not what parse_body does with the text.
    stats = main_module.run_pipeline(settings)
    assert (stats.parsed, stats.failed) == (2, 1)
    conn = sqlite3.connect(settings.db_path)
    [(body,)] = conn.execute("SELECT body FROM emails WHERE message_id = '<1@thyme>'")
    conn.close()
    assert body.startswith("Hello.")


def test_run_closes_the_database(settings):
    main_module.run_pipeline(settings, body_parser=FakeBodyParser())
    # If run_pipeline left its connection open mid-transaction, a second
    # connection could not write; it can, so the run committed and closed.
    conn = sqlite3.connect(settings.db_path)
    conn.execute("DELETE FROM emails")
    conn.commit()
    conn.close()


# --- main(): the command line ------------------------------------------------------------


def argv_for(settings: Settings) -> list[str]:
    return [
        "--maildir", str(settings.maildir),
        "--db", str(settings.db_path),
        "--output-dir", str(settings.output_dir),
        "--mailboxes", *settings.mailboxes,
    ]


def test_main_returns_zero_on_success(settings, monkeypatch, capsys):
    monkeypatch.chdir(settings.error_log.parent)  # the default error_log.txt lands here
    assert main_module.main(argv_for(settings)) == 0
    assert (settings.error_log.parent / "error_log.txt").exists()
    assert "Total files found: 3" in capsys.readouterr().out


def test_main_marks_dedup_and_notify_as_todo(settings, monkeypatch, capsys):
    monkeypatch.chdir(settings.error_log.parent)
    main_module.main(argv_for(settings))
    out = capsys.readouterr().out
    assert "TODO: dedup is not implemented yet" in out
    assert "TODO: notify is not implemented yet" in out


def test_send_live_is_accepted_but_sends_nothing(settings, monkeypatch, capsys):
    monkeypatch.chdir(settings.error_log.parent)
    assert main_module.main(argv_for(settings) + ["--send-live"]) == 0
    out = capsys.readouterr().out
    assert "--send-live has no effect yet" in out
    assert not (settings.output_dir / "replies").exists()
    assert not (settings.output_dir / "send_log.csv").exists()


def test_without_send_live_there_is_no_send_notice(settings, monkeypatch, capsys):
    monkeypatch.chdir(settings.error_log.parent)
    main_module.main(argv_for(settings))
    assert "--send-live" not in capsys.readouterr().out


def test_a_missing_mailbox_is_a_clean_error_not_a_traceback(settings, monkeypatch, capsys):
    monkeypatch.chdir(settings.error_log.parent)
    argv = argv_for(settings) + ["ghost-x"]
    assert main_module.main(argv) == 1
    captured = capsys.readouterr()
    assert "ghost-x" in captured.err
    assert "Traceback" not in captured.err


def test_a_missing_maildir_is_a_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main_module.main(["--maildir", str(tmp_path / "nope"), "--output-dir", str(tmp_path / "o")])
    assert code == 1
    assert "nope" in capsys.readouterr().err
