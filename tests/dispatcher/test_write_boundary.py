"""The write boundary (#423) against a real bubblewrap sandbox and a disposable primary with one linked worktree."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from lu_machinery.dispatcher import write_boundary


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def primary(tmp_path, monkeypatch):
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    (repo / "a.txt").write_text("primary\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    worktree = repo / ".worktrees" / "task"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "task")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    return repo, worktree, tmp_path / "receipt.json"


def _run(primary, script: str) -> tuple[int, dict]:
    _, worktree, receipt = primary
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--", "/bin/sh", "-c", script,
    ])
    return code, json.loads(receipt.read_text())


def _digest(*paths: Path) -> dict:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}


@pytest.mark.parametrize("failure", ["setup", "canary"])
def test_setup_or_canary_failure_refuses_and_never_starts_the_provider(primary, monkeypatch, failure):
    """Defect class: a boundary that cannot start, or whose canary finds a writable primary, still launches the
    provider (an unsandboxed fallback), so the run proceeds without containment."""
    repo, worktree, _ = primary
    if failure == "setup":
        monkeypatch.setattr(write_boundary, "unavailable", lambda: "bubblewrap (bwrap) is not installed")
    else:
        planned = write_boundary._Boundary._probes

        def leaky(self, *args):
            # The canary reports a writable primary file: the boundary does not hold.
            return [*planned(self, *args), {"op": "write_open", "path": str(repo / "a.txt"), "expect_any": ["OK"]}]

        monkeypatch.setattr(write_boundary._Boundary, "_probes", leaky)
    marker = worktree / "provider-started"
    code, receipt = _run(primary, f"touch {marker}")
    assert code == write_boundary.REFUSED
    assert receipt["status"] == "refused"
    assert ("canary failed" if failure == "canary" else "not installed") in receipt["reason"]
    assert not marker.exists()
    assert not write_boundary.scratch("wb-test").exists()


def test_primary_working_tree_writes_are_denied_while_the_worktree_commits(primary):
    """Defect class: a write worker changes or adds files in the primary working tree, through a shell redirect,
    a link from its worktree or an interpreter, while its own worktree commit must still reach the host."""
    repo, worktree, _ = primary
    before = _digest(repo / "a.txt")
    script = (
        f"echo x > {repo}/a.txt; touch {repo}/new.txt; ln -s {repo}/a.txt {worktree}/link && echo x > {worktree}/link;"
        f"python3 -c \"open('{repo}/a.txt', 'a').write('x')\";"
        f"echo change > {worktree}/a.txt && git -C {worktree} commit -qam change"
    )
    code, receipt = _run(primary, script)
    assert code == 0 and receipt["status"] == "ran" and receipt["canary"]["passed"]
    assert _digest(repo / "a.txt") == before
    assert not (repo / "new.txt").exists()
    assert receipt["import"]["status"] == "imported"
    assert _git(repo, "rev-parse", "task") == receipt["import"]["after"] != receipt["import"]["before"]
    assert _git(worktree, "status", "--porcelain", "--untracked-files=no") == ""


def test_protected_primary_git_state_cannot_be_changed(primary):
    """Defect class: a write worker changes the primary's HEAD, index, configuration, hooks or checked-out branch,
    directly or through Git porcelain, so later host-side Git commands in the primary run its code or state."""
    repo, worktree, _ = primary
    git_dir = repo / ".git"
    protected = [git_dir / "HEAD", git_dir / "index", git_dir / "config", git_dir / "refs/heads/main",
                 git_dir / "hooks/post-checkout"]
    before = _digest(*protected)
    script = (
        f"git -C {repo} config core.hooksPath /tmp/evil; git -C {repo} checkout -q -b evil;"
        f"git -C {repo} commit -q --allow-empty -m evil; echo 'ref: refs/heads/evil' > {git_dir}/HEAD;"
        f": > {git_dir}/index; printf '#!/bin/sh\\n' > {git_dir}/hooks/post-checkout;"
        f"git -C {repo} update-ref refs/heads/main HEAD; echo 0000 > {git_dir}/refs/heads/main;"
        f"echo 'gitdir: {git_dir}' > {worktree}/.git"
    )
    _, receipt = _run(primary, script)  # the provider's own exit status is not the subject; the host state is
    assert receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    assert _digest(*protected) == before
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    hooks_path = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], capture_output=True)
    assert hooks_path.returncode == 1
    assert "evil" not in _git(repo, "branch", "--list")
    assert (worktree / ".git").read_text().startswith(f"gitdir: {git_dir / 'worktrees'}")


_BROKER_CLIENT = """
import json, os, socket, sys

def send(verb, payload, request_id):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(os.environ["LU_WRITE_BOUNDARY_BROKER"])
        connection.sendall(json.dumps({"verb": verb, "request_id": request_id, "payload": payload}).encode())
        connection.shutdown(socket.SHUT_WR)
        return json.loads(b"".join(iter(lambda: connection.recv(65536), b"")))["returncode"]

good = {"task_id": "wb-test-review-r1", "pr": 7, "pinned_head": sys.argv[1], "agent": "codex", "model": "gpt-6.1-sol",
        "prompt": "review", "review_profile": "infra", "risk": "critical"}
print(json.dumps({
    "other_task": send("review-dispatch", {**good, "task_id": "other-task-review-r1"}, "1" * 32),
    "stale_head": send("review-dispatch", {**good, "pinned_head": "0" * 40}, "2" * 32),
    "forged_author": send("review-dispatch", {**good, "review_author_model": "gpt-6.1-sol"}, "3" * 32),
    "unknown_verb": send("run", {"argv": ["/bin/sh"]}, "4" * 32),
    "good": send("review-dispatch", good, "5" * 32),
    "replay": send("review-dispatch", good, "5" * 32),
}))
"""


def test_broker_refuses_forged_or_replayed_requests(primary, monkeypatch, tmp_path):
    """Defect class: the host broker admits a forged or replayed request (another task's review, a head other than
    the PR's live head, a request-chosen author model, a replayed request or an unknown verb), so a worker inside
    the boundary acts as another task, forges review provenance or runs an unapproved host operation."""
    _, worktree, receipt = primary
    tip = _git(worktree, "rev-parse", "HEAD")
    monkeypatch.setattr(write_boundary._Broker, "_pull_request",
                        lambda self, payload, state: (payload["pr"], "owner/repo", {"headRefOid": tip}))
    monkeypatch.setattr(write_boundary._Broker, "_repo_key", lambda self, slug: "fixture")
    host_steps = []
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None: host_steps.append(
        (module, argv)) or {"returncode": 0, "stdout": "", "stderr": ""})
    client = tmp_path / "client.py"
    client.write_text(_BROKER_CLIENT)
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--author-model", "claude-opus-5-5", "--",
        "/bin/sh", "-c", f"python3 -I {client} {tip} > {worktree}/broker-results.json",
    ])
    assert code == 0, json.loads(receipt.read_text())
    results = json.loads((worktree / "broker-results.json").read_text())
    assert results == {"other_task": 2, "stale_head": 2, "forged_author": 2, "unknown_verb": 2, "good": 0, "replay": 2}
    assert len(host_steps) == 1
    module, argv = host_steps[0]
    assert module == "dispatcher.cli"
    assert argv[argv.index("--review-author-model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--mode") + 1] == "read-only" and argv[argv.index("--pinned-head") + 1] == tip
    assert argv[argv.index("--task-id") + 1] == "wb-test-review-r1" and argv[argv.index("--branch") + 1] == "task"
    assert argv[argv.index("--repo") + 1] == "fixture"
