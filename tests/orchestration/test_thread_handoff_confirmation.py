"""Confirmation coverage ported from learn-ukrainian's thread_handoff tests.

The former proof fixtures are removed: confirmation uses the exact binding and
lease, while checkout, title, upload and predecessor safeguards remain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration import task_identity
from lu_machinery.orchestration import thread_handoff as th


def sample_snapshot(tmp_path: Path) -> dict:
    return {
        "generated_at": "2026-05-30T08:00:00Z",
        "git": {
            "repo_root": str(tmp_path),
            "branch": "main",
            "head": "abc123def0",
            "full_head": "abc123def0456789",
            "ahead_behind": {"ahead": 1, "behind": 0, "upstream": "origin/main"},
            "last_commits": [
                {"sha": "abc123def0", "subject": "docs(session): handoff"},
                {"sha": "1111111111", "subject": "feat(api): monitor"},
            ],
            "modified_files": [],
        },
        "monitor": {
            "base_url": "http://127.0.0.1:8765",
            "orient": {"git": {"head": "abc123def0"}},
            "active_delegates": {"total": 0, "tasks": []},
            "completed_delegates": {
                "total": 1,
                "tasks": [{"task_id": "codex/example", "agent": "codex", "status": "done", "duration_s": 42}],
            },
            "worktrees": {"count": 2, "worktrees": []},
        },
        "github": {
            "open_prs": [
                {
                    "number": 12,
                    "title": "feat: example",
                    "headRefName": "codex/example",
                    "mergeStateStatus": "CLEAN",
                    "isDraft": False,
                }
            ],
            "open_issues": [{"number": 34, "title": "Need handoff", "updatedAt": "2026-05-30T07:00:00Z"}],
        },
    }


@pytest.fixture(autouse=True)
def clean_invoking_checkout(monkeypatch):
    """Unit CLI fixtures run outside Git; model the clean bound checkout."""
    monkeypatch.setattr(th, "gather_git_state", lambda root: sample_snapshot(root)["git"])
    for variable in (
        "LEARN_UKRAINIAN_CLAUDEX_RUN_ID",
        "LEARN_UKRAINIAN_CLAUDEX_LAUNCH_GENERATION",
        "LEARN_UKRAINIAN_SESSION_ID",
    ):
        monkeypatch.delenv(variable, raising=False)


def prepared(*, agent: str = "orchestrator", thread_id: str = "old-thread") -> dict:
    state = th.prepare_state(
        {"schema_version": th.SCHEMA_VERSION},
        agent=agent,
        now=datetime(2026, 5, 30, 8, 0, tzinfo=UTC),
        active_thread_id=thread_id,
        active_automation_id="old-auto",
        context_percent=86.0,
        force_new_replacement=False,
        harness="codex-app",
    )
    state["replacement"]["source_checkout"] = {
        "full_head": sample_snapshot(Path("."))["git"]["full_head"],
        "clean": True,
    }
    return state


def bind_native_replacement(state: dict, thread_id: str) -> None:
    replacement = state["replacement"]
    replacement["native_lifecycle"]["replacement_thread_id"] = thread_id
    replacement["native_lifecycle"]["status"] = "title_reconciled"
    identity, transition = task_identity.bind_replacement(
        replacement["identity"],
        replacement["title_transition"],
        replacement_task_id=thread_id,
        evidence="test exact binding",
        now="2026-05-30T08:00:30Z",
    )
    identity, transition = task_identity.record_title_acknowledgement(
        identity,
        transition,
        replacement_task_id=thread_id,
        succeeded=True,
        evidence="test native title acknowledgement",
        error="",
        now="2026-05-30T08:00:40Z",
    )
    identity, transition = task_identity.record_title_readback(
        identity,
        transition,
        replacement_task_id=thread_id,
        observed_title=identity["visible_title"],
        succeeded=True,
        evidence="test exact title readback",
        error="",
        now="2026-05-30T08:00:50Z",
    )
    replacement["identity"] = identity
    replacement["title_transition"] = transition


def bind_native_lease(state_path: Path, thread_id: str) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    bind_native_replacement(state, thread_id)
    th.write_json_atomic(state_path, state)


def test_prepare_state_requires_confirmation_before_cleanup(tmp_path: Path):
    now = datetime(2026, 5, 30, 8, 0, tzinfo=UTC)
    state = prepared()
    assert state["active"]["thread_id"] == "old-thread"
    assert state["active"]["automation_id"] == "old-auto"
    assert state["replacement"]["status"] == "pending_start"
    assert state["cleanup"]["old_automation_ready_to_delete"] is False
    resumed = resume_bound(state)
    confirmed = th.confirm_started(
        resumed,
        new_thread_id="new-thread",
        new_automation_id=None,
        confirmed_by="tester",
        now=now + timedelta(minutes=2),
    )
    assert confirmed["replacement"]["status"] == "started"
    assert confirmed["replacement"]["thread_id"] == "new-thread"
    assert confirmed["cleanup"]["old_automation_ready_to_delete"] is True


def test_confirm_started_requires_exact_predecessor_identity(tmp_path: Path):
    state = prepared()
    resumed = resume_bound(state)
    resumed.pop("active")
    with pytest.raises(ValueError, match="no exact predecessor"):
        th.confirm_started(
            resumed,
            new_thread_id="new-thread",
            new_automation_id=None,
            confirmed_by="tester",
            now=datetime(2026, 5, 30, 8, 2, tzinfo=UTC),
        )


def test_confirm_started_rejects_missing_pending_replacement():
    with pytest.raises(ValueError, match="run prepare first"):
        th.confirm_started(
            {},
            new_thread_id="new-thread",
            new_automation_id=None,
            confirmed_by="tester",
            now=datetime(2026, 5, 30, 8, 0, tzinfo=UTC),
        )


def test_resume_and_confirm_independently_recheck_checkout_continuity(tmp_path: Path, capsys, monkeypatch):
    clean = sample_snapshot(tmp_path)["git"]
    monkeypatch.setattr(th, "gather_snapshot", lambda root, url: sample_snapshot(root))
    assert th.main(["--repo-root", str(tmp_path), "prepare", "--active-thread-id", "old-thread"]) == 0
    packet = json.loads(capsys.readouterr().out)
    state_path = tmp_path / packet["state_file"]
    bind_native_lease(state_path, "new-thread")
    monkeypatch.setattr(th, "gather_git_state", lambda root: {**clean, "full_head": "wrong-head"})
    resume_command = [
        "--repo-root",
        str(tmp_path),
        "resume",
        "--lineage-id",
        packet["lineage_id"],
        "--rollover-id",
        packet["rollover_id"],
        "--replacement-thread-id",
        "new-thread",
    ]
    assert th.main(resume_command) == 2
    assert "does not match prepared HEAD" in json.loads(capsys.readouterr().out)["error"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["replacement"]["status"] == "pending_start"
    monkeypatch.setattr(th, "gather_git_state", lambda root: clean)
    assert th.main(resume_command) == 0
    # Retired non-quiz CLI partitions: the resume payload keeps visible_title,
    # a second resume leaves the state bytes identical, and confirm-started
    # reports identity.lifecycle_state confirmed.
    resumed_payload = json.loads(capsys.readouterr().out)
    assert resumed_payload["identity"]["visible_title"] == packet["identity"]["visible_title"]
    resumed_bytes = state_path.read_bytes()
    assert th.main(resume_command) == 0
    capsys.readouterr()
    assert state_path.read_bytes() == resumed_bytes
    monkeypatch.setattr(
        th, "gather_git_state", lambda root: {**clean, "modified_files": [{"status": "M", "path": "tracked.txt"}]}
    )
    confirm_command = [
        "--repo-root",
        str(tmp_path),
        "confirm-started",
        "--lineage-id",
        packet["lineage_id"],
        "--rollover-id",
        packet["rollover_id"],
        "--new-thread-id",
        "new-thread",
    ]
    assert th.main(confirm_command) == 2
    assert "invoking checkout must be clean" in json.loads(capsys.readouterr().out)["error"]
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["replacement"]["status"] == "resumed"
    assert final_state["cleanup"]["old_automation_ready_to_delete"] is False
    monkeypatch.setattr(th, "gather_git_state", lambda root: clean)
    assert th.main(confirm_command) == 0
    confirmed_payload = json.loads(capsys.readouterr().out)
    assert confirmed_payload["identity"]["lifecycle_state"] == "confirmed"


def test_confirm_started_is_scoped_to_selected_agent(tmp_path: Path, capsys):
    orchestrator_state = prepared(thread_id="old-orchestrator")
    claude_state = prepared(agent="claude", thread_id="old-claude")
    orchestrator_path = tmp_path / th.default_state_path("orchestrator", orchestrator_state["lineage_id"])
    claude_path = tmp_path / th.default_state_path("claude", claude_state["lineage_id"])
    th.write_json_atomic(orchestrator_path, orchestrator_state)
    bind_native_replacement(claude_state, "new-claude")
    resumed = th.resume_state(
        claude_state,
        rollover_id=claude_state["replacement"]["rollover_id"],
        replacement_thread_id="new-claude",
        now=datetime(2026, 5, 30, 8, 1, tzinfo=UTC),
    )
    th.write_json_atomic(claude_path, resumed)
    capsys.readouterr()
    rc = th.main(
        [
            "--repo-root",
            str(tmp_path),
            "confirm-started",
            "--agent",
            "claude",
            "--lineage-id",
            resumed["lineage_id"],
            "--rollover-id",
            resumed["replacement"]["rollover_id"],
            "--new-thread-id",
            "new-claude",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["agent"] == "claude"
    assert payload["state_file"] == claude_path.relative_to(tmp_path).as_posix()
    confirmed_claude = json.loads(claude_path.read_text(encoding="utf-8"))
    untouched_orchestrator = json.loads(orchestrator_path.read_text(encoding="utf-8"))
    assert confirmed_claude["replacement"]["thread_id"] == "new-claude"
    assert confirmed_claude["cleanup"]["old_automation_ready_to_delete"] is True
    assert untouched_orchestrator["cleanup"]["old_automation_ready_to_delete"] is False


def test_confirmation_refuses_unresumed_and_identity_mismatched_rollovers(tmp_path: Path):
    state = prepared()
    replacement = state["replacement"]
    with pytest.raises(ValueError, match="must be resumed"):
        th.confirm_started(
            state,
            new_thread_id="new-thread",
            new_automation_id=None,
            confirmed_by="tester",
            now=datetime(2026, 5, 30, 8, 2, tzinfo=UTC),
        )
    bind_native_replacement(state, "bound-thread")
    resumed = th.resume_state(
        state,
        rollover_id=replacement["rollover_id"],
        replacement_thread_id="bound-thread",
        now=datetime(2026, 5, 30, 8, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="does not match the thread"):
        th.confirm_started(
            resumed,
            new_thread_id="new-thread",
            new_automation_id=None,
            confirmed_by="tester",
            now=datetime(2026, 5, 30, 8, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize("command", ["resume", "bootstrap-replacement"])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_successor_refuses_missing_durable_handoff(tmp_path, capsys, monkeypatch, command, damage):
    monkeypatch.setattr(th, "gather_snapshot", lambda root, url: sample_snapshot(root))
    assert (
        th.main(
            [
                "--repo-root",
                str(tmp_path),
                "prepare",
                "--agent",
                "codex",
                "--harness",
                "headless",
                "--active-thread-id",
                "old",
            ]
        )
        == 0
    )
    packet = json.loads(capsys.readouterr().out)
    state_path = tmp_path / packet["state_file"]
    if command == "resume":
        assert (
            th.main(
                [
                    "--repo-root",
                    str(tmp_path),
                    "bind-replacement",
                    "--agent",
                    "codex",
                    "--lineage-id",
                    packet["lineage_id"],
                    "--rollover-id",
                    packet["rollover_id"],
                    "--replacement-task-id",
                    "new",
                    "--evidence",
                    "synthetic binding",
                ]
            )
            == 0
        )
        capsys.readouterr()
    handoff = tmp_path / packet["handoff_file"]
    if damage == "missing":
        handoff.rename(handoff.with_suffix(".unavailable"))
    else:
        handoff.write_text("")
    before = state_path.read_bytes()
    args = [
        "--repo-root",
        str(tmp_path),
        command,
        "--agent",
        "codex",
        "--lineage-id",
        packet["lineage_id"],
        "--rollover-id",
        packet["rollover_id"],
        "--replacement-thread-id",
        "new",
    ]
    if command == "bootstrap-replacement":
        args += ["--evidence", "synthetic binding"]
    assert th.main(args) == 2
    assert "durable handoff" in capsys.readouterr().out
    assert state_path.read_bytes() == before


def resume_bound(state: dict, thread_id: str = "new-thread") -> dict:
    bind_native_replacement(state, thread_id)
    return th.resume_state(
        state,
        rollover_id=state["replacement"]["rollover_id"],
        replacement_thread_id=thread_id,
        now=datetime(2026, 5, 30, 8, 1, tzinfo=UTC),
    )


def headless_packet(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(th, "gather_snapshot", lambda root, url: sample_snapshot(root))
    assert (
        th.main(
            [
                "--repo-root",
                str(tmp_path),
                "prepare",
                "--agent",
                "codex",
                "--harness",
                "headless",
                "--active-thread-id",
                "old",
            ]
        )
        == 0
    )
    packet = json.loads(capsys.readouterr().out)
    bootstrap = [
        "--repo-root",
        str(tmp_path),
        "bootstrap-replacement",
        "--agent",
        "codex",
        "--lineage-id",
        packet["lineage_id"],
        "--rollover-id",
        packet["rollover_id"],
        "--replacement-thread-id",
        "new-thread",
        "--evidence",
        "test exact binding",
    ]
    assert th.main(bootstrap) == 0
    capsys.readouterr()
    return packet, bootstrap


def test_bootstrap_replacement_without_quiz_template_is_idempotent(tmp_path, capsys, monkeypatch):
    packet, bootstrap = headless_packet(tmp_path, capsys, monkeypatch)
    state_path = tmp_path / packet["state_file"]
    before = state_path.read_bytes()
    assert th.main(bootstrap) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "bootstrap_ready"
    assert state_path.read_bytes() == before
    assert not list(tmp_path.rglob("semantic-snapshot*"))


def test_bootstrap_replacement_wrong_id_fails_closed(tmp_path, capsys, monkeypatch):
    packet, bootstrap = headless_packet(tmp_path, capsys, monkeypatch)
    state_path = tmp_path / packet["state_file"]
    before = state_path.read_bytes()
    assert th.main([*bootstrap[:-3], "other-thread", "--evidence", "test exact binding"]) == 2
    assert "replacement task ID does not match the exact persisted binding" in capsys.readouterr().out
    assert state_path.read_bytes() == before


def test_confirm_replacement_without_quiz_reruns_idempotently(tmp_path, capsys, monkeypatch):
    packet, _bootstrap = headless_packet(tmp_path, capsys, monkeypatch)
    state_path = tmp_path / packet["state_file"]
    confirm = [
        "--repo-root",
        str(tmp_path),
        "confirm-replacement",
        "--agent",
        "codex",
        "--lineage-id",
        packet["lineage_id"],
        "--rollover-id",
        packet["rollover_id"],
    ]
    assert th.main(confirm) == 0
    capsys.readouterr()
    confirmed = json.loads(state_path.read_text())
    assert confirmed["replacement"]["status"] == "started"
    assert confirmed["replacement"]["thread_id"] == "new-thread"
    assert confirmed["cleanup"]["old_automation_ready_to_delete"] is True
    assert "strict_verdict" not in confirmed["replacement"]
    assert "canary_proof" not in confirmed["replacement"]
    before = state_path.read_bytes()
    assert th.main(confirm) == 0
    capsys.readouterr()
    assert state_path.read_bytes() == before
    assert th.main([*confirm, "--replacement-thread-id", "wrong-thread"]) == 2
    assert "does not match the thread" in capsys.readouterr().out
    assert state_path.read_bytes() == before


def test_confirm_replacement_stream_uploads_with_parser_monitor_base_url(tmp_path, capsys, monkeypatch):
    """Port of GH #8643: confirmation forwards the configured upload endpoint."""
    packet, _bootstrap = headless_packet(tmp_path, capsys, monkeypatch)
    monkeypatch.setenv("SESSION_STREAM_ID", "epic:4700")
    monkeypatch.setenv("SESSION_STREAM_SESSION_ID", "sess-1")
    monkeypatch.setenv("SESSION_STREAM_LEASE_ID", "lease-1")
    monkeypatch.setenv("SESSION_STREAM_GENERATION", "1")
    monkeypatch.setenv("SESSION_STREAM_FENCING_TOKEN", "1")
    monkeypatch.setenv("SESSION_STREAM_PROCESS_ID", "1234")
    seen_base_urls = []

    class RecordingBundleClient:
        def __init__(self, base_url, *, timeout_s=3.0):
            self.base_url = base_url

        def post(self, path, *, json_body=None, idempotency_key=None, headers=None):
            seen_base_urls.append(self.base_url)
            return 200, json.dumps({"upload_seq": 9}), {}

    monkeypatch.setattr(th, "_bundle_client", RecordingBundleClient)
    assert (
        th.main(
            [
                "--repo-root",
                str(tmp_path),
                "--monitor-base-url",
                "http://127.0.0.1:8767",
                "confirm-replacement",
                "--agent",
                "codex",
                "--lineage-id",
                packet["lineage_id"],
                "--rollover-id",
                packet["rollover_id"],
                "--stream",
                "epic:4700",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle_upload"]["status"] == "uploaded"
    assert payload["bundle_upload"]["upload_seq"] == 9
    assert seen_base_urls == ["http://127.0.0.1:8767"]


_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([str(machinery_path("orchestration/thread_handoff.py"))], id="thread_handoff"),
        pytest.param([str(machinery_path("orchestration/rollover_registry_cli.py"))], id="rollover_registry_cli"),
        pytest.param(["-I", "-c", th.CLAUDEX_SUPERVISOR_BOOTSTRAP, str(_REPO)], id="claudex_supervisor"),
    ],
)
def test_by_file_entrypoint_runs_without_pythonpath(tmp_path: Path, argv: list[str]):
    """Defect class: a removed import bootstrap breaks the hooks and launchers that run these files by path."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, *argv, "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
