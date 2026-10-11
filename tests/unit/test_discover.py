"""Tests for discover.py (behavior fixed in D-018).

Each test builds a fake maildir under pytest's tmp_path, so nothing touches
the real dataset. make_maildir() takes paths relative to the maildir root and
writes a placeholder file for each (discover never reads file contents).

discover_email_files yields "source_file" strings such as
"maildir/beck-s/inbox/1.": relative, forward slashes, starting with the
maildir folder's own name. The tests always create the root as tmp_path /
"maildir" to match the real layout.
"""
import pytest

from enron_pipeline.discover import (
    discover_email_files,
    mailbox_for,
    source_path,
)


def make_maildir(tmp_path, files=(), empty_dirs=()):
    root = tmp_path / "maildir"
    root.mkdir(exist_ok=True)
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    for name in empty_dirs:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def found(root, mailboxes=None):
    return list(discover_email_files(root, mailboxes))


# --- what is found ---------------------------------------------------------------


def test_files_at_different_depths_are_all_found(tmp_path):
    root = make_maildir(
        tmp_path,
        [
            "beck-s/1.",  # directly in the mailbox folder
            "beck-s/inbox/2.",
            "beck-s/inbox/archive/3.",
            "beck-s/notes_inbox/projects/2001/q2/4.",
        ],
    )
    assert found(root, ["beck-s"]) == [
        "maildir/beck-s/1.",
        "maildir/beck-s/inbox/2.",
        "maildir/beck-s/inbox/archive/3.",
        "maildir/beck-s/notes_inbox/projects/2001/q2/4.",
    ]


def test_only_files_are_yielded_not_directories(tmp_path):
    root = make_maildir(
        tmp_path,
        ["beck-s/inbox/1.", "beck-s/inbox/3./inner"],  # "3." is a directory
        empty_dirs=["beck-s/sent"],
    )
    assert found(root, ["beck-s"]) == [
        "maildir/beck-s/inbox/1.",
        "maildir/beck-s/inbox/3./inner",
    ]


def test_hidden_files_and_hidden_directories_are_skipped(tmp_path):
    root = make_maildir(
        tmp_path,
        [
            "beck-s/inbox/1.",
            "beck-s/inbox/.DS_Store",
            "beck-s/.hidden/2.",
            "beck-s/inbox/.cache/deep/3.",
            "beck-s/.nfs0001",
        ],
    )
    assert found(root, ["beck-s"]) == ["maildir/beck-s/inbox/1."]


def test_a_file_name_ending_in_a_dot_is_not_hidden(tmp_path):
    # Real Enron files are named "1.", "2." and so on.
    root = make_maildir(tmp_path, ["beck-s/inbox/1."])
    assert found(root, ["beck-s"]) == ["maildir/beck-s/inbox/1."]


# --- order and naming ---------------------------------------------------------------


def test_output_is_sorted_and_relative(tmp_path):
    root = make_maildir(
        tmp_path,
        ["beck-s/inbox/2.", "beck-s/inbox/10.", "beck-s/inbox/1.", "beck-s/_sent/5."],
    )
    result = found(root, ["beck-s"])
    # Plain string order: "1." < "10." < "2.". Not natural order, but fixed.
    assert result == [
        "maildir/beck-s/_sent/5.",
        "maildir/beck-s/inbox/1.",
        "maildir/beck-s/inbox/10.",
        "maildir/beck-s/inbox/2.",
    ]
    assert result == sorted(result)
    assert all(isinstance(item, str) for item in result)
    assert not any(item.startswith(str(tmp_path)) for item in result)


def test_output_is_sorted_across_mailboxes_whatever_order_they_are_requested_in(tmp_path):
    root = make_maildir(tmp_path, ["b-mail/inbox/1.", "a-mail/inbox/1.", "c-mail/inbox/1."])
    assert found(root, ["c-mail", "a-mail", "b-mail"]) == [
        "maildir/a-mail/inbox/1.",
        "maildir/b-mail/inbox/1.",
        "maildir/c-mail/inbox/1.",
    ]


def test_a_mailbox_listed_twice_is_walked_once(tmp_path):
    root = make_maildir(tmp_path, ["a-mail/inbox/1."])
    assert found(root, ["a-mail", "a-mail"]) == ["maildir/a-mail/inbox/1."]


def test_repeat_runs_give_identical_output(tmp_path):
    root = make_maildir(tmp_path, [f"a-mail/inbox/{n}." for n in range(30)])
    assert found(root, ["a-mail"]) == found(root, ["a-mail"])


# --- which mailboxes ---------------------------------------------------------------


def test_only_requested_mailboxes_are_walked(tmp_path):
    root = make_maildir(
        tmp_path, ["a-mail/inbox/1.", "b-mail/inbox/1.", "c-mail/inbox/1."]
    )
    assert found(root, ["b-mail"]) == ["maildir/b-mail/inbox/1."]


def test_mailboxes_none_means_every_mailbox(tmp_path):
    root = make_maildir(tmp_path, ["a-mail/inbox/1.", "b-mail/inbox/1."])
    assert found(root) == ["maildir/a-mail/inbox/1.", "maildir/b-mail/inbox/1."]


def test_mailboxes_none_ignores_stray_files_and_hidden_folders_at_the_root(tmp_path):
    root = make_maildir(tmp_path, ["a-mail/inbox/1.", "README", ".git/config"])
    assert found(root) == ["maildir/a-mail/inbox/1."]


# --- errors and empties -----------------------------------------------------------------


def test_missing_mailbox_raises_naming_every_missing_one(tmp_path):
    root = make_maildir(tmp_path, ["real-a/inbox/1."])
    # Raised by the call itself, before anything could be yielded.
    with pytest.raises(FileNotFoundError) as error:
        discover_email_files(root, ["ghost-y", "real-a", "ghost-x"])
    message = str(error.value)
    assert "ghost-x" in message
    assert "ghost-y" in message
    assert "real-a" not in message


def test_missing_mailbox_raises_even_if_the_result_is_never_iterated(tmp_path):
    root = make_maildir(tmp_path)
    with pytest.raises(FileNotFoundError):
        discover_email_files(root, ["ghost-x"])


def test_a_file_named_like_a_mailbox_counts_as_missing(tmp_path):
    root = make_maildir(tmp_path, ["not-a-folder"])
    with pytest.raises(FileNotFoundError, match="not-a-folder"):
        discover_email_files(root, ["not-a-folder"])


@pytest.mark.parametrize("name", ["", ".", "..", "a-mail/inbox", "../outside"])
def test_a_mailbox_name_that_is_not_a_plain_folder_name_is_rejected(tmp_path, name):
    # "" or ".." would otherwise resolve to the maildir itself or its parent
    # and walk far more than the one mailbox that was asked for.
    root = make_maildir(tmp_path, ["a-mail/inbox/1."])
    with pytest.raises(ValueError):
        discover_email_files(root, [name])


def test_missing_maildir_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="maildir"):
        discover_email_files(tmp_path / "maildir", ["a-mail"])


def test_empty_mailbox_yields_nothing_and_does_not_raise(tmp_path):
    root = make_maildir(tmp_path, empty_dirs=["a-mail"])
    assert found(root, ["a-mail"]) == []


def test_mailbox_of_only_empty_folders_yields_nothing(tmp_path):
    root = make_maildir(tmp_path, empty_dirs=["a-mail/inbox", "a-mail/sent/old"])
    assert found(root, ["a-mail"]) == []


def test_empty_mailbox_list_yields_nothing(tmp_path):
    root = make_maildir(tmp_path, ["a-mail/inbox/1."])
    assert found(root, []) == []


# --- helpers ---------------------------------------------------------------------------


def test_mailbox_for_reads_the_second_path_part():
    assert mailbox_for("maildir/lay-k/inbox/45") == "lay-k"
    assert mailbox_for("maildir/beck-s/notes_inbox/deep/er/9.") == "beck-s"


def test_mailbox_for_rejects_a_path_with_no_mailbox():
    with pytest.raises(ValueError):
        mailbox_for("maildir")


def test_source_path_points_at_the_real_file(tmp_path):
    root = make_maildir(tmp_path, ["beck-s/inbox/1."])
    [source_file] = found(root, ["beck-s"])
    assert source_path(root, source_file) == root / "beck-s" / "inbox" / "1."
    assert source_path(root, source_file).is_file()
