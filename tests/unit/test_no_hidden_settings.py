"""Guards for D-025: run settings come from config.py and the command line only.

Two source-level checks, done on the parsed code (not a text search), so a
comment or docstring that mentions NOTIFY_TO does not trip them:

- No library module reads the command line or the environment, or builds a
  parser. Only main.py does that; the library takes plain arguments.
- main.py has no literal run settings (file names, folders, the env var name).
  Those are named in config.py.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = sorted((ROOT / "src" / "enron_pipeline").glob("*.py"))

# Names that would mean "reads the outside world": os.environ, os.getenv, sys.argv.
FORBIDDEN_ATTRIBUTES = {("os", "environ"), ("os", "getenv"), ("os", "environb"), ("sys", "argv")}
FORBIDDEN_IMPORTS = {"argparse", "optparse", "click", "typer"}

# Strings that are run settings. They may appear in main.py only as part of a
# longer text (a message or help), never as a value on their own.
SETTING_LITERALS = {
    "output", "enron.db", "data/maildir", "replies",
    "extraction_stats.json", "dedup_stats.json", "send_log.csv", "send_log_dry_run.csv",
    "error_log.txt", "duplicates_report.csv", "NOTIFY_TO",
}


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_library_is_not_empty():
    assert len(LIBRARY) >= 10  # guards against the glob matching nothing


@pytest.mark.parametrize("path", LIBRARY, ids=lambda p: p.name)
def test_library_modules_do_not_read_argv_or_the_environment(path):
    found = []
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (node.value.id, node.attr) in FORBIDDEN_ATTRIBUTES:
                found.append(f"{node.value.id}.{node.attr} (line {node.lineno})")
        if isinstance(node, ast.Import):
            found += [f"import {a.name} (line {node.lineno})" for a in node.names if a.name.split(".")[0] in FORBIDDEN_IMPORTS]
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
            found.append(f"from {node.module} import ... (line {node.lineno})")
    assert found == []


def docstring_nodes(tree: ast.Module) -> set[int]:
    """ids of the string nodes that are docstrings, so they can be skipped."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_main_py_has_no_literal_run_settings():
    tree = parse(ROOT / "main.py")
    skip = docstring_nodes(tree)
    found = [
        f"{node.value!r} (line {node.lineno})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and node.value in SETTING_LITERALS
    ]
    assert found == []
