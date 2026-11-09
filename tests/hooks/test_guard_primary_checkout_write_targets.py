"""#29: classify Git writes by their resolved repository, including compound commands."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path

HOOK = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"


def _git(cwd: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(
        ["/usr/bin/git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, timeout=30,
    )


@pytest.fixture(scope="module")
def repositories(tmp_path_factory):
    root = tmp_path_factory.mktemp("git targets")
    a, b = root / "protected A", root / "foreign B"
    for repo in (a, b):
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        (repo / "file").write_text("tracked\n")
        (repo / ".gitignore").write_text(".worktrees/\n")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
    dispatch = a / ".worktrees/dispatch/codex/fixture"
    _git(a, "worktree", "add", "-b", "codex/fixture", str(dispatch))
    (root / "link to A").symlink_to(a, target_is_directory=True)
    git_probe = root / "real-git-probe"
    git_probe.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if any(arg.startswith('--list-cmds=') for arg in args) or any(arg in {'rev-parse', 'symbolic-ref', 'config'} for arg in args) or args[-3:-1] == ['worktree', 'list']:\n"
        "    os.execv('/usr/bin/git', ['/usr/bin/git', *args])\n"
        "sys.exit(0)  # Only requested effects are suppressed; target and alias probes run real Git.\n"
    )
    git_probe.chmod(0o700)
    return {"probe": git_probe, "A": a, "B": b, "dispatch": dispatch, "root": root, "link": root / "link to A"}


def _decision(repositories, cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.pop("LEARN_UK_ALLOW_PRIMARY_GIT_WRITE", None)
    env.update(LU_PRODUCT_ROOT=str(repositories["A"]), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [sys.executable, str(HOOK)], cwd=cwd, env=env, text=True, capture_output=True, timeout=60,
        input=json.dumps({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}),
    )
    if result.returncode:
        return result
    # The hook no longer guesses expanded Git argv. The real shim follows the
    # shell, with a tripwire suppressing only the requested final Git effect.
    shim = machinery_path("agent_runtime/shims")
    assert (shim / "git").is_file(), "the tested release must contain the Git shim"
    env.update(PATH=f"{shim}:/usr/bin:/bin", AGENT_REAL_GIT=str(repositories["probe"]),
               LEARN_UKRAINIAN_PYTHON=sys.executable)
    env.pop("PAGER", None)
    env.pop("AGENT_GIT_SHIM_GUARD_ACTIVE", None)
    result = subprocess.run(["/bin/bash", "-c", command], cwd=cwd, env=env, text=True, capture_output=True, timeout=60)
    if result.returncode:
        result.returncode = 2
    return result


@pytest.mark.parametrize("operation", ["merge --ff-only origin/main", "pull", "add -- file", "apply change.patch",
                                       "checkout HEAD -- file", "checkout -- file"])
@pytest.mark.parametrize("cwd_name,target,expected", [("A", "B", 0), ("B", "B", 0), ("B", "A", 2),
                                                       ("A", "A", 2), ("A", "dispatch", 0),
                                                       ("B", None, 0), ("A", None, 2)])
def test_git_target_repository_controls_write_decision(repositories, operation, cwd_name, target, expected):
    """A foreign primary on main is writable; A's primary remains protected and its worktree writable."""
    if operation == "checkout -- file" and (target == "A" or target is None and cwd_name == "A"):
        expected = 0  # Existing rescue form, from A's own index.
    prefix = "git" if target is None else f"git -C {shlex.quote(str(repositories[target]))}"
    command = f"{prefix} {operation}"
    result = _decision(repositories, repositories[cwd_name], command)
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("operation", ["merge --ff-only origin/main", "pull --ff-only", "add -- file",
                                       "apply change.patch", "checkout HEAD -- file"])
@pytest.mark.parametrize("shape", ["true && {git}", "{git}; true", "cd {b} && git {operation}",
                                   "git -C {a} status && {git}", "printf '%s' \"$PATH\" >/dev/null; {git}",
                                   "printf '%s' \"${{GIT_EXEC_PATH}}\" >/dev/null; {git}"])
def test_compound_foreign_write_does_not_require_plain_command(repositories, operation, shape):
    """#29 reproducer: a resolved foreign write must not inherit the payload cwd's repository."""
    a, b = (shlex.quote(str(repositories[name])) for name in ("A", "B"))
    command = shape.format(git=f"git -C {b} {operation}", a=a, b=b, operation=operation)
    result = _decision(repositories, repositories["A"], command)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("target,expected", [("A", 2), ("B", 0)])
@pytest.mark.parametrize("selection", ["cwd", "relative", "git-dir", "split-git-dir", "git-dir-only", "work-tree-only", "repeated-C"])
def test_git_target_selection_is_resolved_by_git(repositories, target, expected, selection):
    """Relative -C and explicit metadata select the actual target, never a configured name."""
    repo = repositories[target]
    cwd = repositories["B"]
    quoted = shlex.quote(str(repo))
    if selection == "cwd":
        cwd, prefix = repo, "git"
    elif selection == "relative":
        prefix = f"git -C {shlex.quote(os.path.relpath(repo, cwd))}"
    elif selection == "git-dir":
        prefix = f"git --git-dir={shlex.quote(str(repo / '.git'))} --work-tree={quoted}"
    elif selection == "git-dir-only":
        prefix = f"git --git-dir={shlex.quote(str(repo / '.git'))}"
    elif selection == "work-tree-only":
        prefix = f"git --work-tree={quoted}"
    elif selection == "split-git-dir":
        prefix = f"git --git-dir {shlex.quote(str(repo / '.git'))} --work-tree {quoted}"
    else:
        prefix = f"git -C .. -C {shlex.quote(repo.name)}"
    result = _decision(repositories, cwd, f"{prefix} add -- file")
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("target", ["link", "missing"])
def test_symlink_to_primary_and_unresolvable_git_target_fail_closed(repositories, target):
    """An alias to A is still A; a missing -C target supplies no proof of a foreign repository."""
    path = repositories["link"] if target == "link" else repositories["root"] / "missing"
    result = _decision(repositories, repositories["B"], f"git -C {shlex.quote(str(path))} add -- file")
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("tail", ["git -C {a} add -- file", "printf dirt > {a}/file"])
def test_foreign_write_does_not_exempt_other_primary_writes(repositories, tail):
    """Each Git intent and shell write retains its own containment decision."""
    a, b = (shlex.quote(str(repositories[name])) for name in ("A", "B"))
    command = f"git -C {b} merge --ff-only origin/main; " + tail.format(a=a)
    result = _decision(repositories, repositories["B"], command)
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("shape", ["/usr/bin/git -C {a} reset --hard", "command -p git -C {a} reset --hard",
                                   "PATH=/usr/bin:/bin git -C {a} reset --hard", "env -i git -C {a} reset --hard",
                                   "sh -c '/usr/bin/git reset --hard'"] + [
    pattern.format(name=name, a="{a}", b="{b}")
    for name in ("PATH", "GIT_EXEC_PATH", "AGENT_REAL_GIT", "AGENT_ORIGINAL_PATH",
                 "LC_DURABLE_HELPER_ROOT", "CODEX_CANONICAL_REPO_ROOT", "LU_PRODUCT_ROOT",
                 "LEARN_UKRAINIAN_PYTHON", "LC_SESSION_ROOT", "AGENT_GIT_SHIM_GUARD_ACTIVE")
    for pattern in ("{name}={b} git -C {a} reset --hard", "export {name}={b}; git -C {a} reset --hard",
                    "env {name}={b} git -C {a} reset --hard", "unset -v {name}; git -C {a} reset --hard",
                    "env -u {name} git -C {a} reset --hard", "env --unset={name} git -C {a} reset --hard",
                    "env -u{name} git -C {a} reset --hard",
                    "for {name} in {b}; do git -C {a} reset --hard; done",
                    "read {name} <<< {b}; git -C {a} reset --hard",
                    "printf -v {name} %s {b}; git -C {a} reset --hard",
                    "declare -n r={name}; r={b}; git -C {a} reset --hard",
                    "mapfile {name} <<< {b}; git -C {a} reset --hard",
                    "getopts a {name} -a; git -C {a} reset --hard",
                    "read '{name}' <<< {b}; git -C {a} reset --hard",
                    'printf -v "{name}" %s {b}; git -C {a} reset --hard')
])
def test_visible_git_shim_bypasses_are_refused_before_execution(repositories, shape):
    """The hook keeps explicit bypass refusals while the shim owns normal expanded argv."""
    command = shape.format(a=shlex.quote(str(repositories["A"])), b=shlex.quote(str(repositories["B"])))
    result = _decision(repositories, repositories["B"], command)
    assert result.returncode == 2
    assert "explicit_git_shim_bypass" in result.stderr
