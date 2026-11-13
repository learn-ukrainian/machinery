"""Tests for the centralized worktree containment predicate (issue #4444).

These build *real* git repositories and worktrees in ``tmp_path`` rather than
faking ``.git`` files, because the module deliberately relies on git plumbing
(``rev-parse --git-common-dir``, ``ls-files``, ``check-ignore``,
``worktree list``) rather than string prefixes.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from lu_machinery.common.scratch import ensure_scratch_root
from lu_machinery.guardrails import worktree_containment as wc


def _clean_env() -> dict[str, str]:
    """Env with git-redirect vars stripped.

    Under ``pre-commit`` / a git hook the process inherits ``GIT_DIR``,
    ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``, etc. pointing at the repo being
    committed. Those would hijack the throwaway repos this test builds (``-C``
    alone does not override them), so we scrub them for every git subprocess —
    the same denylist the module applies internally.
    """
    return {k: v for k, v in os.environ.items() if k not in wc._GIT_ENV_DENYLIST}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
        timeout=30,
    )


@dataclass(frozen=True)
class Layout:
    main: Path  # primary checkout root
    dispatch_wt: Path  # .worktrees/dispatch/claude/task-1 (registered)
    flat_wt: Path  # .worktrees/legacy (registered, non-dispatch)
    external_wt: Path  # <tmp>/external-wt (registered, outside .worktrees)
    outside: Path  # <tmp>/outside (plain dir, not a repo)


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(main)],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
        timeout=30,
    )
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "Test")

    # Tracked content + gitignore rules covering runtime/local state.
    (main / ".gitignore").write_text(".worktrees/\nbuild/\n*.log\nlocal_state/\n")
    (main / "tracked.txt").write_text("tracked\n")
    (main / "pkg").mkdir()
    (main / "pkg" / "module.py").write_text("x = 1\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")

    # Real registered worktrees: a dispatch one, a flat non-dispatch one under
    # .worktrees, and an externally-located one sharing the same .git store.
    dispatch_wt = main / ".worktrees" / "dispatch" / "claude" / "task-1"
    _git(main, "worktree", "add", "-q", "-b", "claude/task-1", str(dispatch_wt))
    flat_wt = main / ".worktrees" / "legacy"
    _git(main, "worktree", "add", "-q", "-b", "legacy", str(flat_wt))
    external_wt = tmp_path / "external-wt"
    _git(main, "worktree", "add", "-q", "-b", "ext", str(external_wt))

    outside = tmp_path / "outside"
    outside.mkdir()

    return Layout(
        main=wc.canonicalize(main),
        dispatch_wt=wc.canonicalize(dispatch_wt),
        flat_wt=wc.canonicalize(flat_wt),
        external_wt=wc.canonicalize(external_wt),
        outside=wc.canonicalize(outside),
    )


# ---------------------------------------------------------------------------
# resolve_main_root — same primary root from anywhere
# ---------------------------------------------------------------------------


def test_resolve_main_root_is_stable_from_every_vantage(layout: Layout):
    assert wc.resolve_main_root(layout.main) == layout.main
    assert wc.resolve_main_root(layout.main / "pkg") == layout.main
    # From an added worktree we still resolve the SAME primary root (AC).
    assert wc.resolve_main_root(layout.dispatch_wt) == layout.main
    assert wc.resolve_main_root(layout.dispatch_wt / "pkg") == layout.main
    assert wc.resolve_main_root(layout.external_wt) == layout.main


@pytest.fixture
def outside_any_repo() -> Iterator[Path]:
    """A directory outside every git repository, independent of pytest's basetemp.

    The protected-push gate puts basetemp under the primary checkout's ``.git``
    directory, so ``tmp_path`` (and ``layout.outside``) may itself lie inside a
    repository there; this directory is checked to lie outside one.
    """
    directory = wc.canonicalize(
        tempfile.mkdtemp(prefix="wc-outside-", dir=os.environ.get("TMPDIR", ensure_scratch_root()))
    )
    try:
        assert wc._resolve_main_root_or_none(directory) is None, "the scratch directory is inside a git repository"
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_resolve_main_root_raises_outside_repo(layout: Layout, outside_any_repo: Path):
    with pytest.raises(wc.NotAGitRepositoryError):
        wc.resolve_main_root(outside_any_repo)


def test_resolve_main_root_uses_filesystem_fallback_when_git_cannot_execute(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
):
    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(wc.subprocess, "run", missing_git)

    assert wc.resolve_main_root(layout.main) == layout.main
    assert wc.resolve_main_root(layout.dispatch_wt) == layout.main


def test_git_probe_propagates_non_missing_executable_errors(layout: Layout, monkeypatch: pytest.MonkeyPatch):
    def forbidden_git(*_args, **_kwargs):
        raise PermissionError("git execution forbidden")

    monkeypatch.setattr(wc.subprocess, "run", forbidden_git)

    with pytest.raises(PermissionError, match="forbidden"):
        wc.resolve_main_root(layout.main)


def test_resolve_main_root_without_git_matches_git_and_never_spawns(layout: Layout, monkeypatch: pytest.MonkeyPatch):
    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("resolve_main_root_without_git ran a subprocess")

    monkeypatch.setattr(wc.subprocess, "run", no_subprocess)
    monkeypatch.setenv("GIT_DIR", str(layout.dispatch_wt / ".git"))
    monkeypatch.setenv("PATH", str(layout.outside))
    for checkout in (layout.main, layout.dispatch_wt, layout.flat_wt, layout.external_wt):
        assert wc.resolve_main_root_without_git(checkout) == layout.main


def _worktree_gitdir(worktree: Path) -> Path:
    return Path((worktree / ".git").read_text().removeprefix("gitdir:").strip())


@pytest.mark.parametrize(
    "defect",
    [
        "no_git",
        "symlinked_dot_git",
        "gitdir_outside_worktrees",
        "relative_link_outside_worktrees",
        "multi_line_link",
        "oversized_link",
        "broken_back_link",
        "foreign_commondir",
        "symlinked_common_dir",
        "symlinked_worktrees_dir",
        "empty_dot_git_dir",
        "primary_without_head",
        "primary_with_malformed_head",
        "primary_without_objects",
        "worktree_gitdir_without_head",
        "fabricated_store",
        "nul_byte_link",
    ],
)
def test_resolve_main_root_without_git_refuses_unexpected_links(layout: Layout, defect: str):
    worktree = layout.dispatch_wt
    gitdir = _worktree_gitdir(worktree)
    target = worktree
    if defect == "no_git":
        target = layout.outside
    elif defect == "symlinked_worktrees_dir":
        # An intermediate component of the derived gitdir path, not only its ends.
        moved = layout.main.parent / "moved-worktrees"
        (layout.main / ".git" / "worktrees").rename(moved)
        (layout.main / ".git" / "worktrees").symlink_to(moved, target_is_directory=True)
    elif defect == "empty_dot_git_dir":
        (layout.outside / ".git").mkdir()
        target = layout.outside
    elif defect == "primary_without_head":
        (layout.main / ".git" / "HEAD").unlink()
        target = layout.main
    elif defect == "primary_with_malformed_head":
        (layout.main / ".git" / "HEAD").write_text("not a ref\n")
    elif defect == "primary_without_objects":
        shutil.rmtree(layout.main / ".git" / "objects")
    elif defect == "worktree_gitdir_without_head":
        (gitdir / "HEAD").unlink()
    elif defect == "fabricated_store":
        # Mutually consistent links into a store that is not a repository.
        store = layout.outside / "fake" / ".git" / "worktrees" / "wt"
        store.mkdir(parents=True)
        target = layout.outside / "wt"
        target.mkdir()
        (target / ".git").write_text(f"gitdir: {store}\n")
        (store / "gitdir").write_text(f"{target / '.git'}\n")
        (store / "commondir").write_text("../..\n")
        (store / "HEAD").write_text("ref: refs/heads/main\n")
    elif defect == "nul_byte_link":
        (worktree / ".git").write_text(f"gitdir: {gitdir}\x00x\n")
    elif defect == "symlinked_dot_git":
        (worktree / ".git").unlink()
        (worktree / ".git").symlink_to(layout.main / ".git")
    elif defect == "gitdir_outside_worktrees":
        (worktree / ".git").write_text(f"gitdir: {layout.main / '.git'}\n")
    elif defect == "relative_link_outside_worktrees":
        (worktree / ".git").write_text("gitdir: ../elsewhere\n")
    elif defect == "multi_line_link":
        (worktree / ".git").write_text(f"gitdir: {gitdir}\nextra\n")
    elif defect == "oversized_link":
        (worktree / ".git").write_text("gitdir: " + "x" * 5000 + "\n")
    elif defect == "broken_back_link":
        (gitdir / "gitdir").write_text(f"{layout.flat_wt / '.git'}\n")
    elif defect == "foreign_commondir":
        (gitdir / "commondir").write_text(f"{layout.outside}\n")
    elif defect == "symlinked_common_dir":
        moved = layout.main.parent / "store"
        (layout.main / ".git").rename(moved)
        (layout.main / ".git").symlink_to(moved, target_is_directory=True)
    with pytest.raises(wc.NotAGitRepositoryError):
        wc.resolve_main_root_without_git(target)


@pytest.mark.parametrize("link", [".git", "gitdir", "commondir", "HEAD"])
def test_resolve_main_root_without_git_refuses_a_fifo_link_without_blocking(layout: Layout, link: str):
    worktree = layout.dispatch_wt
    gitdir = _worktree_gitdir(worktree)
    fifo = worktree / ".git" if link == ".git" else gitdir / link
    fifo.unlink()
    os.mkfifo(fifo)
    outcome: dict[str, BaseException] = {}

    def resolve() -> None:
        try:
            wc.resolve_main_root_without_git(worktree)
        except BaseException as exc:  # recorded for the assertion below
            outcome["raised"] = exc

    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()
    thread.join(timeout=10)
    if thread.is_alive():
        # Give the blocked reader a writer so the thread can end, then fail.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(timeout=10)
        pytest.fail(f"resolution blocked on a FIFO {link}")
    assert isinstance(outcome.get("raised"), wc.NotAGitRepositoryError)


def test_resolve_main_root_without_git_follows_relative_worktree_links(layout: Layout):
    """``worktree.useRelativePaths`` writes both links relative to their own files."""
    worktree = layout.dispatch_wt
    gitdir = _worktree_gitdir(worktree)
    (worktree / ".git").write_text(f"gitdir: {os.path.relpath(gitdir, worktree)}\n")
    (gitdir / "gitdir").write_text(f"{os.path.relpath(worktree / '.git', gitdir)}\n")
    assert wc.resolve_main_root_without_git(worktree) == layout.main


# ---------------------------------------------------------------------------
# classify_repo_path
# ---------------------------------------------------------------------------


def test_tracked_file_in_primary_checkout_is_protected(layout: Layout):
    tracked = layout.main / "tracked.txt"
    assert wc.classify_repo_path(tracked, cwd=layout.main) == "primary_checkout"
    assert wc.is_tracked(tracked, layout.main) is True
    allowed, reason = wc.is_write_allowed(tracked, cwd=layout.main)
    assert allowed is False
    assert reason == "tracked_primary_checkout"
    decision = wc.evaluate_write(tracked, cwd=layout.main)
    assert decision.message  # actionable guidance is populated on denial
    assert ".worktrees/dispatch" in decision.message


def test_gitignored_local_state_is_allowed(layout: Layout):
    ignored = layout.main / "build" / "out.txt"
    assert wc.classify_repo_path(ignored, cwd=layout.main) == "primary_checkout"
    assert wc.is_ignored(ignored, layout.main) is True
    assert wc.is_tracked(ignored, layout.main) is False
    allowed, reason = wc.is_write_allowed(ignored, cwd=layout.main)
    assert allowed is True
    assert reason == "gitignored_local_state"


def test_untracked_non_ignored_in_primary_checkout_is_blocked(layout: Layout):
    scratch = layout.main / "scratch_new.txt"
    assert wc.classify_repo_path(scratch, cwd=layout.main) == "primary_checkout"
    assert wc.is_tracked(scratch, layout.main) is False
    assert wc.is_ignored(scratch, layout.main) is False
    allowed, reason = wc.is_write_allowed(scratch, cwd=layout.main)
    assert allowed is False
    assert reason == "untracked_primary_checkout"


def test_dispatch_worktree_path_is_allowed(layout: Layout):
    target = layout.dispatch_wt / "pkg" / "module.py"
    assert wc.classify_repo_path(target, cwd=layout.main) == "dispatch_worktree"
    assert wc.is_dispatch_worktree(target) is True
    allowed, reason = wc.is_write_allowed(target, cwd=layout.main)
    assert allowed is True
    assert reason == "dispatch_worktree"


def test_dispatch_path_allowed_even_before_worktree_exists(layout: Layout):
    # An agent is about to create .worktrees/dispatch/agy/future/ — structural
    # classification must already treat it as an allowed dispatch location.
    future = layout.main / ".worktrees" / "dispatch" / "agy" / "future" / "new.py"
    assert wc.classify_repo_path(future, cwd=layout.main) == "dispatch_worktree"
    assert wc.is_write_allowed(future, cwd=layout.main)[0] is True


def test_non_dispatch_worktree_paths_are_other_worktree(layout: Layout):
    flat = layout.flat_wt / "tracked.txt"
    ext = layout.external_wt / "tracked.txt"
    assert wc.classify_repo_path(flat, cwd=layout.main) == "other_worktree"
    assert wc.classify_repo_path(ext, cwd=layout.main) == "other_worktree"
    assert wc.is_write_allowed(flat, cwd=layout.main) == (True, "other_worktree")
    assert wc.is_write_allowed(ext, cwd=layout.main) == (True, "other_worktree")


def test_outside_repo_path(layout: Layout):
    target = layout.outside / "note.txt"
    assert wc.classify_repo_path(target, cwd=layout.main) == "outside_repo"
    assert wc.is_write_allowed(target, cwd=layout.main) == (True, "outside_repo")


# ---------------------------------------------------------------------------
# canonicalization: symlink + ".." escapes
# ---------------------------------------------------------------------------


def test_symlink_escape_is_classified_by_real_target(layout: Layout, tmp_path: Path):
    outside_target = tmp_path / "escape_target"
    outside_target.mkdir()
    link = layout.main / "escape"
    link.symlink_to(outside_target)
    # Path is lexically "inside" main, but the symlink lands outside the repo.
    escaped = link / "secret.txt"
    assert wc.classify_repo_path(escaped, cwd=layout.main) == "outside_repo"


def test_symlink_staying_inside_is_primary_checkout(layout: Layout):
    link = layout.main / "inward"
    link.symlink_to(layout.main / "pkg")
    target = link / "module.py"
    assert wc.classify_repo_path(target, cwd=layout.main) == "primary_checkout"
    # And it resolves to the tracked file, so writes are blocked.
    assert wc.is_write_allowed(target, cwd=layout.main)[0] is False


# ---------------------------------------------------------------------------
# missing-path parent resolution
# ---------------------------------------------------------------------------


def test_missing_path_under_untracked_dir_is_blocked(layout: Layout):
    # Parent dir does not exist yet; must still classify + decide.
    missing = layout.main / "newpkg" / "deeply" / "new_module.py"
    assert wc.classify_repo_path(missing, cwd=layout.main) == "primary_checkout"
    assert wc.is_write_allowed(missing, cwd=layout.main) == (
        False,
        "untracked_primary_checkout",
    )


def test_missing_path_under_ignored_dir_is_allowed(layout: Layout):
    missing = layout.main / "build" / "sub" / "artifact.log"
    assert wc.is_ignored(missing, layout.main) is True
    assert wc.is_write_allowed(missing, cwd=layout.main) == (
        True,
        "gitignored_local_state",
    )


# ---------------------------------------------------------------------------
# is_primary_checkout / branch helpers
# ---------------------------------------------------------------------------


def test_is_primary_checkout_matrix(layout: Layout, outside_any_repo: Path):
    assert wc.is_primary_checkout(layout.main) is True
    assert wc.is_primary_checkout(layout.main / "pkg") is True
    assert wc.is_primary_checkout(layout.dispatch_wt) is False
    assert wc.is_primary_checkout(layout.external_wt) is False
    assert wc.is_primary_checkout(outside_any_repo) is False


def test_branch_helpers(layout: Layout):
    assert wc.current_branch(layout.main) == "main"
    assert wc.is_protected_branch(layout.main) is True
    # Dispatch worktree sits on an agent branch, never protected.
    assert wc.current_branch(layout.dispatch_wt) == "claude/task-1"
    assert wc.is_protected_branch(layout.dispatch_wt) is False


# ---------------------------------------------------------------------------
# read-only invocation must not mutate anything
# ---------------------------------------------------------------------------


def test_read_only_operations_do_not_dirty_the_checkout(layout: Layout):
    before = _git(layout.main, "status", "--porcelain").stdout
    # Exercise the full surface from the repo root.
    wc.classify_repo_path(layout.main / "tracked.txt", cwd=layout.main)
    wc.is_write_allowed(layout.main / "tracked.txt", cwd=layout.main)
    wc.is_primary_checkout(layout.main)
    wc.registered_worktrees(layout.main)
    after = _git(layout.main, "status", "--porcelain").stdout
    assert before == after


def test_module_is_robust_to_hostile_git_env(layout: Layout, monkeypatch):
    # Consumers (#4448 hooks, #4450 git shim) run inside git hooks where GIT_DIR
    # etc. point elsewhere. The module must scrub them, so resolution/decisions
    # stay correct regardless of the ambient (redirecting) git environment.
    monkeypatch.setenv("GIT_DIR", str(layout.external_wt / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(layout.external_wt))
    assert wc.resolve_main_root(layout.main / "pkg") == layout.main
    assert wc.classify_repo_path(layout.main / "tracked.txt", cwd=layout.main) == "primary_checkout"
    assert wc.is_write_allowed(layout.main / "tracked.txt", cwd=layout.main) == (
        False,
        "tracked_primary_checkout",
    )


def test_git_write_classified_by_mutated_work_tree_not_cwd(layout: Layout, tmp_path: Path):
    """#29 defect class: the write guard classified Git's cwd, not the work tree Git mutates.

    From a dispatch worktree, ``--git-dir=<other repo> --work-tree=<primary>``
    keeps Git's cwd inside the dispatch worktree while Git rewrites the
    primary; a foreign work tree that contains the primary reaches it too.
    """
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    foreign = tmp_path / "foreign"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(foreign)], check=True, capture_output=True, env=_clean_env(), timeout=30
    )
    env = {k: v for k, v in _clean_env().items() if k != "LEARN_UK_ALLOW_PRIMARY_GIT_WRITE"}
    env["LU_PRODUCT_ROOT"] = str(layout.main)

    def decision(cwd: Path, command: str) -> int:
        payload = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}
        result = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return result.returncode

    to_primary = f"git --git-dir={foreign}/.git --work-tree={layout.main}"
    to_ancestor = f"git --git-dir={foreign}/.git --work-tree={tmp_path}"
    assert decision(layout.dispatch_wt, f"{to_primary} reset --hard") == 2
    assert decision(layout.dispatch_wt, f"{to_primary} merge --ff-only main") == 2
    assert decision(layout.dispatch_wt, f"{to_primary}/pkg merge --ff-only main") == 2
    assert decision(foreign, f"{to_ancestor} merge --ff-only main") == 2
    # Pathspecs still resolve against Git's cwd: into the primary blocks,
    # inside the dispatch worktree stays allowed.
    assert decision(layout.dispatch_wt, f"{to_primary} checkout -- ../../../../tracked.txt") == 2
    assert decision(layout.dispatch_wt, f"{to_primary} add -- tracked.txt") == 0
    # Paths Git selects that the hook resolves against the wrong base or cannot see.
    assert decision(layout.dispatch_wt, f"{to_primary} checkout -- :/tracked.txt") == 2
    own_metadata = f"git --git-dir={layout.main}/.git --work-tree={layout.main}"
    assert decision(layout.dispatch_wt, f"{own_metadata} add -- tracked.txt") == 2
    assert decision(layout.dispatch_wt, f"{to_primary} checkout --pathspec-from-file=f HEAD") == 2
    assert decision(layout.dispatch_wt, f"{to_primary} checkout --pathspec-from-fil=f HEAD") == 2


@pytest.mark.parametrize("redirect", ["gitfile", "symlinked_gitfile", "git_env"])
@pytest.mark.parametrize("tool", ["Write", "Bash", "glob", "nested"])
def test_primary_guard_rejects_cwd_repository_identity_substitution(layout: Layout, redirect: str, tool: str):
    """Worker-controlled Git identity must not redefine the configured protected checkout."""
    foreign = layout.outside / "foreign"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(foreign)],
        check=True, capture_output=True, env=_clean_env(), timeout=30,
    )
    env = {
        key: value for key, value in _clean_env().items()
        if key not in {"LEARN_UK_ALLOW_PRIMARY_GIT_WRITE", "LEARN_UK_ALLOW_PRIMARY_WRITE"}
    }
    env["LU_PRODUCT_ROOT"] = str(layout.main)
    if redirect == "git_env":
        env["GIT_DIR"] = str(foreign / ".git")
        env["GIT_WORK_TREE"] = str(foreign)
    else:
        gitfile = layout.dispatch_wt / ".git"
        if redirect == "symlinked_gitfile":
            replacement = layout.outside / "gitfile"
            replacement.write_text(f"gitdir: {foreign}/.git\n")
            gitfile.unlink()
            gitfile.symlink_to(replacement)
        else:
            gitfile.write_text(f"gitdir: {foreign}/.git\n")
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"

    def decision(target: Path) -> subprocess.CompletedProcess[str]:
        if tool == "Write":
            name, args = "Write", {"file_path": str(target), "content": "replacement"}
        else:
            command = f"touch {target}"
            if tool == "glob":
                command = f"touch {target.parent}/trac*.txt"
            elif tool == "nested":
                command = f"cd {foreign}; touch {target}"
            name, args = "Bash", {"command": command}
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": name, "cwd": str(layout.dispatch_wt), "tool_input": args}),
            cwd=layout.dispatch_wt, env=env, capture_output=True, text=True, timeout=60,
        )

    denied = decision(layout.main / "tracked.txt")
    assert denied.returncode == 2, denied.stderr
    assert "BLOCKED" in denied.stderr
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"
    allowed = decision(layout.dispatch_wt / "tracked.txt")
    assert allowed.returncode == 0, allowed.stderr


@pytest.mark.parametrize(
    "product_kind",
    ["primary", "worktree", "alias", "other_primary", "primary_alias", "subdirectory", "snapshot", "other_worktree", "foreign_cwd"],
)
@pytest.mark.parametrize("tool", ["Write", "Bash", "glob", "stdin", "stdin_inplace", "find_list"])
def test_primary_guard_keeps_configured_and_additional_primary_protection(layout: Layout, product_kind: str, tool: str):
    """Root contexts must preserve direct primary denials and configured/cwd parser refusals."""
    other = layout.outside
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.email", "fixture@example.invalid")
    _git(other, "config", "user.name", "Fixture")
    (other / "tracked.txt").write_text("other\n")
    _git(other, "add", "tracked.txt")
    _git(other, "commit", "-qm", "fixture")
    alias = layout.main.parent / "primary-alias"
    alias.symlink_to(layout.main, target_is_directory=True)
    snapshot = layout.main.parent / "snapshot"
    snapshot.mkdir()
    other_worktree = other / ".worktrees/dispatch/codex/product"
    _git(other, "worktree", "add", "-q", "-b", "product", str(other_worktree))
    product = {
        "worktree": layout.dispatch_wt, "alias": alias, "other_primary": other,
        "subdirectory": layout.main / "pkg", "snapshot": snapshot, "other_worktree": other_worktree,
    }.get(product_kind, layout.main)
    cwd = other_worktree if product_kind in {"other_worktree", "foreign_cwd"} else layout.dispatch_wt
    env = {
        key: value for key, value in _clean_env().items()
        if key not in {"LEARN_UK_ALLOW_PRIMARY_GIT_WRITE", "LEARN_UK_ALLOW_PRIMARY_WRITE"}
    }
    env.update(
        LU_PRODUCT_ROOT=str(product),
        LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=str(alias if product_kind == "primary_alias" else layout.main),
    )
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"

    def decision(name: str, args: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": name, "cwd": str(cwd), "tool_input": args}),
            cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
        )

    readonly = decision("Bash", {"command": "ls"})
    assert readonly.returncode == 0, readonly.stderr
    for target, expected in ((layout.main / "tracked.txt", 2), (layout.dispatch_wt / "tracked.txt", 0), (other / "tracked.txt", 2)):
        if tool == "Write":
            name, args = "Write", {"file_path": str(target), "content": "replacement"}
        elif tool in {"Bash", "glob"}:
            word = shlex.quote(str(target)) if tool == "Bash" else f"{shlex.quote(str(target.parent))}/trac*.txt"
            name, args = "Bash", {"command": f"touch {word}"}
        else:
            if tool == "find_list":
                command = f"find -files0-from {shlex.quote(str(target.parent / 'list'))} -delete"
            else:
                writer = "sed -i s/a/b/" if tool == "stdin_inplace" else "rm"
                command = f"printf '%s\\n' {shlex.quote(str(target))} | xargs {writer}"
            name, args = "Bash", {"command": command}
            # Stdin is undecidable for configured/cwd primaries. An unrelated
            # source word alone did not select a primary in the original parser.
            if target.parent == other and product_kind not in {"other_primary", "other_worktree", "foreign_cwd"}:
                expected = 0
            # Unknown find starts include the configured product itself. Its
            # metadata can no longer be removed, even inside an isolated tree.
            if tool == "find_list" and product_kind in {"worktree", "other_worktree"}:
                expected = 2
        result = decision(name, args)
        assert result.returncode == expected, result.stderr
    if product_kind in {"other_primary", "other_worktree"}:
        for primary in (layout.main, other):
            result = decision("Bash", {"command": f"git -C {shlex.quote(str(primary))} reset --hard"})
            assert result.returncode == 2, result.stderr
    elif product_kind == "foreign_cwd":
        result = decision("Bash", {"command": f"git -C {shlex.quote(str(other))} merge --ff-only main"})
        assert result.returncode == 0, result.stderr
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"
    assert (other / "tracked.txt").read_text() == "other\n"


def test_tar_list_mode_option_word_is_not_a_primary_write(layout: Layout):
    """Defect class: tar's dash-less option word (``tar tzf``) read as a file written in the primary.

    ``-t`` lists the archive named by ``-f`` and writes nothing; later words are
    member names. Controls: extracting or creating into the primary is still
    refused, and so is a listing whose ``TAR_OPTIONS`` the command sets or whose
    words Bash can expand into other options.
    """
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    env = {**_clean_env(), "LU_PRODUCT_ROOT": str(layout.main)}

    def decision(command: str) -> int:
        payload = {"tool_name": "Bash", "cwd": str(layout.main), "tool_input": {"command": command}}
        result = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            cwd=str(layout.main),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return result.returncode

    archive = layout.outside / "a.tgz"
    assert decision(f"tar tzf {archive}") == 0
    assert decision(f"tar tvzf {archive} tracked.txt") == 0
    assert decision(f"tar xzf {archive}") == 2
    assert decision(f"tar -xzf {archive} -C {layout.main}") == 2
    assert decision(f"tar czf {layout.main}/out.tgz pkg") == 2
    assert decision(f"TAR_OPTIONS=--checkpoint-action=exec=touch tar tzf {archive}") == 2
    assert decision(f"tar tzf {archive} {{--checkpoint=1,--checkpoint-action=exec=touch}}") == 2
    assert decision(f"tar tzf {archive} *") == 2
    # A first textual segment may execute again or later with a changed environment.
    for command in (
        f"while tar tzf {archive}; do export TAR_OPTIONS=--index-file=out; done",
        f"until tar tzf {archive} && test -e out; do export TAR_OPTIONS=--index-file=out; done",
        f"function f {{ tar tzf {archive}; }}; TAR_OPTIONS=--index-file=out f",
        f"while tar tzf {archive}\ndo source env.sh\ndone",
        f"f() {{ tar tzf {archive}; }}; TAR_OPTIONS=--index-file=out f",
    ):
        assert decision(command) == 2, command


def test_relative_gitignored_path_resolved_against_cwd(layout: Layout):
    """#5404: relative write under a gitignored subdir cwd must be allowed."""
    ignored_dir = layout.main / "local_state" / "nested"
    ignored_dir.mkdir(parents=True, exist_ok=True)
    # ensure local_state is gitignored in fixture (test_gitignored_local_state uses local_state/)
    rel = "nested/out.log"
    decision = wc.evaluate_write(rel, cwd=ignored_dir)
    assert decision.allowed is True, decision
    assert decision.reason == "gitignored_local_state"


@pytest.mark.parametrize("shape", ["agy", "claude"])
@pytest.mark.parametrize("where", ["primary", "worktree", "malformed"])
@pytest.mark.parametrize("tool", ["write_file", "edit_file", "run_command"])
def test_agy_tool_call_shape_cannot_bypass_primary_write_guard(layout: Layout, shape: str, where: str, tool: str):
    """Defect class: a provider payload shape the guard does not parse is silently allowed.

    AGY sends ``toolCall.args`` and reads a denial only as stdout JSON (exit 0); worktree
    writes stay allowed. Claude-shaped payloads keep their exit-2 contract unchanged.
    """
    cwd = layout.main if where != "worktree" else layout.dispatch_wt
    target = str(cwd / "tracked.txt")
    if tool == "run_command":
        args, claude_tool, claude_input = {"CommandLine": f"echo x > {target}"}, "Bash", {"command": f"echo x > {target}"}
    else:
        args = {"Path": target, "Content": "x"} if tool == "write_file" else {"Path": target, "Target": "o", "Replacement": "x"}
        claude_tool, claude_input = ("Write" if tool == "write_file" else "Edit"), {"file_path": target}
    if shape == "agy":
        call = {"id": "t1", "name": tool, "args": "malformed" if where == "malformed" else args}
        payload = {"hookEventName": "PreToolUse", "cwd": str(cwd), "toolCall": call}
    else:
        payload = {"tool_name": claude_tool, "cwd": str(cwd), "tool_input": "malformed" if where == "malformed" else claude_input}
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    result = subprocess.run(
        [sys.executable, str(hook)], input=json.dumps(payload), cwd=cwd,
        env={**_clean_env(), "LU_PRODUCT_ROOT": str(layout.main)},
        capture_output=True, text=True, timeout=60,
    )
    blocked = where != "worktree"
    if shape == "claude":
        assert (result.returncode, result.stdout) == (2 if blocked else 0, ""), result.stderr
    elif blocked:
        assert result.returncode == 0, result.stderr
        decision = json.loads(result.stdout)
        assert decision["decision"] == "deny"
        assert decision["reason"].startswith("BLOCKED by guard-primary-checkout-write")
    else:
        assert (result.returncode, result.stdout) == (0, ""), result.stderr


@pytest.mark.parametrize(
    "command,expected",
    [
        ("trap 'touch tracked.txt' EXIT", 2),
        ("trap -- 'touch tracked.txt' EXIT", 2),
        ("trap 'git reset --hard' EXIT", 2),
        ("trap 'touch $HOME/tracked.txt' EXIT; HOME=<primary>", 2),
        ("trap 'touch ~/tracked.txt' EXIT; HOME=<primary>", 2),
        ("coproc git reset --hard", 2),
        ("coproc cd <dispatch>; touch tracked.txt", 2),
        ("trap 'tar xzf archive.tgz' EXIT", 2),
        ("coproc touch tracked.txt", 2),
        ("coproc tar xzf archive.tgz", 2),
        ("coproc { touch tracked.txt; }", 2),
        ("coproc WORKER { touch tracked.txt; }", 2),
        ("coproc WORKER { tar xzf archive.tgz; }", 2),
        ("coproc WORKER while touch tracked.txt; do :; done", 2),
        ("coproc WORKER if tar xzf archive.tgz; then :; fi", 2),
        ("coproc ( touch tracked.txt )", 2),
        ("bash -c \"trap 'touch tracked.txt' EXIT\"", 2),
        ("eval \"coproc touch tracked.txt\"", 2),
        ("coproc { coproc touch tracked.txt; }", 2),
        ("cd <dispatch>; export UV_WORKING_DIR=<primary>; coproc uv run touch tracked.txt", 2),
        (
            "cd <dispatch>; trap 'cd <dispatch>; uv run touch tracked.txt' EXIT; export UV_WORKING_DIR=<primary>",
            2,
        ),
        ("env UV_WORKING_DIR=<dispatch> bash -c 'coproc uv run touch tracked.txt'", 0),
        ("coproc printf done", 0),
        ("trap -p", 0),
        ("trap '' EXIT", 0),
    ],
)
def test_trap_coproc_primary_write_bypass_refused(layout: Layout, command: str, expected: int):
    """R34: deferred trap actions and asynchronous coprocesses must expose writers."""
    repo = layout.main
    command = command.replace("<dispatch>", str(repo / ".worktrees/dispatch/claude/task-1")).replace(
        "<primary>", str(repo)
    )
    payload = {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}}
    hook_path = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    env = {
        key: value for key, value in _clean_env().items()
        if key not in {"LEARN_UK_ALLOW_PRIMARY_GIT_WRITE", "LEARN_UK_ALLOW_PRIMARY_WRITE"}
    }
    env["LU_PRODUCT_ROOT"] = str(repo)
    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    "timing,nested",
    [("inherited", True), ("inherited", False), ("before", True), ("after", True)],
)
def test_deferred_trap_envfile_cannot_clear_uv_context(layout: Layout, timing: str, nested: bool):
    """Deferred dotenv bindings must survive unsetting only UV_WORKING_DIR."""
    repo = layout.main
    dispatch = layout.dispatch_wt
    env_file = dispatch / "uv.env"
    env_file.write_text(f"UV_WORKING_DIR={repo}\n")
    writer = 'sh -c "uv run touch tracked.txt"' if nested else "touch tracked.txt"
    action = f"cd {dispatch}; env -u UV_WORKING_DIR uv run {writer}"
    command = f"cd {dispatch}; trap '{action}' EXIT"
    if timing == "before":
        command = f"export UV_ENV_FILE={env_file}; {command}"
    elif timing == "after":
        command += f"; export UV_ENV_FILE={env_file}"
    env = {
        key: value for key, value in _clean_env().items()
        if key not in {
            "LEARN_UK_ALLOW_PRIMARY_GIT_WRITE", "LEARN_UK_ALLOW_PRIMARY_WRITE", "UV_WORKING_DIR", "UV_ENV_FILE"
        }
    }
    env["LU_PRODUCT_ROOT"] = str(repo)
    if timing == "inherited":
        env["UV_ENV_FILE"] = str(env_file)
    payload = {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}}
    hook_path = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("directory_link", [False, True])
def test_primary_guard_blocks_glob_matches_through_symlinks(layout: Layout, directory_link: bool):
    """A matched symlink must not hide a protected target behind an isolated prefix."""
    link = layout.dispatch_wt / "linked-target"
    link.symlink_to(layout.main if directory_link else layout.main / "tracked.txt", target_is_directory=directory_link)
    (layout.dispatch_wt / ".secret-link").symlink_to(layout.main / "tracked.txt")
    (layout.dispatch_wt / "}a{file").symlink_to(layout.main / "tracked.txt")
    (layout.dispatch_wt / "}a").symlink_to(layout.main, target_is_directory=True)
    operand = "linked-*/tracked.txt" if directory_link else "linked-*"
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    env = {**_clean_env(), "LU_PRODUCT_ROOT": str(layout.main), "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(layout.main)}
    for word, expected in ((operand, 2), ("trac*.txt", 0)):
        result = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": "Bash", "cwd": str(layout.dispatch_wt), "tool_input": {"command": f"touch {word}"}}),
            cwd=layout.dispatch_wt, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == expected, result.stderr
    for command in (
        f"shopt -s nocaseglob; touch {operand.upper()}",
        f"shopt -s dotglob; touch {operand}",
        f"GLOBIGNORE=unused; touch {operand}",
        "shopt -s extglob; touch @(linked-target)",
        "touch linked-[[:alpha:]]*",
        "touch linked-[a[:alpha:]]*",
        "touch linked-[^x]*",
        "touch linked-{a..z}",
        "touch linked-{target/,other/}tracked.txt",
        "touch }{a,/b}{file",
        "touch }{a/,b/}{tracked.txt",
        "bash -O dotglob -c 'touch *secret*'",
        "env BASHOPTS=dotglob bash -c 'touch *secret*'",
        "bash -O extglob -c 'touch @(.secret-link)'",
    ):
        result = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": "Bash", "cwd": str(layout.dispatch_wt), "tool_input": {"command": command}}),
            cwd=layout.dispatch_wt, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 2, (command, result.stderr)
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"


@pytest.mark.parametrize("redirect", [False, True])
def test_primary_guard_refuses_product_metadata_demotion(layout: Layout, redirect: bool):
    """Corrupt or redirected product gitfiles cannot silently remove additive protection."""
    other = layout.outside / "other"
    _git(layout.outside, "init", "-q", "-b", "main", str(other))
    _git(other, "config", "user.email", "fixture@example.invalid")
    _git(other, "config", "user.name", "Fixture")
    (other / "tracked.txt").write_text("other\n")
    _git(other, "add", "tracked.txt")
    _git(other, "commit", "-qm", "fixture")
    product = layout.outside / "product"
    _git(other, "worktree", "add", "-q", "-b", "product", str(product))
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    env = {**_clean_env(), "LU_PRODUCT_ROOT": str(product), "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(layout.main)}

    def decision(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"tool_name": "Bash", "cwd": str(layout.dispatch_wt), "tool_input": {"command": command}}),
            cwd=layout.dispatch_wt, env=env, capture_output=True, text=True, timeout=60,
        )

    command = f"printf '%s\\n' {shlex.quote(str(other / 'tracked.txt'))} | xargs rm"
    assert decision("ls").returncode == 0
    assert decision(command).returncode == 2
    for mutation in (f"rm {shlex.quote(str(product / '.git'))}", f"rm -rf {shlex.quote(str(product))}"):
        result = decision(mutation)
        assert result.returncode == 2, result.stderr
        assert "protected_product_metadata" in result.stderr
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"tool_name": "Write", "cwd": str(layout.dispatch_wt), "tool_input": {"file_path": str(product / '.git'), "content": "invalid"}}),
        cwd=layout.dispatch_wt, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stderr
    assert "protected_product_metadata" in result.stderr
    nested = product / "nested"
    nested.mkdir()
    env["LU_PRODUCT_ROOT"] = str(nested)
    assert decision(command).returncode == 2
    _git(nested, "init", "-q", "-b", "main")
    assert decision("ls").returncode == 0
    assert decision(command).returncode == 2  # The nearer repository only adds authority.
    assert decision(f"rm {shlex.quote(str(product / '.git'))}").returncode == 2
    env["LU_PRODUCT_ROOT"] = str(product)
    (product / ".git").write_text(f"gitdir: {layout.main / '.git'}\n" if redirect else "invalid\n")
    result = decision(command)
    assert result.returncode == 2, result.stderr
    assert "product_root_unavailable" in result.stderr
    assert (other / "tracked.txt").read_text() == "other\n"
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"


def _bash_decision(layout: Layout, command: str) -> subprocess.CompletedProcess[str]:
    hook = Path(__file__).resolve().parents[2] / "agents_extensions/shared/hooks/guard-primary-checkout-write.py"
    env = {**_clean_env(), "LU_PRODUCT_ROOT": str(layout.main), "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(layout.main)}
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"tool_name": "Bash", "cwd": str(layout.dispatch_wt), "tool_input": {"command": command}}),
        cwd=layout.dispatch_wt, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize(
    "command",
    [
        "printf x | xargs bash -O dotglob -c 'touch ./*secret*'",
        "find . -maxdepth 0 -exec env BASHOPTS=dotglob bash -c 'touch ./*secret*' \\;",
    ],
)
def test_template_launched_shell_glob_options_cannot_hide_primary_link(layout: Layout, command: str):
    """Defect class: a nested shell's own glob options widen a pattern beyond the guard's matcher."""
    (layout.dispatch_wt / ".secret-link").symlink_to(layout.main / "tracked.txt")
    result = _bash_decision(layout, command)
    assert result.returncode == 2, result.stderr
    assert "undecidable_glob_write_target" in result.stderr
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"


@pytest.mark.parametrize(
    ("entry", "command"),
    [("{a,b}x", "touch ./'{a,b}'*"), ("linked--", "touch linked-[a\\-z]")],
)
def test_quoted_or_escaped_pattern_cannot_hide_primary_link(layout: Layout, entry: str, command: str):
    """Defect class: quote or escape removal changes what a pattern selects before the guard sees it."""
    (layout.dispatch_wt / entry).symlink_to(layout.main / "tracked.txt")
    result = _bash_decision(layout, command)
    assert result.returncode == 2, result.stderr
    assert "undecidable_glob_write_target" in result.stderr
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"


@pytest.mark.parametrize(
    "command",
    [
        r"find . -maxdepth 0 -exec xargs bash -c ./run* \;",
        r"printf x | xargs xargs xargs find . -exec xargs sh -c ./run* \;",
    ],
)
def test_composed_template_shell_cannot_run_pattern_code(layout: Layout, command: str):
    """Defect class: a shell reached through composed command templates runs pattern-expanded code unrefused."""
    (layout.dispatch_wt / "run-me").write_text("touch ../../../../tracked.txt\n")
    result = _bash_decision(layout, command)
    assert result.returncode == 2, result.stderr
    assert "undecidable_glob_write_target" in result.stderr
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"


@pytest.mark.parametrize(
    "command",
    ["printf x | xargs setsid sh -c ./run*", "watch -n1 bash -c ./run*"],
)
def test_other_launcher_shell_cannot_run_pattern_code(layout: Layout, command: str):
    """Defect class: a launcher the template parser never modelled hands pattern-expanded code to a shell."""
    (layout.dispatch_wt / "run-me").write_text("touch ../../../../tracked.txt\n")
    result = _bash_decision(layout, command)
    assert result.returncode == 2, result.stderr
    assert "undecidable_glob_write_target" in result.stderr
    assert (layout.main / "tracked.txt").read_text() == "tracked\n"
