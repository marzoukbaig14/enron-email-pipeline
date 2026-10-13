"""mcp_config.json.example must stay valid, in step with the sender, and free of secrets (D-024 item 8)."""
import inspect
import json
import re
from pathlib import Path

from enron_pipeline import notify

EXAMPLE = Path(__file__).resolve().parents[2] / "mcp_config.json.example"


def test_the_example_is_valid_json_with_the_gmail_server_entry():
    config = json.loads(EXAMPLE.read_text())
    assert list(config["mcpServers"]) == ["gmail"]


def test_the_example_launches_the_same_server_as_the_live_sender():
    entry = json.loads(EXAMPLE.read_text())["mcpServers"]["gmail"]
    assert entry["command"] == notify.MCP_SERVER_COMMAND
    assert tuple(entry["args"]) == notify.MCP_SERVER_ARGS


def test_the_example_lists_the_servers_two_path_variables_with_placeholder_values():
    # GMAIL_OAUTH_PATH and GMAIL_CREDENTIALS_PATH are the variables the server
    # reads (checked against @gongrzhe/server-gmail-autoauth-mcp 1.1.11). Only
    # placeholders belong here, never a real path.
    entry = json.loads(EXAMPLE.read_text())["mcpServers"]["gmail"]
    assert entry["env"] == {
        "GMAIL_OAUTH_PATH": "/path/to/gcp-oauth.keys.json",
        "GMAIL_CREDENTIALS_PATH": "/path/to/credentials.json",
    }


def test_the_note_that_the_sender_passes_no_variables_is_true():
    # The note is true because notify starts the server without an env argument,
    # so the server falls back to ~/.gmail-mcp. If the sender ever passes env,
    # this fails and the note in the example has to change with it.
    notes = " ".join(json.loads(EXAMPLE.read_text())["_notes"])
    assert "does not pass" in notes
    assert "env=" not in inspect.getsource(notify._stdio_session)


def test_the_example_points_at_the_oauth_files_and_holds_no_secrets():
    text = EXAMPLE.read_text()
    assert "~/.gmail-mcp/gcp-oauth.keys.json" in text
    assert "~/.gmail-mcp/credentials.json" in text
    assert not re.search(r"/home/\w+|/Users/\w+|C:\\\\Users", text)  # no real home folder
    assert not re.search(r"[\w.+-]+@(?!example\.com)[\w-]+\.[a-z]{2,}", text.replace("@gongrzhe/server-gmail-autoauth-mcp", ""))
    assert not re.search(r"(?i)(client_secret|refresh_token|access_token|AIza|ya29\.|GOCSPX)", text)
