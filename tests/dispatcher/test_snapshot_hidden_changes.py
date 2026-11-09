"""Exit snapshot and removal check of tracked changes that git status hides (Refs #100).

Defect class: ``snapshot_delivery`` read only ``git status --porcelain``, which trusts the assume-unchanged and
skip-worktree index flags and the submodule ignore settings, so a worker that exited with such uncommitted
tracked work was reported clean (``worktree_dirty_on_exit=False``) and passed the delivery rescue.
"""

import subprocess
from types import SimpleNamespace

import pytest

from lu_machinery.dispatcher import git

# Converted on checkout (UTF-8 stored, UTF-16LE in the tree); the name has a leading blank and a carriage return.
ENCODED = " encoded\r.txt"


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
    dirty = git.snapshot_delivery(SimpleNamespace(worktree=work, mode="read-only"), record)["worktree_dirty_on_exit"]
    changed, untracked = git._tree_changes(work)
    assert bool(changed) is dirty
    assert untracked == []
    return dirty


@pytest.fixture
def work(tmp_path):
    work = _repo(tmp_path / "work")
    (work / "tracked.txt").write_text("committed\n")
    (work / ".gitattributes").write_text("*encoded* working-tree-encoding=UTF-16LE\n")
    (work / ENCODED).write_bytes("кодоване\n".encode("utf-16-le"))
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
    """A clean tree stays clean, also as a dispatch tree: sparse-excluded entries and unedited flagged ones,
    including one converted on checkout whose name a cat-file batch request or a text read would alter."""
    assert _dirty(work) is False
    (work / "tracked.txt").write_text("plain edit\n")
    assert _dirty(work) is True
    (work / "tracked.txt").write_text("committed\n")
    _git(work, "sparse-checkout", "set", "--no-cone", "--", "/*", "!/excluded/")
    assert not (work / "excluded").exists()
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ENCODED)
    assert _dirty(work) is False


def test_skip_worktree_deletion_reads_dirty(work):
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").unlink()
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    # An absent skip-worktree entry still counts when the sparse rules include it.
    _git(work, "sparse-checkout", "set", "--no-cone", "--", "/*", "!/excluded/")
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").unlink(missing_ok=True)
    assert _dirty(work) is True


def test_skip_worktree_edit_reads_dirty(work):
    _git(work, "update-index", "--skip-worktree", "tracked.txt")
    (work / "tracked.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (work / "tracked.txt").write_text("committed\n")
    assert _dirty(work) is False


def test_assume_unchanged_edit_reads_dirty(work):
    """Also an edit that leaves the stored blob's own bytes in a file converted on checkout."""
    _git(work, "update-index", "--assume-unchanged", "tracked.txt", ENCODED)
    (work / "tracked.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (work / "tracked.txt").write_text("committed\n")
    assert _dirty(work) is False
    (work / ENCODED).write_bytes(subprocess.run(["git", "-C", str(work), "show", f"HEAD:{ENCODED}"],
                                                check=True, capture_output=True).stdout)
    assert _dirty(work) is True


def test_submodule_edit_with_ignore_dirty_reads_dirty(tmp_path, work):
    """Also an edit that the submodule's own assume-unchanged flag hides from its status, with or without the
    submodule's gitfile."""
    sub = _submodule(tmp_path, work, "dirty")
    (sub / "inner.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True
    (sub / "inner.txt").write_text("inner\n")
    _git(sub, "update-index", "--assume-unchanged", "inner.txt")
    assert _dirty(work) is False
    (sub / "inner.txt").write_text("hidden edit\n")
    assert _git(work, "status", "--porcelain", "--ignore-submodules=none") == ""
    assert _dirty(work) is True
    # Without its gitfile the populated submodule is invisible to the superproject's status.
    (sub / ".git").unlink()
    assert _git(work, "status", "--porcelain", "--ignore-submodules=none") == ""
    assert _dirty(work) is True


def test_new_submodule_commit_with_ignore_all_reads_dirty(tmp_path, work):
    sub = _submodule(tmp_path, work, "all")
    (sub / "inner.txt").write_text("new commit\n")
    _git(sub, "commit", "-q", "-am", "inner change")
    assert _git(work, "status", "--porcelain") == ""
    assert _dirty(work) is True

