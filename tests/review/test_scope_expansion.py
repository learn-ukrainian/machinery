"""The expansion breaker counts author changes across base-branch merges."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace

import pytest

from lu_machinery.review import closeout_cli
from lu_machinery.review.target_resolution import resolve_branch_target


@pytest.mark.parametrize("mode", ["branch", "pr"])
@pytest.mark.parametrize("growth", ["files", "loc"])
def test_main_merge_excludes_unrelated_growth_but_author_growth_trips(
    tmp_path, capsys, mode, growth
):
    """A main merge must not inflate either files or LOC beyond the frozen scope."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit_file(name, content):
        (repo / name).write_text(content)
        git("add", name)
        git("commit", "-m", name)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    commit_file("seed.txt", "seed\n")
    git("checkout", "-b", "feature")
    commit_file("author.txt", "author\n")
    target = replace(
        resolve_branch_target(repo, "feature", "main"),
        mode=mode,
        base_ref_name="main" if mode == "pr" else None,
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "target": asdict(target),
                "target_args": {"repo_root": str(repo), "mode": mode, "base": "main"},
            }
        )
    )
    prefix = ["--state-file", str(state_file)]
    assert (
        closeout_cli.main(
            prefix
            + [
                "freeze",
                "--issue",
                "Refs #100",
                "--intended-behavior",
                "author change",
                "--non-goals",
                "unrelated main changes",
                "--owner-boundary",
                "author files",
            ]
        )
        == 0
    )
    frozen_receipt = state_file.read_bytes()
    capsys.readouterr()

    git("checkout", "main")
    for index in range(3):
        commit_file(f"unrelated{index}.txt", "main\n" * 10)
    # PR checks use the remote-tracking base, even if local main is stale.
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "feature")
    git("merge", "--no-ff", "main", "-m", "merge main")
    if mode == "pr":
        git("branch", "-f", "main", target.base_sha)
    assert (
        closeout_cli.main(prefix + ["check-expansion", "--current-head", "HEAD"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == {"triggered": False, "reason": None}
    assert state_file.read_bytes() == frozen_receipt

    if growth == "files":
        commit_file("growth1.txt", "growth\n")
        commit_file("growth2.txt", "growth\n")
        reason = "review-triggered files (3) exceed 2x frozen baseline (1)"
    else:
        commit_file("author.txt", "author\n" * 3)
        reason = "review-triggered non-test LOC (3) exceeds 2x frozen baseline (1)"
    assert (
        closeout_cli.main(prefix + ["check-expansion", "--current-head", "HEAD"]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "triggered": True,
        "reason": reason,
    }
    assert state_file.read_bytes() == frozen_receipt
