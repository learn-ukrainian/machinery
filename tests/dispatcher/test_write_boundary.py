"""The write boundary (#423) against a real bubblewrap sandbox and a disposable primary with one linked worktree."""

import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.dispatcher import write_boundary
from tests.opsec.test_git_push_scan import push_sandbox  # noqa: F401

# One of GitHub's published address ranges; the launcher reads the real ones on the host.
_GITHUB_RANGE = "140.82.112.0/20"


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
    # GitHub's published address ranges, which the launcher reads with the host login in production.
    monkeypatch.setattr(write_boundary, "github_networks", lambda: (ipaddress.ip_network(_GITHUB_RANGE),))
    # The test's own scripts and fixtures live below its temporary root, which may be a data root the view
    # empties (/var/tmp): keep that one root visible, every other data root stays emptied.
    monkeypatch.setattr(write_boundary, "_DATA_ROOTS",
                        tuple(root for root in write_boundary._DATA_ROOTS if not tmp_path.resolve().is_relative_to(root)))
    return repo, worktree, tmp_path / "receipt.json"


def _run(primary, script: str, *extra: str) -> tuple[int, dict]:
    _, worktree, receipt = primary
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), *extra, "--", "/bin/sh", "-c", script,
    ])
    return code, json.loads(receipt.read_text())


def _digest(*paths: Path) -> dict:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}


@pytest.mark.parametrize("failure", ["setup", "canary", "interpreter", "key-helper"])
def test_setup_or_canary_failure_refuses_and_never_starts_the_provider(primary, monkeypatch, failure, tmp_path):
    """Defect class: a boundary that cannot start, or whose canary finds a writable primary, still launches the
    provider (an unsandboxed fallback), so the run proceeds without containment; or it starts with the Sources
    interpreter below a writable private root, whose parent a worker can move aside to plant its own (PR 528 F2);
    or with a Claude key helper whose input it cannot name, so the helper reads an emptied path (PR 528 F3)."""
    repo, worktree, _ = primary
    extra = []
    if failure == "setup":
        monkeypatch.setattr(write_boundary, "unavailable", lambda: "bubblewrap (bwrap) is not installed")
    elif failure == "interpreter":
        monkeypatch.setattr(write_boundary, "_interpreter_root", lambda: Path("/tmp/tools/python"))
    elif failure == "key-helper":
        login = tmp_path / "home" / ".claude-api"
        login.mkdir(parents=True)
        (login / "settings.json").write_text(json.dumps({"apiKeyHelper": "sh -c 'cat \"$KEY_FILE\"'"}))
        extra = ["--claude-login", str(login)]
    else:
        planned = write_boundary._Boundary._probes

        def leaky(self, *args):
            # The canary reports a writable primary file: the boundary does not hold.
            return [*planned(self, *args), {"op": "write_open", "path": str(repo / "a.txt"), "expect_any": ["OK"]}]

        monkeypatch.setattr(write_boundary._Boundary, "_probes", leaky)
    marker = worktree / "provider-started"
    code, receipt = _run(primary, f"touch {marker}", *extra)
    assert code == write_boundary.REFUSED
    assert receipt["status"] == "refused"
    assert {"canary": "canary failed", "setup": "not installed", "interpreter": "writable private root",
            "key-helper": "key helper"}[failure] in receipt["reason"]
    assert not marker.exists()
    assert not write_boundary.scratch("wb-test").exists()


@pytest.mark.parametrize("writable_file", [True, False])
def test_canary_probes_the_bind_with_a_writable_tracked_file_not_a_read_only_first_one(primary, writable_file):
    """Defect class: the canary probes the first tracked file even when it is read-only on disk, so the kernel's
    permission check (EACCES) answers before the read-only bind (EROFS) and every write worker is refused; or,
    with no tracked file the host user may write, the canary passes or runs without having tested the bind."""
    repo, worktree, _ = primary
    first = repo / ".first.txt"  # git ls-files lists it before a.txt
    first.write_text("read-only on disk\n")
    _git(repo, "add", first.name)
    _git(repo, "commit", "-qm", "read-only first file")
    first.chmod(0o444)
    if not writable_file:
        (repo / "a.txt").chmod(0o444)
    marker = worktree / "provider-started"
    code, receipt = _run(primary, f"touch {marker}")
    if writable_file:
        assert code == 0 and receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
        tracked = [probe for probe in receipt["canary"]["probes"] if probe["op"] in ("write_open", "chmod")
                   and Path(probe["path"]).parent == repo]
        assert {(probe["op"], probe["path"], probe["outcome"]) for probe in tracked} == {
            ("write_open", str(repo / "a.txt"), "EROFS"), ("chmod", str(repo / "a.txt"), "EROFS")}
        assert marker.exists()
    else:
        assert code == write_boundary.REFUSED and receipt["status"] == "refused"
        assert "no tracked regular file the host user may write" in receipt["reason"]
        assert not marker.exists()


def test_primary_working_tree_writes_are_denied_while_the_worktree_commits(primary):
    """Defect class: a write worker changes or adds files in the primary working tree, through a shell redirect,
    a link from its worktree or an interpreter, while its own worktree commit must still reach the host. Its task
    scratch is not writable, or the deploy's copy-tool check refuses the root-owned system tool, which the user
    namespace shows as the overflow uid (#500)."""
    from lu_machinery.agent_deploy import sync_agent_mirror

    repo, worktree, _ = primary
    before = _digest(repo / "a.txt")
    script = (
        f"echo x > {repo}/a.txt; touch {repo}/new.txt; ln -s {repo}/a.txt {worktree}/link && echo x > {worktree}/link;"
        f"python3 -c \"open('{repo}/a.txt', 'a').write('x')\";"
        f'mkdir -p "$LU_TASK_SCRATCH" && echo scratch > "$LU_TASK_SCRATCH/probe" && '
        f'cat "$LU_TASK_SCRATCH/probe" > {worktree}/task-scratch.out;'
        f"PATH=/usr/bin:/bin {sys.executable} -I {sync_agent_mirror.__file__} --resolve-rsync > {worktree}/copy-tool.out;"
        f"echo change > {worktree}/a.txt && git -C {worktree} commit -qam change"
    )
    code, receipt = _run(primary, script)
    assert code == 0 and receipt["status"] == "ran" and receipt["canary"]["passed"]
    assert (worktree / "task-scratch.out").read_text() == "scratch\n"
    system_tool = shutil.which("rsync", path="/usr/bin:/bin")
    if system_tool is not None and Path(system_tool).resolve().stat().st_uid == 0:
        assert (worktree / "copy-tool.out").read_text() == f"{Path(system_tool).resolve()}\n"
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


# -- publication through the broker (decision PUBLISH-CRED-1113, #434) ------------------------------------------------

_ROUTES = ("claude", "codex", "cursor", "agy", "opencode")
_SENTINEL = "ghp_PROBE_SENTINEL_0123456789abcdefABCDEF01"
# The last name is not GitHub's but resolves to one of GitHub's published addresses (a DNS alias).
_GITHUB_HOSTS = ("github.com", "api.github.com", "uploads.github.com", "ssh.github.com", "codeload.github.com",
                 "ghcr.io", "alias.example.org")

_CREDENTIAL_PROBE = """
import json, os, socket, subprocess, sys
sentinel, paths, report = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
cells = {}

def holds(path):
    try:
        if os.path.isdir(path):
            return any(holds(os.path.join(path, name)) for name in os.listdir(path))
        with open(path, "rb") as handle:
            return sentinel.encode() in handle.read()
    except OSError:
        return False

cells["environment"] = not any(sentinel in value for value in os.environ.values()) and not any(
    name.startswith(("GH_", "GITHUB_")) for name in os.environ)
for name, path in paths.items():
    cells["file:" + name] = not holds(path)
fds = []
for fd in os.listdir("/proc/self/fd"):
    try:
        fds.append(holds(os.readlink(f"/proc/self/fd/{fd}")) or holds(f"/proc/self/fd/{fd}"))
    except OSError:
        pass
cells["inherited_descriptors"] = not any(fds)
proxy = os.environ.get("HTTPS_PROXY", "")
host, _, port = proxy.removeprefix("http://").rstrip("/").partition(":")
for name in json.loads(sys.argv[4]):
    with socket.create_connection((host, int(port)), timeout=10) as connection:
        connection.sendall(f"CONNECT {name}:443 HTTP/1.1\\r\\nHost: {name}:443\\r\\n\\r\\n".encode())
        cells["connect:" + name] = connection.recv(64).startswith(b"HTTP/1.1 403")
push = subprocess.run(["git", "-C", os.getcwd(), "push", "https://github.com/owner/repo.git", "HEAD:refs/heads/probe"],
                      capture_output=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
cells["git_push"] = push.returncode != 0
for name, url in (("rest", "https://api.github.com/repos/owner/repo/issues"), ("graphql", "https://api.github.com/graphql")):
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=20)
        cells[name] = False
    except Exception:
        cells[name] = True
open(report, "w").write(json.dumps(cells))
"""


@pytest.mark.parametrize("mode,route", [("workspace-write", route) for route in _ROUTES] + [
    ("read-only", route) for route in ("claude", "codex", "cursor", "opencode")])
def test_no_github_write_authority_is_reachable_inside_any_route(primary, monkeypatch, tmp_path, route, mode):
    """Defect class: a write worker recovers the host's GitHub login inside the boundary (from the environment, gh's
    configuration or an alternate one, Git credential files, netrc, SSH keys or an inherited descriptor) or reaches
    GitHub directly (CLI, REST, GraphQL or Git, by GitHub's names or another name resolving to its addresses), so it
    writes to GitHub without the broker's ownership and gates."""
    _, worktree, receipt = primary
    home = Path(write_boundary.os.environ["HOME"])
    stores = {"gh": home / ".config/gh/hosts.yml", "alternate_gh": tmp_path / "alt-gh/hosts.yml",
              "xdg_gh": tmp_path / "xdg/gh/hosts.yml", "git_credentials": home / ".git-credentials",
              "xdg_git_credentials": tmp_path / "xdg/git/credentials", "netrc": home / ".netrc",
              "ssh_key": home / ".ssh/id_ed25519"}
    for path in stores.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"oauth_token: {_SENTINEL}\n")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "alt-gh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, _SENTINEL)
    from lu_machinery.agent_runtime.attempt_network import AttemptEgress

    # Every name here resolves into GitHub's published ranges (the ``primary`` fixture supplies them).
    async def resolve(self, host):
        return ["140.82.112.3"]

    monkeypatch.setattr(AttemptEgress, "_resolve", resolve)
    credential = {"claude": home / ".claude/.credentials.json", "codex": home / ".codex/auth.json",
                  "agy": home / ".agy/token", "opencode": home / ".local/share/opencode/auth.json",
                  "cursor": home / ".config/cursor/auth.json"}.get(route)
    extra = []
    if credential is not None:  # provider refreshes persist for both authors and reviewers
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.write_text("{}")
        extra = ["--credential", str(credential)]
    # Codex runs on a task-local CODEX_HOME: the Sources server and the selected login, nothing of the real home.
    home_check, refreshed = "", str(credential)
    if route == "codex":
        (home / ".codex/config.toml").write_text('[mcp_servers.foreign]\nurl = "http://127.0.0.1:8766/mcp"\n')
        extra += ["--codex-auth", str(credential)]
        # Beside TMPDIR, never in it: Codex creates no helper PATH aliases under its temporary directory.
        home_check = (' && case "$CODEX_HOME/" in "$TMPDIR"/*) false;; esac'
                      f' && test "$(readlink "$CODEX_HOME/auth.json")" = {credential}'
                      ' && test "$(ls -A "$CODEX_HOME")" = "$(printf \'auth.json\\nconfig.toml\')"'
                      ' && grep -qFx "mcp_optional_startup_grace_ms = 0" "$CODEX_HOME/config.toml"'
                      ' && ! grep -q foreign "$CODEX_HOME/config.toml"'
                      ' && ! (echo "[mcp_servers.planted]" >> "$CODEX_HOME/config.toml") 2>/dev/null'
                      ' && grep -qF \'"/run/lu-write-boundary/sources-stdio.py", "/run/lu-write-boundary/sources.sock"]\''
                      ' "$CODEX_HOME/config.toml"')
        refreshed = '"$CODEX_HOME/auth.json"'  # a refresh through the link lands in the selected login
    probe, report = tmp_path / "probe.py", tmp_path / "cells.json"
    probe.write_text(_CREDENTIAL_PROBE)
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--mode", mode, "--output", str(report), *extra, "--", "/bin/sh", "-c",
        f"python3 -I {probe} {_SENTINEL} '{json.dumps({k: str(v) for k, v in stores.items()})}' {report} "
        f"'{json.dumps(_GITHUB_HOSTS)}'"
        # The Sources proxy every provider names sits at its fixed path inside (sources_command), and its
        # directory cannot be renamed away and replaced by one the worker controls.
        " && test -r /run/lu-write-boundary/sources-stdio.py && ! mv /run/lu-write-boundary /run/moved 2>/dev/null"
        " && test -r /run/lu-write-boundary/sources-stdio.py"
        + home_check + (f" && echo refreshed > {refreshed}" if credential else ""),
    ])
    result = json.loads(receipt.read_text())
    assert code == 0 and result["status"] == "ran", result
    if route == "codex":  # the canary itself proved the trusted Sources definition read-only before Codex started
        assert [probe["outcome"] for probe in result["canary"]["probes"] if probe["op"] == "write_open"
                and probe["path"].endswith("/codex-home/config.toml")] == ["EROFS"], result["canary"]
    cells = json.loads(report.read_text())
    denominator = 1 + len(stores) + 1 + len(_GITHUB_HOSTS) + 3
    assert len(cells) == denominator and all(cells.values()), cells
    # Zero bytes reached any GitHub host: each CONNECT was refused before a connection existed.
    github = [row for row in result["egress"] if row["destination"].split(":")[0] in _GITHUB_HOSTS]
    assert len(github) == len(_GITHUB_HOSTS) and all(row.get("refused") and row["bytes"] == 0 for row in github)
    assert all(path.read_text() == f"oauth_token: {_SENTINEL}\n" for path in stores.values())
    if credential is not None:
        assert credential.read_text() == "refreshed\n"
    if route == "codex":
        assert result["codex_home"] == {"config_sha256": hashlib.sha256(write_boundary.codex_home_config().encode())
                                        .hexdigest(), "auth": "linked", "auth_writable": True}


class _Launch:
    """The launch facts the broker reads: the task, its branch and author model, the primary and its scratch."""

    def __init__(self, repo: Path, scratch: Path, tip: str):
        self.args = type("Args", (), {"task_id": "wb-test", "author_model": "claude-opus-5-5",
                                     "mode": "workspace-write"})()
        self.branch, self.repository, self.common, self.base, self.tip = "refs/heads/task", repo, repo / ".git", scratch, tip
        self.fetched: list[str] = []

    def current_tip(self) -> str:
        return self.tip

    def fetch_objects(self, tip: str) -> None:
        self.fetched.append(tip)


def test_read_only_broker_refuses_publication_and_preserves_checkout(primary, monkeypatch, tmp_path):
    """Defect class: a reviewer publishes through the host broker or writes reviewed files/Git metadata."""
    repo, worktree, receipt = primary
    report = tmp_path / "review-results.json"
    tip = _git(worktree, "rev-parse", "HEAD")
    before = _digest(worktree / "a.txt", repo / ".git/config", repo / ".git/refs/heads/task")
    steps = []
    monkeypatch.setattr(write_boundary._Broker, "_repository_slug", lambda self: "owner/repo")
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None: steps.append(
        (module, argv)) or {"returncode": 0, "stdout": "read-ok", "stderr": ""})
    probe = tmp_path / "review-probe.py"
    probe.write_text(_BROKER_CLIENT[:_BROKER_CLIENT.index("good =")] + '''
verbs = sys.argv[2].split(",")
results = {verb: send(verb, {}, f"{index:032x}") for index, verb in enumerate(verbs)}
results["github-read"] = send("github-read", {"name": "pull", "number": 7}, "f" * 32)
for name, path in (("worktree", "a.txt"), ("git", sys.argv[3])):
    try:
        with open(path, "a") as stream:
            stream.write("changed")
        results[name] = False
    except OSError:
        results[name] = True
open(sys.argv[1], "w").write(json.dumps(results))
''')
    verbs = [verb for verb in write_boundary._BROKER_VERBS if verb != "github-read"]
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--mode", "read-only", "--output", str(report), "--", sys.executable, "-I",
        str(probe), str(report), ",".join(verbs), str(repo / ".git/config"),
    ])
    evidence = json.loads(receipt.read_text())
    assert code == 0 and evidence["canary"]["passed"], evidence
    assert json.loads(report.read_text()) == {**dict.fromkeys(verbs, 2), "github-read": 0, "worktree": True, "git": True}
    assert len(steps) == 1 and steps[0][0] == "publish.github"
    assert _digest(worktree / "a.txt", repo / ".git/config", repo / ".git/refs/heads/task") == before
    assert evidence["import"] == {"status": "unchanged", "before": tip, "after": tip}
    task_file = receipt.with_suffix(".task.json")
    write_boundary.receipt_file(task_file).write_text(receipt.read_text())
    assert write_boundary.verify(task_file, "wb-test", "read-only")[1] is None
    assert write_boundary.verify(task_file, "wb-test", "workspace-write")[1] == (
        "write_boundary_unproven: the receipt names another mode")


def test_declared_resources_in_masked_roots_stay_read_only_without_host_socket_access(primary, monkeypatch, tmp_path):
    """Defect class: private tmpfs hides declared resources, or restoring them exposes host sockets/writes; or the
    view restores more than decision A4 allows, so a worker reads another provider's login, the operator's private
    state, another task's records or a checkout's secrets, and sends them out over its public egress; or it hides
    what the worker needs (the selected login, the toolchain, its own staged inputs)."""
    import socket

    from lu_machinery.dispatcher import runtime_context

    repo, worktree, receipt = primary
    home = Path(os.environ["HOME"])
    for relative, text in ((".codex/auth.json", "other login"), ("ops-state/notes", "private"),
                           (".claude/projects/other.jsonl", "transcript"), (".claude/.credentials.json", "selected"),
                           (".local/bin/tool", "toolchain"), (".claude/settings.json", '{"env": {}}'),
                           (".claude/.claude.json", json.dumps({"oauthAccount": {
                               "accountUuid": "u", "organizationUuid": "o", "emailAddress": "probe@example.invalid"},
                               "projects": {"/elsewhere": {}}}))):
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text(text)
    tasks = repo / "batch_state" / "tasks"
    (tasks / "wb-test.inputs").mkdir(parents=True)
    (tasks / "wb-test.inputs" / "brief.md").write_text("own input")
    (tasks / "other-task.result").write_text("another task")
    (repo / ".secrets").mkdir()
    (repo / ".secrets" / "key").write_text("secret")
    # A Fleet plane named through a link inside the restored primary is emptied where it resolves (PR 528 F1).
    (repo / "dedicated-plane").mkdir()
    (repo / "dedicated-plane" / "private-record").write_text("private")
    (repo / "plane-link").symlink_to("dedicated-plane")
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(repo / "plane-link"))
    # An API-key login's helper file is its selected credential; the file beside it stays hidden (PR 528 F3).
    (home / ".keys").mkdir()
    (home / ".keys" / "selected").write_text("selected key")
    (home / ".keys" / "other").write_text("other key")
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {}, "apiKeyHelper": f"cat {home}/.keys/selected"}))
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(write_boundary, "_HOME_TOOLCHAIN", (".local/bin",))
    masked = tmp_path.parent / ("ro-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:8])
    shared = masked / "shared"
    shared.mkdir(parents=True)
    (shared / "resource").write_text("declared")
    (masked / "hidden").write_text("sibling")
    monkeypatch.setattr(write_boundary, "_MASKED_ROOTS", (*write_boundary._MASKED_ROOTS, masked))
    # HOME named through a link below an emptied root, its login named through that link too (PR 528 F3).
    (masked / "home-link").symlink_to(home)
    monkeypatch.setenv("HOME", str(masked / "home-link"))
    monkeypatch.setattr(runtime_context, "shared_root", lambda: shared)
    client = tmp_path / "resource-probe.py"
    client.write_text("""
import json, os, socket, sys
from pathlib import Path
shared, masked, report = map(Path, sys.argv[1:4])
assert (shared / "resource").read_text() == "declared"
assert not (masked / "hidden").exists()
try:
    (shared / "resource").open("w")
except OSError as exc:
    assert exc.errno == 30
else:
    raise AssertionError("declared resource writable")
with socket.socket(socket.AF_UNIX) as connection:
    try:
        connection.connect(str(shared / "host.sock"))
    except OSError:
        pass
    else:
        raise AssertionError("host socket reachable")
home, repo = Path(os.environ["HOME"]), Path(sys.argv[4])
view = {relative: (home / relative).exists() for relative in (
    ".codex/auth.json", "ops-state", ".claude/projects", ".claude/.credentials.json", ".local/bin/tool",
    ".claude/settings.json")}
# The selected login's global config holds only what its launch guard binds.
view["global_config"] = json.loads((home / ".claude/.claude.json").read_text())
view.update({relative: (repo / relative).exists() for relative in (
    "batch_state/tasks/other-task.result", ".secrets/key", "batch_state/tasks/wb-test.inputs/brief.md", "a.txt",
    "plane-link/private-record", "dedicated-plane/private-record")})
view.update({relative: (home / relative).exists() for relative in (".keys/selected", ".keys/other")})
assert view == {".codex/auth.json": False, "ops-state": False, ".claude/projects": False,
                ".claude/.credentials.json": True, ".local/bin/tool": True, ".claude/settings.json": True,
                "global_config": {"oauthAccount": {"accountUuid": "u", "organizationUuid": "o"}},
                "batch_state/tasks/other-task.result": False, "plane-link/private-record": False,
                "dedicated-plane/private-record": False, ".keys/selected": True, ".keys/other": False,
                ".secrets/key": False, "batch_state/tasks/wb-test.inputs/brief.md": True, "a.txt": True}, view
report.write_text("resources-readable-writes-and-sockets-denied")
""")
    report = tmp_path / "resource-result"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(shared / "host.sock"))
        listener.listen()
        code = write_boundary.main([
            "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
            "--receipt", str(receipt), "--mode", "read-only", "--output", str(report),
            "--credential", str(masked / "home-link/.claude/.credentials.json"), "--claude-login", str(home / ".claude"),
            "--", sys.executable, "-I",
            str(client), str(shared), str(masked), str(report), str(repo),
        ])
    evidence = json.loads(receipt.read_text())
    assert code == 0, evidence
    assert report.read_text() == "resources-readable-writes-and-sockets-denied"
    assert (shared / "resource").read_text() == "declared"
    # The canary itself checked the hidden paths for absence before the provider started.
    absent = {probe["path"] for probe in evidence["canary"]["probes"] if probe["expect_any"] == ["ENOENT"]}
    assert {str(home / ".codex/auth.json"), str(home / "ops-state"), str(home / ".claude/projects"),
            str(tasks / "other-task.result"), str(repo / ".secrets/key")} <= absent, sorted(absent)


@pytest.fixture
def broker(primary, tmp_path, monkeypatch):
    """A broker on the disposable primary, without a socket; GitHub reads and host steps are recorded fakes."""
    repo, worktree, _ = primary
    scratch = tmp_path / "broker-scratch"
    scratch.mkdir()
    instance = write_boundary._Broker.__new__(write_boundary._Broker)
    instance.boundary = _Launch(repo, scratch, _git(worktree, "rev-parse", "HEAD"))
    instance.steps = []
    instance.pulls = {7: {"state": "OPEN", "headRefName": "task", "headRefOid": instance.boundary.tip,
                          "isCrossRepository": False, "baseRefName": "main"},
                      9: {"state": "OPEN", "headRefName": "task", "headRefOid": instance.boundary.tip,
                          "isCrossRepository": False, "baseRefName": "release"},
                      8: {"state": "OPEN", "headRefName": "other", "headRefOid": "1" * 40, "isCrossRepository": False}}
    monkeypatch.setattr(write_boundary._Broker, "_repository_slug", lambda self: "owner/repo")

    def gh_json(self, argv):
        if argv[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "main"}}
        return self.pulls.get(int(argv[2]))

    monkeypatch.setattr(write_boundary._Broker, "_gh_json", gh_json)
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None: self.steps.append(
        (module, argv)) or {"returncode": 0, "stdout": "", "stderr": ""})
    return instance


def _refused(call) -> str:
    with pytest.raises(write_boundary._BrokerRefusal) as refusal:
        call()
    return str(refusal.value)


def test_push_publishes_only_the_launch_branch_at_its_tip_fast_forward(broker, primary, tmp_path, monkeypatch):
    """Defect class: the push verb publishes another branch, a commit other than the worker's tip, a rewrite of the
    remote branch or unscanned text, so a worker overwrites someone else's work or leaks text past the push scan."""
    repo, worktree, _ = primary
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    monkeypatch.setattr(write_boundary._Broker, "_remote", lambda self, slug: (str(remote), []))
    scans = []
    run = subprocess.run

    def scanned(argv, **kwargs):
        if "-I" in argv and "opsec.git_push" in argv[3]:
            scans.append(kwargs["input"])
            return subprocess.CompletedProcess(argv, broker.scan, b"", b"")
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", scanned)
    broker.scan = 0
    tip = broker.boundary.tip
    # A ref whose name ends like the dispatched one is not the dispatched branch.
    _git(repo, "push", "-q", str(remote), f"{tip}:refs/heads/other/refs/heads/task")
    assert "branch tip" in _refused(lambda: broker._push({"head": "0" * 40}))
    assert "dispatched branch" in _refused(lambda: broker._push({"head": tip, "branch": "main"}))
    assert "repository" in _refused(lambda: broker._push({"head": tip, "repo": "owner/other"}))
    broker.scan = 1  # the push scan refuses: nothing is sent
    assert broker._push({"head": tip})["returncode"] == 2
    assert _git(tmp_path, "--git-dir", str(remote), "for-each-ref", "refs/heads/task") == ""
    broker.scan = 0
    assert broker._push({"head": tip})["returncode"] == 0
    assert _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/task") == tip
    assert _git(tmp_path, "--git-dir", str(remote), "for-each-ref", "--format=%(refname)").split() == [
        "refs/heads/other/refs/heads/task", "refs/heads/task"]
    assert scans[-1] == f"refs/heads/task {tip} refs/heads/task {'0' * 40}\n".encode()
    # A rewrite of the published branch is not a fast-forward and is refused before anything is sent.
    _git(worktree, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    broker.boundary.tip = _git(worktree, "rev-parse", "HEAD")
    assert "fast-forward" in _refused(lambda: broker._push({"head": broker.boundary.tip}))
    assert _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/task") == tip


def test_fetch_base_updates_only_the_default_branch_tracking_ref(broker, primary, tmp_path, monkeypatch):
    """Defect class: the fetch verb takes a request-chosen ref or refspec, or rewrites a tracking ref, so a worker
    fetches arbitrary remote state into the shared primary."""
    repo, _, _ = primary
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(remote))
    monkeypatch.setattr(write_boundary._Broker, "_remote", lambda self, slug: (str(remote), []))
    assert "no fields" in _refused(lambda: broker._fetch_base({"ref": "refs/heads/task"}))
    assert broker._fetch_base({})["returncode"] == 0
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == _git(repo, "rev-parse", "main")
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes") == "refs/remotes/origin/main"


def test_pr_create_uses_the_launch_branch_and_default_base(broker):
    """Defect class: pr-create takes the head, base or repository from the request, so a worker opens a PR for
    another branch or against a protected base it was not dispatched for."""
    assert "dispatched branch" in _refused(lambda: broker._pr_create({"title": "t", "body": "b", "head": "main"}))
    assert "default branch" in _refused(lambda: broker._pr_create({"title": "t", "body": "b", "base": "x"}))
    assert "repository" in _refused(lambda: broker._pr_create({"title": "t", "body": "b", "repo": "a/b"}))
    assert broker._pr_create({"title": "Title", "body": "Body", "draft": True})["returncode"] == 0
    module, argv = broker.steps[-1]
    assert module == "publish.github" and argv[:3] == ["pr-create", "--repo", "owner/repo"]
    assert argv[argv.index("--head") + 1] == "task" and argv[argv.index("--base") + 1] == "main" and "--draft" in argv
    assert Path(argv[argv.index("--body-file") + 1]).read_text() == "Body"


def test_pr_edit_changes_only_title_and_body_of_the_own_pr(broker):
    """Defect class: pr-edit changes another task's PR or fields outside title and body (labels, base, state), so a
    worker rewrites someone else's PR or its landing metadata."""
    assert "head is not this task" in _refused(lambda: broker._pr_edit({"pr": 8, "title": "t"}))
    assert "at least one" in _refused(lambda: broker._pr_edit({"pr": 7, "add_labels": ["x"]}))
    assert broker._pr_edit({"pr": 7, "title": "--draft"})["returncode"] == 0
    assert broker.steps[-1] == ("publish.github", ["pr-edit", "--repo", "owner/repo", "--number", "7", "--title=--draft"])


def test_pr_comment_posts_only_on_the_own_pr_through_the_publisher(broker):
    """Defect class: pr-comment posts on another task's PR or bypasses the publisher (whose scan refuses verdict
    markers), so a worker forges a review verdict or comments as another task."""
    assert "head is not this task" in _refused(lambda: broker._pr_comment({"pr": 8, "body": "x"}))
    assert "repository" in _refused(lambda: broker._pr_comment({"pr": 7, "body": "x", "repo": "a/b"}))
    assert broker._pr_comment({"pr": 7, "body": "<!-- cf-verdict v=1 -->"})["returncode"] == 0
    module, argv = broker.steps[-1]
    assert module == "publish.github" and argv[:5] == ["pr-comment", "--repo", "owner/repo", "--number", "7"]


def _review_record(root: Path, name: str, **fields) -> None:
    reply = '{"verdict": "APPROVE", "findings": []}'
    task = {"task_id": name, "status": "done", "initiator": "wb-test", "review_author_model": "claude-opus-5-5",
            "review_mode": "cross_family", "result_sha256": hashlib.sha256(reply.encode()).hexdigest(), **fields}
    (root / f"{name}.json").write_text(json.dumps(task))
    (root / f"{name}.result").write_text(reply)


def test_pr_merge_needs_a_host_held_approval_of_the_live_head(broker, tmp_path, monkeypatch):
    """Defect class: pr-merge enqueues on a worker-written comment, a review of another head or task, a same-family
    review, a stale head or a PR retargeted to another base, so a PR lands without an independent exact-head
    approval or into a base the broker never authorized."""
    from lu_machinery.review import record_cf_verdict as recorder

    root = tmp_path / "tasks"
    root.mkdir()
    monkeypatch.setenv("LU_TASKS_DIR", str(root))
    tip = broker.boundary.tip

    def verdict(task, reply):
        return type("Verdict", (), {"verdict": task["v"], "sha": task["sha"], "family": task["family"]})()

    monkeypatch.setattr(recorder, "_task_verdict", verdict)
    monkeypatch.setattr(recorder, "_task", lambda name, task_root: (json.loads((task_root / f"{name}.json").read_text()), ""))
    assert "branch tip" in _refused(lambda: broker._pr_merge({"pr": 7, "head": "0" * 40}))
    assert "no cross-family APPROVE" in _refused(lambda: broker._pr_merge({"pr": 7, "head": tip}))
    for name, fields in (("other-review-r1", {}), ("wb-test-review-r1", {"sha": "2" * 40}),
                         ("wb-test-review-r2", {"family": "anthropic"}), ("wb-test-review-r3", {"initiator": "other"}),
                         ("wb-test-review-r4", {"v": "CHANGES_REQUESTED"})):
        _review_record(root, name, **{"v": "APPROVED", "sha": tip, "family": "openai", **fields})
    assert "no cross-family APPROVE" in _refused(lambda: broker._pr_merge({"pr": 7, "head": tip}))
    assert broker.steps == []
    _review_record(root, "wb-test-review-r5", v="APPROVED", sha=tip, family="openai")
    # The same approved head on a PR retargeted to another base is not this task's landing.
    assert "default branch" in _refused(lambda: broker._pr_merge({"pr": 9, "head": tip}))
    reply = broker._pr_merge({"pr": 7, "head": tip})
    assert reply["returncode"] == 0 and "wb-test-review-r5" in reply["stdout"]
    assert broker.steps == [("publish.github", ["pr-merge", "--repo", "owner/repo", "--number", "7", "--match-head", tip])]


def test_github_read_runs_only_named_reads_of_the_own_repository(broker):
    """Defect class: the read verb forwards a request-chosen endpoint, method or repository, so the host login
    performs a write or reads another repository's security state for the worker."""
    assert "name must be one of" in _refused(lambda: broker._github_read({"name": "dependabot-alerts"}))
    assert "github-read takes" in _refused(lambda: broker._github_read({"name": "pull", "repo": "a/b"}))
    assert "positive id" in _refused(lambda: broker._github_read({"name": "pull", "number": "7; rm"}))
    assert broker._github_read({"name": "checks", "sha": broker.boundary.tip})["returncode"] == 0
    assert broker.steps[-1] == ("publish.github", ["read", "checks", "--repo", "owner/repo", "--sha", broker.boundary.tip])


# -- the skills' own publish commands forward to the broker inside the boundary (#443) --------------------------------

_FORWARDED = {
    # operation: (the command a skill names for the worker's own target, the same command aimed at another target)
    "push": ("git push origin task", "git push origin HEAD:refs/heads/main"),
    "pr-create": ("{py} -m lu_machinery.publish pr-create --title T --body-file {body} --base main --head task",
                  "{py} -m lu_machinery.publish pr-create --title T --body-file {body} --base main --head other"),
    "pr-edit": ("{py} -m lu_machinery.publish pr-edit --number 7 --body-file {body}",
                "{py} -m lu_machinery.publish pr-edit --number 8 --body-file {body}"),
    "pr-comment": ("{py} -m lu_machinery.publish pr-comment --number 7 --body-file {body}",
                   "{py} -m lu_machinery.publish pr-comment --number 8 --body-file {body}"),
    "pr-merge": ("{py} -m lu_machinery.publish pr-merge --number 7 --match-head {tip}",
                 "{py} -m lu_machinery.publish pr-merge --number 8 --match-head {tip}"),
}


@pytest.mark.parametrize("target", ["own", "foreign", "private"])
@pytest.mark.parametrize("operation", sorted(_FORWARDED))
def test_skill_publish_commands_reach_the_broker_for_the_own_target_only(primary, tmp_path, monkeypatch, operation,
                                                                         target, push_sandbox):  # noqa: F811
    """Defect class: inside the boundary the commands the skills name (git push, the publisher's PR verbs) do not
    reach the broker, so every worker is stuck at its first publish; or they reach it aimed at another branch or PR
    and the broker publishes there (or silently on the worker's own branch instead) rather than refusing; or the
    primary is a registered private repository and the host login still acts on it for a worker that reaches the
    public internet (decision A6)."""
    import sys

    from lu_machinery.dispatcher import agy_sandbox
    from lu_machinery.orchestration.fleet_repos import load_fleet_repos

    repo, worktree, receipt = primary
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(push_sandbox.root))
    monkeypatch.setattr(write_boundary, "machinery_path", lambda relative: push_sandbox.root / "lu_machinery" / relative)
    slug = "owner/repo"
    if target == "private":  # the registry's own private entry, checked by the real eligibility rule
        slug = next(item.github for item in load_fleet_repos().values() if item.role.startswith("private-"))
        assert not agy_sandbox.public_repository(slug)
    else:
        monkeypatch.setattr(agy_sandbox, "public_repository", lambda name: name == "owner/repo")
    _git(repo, "remote", "add", "origin", f"https://github.com/{slug}.git")
    tip = _git(worktree, "rev-parse", "HEAD")
    remote = tmp_path / "broker-remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    pulls = {7: {"state": "OPEN", "headRefName": "task", "headRefOid": tip, "isCrossRepository": False,
                 "baseRefName": "main"},
             8: {"state": "OPEN", "headRefName": "other", "headRefOid": tip, "isCrossRepository": False,
                 "baseRefName": "main"}}

    def gh_json(self, argv):
        return {"defaultBranchRef": {"name": "main"}} if argv[:2] == ["repo", "view"] else pulls.get(int(argv[2]))

    steps = []
    monkeypatch.setattr(write_boundary._Broker, "_gh_json", gh_json)
    monkeypatch.setattr(write_boundary._Broker, "_remote", lambda self, slug: (str(remote), []))
    monkeypatch.setattr(write_boundary._Broker, "_approval", lambda self, head: "wb-test-review-r1")
    monkeypatch.setattr(write_boundary._Broker, "_run", lambda self, argv, key, stdin=None, env=None: {
        "returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None: steps.append(
        argv) or {"returncode": 0, "stdout": "published\n", "stderr": ""})
    body = tmp_path / "body.md"
    body.write_text("Body\n")
    # A private primary gets the own-target command: only the repository's eligibility refuses it.
    command = _FORWARDED[operation][target == "foreign"].format(py=sys.executable, body=body, tip=tip)
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--author-model", "claude-opus-5-5", "--",
        "/bin/sh", "-c", f"{command} > {worktree}/out.txt 2>&1; echo $? > {worktree}/code.txt",
    ])
    result = json.loads(receipt.read_text())
    assert code == 0, result
    exit_code, output = int((worktree / "code.txt").read_text()), (worktree / "out.txt").read_text()
    requests = [(row["verb"], row["returncode"]) for row in result["broker"]]
    remote_refs = _git(tmp_path, "--git-dir", str(remote), "for-each-ref", "--format=%(objectname) %(refname)")
    if target == "own":
        assert exit_code == 0 and requests == [(operation, 0)], output
        if operation == "push":
            assert remote_refs == f"{tip} refs/heads/task" and steps == []
        else:
            assert steps and steps[-1][0] == operation and steps[-1][steps[-1].index("--repo") + 1] == "owner/repo"
    else:
        assert exit_code != 0 and requests == [(operation, 2)] and "refused" in output, output
        assert steps == [] and remote_refs == ""
        if target == "private":
            assert "not a registered public repository" in result["broker"][0]["refused"], result["broker"]
