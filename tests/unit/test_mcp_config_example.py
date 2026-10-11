"""mcp_config.json.example must stay valid, in step with the sender, and free of secrets (D-024 item 8)."""
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


def test_the_example_points_at_the_oauth_files_and_holds_no_secrets():
    text = EXAMPLE.read_text()
    assert "~/.gmail-mcp/gcp-oauth.keys.json" in text
    assert "~/.gmail-mcp/credentials.json" in text
    assert not re.search(r"/home/\w+|/Users/\w+|C:\\\\Users", text)  # no real home folder
    assert not re.search(r"[\w.+-]+@(?!example\.com)[\w-]+\.[a-z]{2,}", text.replace("@gongrzhe/server-gmail-autoauth-mcp", ""))
    assert not re.search(r"(?i)(client_secret|refresh_token|access_token|AIza|ya29\.|GOCSPX)", text)
