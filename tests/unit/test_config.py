"""Tests for config.py: the D-013 mailbox list, the default paths (D-018) and
the single home of every default (D-025)."""
import dataclasses
import inspect
from pathlib import Path

import pytest

from enron_pipeline import dedup
from enron_pipeline.config import (
    DB_FILE,
    DEDUP_STATS_FILE,
    DRY_RUN_LOG_FILE,
    EXTRACTION_STATS_FILE,
    LIVE_SEND_LIMIT,
    NOTIFY_TO_ENV,
    OUTPUT_DIR,
    REPLIES_DIR,
    SEND_LOG_FILE,
    SIMILARITY_THRESHOLD,
    Settings,
    default_settings,
)

# D-013, Set 1, in size-band order (smallest to largest).
D013_MAILBOXES = ("ring-a", "ybarbo-p", "presto-k", "baughman-d", "beck-s")


def test_default_mailboxes_are_the_d013_set():
    assert default_settings().mailboxes == D013_MAILBOXES


def test_mailboxes_are_an_immutable_tuple():
    assert isinstance(default_settings().mailboxes, tuple)


def test_default_paths():
    settings = default_settings()
    assert settings.maildir == Path("data/maildir")
    assert settings.db_path == Path("output/enron.db")
    assert settings.output_dir == Path("output")
    assert settings.error_log == Path("error_log.txt")  # repo root, a spec deliverable
    assert settings.duplicates_report == Path("duplicates_report.csv")  # repo root (D-021)


def test_other_defaults_are_unchanged():
    settings = default_settings()
    assert settings.similarity_threshold == 90.0
    assert settings.send_live is False


def test_live_send_limit_defaults_to_10():
    # D-022 item 4: a live run can never send more than this many emails
    # unless --limit says otherwise.
    assert default_settings().limit == 10


def test_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        default_settings().send_live = True  # type: ignore[misc]


def test_settings_can_be_overridden():
    # main.py builds Settings from CLI flags; a caller can still pass its own list.
    settings = Settings(mailboxes=("lay-k",))
    assert settings.mailboxes == ("lay-k",)


# --- D-025: one home for every default --------------------------------------------------


def test_the_named_constants_hold_the_documented_values():
    assert SIMILARITY_THRESHOLD == 90.0  # the spec value
    assert LIVE_SEND_LIMIT == 10
    assert OUTPUT_DIR == Path("output")
    assert (DB_FILE, EXTRACTION_STATS_FILE, DEDUP_STATS_FILE) == (
        "enron.db", "extraction_stats.json", "dedup_stats.json",
    )
    assert (REPLIES_DIR, SEND_LOG_FILE, DRY_RUN_LOG_FILE) == (
        "replies", "send_log.csv", "send_log_dry_run.csv",
    )
    assert NOTIFY_TO_ENV == "NOTIFY_TO"


def test_settings_defaults_are_built_from_the_constants():
    settings = default_settings()
    assert settings.output_dir == OUTPUT_DIR
    assert settings.db_path == OUTPUT_DIR / DB_FILE
    assert settings.similarity_threshold == SIMILARITY_THRESHOLD
    assert settings.limit == LIVE_SEND_LIMIT


def test_the_recipient_has_no_default():
    assert default_settings().notify_to is None


def test_derived_output_paths_follow_the_output_folder():
    settings = Settings(output_dir=Path("somewhere"))
    assert settings.extraction_stats == Path("somewhere/extraction_stats.json")
    assert settings.dedup_stats == Path("somewhere/dedup_stats.json")
    assert settings.replies_dir == Path("somewhere/replies")


def test_derived_paths_of_the_default_settings_are_the_current_locations():
    settings = default_settings()
    assert settings.extraction_stats == Path("output/extraction_stats.json")
    assert settings.dedup_stats == Path("output/dedup_stats.json")
    assert settings.replies_dir == Path("output/replies")


def test_the_send_log_depends_on_the_mode():
    # D-024 item 7: send_log.csv is for live sends only; a dry run has its own file.
    dry = Settings(output_dir=Path("o"))
    live = Settings(output_dir=Path("o"), send_live=True)
    assert dry.send_log == Path("o/send_log_dry_run.csv")
    assert live.send_log == Path("o/send_log.csv")


def test_dedups_own_default_threshold_matches_the_config_default():
    # dedup keeps its own default so it works in isolation (D-014); this keeps the two equal.
    parameter = inspect.signature(dedup.flag_duplicates).parameters["threshold"]
    assert parameter.default == SIMILARITY_THRESHOLD
