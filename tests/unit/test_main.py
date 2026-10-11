"""Tests for main.py: argument handling and the wired-up ingest stage.

main.py lives at the repo root, outside the package, so it is loaded here by
file path. Every run uses a fake maildir and tmp_path outputs, and the body
parser is injected, so these tests never touch the dataset and do not depend
on parse_body's behavior. One test runs the real parse_body, only to prove it
is the default.

After ingest, main runs dedup and then notify (D-021, D-022, D-024). By
default notify is a dry run that writes drafts and logs them to
send_log_dry_run.csv. --send-live needs NOTIFY_TO and logs real attempts to
send_log.csv (appending). The real McpSender would launch a Gmail MCP server,
so every live test here swaps in a fake McpSender.
"""
import csv
import email
import importlib.util
import json
import sqlite3
from dataclasses import dataclass
from email import policy
from pathlib import Path

import pytest

from enron_pipeline import dedup, notify
from enron_pipeline.config import MAILBOXES, Settings, default_settings
from enron_pipeline.models import SendResult, SendStatus

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
        duplicates_report=tmp_path / "duplicates_report.csv",
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
    assert settings.duplicates_report == Path("duplicates_report.csv")
    assert settings.mailboxes == MAILBOXES
    assert settings.similarity_threshold == 90.0
    assert settings.send_live is False
    assert settings.limit == 10


def test_arguments_override_the_defaults(tmp_path):
    settings = main_module.settings_from_args(
        [
            "--maildir", str(tmp_path / "m"),
            "--db", str(tmp_path / "x.db"),
            "--output-dir", str(tmp_path / "o"),
            "--mailboxes", "a-mail", "b-mail",
            "--threshold", "80",
            "--send-live",
            "--limit", "3",
        ]
    )
    assert settings.maildir == tmp_path / "m"
    assert settings.db_path == tmp_path / "x.db"
    assert settings.output_dir == tmp_path / "o"
    assert settings.mailboxes == ("a-mail", "b-mail")
    assert settings.similarity_threshold == 80.0
    assert settings.send_live is True
    assert settings.limit == 3


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


# --- dedup, notify and the CLI, on a maildir with real duplicate groups --------------------------
#
# Group A: an original (<a1>) plus two later re-sends (<a2>, <a3>), same sender,
# subject and body. Group B: an original (<b1>) and one re-send (<b2>). One more
# email (<c1>) is unique. So dedup flags 3 emails in 2 groups, a dry run writes
# 3 drafts, and a live run sends 2 (the latest of each group: <a3> and <b2>).

NOTIFY_TO = "me@gmail.com"
DATES = {
    "a1": "Thu, 10 May 2001 10:00:00 -0700 (PDT)",
    "a2": "Mon, 14 May 2001 10:00:00 -0700 (PDT)",
    "a3": "Sun, 20 May 2001 10:00:00 -0700 (PDT)",
    "b1": "Fri, 01 Jun 2001 10:00:00 -0700 (PDT)",
    "b2": "Sun, 03 Jun 2001 10:00:00 -0700 (PDT)",
    "c1": "Mon, 04 Jun 2001 10:00:00 -0700 (PDT)",
}


@pytest.fixture
def dup_settings(tmp_path):
    root = tmp_path / "dup" / "maildir"
    people = {"a": ("phillip.allen@enron.com", "Q2 numbers", "Please review the Q2 numbers."),
              "b": ("jane.doe@enron.com", "Lunch", "Lunch at noon on Friday?"),
              "c": ("bob.smith@enron.com", "Other", "Something else entirely.")}
    for number, key in enumerate(DATES, start=1):
        sender, subject, body = people[key[0]]
        write_email(
            root,
            f"beck-s/inbox/{number}.",
            {"Message-ID": f"<{key}@thyme>", "From": sender, "Subject": subject, "Date": DATES[key]},
            body=body + "\n",
        )
    return Settings(
        maildir=root,
        db_path=tmp_path / "out" / "enron.db",
        output_dir=tmp_path / "out",
        error_log=tmp_path / "error_log.txt",
        duplicates_report=tmp_path / "duplicates_report.csv",
        mailboxes=("beck-s",),
    )


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def email_state(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            mid: (dup, sent, date)
            for mid, dup, sent, date in conn.execute(
                "SELECT message_id, is_duplicate, notification_sent, notification_date FROM emails"
            )
        }
    finally:
        conn.close()


class FakeMcp:
    """Stands in for notify.McpSender: records what it is given, delivers to notify_to."""

    instances: list = []

    def __init__(self, notify_to):
        self.notify_to = notify_to
        self.messages = []
        self.closed = 0
        FakeMcp.instances.append(self)

    def close(self):
        self.closed += 1

    def send(self, message):
        self.messages.append(message)
        return SendResult(
            timestamp=message.date,
            recipient=self.notify_to,
            subject=message.subject,
            status=SendStatus.SENT,
            error=None,
            duplicate_message_id=message.references,
        )


@pytest.fixture
def fake_mcp(monkeypatch):
    FakeMcp.instances = []
    monkeypatch.setattr(notify, "McpSender", FakeMcp)
    monkeypatch.setenv("NOTIFY_TO", NOTIFY_TO)
    return FakeMcp


def run_cli(dup_settings, monkeypatch, *extra):
    """main() on the duplicates maildir; the default error log and report land in tmp."""
    monkeypatch.chdir(dup_settings.error_log.parent)
    return main_module.main(argv_for(dup_settings) + list(extra))


def test_dedup_stage_flags_duplicates_and_writes_report_and_stats(dup_settings, capsys):
    main_module.run_pipeline(dup_settings, body_parser=FakeBodyParser())

    rows = read_csv(dup_settings.duplicates_report)
    assert [(r["duplicate_message_id"], r["original_message_id"]) for r in rows] == [
        ("<a2@thyme>", "<a1@thyme>"),
        ("<a3@thyme>", "<a1@thyme>"),
        ("<b2@thyme>", "<b1@thyme>"),
    ]
    stats = json.loads((dup_settings.output_dir / "dedup_stats.json").read_text())
    assert stats["duplicate_groups"] == 2
    assert stats["emails_flagged"] == 3
    assert stats["largest_group_size"] == 3
    assert {k: v[0] for k, v in email_state(dup_settings.db_path).items()} == {
        "<a1@thyme>": 0, "<a2@thyme>": 1, "<a3@thyme>": 1,
        "<b1@thyme>": 0, "<b2@thyme>": 1, "<c1@thyme>": 0,
    }
    out = capsys.readouterr().out
    assert "Duplicate detection" in out
    assert "emails_flagged: 3" in out


def test_the_threshold_setting_reaches_dedup(dup_settings):
    import dataclasses

    strict = dataclasses.replace(dup_settings, similarity_threshold=100.5)  # nothing scores that high
    main_module.run_pipeline(strict, body_parser=FakeBodyParser())
    assert read_csv(strict.duplicates_report) == []
    assert list((strict.output_dir / "replies").glob("*.eml")) == []


def test_the_default_run_is_a_dry_run_with_one_draft_per_flagged_duplicate(
    dup_settings, monkeypatch, capsys
):
    monkeypatch.delenv("NOTIFY_TO", raising=False)  # a dry run must not need it

    assert run_cli(dup_settings, monkeypatch) == 0

    drafts = sorted((dup_settings.output_dir / "replies").glob("*.eml"))
    assert len(drafts) == 3
    parsed = [email.message_from_bytes(d.read_bytes(), policy=policy.default) for d in drafts]
    assert [m["References"] for m in parsed] == ["<a2@thyme>", "<a3@thyme>", "<b2@thyme>"]
    assert [m["To"] for m in parsed] == [
        "phillip.allen@enron.com", "phillip.allen@enron.com", "jane.doe@enron.com"
    ]
    rows = read_csv(dup_settings.output_dir / "send_log_dry_run.csv")
    assert [r["status"] for r in rows] == ["dry_run"] * 3
    # send_log.csv is the log of live sends only (D-024), so a dry run does not create it.
    assert not (dup_settings.output_dir / "send_log.csv").exists()
    # A dry run never marks anything as notified.
    assert all(sent == 0 and date is None for _, sent, date in email_state(dup_settings.db_path).values())
    assert "dry run" in capsys.readouterr().out.lower()


def test_a_second_dry_run_replaces_the_drafts_of_the_first(dup_settings, monkeypatch):
    import dataclasses

    main_module.run_pipeline(dup_settings, body_parser=FakeBodyParser())
    assert len(list((dup_settings.output_dir / "replies").glob("*.eml"))) == 3

    strict = dataclasses.replace(dup_settings, similarity_threshold=100.5)
    main_module.run_pipeline(strict, body_parser=FakeBodyParser())

    assert list((dup_settings.output_dir / "replies").glob("*.eml")) == []
    assert read_csv(dup_settings.output_dir / "send_log_dry_run.csv") == []


def test_live_run_sends_the_latest_duplicate_of_each_group_to_notify_to(
    dup_settings, monkeypatch, fake_mcp, capsys
):
    assert run_cli(dup_settings, monkeypatch, "--send-live") == 0

    [sender] = fake_mcp.instances
    assert sender.notify_to == NOTIFY_TO  # read from the environment
    assert [m.references for m in sender.messages] == ["<a3@thyme>", "<b2@thyme>"]
    assert [m.to for m in sender.messages] == ["phillip.allen@enron.com", "jane.doe@enron.com"]
    rows = read_csv(dup_settings.output_dir / "send_log.csv")
    assert [(r["recipient"], r["status"]) for r in rows] == [(NOTIFY_TO, "sent")] * 2
    state = email_state(dup_settings.db_path)
    assert [mid for mid, (_, sent, _) in state.items() if sent == 1] == ["<a3@thyme>", "<b2@thyme>"]
    assert all(date for _, sent, date in state.values() if sent == 1)
    assert not (dup_settings.output_dir / "replies").exists()  # live mode writes no drafts
    assert "live" in capsys.readouterr().out.lower()


def test_limit_caps_live_sends(dup_settings, monkeypatch, fake_mcp):
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--limit", "1") == 0
    [sender] = fake_mcp.instances
    assert [m.references for m in sender.messages] == ["<a3@thyme>"]  # first group of the report
    assert len(read_csv(dup_settings.output_dir / "send_log.csv")) == 1
    assert sum(sent for _, sent, _ in email_state(dup_settings.db_path).values()) == 1


def test_limit_does_not_cap_the_dry_run(dup_settings, monkeypatch):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    assert run_cli(dup_settings, monkeypatch, "--limit", "1") == 0
    assert len(list((dup_settings.output_dir / "replies").glob("*.eml"))) == 3


def test_limit_zero_sends_nothing_live(dup_settings, monkeypatch, fake_mcp):
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--limit", "0") == 0
    assert fake_mcp.instances[0].messages == []


@pytest.mark.parametrize("value", [None, "", "   ", "not-an-address"])
def test_live_mode_refuses_to_start_without_notify_to(
    dup_settings, monkeypatch, capsys, value
):
    if value is None:
        monkeypatch.delenv("NOTIFY_TO", raising=False)
    else:
        monkeypatch.setenv("NOTIFY_TO", value)

    assert run_cli(dup_settings, monkeypatch, "--send-live") == 1

    captured = capsys.readouterr()
    assert "NOTIFY_TO" in captured.err
    assert "--notify-to" in captured.err  # D-025: the message names both ways to give it
    assert "Traceback" not in captured.err
    # It refused before doing any work: nothing was ingested or written.
    assert not dup_settings.db_path.exists()
    assert not dup_settings.error_log.exists()
    assert not dup_settings.output_dir.exists()


class FailingMcp(FakeMcp):
    """A live sender whose every send fails, the way a broken MCP session does."""

    def send(self, message):
        self.messages.append(message)
        return SendResult(
            timestamp=message.date,
            recipient=self.notify_to,
            subject=message.subject,
            status=SendStatus.FAILED,
            error="MCP session lost: BrokenPipeError: gone",
            duplicate_message_id=message.references,
        )


def test_a_failing_live_sender_is_logged_and_the_run_still_finishes(
    dup_settings, monkeypatch, capsys
):
    FailingMcp.instances = []
    monkeypatch.setattr(notify, "McpSender", FailingMcp)
    monkeypatch.setenv("NOTIFY_TO", NOTIFY_TO)

    assert run_cli(dup_settings, monkeypatch, "--send-live") == 0

    rows = read_csv(dup_settings.output_dir / "send_log.csv")
    assert [r["status"] for r in rows] == ["failed", "failed"]
    assert all(r["error"] == "MCP session lost: BrokenPipeError: gone" for r in rows)
    assert all(sent == 0 for _, sent, _ in email_state(dup_settings.db_path).values())
    assert "MCP session lost" in capsys.readouterr().out


def test_the_live_sender_is_closed_when_the_run_ends(dup_settings, monkeypatch, fake_mcp):
    assert run_cli(dup_settings, monkeypatch, "--send-live") == 0
    [sender] = fake_mcp.instances
    assert sender.closed == 1


def test_the_live_sender_is_closed_even_when_the_pipeline_fails(dup_settings, monkeypatch, fake_mcp, capsys):
    missing = dup_settings.maildir.parent / "no-such-maildir"
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--maildir", str(missing)) == 1
    [sender] = fake_mcp.instances
    assert sender.closed == 1
    assert "Traceback" not in capsys.readouterr().err


def test_the_live_sender_is_closed_even_on_an_unexpected_crash(dup_settings, monkeypatch, fake_mcp):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main_module, "run_pipeline", boom)
    with pytest.raises(RuntimeError):
        run_cli(dup_settings, monkeypatch, "--send-live")
    assert fake_mcp.instances[0].closed == 1


def test_a_dry_run_after_a_live_run_leaves_the_live_log_alone(dup_settings, monkeypatch, fake_mcp):
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--limit", "1") == 0
    live_log = (dup_settings.output_dir / "send_log.csv").read_bytes()
    assert [r["status"] for r in read_csv(dup_settings.output_dir / "send_log.csv")] == ["sent"]

    assert run_cli(dup_settings, monkeypatch) == 0  # dry run

    assert (dup_settings.output_dir / "send_log.csv").read_bytes() == live_log
    assert [r["status"] for r in read_csv(dup_settings.output_dir / "send_log_dry_run.csv")] == ["dry_run"] * 3


def test_live_runs_append_to_the_live_log(dup_settings, monkeypatch, fake_mcp):
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--limit", "1") == 0
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--limit", "2") == 0
    rows = read_csv(dup_settings.output_dir / "send_log.csv")
    assert [r["status"] for r in rows] == ["sent"] * 3  # 1 from the first run, 2 from the second
    assert (dup_settings.output_dir / "send_log.csv").read_text().count("timestamp,recipient") == 1


def test_the_final_sequence_dry_run_then_live(dup_settings, monkeypatch, fake_mcp, capsys):
    # What the delivery does: a dry run for the drafts, then a limited live send.
    assert run_cli(dup_settings, monkeypatch) == 0
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--limit", "1") == 0

    assert len(list((dup_settings.output_dir / "replies").glob("*.eml"))) == 3  # live run keeps the drafts
    live = read_csv(dup_settings.output_dir / "send_log.csv")
    assert [(r["recipient"], r["status"]) for r in live] == [(NOTIFY_TO, "sent")]
    assert len(read_csv(dup_settings.output_dir / "send_log_dry_run.csv")) == 3
    assert "send_log.csv" in capsys.readouterr().out


def test_a_negative_limit_is_a_usage_error():
    with pytest.raises(SystemExit) as stopped:
        main_module.settings_from_args(["--limit", "-1"])
    assert stopped.value.code == 2


def test_run_pipeline_without_a_sender_in_live_mode_is_an_error(dup_settings):
    import dataclasses

    live = dataclasses.replace(dup_settings, send_live=True)
    with pytest.raises(ValueError, match="sender"):
        main_module.run_pipeline(live, body_parser=FakeBodyParser())


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


# --- the parameter-driven command line (D-025) ---------------------------------------------------
#
# The fixture maildir has two mailboxes: beck-s (3 files, 2 parse) and ring-a (1 file).
# `cli` runs main() from a scratch "repo root", so the default error_log.txt and
# duplicates_report.csv land in tmp_path, never in the real repo.


@pytest.fixture
def cli(tmp_path, maildir, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NOTIFY_TO", raising=False)

    def run(*args, maildir_path=None):
        argv = [
            "--maildir", str(maildir_path or maildir),
            "--output-dir", str(tmp_path / "out"),
            *args,
        ]
        return main_module.main(argv)

    return run


def mailboxes_in_db(tmp_path) -> list[str]:
    conn = sqlite3.connect(tmp_path / "out" / "enron.db")
    try:
        return [m for (m,) in conn.execute("SELECT DISTINCT mailbox FROM emails ORDER BY mailbox")]
    finally:
        conn.close()


def make_precious_db(tmp_path) -> bytes:
    """A database from an earlier run. A failed check must leave it byte for byte alone."""
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    conn = sqlite3.connect(out / "enron.db")
    conn.execute("CREATE TABLE emails (marker TEXT)")
    conn.execute("INSERT INTO emails VALUES ('from the previous run')")
    conn.commit()
    conn.close()
    return (out / "enron.db").read_bytes()


def assert_nothing_was_written(tmp_path, precious=None):
    assert not (tmp_path / "error_log.txt").exists()
    assert not (tmp_path / "duplicates_report.csv").exists()
    out = tmp_path / "out"
    if precious is None:
        assert not out.exists()  # not even the output folder
    else:
        assert sorted(p.name for p in out.iterdir()) == ["enron.db"]
        assert (out / "enron.db").read_bytes() == precious


def test_a_run_with_one_mailbox_ingests_only_that_mailbox(cli, tmp_path, capsys):
    assert cli("--mailboxes", "ring-a") == 0
    assert mailboxes_in_db(tmp_path) == ["ring-a"]
    assert count(tmp_path / "out" / "enron.db", "emails") == 1
    assert json.loads((tmp_path / "out" / "extraction_stats.json").read_text())["total_found"] == 1
    assert "Total files found: 1" in capsys.readouterr().out


def test_a_run_with_one_other_mailbox(cli, tmp_path):
    assert cli("--mailboxes", "beck-s") == 0
    assert mailboxes_in_db(tmp_path) == ["beck-s"]
    assert count(tmp_path / "out" / "enron.db", "emails") == 2  # the third file has no To


def test_a_run_with_two_mailboxes(cli, tmp_path):
    assert cli("--mailboxes", "beck-s", "ring-a") == 0
    assert mailboxes_in_db(tmp_path) == ["beck-s", "ring-a"]
    assert count(tmp_path / "out" / "enron.db", "emails") == 3


def test_any_number_of_mailboxes_works(cli, tmp_path, maildir):
    write_email(maildir, "presto-k/inbox/1.", {"Message-ID": "<5@thyme>"})
    write_email(maildir, "ybarbo-p/inbox/1.", {"Message-ID": "<6@thyme>"})
    assert cli("--mailboxes", "beck-s", "ring-a", "presto-k", "ybarbo-p") == 0
    assert mailboxes_in_db(tmp_path) == ["beck-s", "presto-k", "ring-a", "ybarbo-p"]


def test_the_order_of_the_names_does_not_change_the_result(cli, tmp_path):
    assert cli("--mailboxes", "ring-a", "beck-s") == 0
    first = [
        row for row in sqlite3.connect(tmp_path / "out" / "enron.db").execute(
            "SELECT message_id, source_file FROM emails ORDER BY id"
        )
    ]
    assert cli("--mailboxes", "beck-s", "ring-a") == 0
    second = [
        row for row in sqlite3.connect(tmp_path / "out" / "enron.db").execute(
            "SELECT message_id, source_file FROM emails ORDER BY id"
        )
    ]
    assert first == second  # discover sorts, so the database is the same either way


def test_a_nonexistent_mailbox_is_a_clean_error(cli, tmp_path, capsys):
    assert cli("--mailboxes", "no-such-x") == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "no-such-x" in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1  # one clear line
    assert captured.out == ""  # no stage ran
    assert_nothing_was_written(tmp_path)


def test_a_nonexistent_mailbox_leaves_the_previous_database_untouched(cli, tmp_path):
    precious = make_precious_db(tmp_path)
    assert cli("--mailboxes", "no-such-x") == 1
    assert_nothing_was_written(tmp_path, precious)


def test_one_missing_mailbox_among_good_ones_stops_everything(cli, tmp_path, capsys):
    precious = make_precious_db(tmp_path)
    assert cli("--mailboxes", "ring-a", "no-such-x", "beck-s") == 1
    err = capsys.readouterr().err
    assert "no-such-x" in err and "ring-a" not in err  # names only what is missing
    assert_nothing_was_written(tmp_path, precious)  # ring-a was NOT ingested either


def test_every_missing_mailbox_is_named(cli, tmp_path, capsys):
    assert cli("--mailboxes", "ghost-1", "ring-a", "ghost-2") == 1
    err = capsys.readouterr().err
    assert "ghost-1" in err and "ghost-2" in err


def test_the_default_mailboxes_are_checked_too(cli, tmp_path, capsys):
    # With no --mailboxes the D-013 five are used. The fixture has only ring-a and beck-s.
    precious = make_precious_db(tmp_path)
    assert cli() == 1
    err = capsys.readouterr().err
    assert "ybarbo-p" in err and "presto-k" in err and "baughman-d" in err
    assert_nothing_was_written(tmp_path, precious)


def test_a_missing_maildir_leaves_the_previous_database_untouched(cli, tmp_path, capsys):
    precious = make_precious_db(tmp_path)
    assert cli("--mailboxes", "ring-a", maildir_path=tmp_path / "no-maildir") == 1
    assert "no-maildir" in capsys.readouterr().err
    assert_nothing_was_written(tmp_path, precious)


def test_run_pipeline_itself_checks_before_it_wipes(settings, tmp_path):
    # Not only main(): a caller of run_pipeline gets the same protection.
    import dataclasses

    precious = make_precious_db(tmp_path)
    ghost = dataclasses.replace(settings, mailboxes=("no-such-x",))
    with pytest.raises(FileNotFoundError, match="no-such-x"):
        main_module.run_pipeline(ghost, body_parser=FakeBodyParser())
    assert (tmp_path / "out" / "enron.db").read_bytes() == precious


@pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", ".", ".."])
def test_a_mailbox_name_that_is_not_a_plain_folder_name_is_a_usage_error(cli, tmp_path, capsys, name):
    with pytest.raises(SystemExit) as stopped:
        cli("--mailboxes", name)
    assert stopped.value.code == 2
    assert "mailbox" in capsys.readouterr().err
    assert_nothing_was_written(tmp_path)


def test_an_empty_mailbox_name_is_a_usage_error(cli, tmp_path):
    with pytest.raises(SystemExit) as stopped:
        cli("--mailboxes", "")
    assert stopped.value.code == 2


def test_mailboxes_needs_at_least_one_name(cli, tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli("--mailboxes")
    assert stopped.value.code == 2
    assert_nothing_was_written(tmp_path)


def test_repeated_mailbox_names_are_dropped_keeping_the_order():
    settings = main_module.settings_from_args(["--mailboxes", "b-mail", "a-mail", "b-mail"])
    assert settings.mailboxes == ("b-mail", "a-mail")


@pytest.mark.parametrize("value", ["-1", "100.5", "abc", "nan", "inf", ""])
def test_a_threshold_outside_0_to_100_is_a_usage_error(cli, tmp_path, capsys, value):
    with pytest.raises(SystemExit) as stopped:
        cli("--mailboxes", "ring-a", "--threshold", value)
    assert stopped.value.code == 2
    assert "threshold" in capsys.readouterr().err
    assert_nothing_was_written(tmp_path)


@pytest.mark.parametrize("value, expected", [("0", 0.0), ("100", 100.0), ("85.5", 85.5), ("90", 90.0)])
def test_a_threshold_from_0_to_100_is_accepted(value, expected):
    assert main_module.settings_from_args(["--threshold", value]).similarity_threshold == expected


def test_the_threshold_reaches_dedup(cli, tmp_path, monkeypatch):
    seen = []
    real = dedup.flag_duplicates

    def spy(conn, threshold=90.0, *args, **kwargs):
        seen.append(threshold)
        return real(conn, threshold, *args, **kwargs)

    monkeypatch.setattr(dedup, "flag_duplicates", spy)
    assert cli("--mailboxes", "ring-a", "--threshold", "77.5") == 0
    assert cli("--mailboxes", "ring-a") == 0
    assert seen == [77.5, 90.0]  # the flag, then the spec default


@pytest.mark.parametrize("flag", ["--db-path", "--db"])
def test_db_path_and_its_older_name_both_work(flag, tmp_path):
    settings = main_module.settings_from_args([flag, str(tmp_path / "x.db")])
    assert settings.db_path == tmp_path / "x.db"


def test_db_path_is_used_for_the_database(cli, tmp_path):
    assert cli("--mailboxes", "ring-a", "--db-path", str(tmp_path / "elsewhere.db")) == 0
    assert count(tmp_path / "elsewhere.db", "emails") == 1
    assert not (tmp_path / "out" / "enron.db").exists()


def test_no_arguments_gives_exactly_the_config_defaults(monkeypatch):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    assert main_module.settings_from_args([]) == default_settings()


def test_help_shows_the_defaults_from_config(capsys):
    with pytest.raises(SystemExit) as stopped:
        main_module.settings_from_args(["--help"])
    assert stopped.value.code == 0
    # argparse may wrap a long help line at a hyphen, so compare with all whitespace removed.
    out = "".join(capsys.readouterr().out.split())
    defaults = default_settings()
    for shown in (str(defaults.maildir), str(defaults.output_dir), "90", ", ".join(defaults.mailboxes), "NOTIFY_TO"):
        assert "".join(shown.split()) in out
    for flag in ("--maildir", "--mailboxes", "--output-dir", "--db-path", "--threshold",
                 "--send-live", "--limit", "--notify-to"):
        assert flag in out


# --- --notify-to and NOTIFY_TO -----------------------------------------------------------------------


def test_the_flag_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("NOTIFY_TO", "env@gmail.com")
    assert main_module.settings_from_args(["--notify-to", "flag@gmail.com"]).notify_to == "flag@gmail.com"


def test_the_environment_is_the_fallback(monkeypatch):
    monkeypatch.setenv("NOTIFY_TO", "env@gmail.com")
    assert main_module.settings_from_args([]).notify_to == "env@gmail.com"


def test_with_neither_there_is_no_recipient(monkeypatch):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    assert main_module.settings_from_args([]).notify_to is None


def test_a_live_run_uses_the_notify_to_flag(dup_settings, monkeypatch, fake_mcp, capsys):
    # fake_mcp also sets NOTIFY_TO in the environment; the flag must beat it.
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--notify-to", "flag@gmail.com") == 0
    [sender] = fake_mcp.instances
    assert sender.notify_to == "flag@gmail.com"
    assert "flag@gmail.com" in capsys.readouterr().out  # the summary says where the mail went
    assert {r["recipient"] for r in read_csv(dup_settings.output_dir / "send_log.csv")} == {"flag@gmail.com"}


def test_a_live_run_works_with_the_flag_and_no_environment_variable(dup_settings, monkeypatch, fake_mcp):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--notify-to", "only-flag@gmail.com") == 0
    assert fake_mcp.instances[0].notify_to == "only-flag@gmail.com"


def test_live_mode_with_neither_fails_fast_naming_both_ways(dup_settings, monkeypatch, capsys):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    assert run_cli(dup_settings, monkeypatch, "--send-live") == 1
    err = capsys.readouterr().err
    assert "--notify-to" in err and "NOTIFY_TO" in err and "Traceback" not in err
    assert not dup_settings.db_path.exists() and not dup_settings.output_dir.exists()


def test_a_blank_flag_is_an_error_not_a_fallback_to_the_environment(dup_settings, monkeypatch, capsys):
    # The real McpSender checks the address when it is built, before it connects to
    # anything, so this starts no server. The environment variable is set and must be ignored.
    monkeypatch.setenv("NOTIFY_TO", "env@gmail.com")
    assert run_cli(dup_settings, monkeypatch, "--send-live", "--notify-to", "") == 1
    err = capsys.readouterr().err
    assert "--notify-to" in err and "Traceback" not in err
    assert not dup_settings.db_path.exists() and not dup_settings.output_dir.exists()


def test_a_dry_run_needs_no_recipient_at_all(dup_settings, monkeypatch):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    assert run_cli(dup_settings, monkeypatch) == 0
