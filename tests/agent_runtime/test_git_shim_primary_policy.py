"""#208/#29: real exec-time containment, including former shell-parser escape classes."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

import pytest

from lu_machinery.common.repo_root import machinery_path
from tests.hooks import test_guard_primary_checkout_write_targets as target_cases

_git = target_cases._git
repositories = target_cases.repositories

SHIM = machinery_path("agent_runtime/shims/git")


def _environment(repositories):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(PATH=f"{SHIM.parent}:/usr/bin:/bin", AGENT_REAL_GIT="/usr/bin/git",
               LEARN_UKRAINIAN_PYTHON=sys.executable, LU_PRODUCT_ROOT=str(repositories["A"]))
    for key in ("AGENT_GIT_SHIM_GUARD_ACTIVE", "LU_GIT_SHIM_ACTIVE", "LU_GIT_SHIM_DEPTH", "PAGER"):
        env.pop(key, None)
    return env


def _run(repositories, cwd, args, extra_env=None, *, pass_fds=()):
    env = _environment(repositories)
    env.update(extra_env or {})
    return subprocess.run([str(SHIM), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30, pass_fds=pass_fds)


@pytest.mark.parametrize("argv", [["stash"], ["stash", "push"], ["checkout-index", "-a", "-f"],
                                  ["submodule", "update", "--force"], ["reset", "--hard"], ["add", "--", "file"],
                                  ["clean", "-f", "-e", "--", "-e", "-n"]])
@pytest.mark.parametrize("route", ["primary", "foreign_worktree", "primary_metadata", "environment", "symlink", "relative", "poisoned_anchor"])
def test_writers_cannot_change_primary_files_or_index(repositories, tmp_path, argv, route):
    """Every route is resolved before choosing the read-only policy, independent of AGENT_NO_MERGE."""
    a, b = repositories["A"], repositories["B"]
    victim = a / "victim"
    victim.write_text("must remain\n")
    (a / "file").write_text("dirty\n")
    before = (a / ".git/index").read_bytes()
    env = {"AGENT_NO_MERGE": "0"}
    cwd, prefix = a, ["--no-pager"]
    if route == "foreign_worktree":
        cwd, prefix = b, [f"--git-dir={b / '.git'}", f"--work-tree={a}"]
    elif route == "primary_metadata":
        cwd, prefix = b, [f"--git-dir={a / '.git'}", f"--work-tree={b}"]
    elif route == "environment":
        cwd, prefix = b, []
        env.update(GIT_DIR=str(b / ".git"), GIT_WORK_TREE=str(a))
    elif route == "symlink":
        cwd, prefix = b, ["-C", str(repositories["link"])]
    elif route == "relative":
        cwd, prefix = b, ["-C", os.path.relpath(a, b)]
    elif route == "poisoned_anchor":
        _git(a, "config", "core.worktree", str(b))
        prefix = [f"--git-dir={a / '.git'}", f"--work-tree={a}"]
    try:
        result = _run(repositories, cwd, [*prefix, *argv], env)
    finally:
        if route == "poisoned_anchor":
            _git(a, "config", "--unset", "core.worktree")
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "primary Git policy" in result.stderr, result.stderr
    assert (a / "file").read_text() == "dirty\n"
    assert victim.read_text() == "must remain\n"
    assert (a / ".git/index").read_bytes() == before


@pytest.mark.parametrize("shape", ["export CMD='git stash'; flock {lock} -c '$CMD'", ". {script}",
                                   "printf 'git stash\\n' | xargs -n 1 -I{{}} sh -c '{{}}'",
                                   "OPT=-e; git clean -f -e -- \"$OPT\" -n"])
def test_shell_expansion_wrappers_and_dot_source_reach_exec_policy(repositories, tmp_path, shape):
    """#216 round-3 blockers: the shell expands the command; the shim sees the actual Git argv."""
    a = repositories["A"]
    (a / "file").write_text("dirty\n")
    victim = a / "victim"
    victim.write_text("keep\n")
    script = tmp_path / "source.sh"
    script.write_text("git stash\n")
    command = shape.format(lock=shlex.quote(str(tmp_path / "lock")), script=shlex.quote(str(script)))
    result = subprocess.run(["/bin/bash", "-c", command], cwd=a, env=_environment(repositories),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0, result.stderr
    assert "primary Git policy" in result.stderr
    assert (a / "file").read_text() == "dirty\n"
    assert victim.read_text() == "keep\n"


@pytest.mark.parametrize("operation", ["merge --ff-only main", "pull --ff-only . main", "add -- file",
                                       "checkout HEAD -- file"])
@pytest.mark.parametrize("shape", ["true && git -C {b} {operation}", "cd {b} && git {operation}"])
def test_compound_foreign_repository_executes_successfully(repositories, operation, shape):
    """Unrelated B on main receives the operation instead of the payload cwd's policy."""
    command = shape.format(b=shlex.quote(str(repositories["B"])), operation=operation)
    result = subprocess.run(["/bin/bash", "-c", command], cwd=repositories["A"], env=_environment(repositories),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("args", [["status", "--porcelain"], ["log", "--oneline", "-1"], ["diff", "--stat"],
                                  ["clean", "-n", "-e", "build"], ["checkout", "--", "file"], ["restore", "file"]])
@pytest.mark.parametrize("inherited_root_fd", [False, True])
@pytest.mark.parametrize("trace", [None, ("GIT_TRACE", "1"), ("GIT_TRACE", "primary"),
                                  ("GIT_TRACE2_EVENT", "primary"), ("GIT_TRACE", "outside")])
def test_enumerated_readers_and_rescue_forms_execute(repositories, tmp_path, args, inherited_root_fd, trace):
    """Inherited-root discovery works; trace environment sinks are refused before Git runs."""
    a = repositories["A"]
    (a / "file").write_text("dirty\n")
    extra_env = {}
    if trace is not None:
        name, value = trace
        value = str(a / "file") if value == "primary" else str(tmp_path / "trace") if value == "outside" else value
        extra_env[name] = value
    if inherited_root_fd:
        fd = os.open(a, os.O_RDONLY | os.O_DIRECTORY)
        try:
            result = _run(repositories, a, ["-C", f"/proc/self/fd/{fd}", *args], extra_env, pass_fds=(fd,))
        finally:
            os.close(fd)
    else:
        result = _run(repositories, a, args, extra_env)
    if trace is not None and trace[1] in {"primary", "outside"}:
        assert result.returncode != 0
        assert "git_trace_destination_refused" in result.stderr
        assert (a / "file").read_text() == "dirty\n"
        assert not (tmp_path / "trace").exists()
        return
    assert result.returncode == 0, result.stderr
    expected = "tracked\n" if args[0] in {"checkout", "restore"} else "dirty\n"
    assert (a / "file").read_text() == expected


def test_foreign_configuration_reader_cannot_run_against_primary(repositories, tmp_path):
    a, b = repositories["A"], repositories["B"]
    marker = tmp_path / "helper-ran"
    _git(b, "config", "diff.external", f"touch {shlex.quote(str(marker))}")
    result = _run(repositories, b, [f"--git-dir={b / '.git'}", f"--work-tree={a}", "diff"])
    assert result.returncode != 0
    assert "git_foreign_config_on_primary" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("index_path", [".git/index", "victim-index"])
@pytest.mark.parametrize("key", ["GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"])
@pytest.mark.parametrize("relative", [False, True])
def test_metadata_environment_cannot_redirect_foreign_add_to_primary_index(repositories, tmp_path, index_path, key, relative):
    """Git's post--C metadata paths must not overwrite primary files or metadata."""
    a, b = repositories["A"], repositories["B"]
    if key == "GIT_INDEX_FILE":
        destination = a / index_path
        destination.write_bytes((a / ".git/index").read_bytes())
    else:
        source = a / (".git/objects" if key == "GIT_OBJECT_DIRECTORY" else ".git")
        suffix = "-relative" if relative else "-absolute"
        destination = source if index_path.startswith(".git") else a / ("victim-" + key.lower() + suffix)
        if destination != source:
            shutil.copytree(source, destination)
    outside = tmp_path / "outside/deep"
    outside.mkdir(parents=True)
    value = os.path.relpath(destination, b) if relative else str(destination)
    before = {str(path.relative_to(a)): path.read_bytes() for path in a.rglob("*") if path.is_file()}
    result = _run(repositories, outside, ["-C", str(b), "add", "file"], {key: value})
    assert result.returncode != 0
    after = {str(path.relative_to(a)): path.read_bytes() for path in a.rglob("*") if path.is_file()}
    assert after == before


def test_missing_target_fails_closed_and_dispatch_stays_writable(repositories):
    result = _run(repositories, repositories["B"], ["-C", str(repositories["root"] / "missing"), "add", "file"])
    assert result.returncode != 0
    assert "undecidable_git_target" in result.stderr or "could not list Git's commands" in result.stderr
    result = _run(repositories, repositories["dispatch"], ["add", "file"])
    assert result.returncode == 0, result.stderr


def test_bounded_maintenance_preserves_primary_content(repositories, tmp_path):
    """Allowed maintenance actually runs; branch/worktree destinations cannot reach protected files."""
    a = repositories["A"]
    (a / "file").write_text("tracked\n")
    for args in (["fetch", "."], ["pull", "--ff-only", ".", "main"]):
        result = _run(repositories, a, args)
        assert result.returncode == 0, result.stderr
        assert (a / "file").read_text() == "tracked\n"
    _git(a, "branch", "disposable")
    result = _run(repositories, a, ["branch", "-D", "disposable"])
    assert result.returncode == 0, result.stderr
    tree = tmp_path / "new worktree"
    result = _run(repositories, a, ["worktree", "add", "--detach", str(tree), "main"])
    assert result.returncode == 0, result.stderr
    assert (tree / "file").read_text() == "tracked\n"
    _git(a, "worktree", "lock", str(tree))
    result = _run(repositories, a, ["worktree", "unlock", str(tree)])
    assert result.returncode == 0, result.stderr
    result = _run(repositories, a, ["worktree", "remove", str(tree)])
    assert result.returncode == 0, result.stderr
    assert not tree.exists()
    result = _run(repositories, a, ["worktree", "add", str(a / "intruder"), "--detach", "main"])
    assert result.returncode != 0
    assert not (a / "intruder").exists()
    result = _run(repositories, a, ["worktree", "add", "--detach", "~/wt", "main"])
    assert result.returncode != 0
    assert not (a / "~").exists()
    b = repositories["B"]
    foreign = [f"--git-dir={b / '.git'}", f"--work-tree={b}"]
    routes = [
        (a, foreign),
        (b, ["-C", str(a), "-C", "", *foreign]),
        (b, ["-C", str(repositories["root"]), "-C", a.name, *foreign]),
        (b, ["-C", str(repositories["link"]), *foreign]),
    ]
    for index, (cwd, prefix) in enumerate(routes):
        # Explicit foreign metadata/worktree does not move the execution cwd.
        result = _run(repositories, cwd, [*prefix, "worktree", "add", "--detach", "primary-intruder", "main"])
        assert result.returncode != 0
        assert not (a / "primary-intruder").exists()
        destination = b / f"allowed-maintenance-{index}"
        relative = os.path.relpath(destination, a)
        result = _run(repositories, cwd, [*prefix, "worktree", "add", "--detach", relative, "main"])
        assert result.returncode == 0, result.stderr
        assert (destination / "file").read_text() == "tracked\n"
        result = _run(repositories, cwd, [*prefix, "worktree", "remove", relative])
        assert result.returncode == 0, result.stderr
        assert not destination.exists()


# `git help -a` of Git 2.53 (commands; the guide and external sections excluded).
_GIT_SUBCOMMANDS = """
add am archive backfill bisect branch bundle checkout cherry-pick citool clean clone commit describe diff fetch
format-patch gc gitk grep gui init log maintenance merge mv notes pull push range-diff rebase reset restore revert rm
scalar shortlog show sparse-checkout stash status submodule switch tag worktree config fast-export fast-import
filter-branch mergetool pack-refs prune reflog refs remote repack replace annotate blame bugreport count-objects
diagnose difftool fsck gitweb help instaweb merge-tree rerere show-branch verify-commit verify-tag version whatchanged
archimport cvsexportcommit cvsimport cvsserver imap-send p4 quiltimport request-pull send-email svn apply
checkout-index commit-graph commit-tree hash-object index-pack merge-file merge-index mktag mktree multi-pack-index
pack-objects prune-packed read-tree replay symbolic-ref unpack-objects update-index update-ref write-tree cat-file
cherry diff-files diff-index diff-pairs diff-tree for-each-ref for-each-repo get-tar-commit-id last-modified ls-files
ls-remote ls-tree merge-base name-rev pack-redundant repo rev-list rev-parse show-index show-ref unpack-file var
verify-pack daemon fetch-pack http-backend send-pack update-server-info check-attr check-ignore check-mailmap
check-ref-format column credential credential-cache credential-store fmt-merge-msg hook interpret-trailers mailinfo
mailsplit merge-one-file patch-id sh-i18n sh-setup stripspace
""".split()
# Bare `git <sub>` forms that read; every other subcommand is refused bare on the primary.
_BARE_READERS = set(
    """
    annotate blame branch cat-file check-attr check-ignore check-ref-format cherry count-objects describe diff
    diff-files diff-index diff-tree for-each-ref grep help log ls-files ls-remote ls-tree merge-base name-rev notes
    range-diff reflog remote rev-list rev-parse shortlog show show-branch show-ref status submodule tag var version
    whatchanged
    """.split()
)


def test_classifier_covers_pinned_git_subcommand_denominator():
    from lu_machinery.guardrails.git_primary_policy import git_invocation_form

    readers = {sub for sub in _GIT_SUBCOMMANDS if git_invocation_form(sub, []) == ("read", [])}
    assert readers == _BARE_READERS
    others = {sub: git_invocation_form(sub, []) for sub in set(_GIT_SUBCOMMANDS) - readers}
    assert {sub: found for sub, found in others.items() if found is not None} == {"fetch": ("fetch", [])}


@pytest.mark.parametrize("sub", sorted(set(_GIT_SUBCOMMANDS) - _BARE_READERS - {"fetch"}))
def test_every_non_enumerated_subcommand_is_refused_before_execution(repositories, sub):
    a = repositories["A"]
    before = (a / "file").read_bytes()
    result = _run(repositories, a, [sub])
    assert result.returncode != 0, (sub, result.stdout, result.stderr)
    assert (a / "file").read_bytes() == before


@pytest.mark.parametrize("action", ["add", "remove"])
def test_foreign_worktree_maintenance_cannot_target_primary(repositories, action):
    """A foreign Git common directory does not exempt an explicit primary filesystem destination."""
    a, b = repositories["A"], repositories["B"]
    destination = a / "new-tree" if action == "add" else a
    result = _run(repositories, b, ["worktree", action, str(destination)])
    assert result.returncode != 0
    assert "git_worktree_path_reaches_primary" in result.stderr
    assert not (a / "new-tree").exists()
