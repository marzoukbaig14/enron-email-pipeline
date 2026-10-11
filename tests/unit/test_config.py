"""Tests for config.py: the D-013 mailbox list and the default paths (D-018)."""
import dataclasses
from pathlib import Path

import pytest

from enron_pipeline.config import Settings, default_settings

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


def test_other_defaults_are_unchanged():
    settings = default_settings()
    assert settings.similarity_threshold == 90.0
    assert settings.send_live is False


def test_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        default_settings().send_live = True  # type: ignore[misc]


def test_settings_can_be_overridden():
    # main.py builds Settings from CLI flags; a caller can still pass its own list.
    settings = Settings(mailboxes=("lay-k",))
    assert settings.mailboxes == ("lay-k",)
