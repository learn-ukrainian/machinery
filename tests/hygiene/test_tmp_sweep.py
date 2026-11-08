"""Ported temp-sweep protections outside the managed pytest namespace."""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.hygiene import tmp_sweep as sweep


@pytest.fixture
def inventory(tmp_path, tmp_path_factory, monkeypatch):
    # The pre-push gate runs pytest with a basetemp outside /tmp. The sweep
    # only accepts a real system temp, so the fixture root cannot be tmp_path.
    root = Path(tempfile.mkdtemp(prefix="impl-8755-temp-", dir="/tmp"))
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    # Never the user's real ledger (#9887).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.setattr(sweep, "process_snapshot", lambda: ([], True))
    monkeypatch.setattr(sweep, "registered_worktrees", lambda _: set())
    monkeypatch.setattr(sweep, "protected_roots", lambda: set())

    def make(name="impl-8755-probe", status="done", finished="2020-01-01T00:00:00Z"):
        path = root / name
        path.mkdir()
        (path / "payload").write_bytes(b"x" * 8192)
        for entry in (path / "payload", path):
            os.utime(entry, (time.time() - 86400, time.time() - 86400))
        (tasks / "impl-8755.json").write_text(
            json.dumps({"task_id": "impl-8755", "status": status, "finished_at": finished})
        )
        return path

    def run(**kwargs):
        return sweep.sweep(temp_root=root, task_root=tasks, repo_root=tmp_path, **kwargs)

    try:
        yield SimpleNamespace(root=root, tasks=tasks, make=make, run=run)
    finally:
        shutil.rmtree(root, ignore_errors=True)



@pytest.mark.parametrize(
    "kind",
    ["live", "unknown", "registered", "registration_unknown", "managed", "harness", "git", "mount_unknown", "mount"],
)
def test_protected_paths_never_reaped(inventory, monkeypatch, kind):
    path = inventory.make(name="claude-123" if kind == "harness" else "impl-8755-probe")
    if kind == "live":
        monkeypatch.setattr(sweep, "process_snapshot", lambda: ([(99, path / "payload")], True))
    elif kind == "unknown":
        monkeypatch.setattr(sweep, "process_snapshot", lambda: ([], False))
    elif kind.startswith("registration") or kind == "registered":
        monkeypatch.setattr(
            sweep, "registered_worktrees", lambda _: {path / "nested"} if kind == "registered" else None
        )
    elif kind == "managed":
        monkeypatch.setattr(sweep, "protected_roots", lambda: {path / "task-scratch"})
    elif kind == "git":
        (path / ".git").write_text("gitdir: elsewhere")
    elif kind == "mount_unknown":
        monkeypatch.setattr(sweep, "mount_points", lambda: None)
    elif kind == "mount":
        monkeypatch.setattr(sweep, "mount_points", lambda: frozenset({str(path)}))
    report = inventory.run(apply=True)
    assert report["rows"][0]["decision"] == "preserve" and path.exists()
    assert report["bytes_reclaimed"] == 0
    if kind == "live":
        assert report["rows"][0]["live_pids"] == [99]



def test_live_listener_is_named_and_a_closed_one_stays_held(inventory, monkeypatch):
    """A listening socket is reported as such; its leftover file keeps the tree held (the ledger cannot manifest it)."""
    path = inventory.make()
    try:
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError as error:
        pytest.skip(f"this sandbox forbids AF_UNIX socket creation: {error}")
    try:
        endpoint.bind(str(path / "s"))
    except OSError:  # path longer than sun_path
        monkeypatch.chdir(path)
        endpoint.bind("s")
    endpoint.listen()
    try:
        report = inventory.run(apply=True)
        assert path.exists() and report["rows"][0]["reason"] == "live_socket"
        assert report["bytes_reclaimed"] == 0
    finally:
        endpoint.close()
    report = inventory.run(apply=True)
    assert path.exists() and report["rows"][0]["reason"] == "special_file"



