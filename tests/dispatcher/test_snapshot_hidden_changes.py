"""Exit snapshot of tracked changes that git status hides (machinery #122).

Defect class: ``snapshot_delivery`` read only ``git status --porcelain``, which trusts the assume-unchanged and
skip-worktree index flags and the submodule ignore settings, so a worker that exited with such uncommitted
tracked work was reported clean (``worktree_dirty_on_exit=False``) and passed the delivery rescue.
"""

import subprocess
from types import SimpleNamespace

import pytest

from lu_machinery.dispatcher import git


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(path):
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(path, "config", key, value)
    return path


def _dirty(work):
    record = {"worktree_base_sha": _git(work, "rev-parse", "HEAD")}
    return git.snapshot_delivery(SimpleNamespace(worktree=work, mode="read-only"), record)["worktree_dirty_on_exit"]


@pytest.fixture
def work(tmp_path):
    work = _repo(tmp_path / "work")
    (work / "tracked.txt").write_text("committed\n")
    (work / "excluded").mkdir()
    (work / "excluded" / "data.txt").write_text("outside the sparse rules\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    return work


def _submodule(tmp_path, work, ignore):
    sub = _repo(tmp_path / "sub")
    (sub / "inner.txt").write_text("inner\n")
    _git(sub, "add", ".")
    _git(sub, "commit", "-q", "-m", "inner")
    _git(work, "submodule", "add", "-q", str(sub), "sub")
    _git(work, "commit", "-q", "-m", "submodule")
    _git(work, "config", "submodule.sub.ignore", ignore)
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
        _git(work / "sub", "config", key, value)
    assert _dirty(work) is False
    return work / "sub"


def test_clean_tree_reads_clean(work):
    """A clean tree stays clean, also as a dispatch tree: sparse-excluded entries and an unedited flagged one."""
    assert _dirty(work) is False
    _git(work, "sparse-checkout", "set", "--no-cone", "--", "/*", "!/excluded/")
    assert not (work / "excluded").exists()
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    assert _dirty(work) is False


def test_skip_worktree_deletion_reads_dirty(work):
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").unlink()
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True


def test_skip_worktree_edit_reads_dirty(work):
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True


def test_assume_unchanged_edit_reads_dirty(work):
    _git(work, "update-index", "--assume-unchanged", "tracked.txt")
    (work / "tracked.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True


def test_submodule_edit_with_ignore_dirty_reads_dirty(tmp_path, work):
    sub = _submodule(tmp_path, work, "dirty")
    (sub / "inner.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True


def test_new_submodule_commit_with_ignore_all_reads_dirty(tmp_path, work):
    sub = _submodule(tmp_path, work, "all")
    (sub / "inner.txt").write_text("new commit\n")
    _git(sub, "commit", "-q", "-am", "inner change")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True

