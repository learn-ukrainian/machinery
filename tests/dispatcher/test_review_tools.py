"""Read-only AGY runs whole inside its bubblewrap boundary (C-3027).

Read-only native Claude runs inside the whole-process write boundary, not a per-shell prefix.
"""

import ipaddress
import json
import os
import shutil
import subprocess
import pytest
from lu_machinery.agent_runtime.attempt_network import canonical_host
from lu_machinery.dispatcher import agy_sandbox
from lu_machinery.orchestration.fleet_repos import load_fleet_repos
from tests.dispatcher.test_providers_b import _bwrap_usable, _launch, _linked_worktree
from tests.helpers.product_tree import configured_roots  # noqa: F401






def test_review_checkout_exposes_project_interpreter(tmp_path):
    """Defect: literal .venv/bin/python is missing or an existing foreign interpreter is replaced."""
    tmp_worktree, head = _linked_worktree(tmp_path)
    assert agy_sandbox._link_interpreter(tmp_worktree) is True
    result = subprocess.run([str(tmp_worktree / ".venv/bin/python"), "-c", "print('review-interpreter-ok')"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "review-interpreter-ok\n"
    assert agy_sandbox._link_interpreter(tmp_worktree) is False  # the project link is kept, not recreated
    assert agy_sandbox._input_state(tmp_worktree) == {"head": head, "clean": True}
    untracked = tmp_worktree / ".venv-other"
    untracked.write_text("not the interpreter\n")
    assert agy_sandbox._input_state(tmp_worktree) == {"head": head, "clean": False}
    untracked.unlink()
    (tmp_worktree / "README.md").write_text("changed\n")
    assert agy_sandbox._input_state(tmp_worktree) == {"head": head, "clean": False}
    subprocess.run(["git", "-C", str(tmp_worktree), "add", "README.md"], check=True, timeout=30)
    assert agy_sandbox._input_state(tmp_worktree) == {"head": head, "clean": False}
    subprocess.run(["git", "-C", str(tmp_worktree), "restore", "--staged", "--worktree", "README.md"],
                   check=True, timeout=30)
    subprocess.run(["git", "-C", str(tmp_worktree), "add", ".venv"], check=True, timeout=30)
    assert agy_sandbox._input_state(tmp_worktree) == {"head": head, "clean": False}
    other = tmp_path / "foreign"
    other.mkdir()
    (other / ".venv").mkdir()
    with pytest.raises(ValueError, match="not the project interpreter"):
        agy_sandbox._link_interpreter(other)
    assert (other / ".venv").is_dir() and not (other / ".venv").is_symlink()
    (other / ".venv").rmdir()
    (other / ".venv").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="not the project interpreter"):
        agy_sandbox._link_interpreter(other)


_PY = ".venv/bin/python"
_GIT_ID = "'-c','user.email=s@example.invalid','-c','user.name=S'"
# Each Grok cf-12404-grok-r1 bypass class (F1 publication and primary write, F2 private egress) must
# fail inside the boundary; the granted capabilities must work. "{remote}" and "{primary}" are host
# paths filled in per run; {metadata} is the link-local cloud metadata address and {private} an
# RFC 1918 address, built from integers at run time. The fetch case stands in for any in-sandbox fetcher, such as AGY's web tool.
_CASES = {
    "F1-python-git-push": (False, f"{_PY} -c \"import subprocess,sys; "
                                  f"sys.exit(subprocess.run(['git','push','{{remote}}','HEAD:refs/heads/pwned']).returncode)\""),
    "F1-python-git-commit": (False, f"{_PY} -c \"import subprocess,sys; sys.exit(subprocess.run(['git',{_GIT_ID},"
                                    "'commit','--allow-empty','-qm','x']).returncode)\""),
    # zsh where installed; otherwise another shell than the sh the launcher runs, so -c still nests.
    "F1-zsh-git-push": (False, "$(command -v zsh || command -v dash || command -v bash) -c "
                               "'git push {remote} HEAD:refs/heads/pwned'"),
    "F1-sh-git-push-github": (False, "git push https://github.com/example/repo.git HEAD:refs/heads/pwned"),
    # GitHub is reachable (operator decision 2026-11-10), but the sandbox holds no credential to write with.
    "F1-curl-github-api-post": (False, "curl -sSf --max-time 10 -X POST https://api.github.com/repos/example/repo/pulls/1/merge"),
    "F1-python-primary-write": (False, f"{_PY} -c \"open('{{primary}}/review-mutation-probe','w').write('x')\""),
    "F2-curl-metadata-proxy": (False, "curl -sS --max-time 10 https://{metadata}/latest/meta-data/"),
    "F2-curl-metadata-direct": (False, "curl -sS --noproxy '*' --max-time 5 http://{metadata}/latest/meta-data/"),
    "F2-curl-rfc1918-proxy": (False, "curl -sS --max-time 10 https://{private}/"),
    "F2-curl-rfc1918-direct": (False, "curl -sS --noproxy '*' --max-time 5 http://{private}/"),
    "F2-fetch-metadata": (False, f"{_PY} -c \"import urllib.request as u; "
                                 "u.urlopen('https://{metadata}/latest/meta-data/', timeout=10)\""),
    # GitHub writes through gh: the gh relay of both routes refuses them; neither sandbox has a gh login.
    "F1-gh-pr-merge": (False, "gh pr merge 1 --squash"),
    "F1-gh-api-post": (False, "gh api -X POST repos/example/repo/issues -f title=x"),
    "F1-gh-pr-view-web": (False, "gh pr view 1 -cw"),
    # Destination selectors: the host's gh would connect to them outside the boundary (cf-12521-sol-r1 F1).
    "F2-gh-repo-host": (False, "gh pr view 1 --repo localhost:9/a/b --json number"),
    "F2-gh-url-operand": (False, "gh issue view https://localhost:9/a/b/issues/1"),
    # The host login reads only registered public repositories (decision A6): a registered private one, an
    # unregistered one, and the worktree's own remote when it is neither are refused before gh runs.
    "F2-gh-private-repo": (False, "gh pr view 1 --repo {private_repo} --json number"),
    "F2-gh-unregistered-repo": (False, "gh issue view 1 -R example/unregistered --json number"),
    "F2-gh-unresolved-repo": (False, "gh pr view 1 --json number"),
    "granted-shell-scratch": (True, "echo ok > \"$TMPDIR/probe\" && cat \"$TMPDIR/probe\""),
    "granted-interpreter": (True, f"{_PY} -c \"import pytest; print('interpreter-ok')\""),
    "granted-named-test": (True, f"{_PY} -m pytest -q -p no:cacheprovider test_probe.py"),
    "granted-project-scratch": (True, f"{_PY} -c \"import os, tempfile; "
                                      "tempfile.TemporaryDirectory(dir=os.environ['LU_SCRATCH_ROOT']).cleanup()\""),
}


@pytest.mark.skipif(not _bwrap_usable(), reason="needs a usable unprivileged bubblewrap")
@pytest.mark.parametrize("case", sorted(_CASES))
def test_review_boundary_refuses_bypass_classes_and_keeps_execution(tmp_path, case):
    """Defect: a review interpreter publishes, writes the primary checkout or reaches a private address."""
    worktree, _head = _linked_worktree(tmp_path)
    granted, template = _CASES[case]
    primary = tmp_path / "repo"
    (worktree / "test_probe.py").write_text("def test_probe():\n    assert 1 + 1 == 2\n")
    subprocess.run(["git", "-C", str(worktree), "add", "test_probe.py"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.email=s@example.invalid", "-c", "user.name=S",
                    "commit", "-qm", "probe"], check=True, timeout=30)
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=30)
    private_repo = next(item.github for item in load_fleet_repos().values() if item.role.startswith("private-"))
    command = template.format(remote=remote, primary=primary, metadata=ipaddress.ip_address(2852039166),
                              private=ipaddress.ip_address(167772161), private_repo=private_repo)
    argv, env = _launch(tmp_path, worktree, "adversarial", ["/bin/sh", "-c", command])
    run = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180, check=False, cwd=worktree)
    receipt = json.loads((tmp_path / "adversarial.sandbox.json").read_text())
    assert receipt["status"] == "ran" and receipt["canary"]["passed"] is True, (receipt.get("reason"), run.stderr)
    if granted:
        assert run.returncode == 0, run.stdout + run.stderr
    else:
        assert run.returncode != 0, run.stdout + run.stderr
    # Outcome checks on the host, independent of the command's own exit status.
    assert subprocess.run(["git", "-C", str(remote), "for-each-ref"], check=True, capture_output=True, text=True,
                          timeout=30).stdout == ""
    assert receipt["input"] == {"start": {"head": head, "clean": True}, "end": {"head": head, "clean": True}}
    assert not (primary / "review-mutation-probe").exists()
    # Every route goes through the egress proxy; it records host names only, never an address literal.
    assert all(canonical_host(record["destination"].removesuffix(":443")) for record in receipt["egress"])
    if "-gh-" in case:  # the relay itself refused, not gh failing afterwards (cf-12521-sol-r2 F2)
        assert receipt["gh"][0]["refused"] and "review boundary: " in run.stderr
    assert not (worktree / ".venv").exists()  # the boundary removes the link it created


pytestmark = pytest.mark.usefixtures("configured_roots")


@pytest.mark.skipif(not _bwrap_usable(), reason="needs a usable unprivileged bubblewrap")
@pytest.mark.skipif(shutil.which("gh") is None or shutil.which("curl") is None, reason="needs gh and curl on the host")
@pytest.mark.live_network
@pytest.mark.live_github
def test_sandbox_reaches_public_hosts_and_github_while_filesystem_boundary_holds(tmp_path):
    """Defect class: sandbox egress blocks non-provider hosts or GitHub, or opening it loosens the filesystem boundary.

    Operator decision 2026-11-10: sandboxed agents reach the internet and GitHub. The AGY boundary must reach a
    public non-provider host and read GitHub through the gh relay, while the primary checkout stays
    unwritable, host credentials stay unreadable and the relay still refuses another host. The issue read
    itself is asserted where the host's gh has a login; without one (a CI runner) the relay must still run gh.
    """
    worktree, head = _linked_worktree(tmp_path)
    primary, home = tmp_path / "repo", os.path.expanduser("~")
    # Compare the actual read under the relay's host environment. An auth-status probe under the
    # test runner's different shim/config can fail even while this credential-free-env read works.
    host_env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "XDG_CONFIG_HOME", "GH_CONFIG_DIR")
                if name in os.environ}
    host_read = subprocess.run(["gh", "issue", "view", "100", "-R", "learn-ukrainian/machinery", "--json", "number",
                                "--jq", ".number"], env=host_env, capture_output=True, text=True, timeout=60, check=False)
    logged_in = host_read.returncode == 0 and host_read.stdout == "100\n"
    # Each probe runs on its own line, so one outcome never hides the next.
    command = "\n".join([
        "curl -sSfI --max-time 30 https://www.example.org >/dev/null && echo public-host-reached",
        "curl -sSfI --max-time 10 https://api.github.com >/dev/null 2>&1 || echo direct-github-refused",
        "gh issue view 100 -R learn-ukrainian/machinery --json number --jq .number 2>/dev/null; echo gh-exit=$?",
        f"touch {primary}/sandbox-egress-probe 2>/dev/null || echo primary-unwritable",
        f"ls -d {home}/.config/gh >/dev/null 2>&1 || echo host-gh-config-hidden",
        "gh issue view 100 -R localhost/learn-ukrainian/machinery 2>/dev/null || echo other-host-refused",
    ])
    expected = ["public-host-reached", "direct-github-refused", *(["100", "gh-exit=0"] if logged_in else []), "primary-unwritable",
                "host-gh-config-hidden", "other-host-refused"]
    task_id = "egress-agy"
    argv, env = _launch(tmp_path, worktree, task_id, ["/bin/sh", "-c", command])
    run = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180, check=False, cwd=worktree)
    receipt = json.loads((tmp_path / f"{task_id}.sandbox.json").read_text())
    assert receipt["status"] == "ran" and receipt["canary"]["passed"] is True, (receipt.get("reason"), run.stderr)
    lines = run.stdout.splitlines()
    if not logged_in:  # the relay ran the host's gh, which failed only for want of a login
        assert lines.pop(2) != "gh-exit=0" and lines[0] == "public-host-reached", run.stdout
    assert (run.returncode, lines) == (0, expected), (run.stdout, run.stderr)
    contacted = {record["destination"]: record["bytes"] for record in receipt["egress"]}
    assert contacted.get("www.example.org:443", 0) > 0, contacted
    assert any(record["destination"] == "api.github.com:443" and record.get("refused") and record["bytes"] == 0
               for record in receipt["egress"])
    assert [(record["command"], record["refused"], record["returncode"] == 0) for record in receipt["gh"]] == [
        (["issue", "view"], None, logged_in),
        (["issue", "view"], "--repo/-R accepts only OWNER/REPO on github.com", False)]
    assert receipt["input"] == {"start": {"head": head, "clean": True}, "end": {"head": head, "clean": True}}
    assert not (primary / "sandbox-egress-probe").exists()
