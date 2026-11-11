"""Exercise the inbox hook's embedded stdlib SQLite probe (#9662)."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path

HOOK = machinery_path('agents_extensions/shared/hooks/check-agent-inbox.sh')


def _run_probe(monkeypatch, db: Path, recipient: str) -> None:
    # Execute the actual heredoc, independent of whether sqlite3 CLI is installed.
    source = HOOK.read_text(encoding="utf-8").split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    monkeypatch.setattr(sys, "argv", ["-", str(db), recipient])
    exec(compile(source, str(HOOK), "exec"), {})


@pytest.mark.parametrize("recipient", ["codex", "grok", "grok-build"])
def test_probe_special_path_counts_one_unread_and_is_readonly(tmp_path, monkeypatch, capsys, recipient):
    db = tmp_path / "inbox ? # % space.db"
    target = "grok-build" if recipient == "grok" else "grok" if recipient == "grok-build" else recipient
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE messages (to_llm TEXT, acknowledged INTEGER, consumed_by_live_driver INTEGER)")
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?)",
            [(target, None, None), (target, 1, 0), (target, 0, 1), ("claude", 0, 0)],
        )
        conn.commit()
    before = db.read_bytes()
    connect = sqlite3.connect
    opened = []

    def inspect_connection(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == db.resolve()
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (1000,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO messages VALUES ('codex', 0, 0)")
        return conn

    monkeypatch.setattr(sqlite3, "connect", inspect_connection)
    _run_probe(monkeypatch, db, recipient)

    assert capsys.readouterr().out == "1\n"
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert db.read_bytes() == before
    assert list(tmp_path.iterdir()) == [db]


@pytest.mark.parametrize("state", ["empty", "missing", "corrupt", "schema_drift"])
def test_probe_empty_and_error_results(tmp_path, monkeypatch, capsys, state):
    db = tmp_path / "inbox ? #.db"
    if state == "corrupt":
        db.write_bytes(b"not a SQLite database")
    elif state in ("empty", "schema_drift"):
        with closing(sqlite3.connect(db)) as conn:
            if state == "empty":
                conn.execute("CREATE TABLE messages (to_llm TEXT, acknowledged INTEGER, consumed_by_live_driver INTEGER)")
            else:
                conn.execute("CREATE TABLE unrelated (id INTEGER)")
            conn.commit()
    before = db.read_bytes() if db.exists() else None

    _run_probe(monkeypatch, db, "codex")

    captured = capsys.readouterr()
    assert captured.out == ("0\n" if state == "empty" else "")
    assert captured.err == ""
    assert (db.read_bytes() if db.exists() else None) == before
    assert list(tmp_path.iterdir()) == ([] if state == "missing" else [db])

    if state == "empty":
        # The installed entry must reach the empty-inbox fast path with only
        # the configured primary, rather than its own path.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        nested = tmp_path / "nested"
        nested.mkdir()
        result = subprocess.run(
            ["bash", str(HOOK)], input="{}", text=True, capture_output=True,
            cwd=nested, env={
                "PATH": os.environ["PATH"], "HOME": str(tmp_path),
                "LEARN_UKRAINIAN_PYTHON": sys.executable,
                "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(tmp_path),
                "AB_DB_PATH": str(db), "LEARN_UK_HOOK_RECIPIENT": "codex",
            }, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert result.stderr == ""
        assert db.read_bytes() == before
