"""Recorded approval and queue-only publishing invariants."""

import json
import subprocess
from pathlib import Path

import pytest

from lu_machinery.publish import github as publisher

HEAD = "a" * 40
_ABSENT = object()
_UNVERIFIABLE_RECORDER = (
    "OPSEC: merge refused: the repository's verdict_recorder setting is unverifiable."
)


def enqueue_transport(writes, *, state="CLEAN", draft=False, priority=False, head=HEAD, queued=False):
    """Stub GitHub reads, recording every write including legacy merge commands."""
    def send(args, **_kwargs):
        pull = {
            "id": "PR_node", "number": 1, "isDraft": draft, "headRefOid": head,
            "mergeStateStatus": state, "isCrossRepository": False, "baseRefName": "main",
            "labels": [{"name": "merge-priority"}] if priority else [], "isInMergeQueue": queued,
            "isMergeQueueEnabled": True, "viewerMergeHeadlineText": "Safe change",
            "viewerMergeBodyText": "Safe change description",
        }
        if args[:3] == ["gh", "pr", "view"]:
            value = pull
        elif args[:3] == ["gh", "pr", "checks"]:
            value = []
        elif args[:5] == ["gh", "api", "--method", "POST", "graphql"]:
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            if payload["query"].startswith("mutation"):
                writes.append(payload)
                value = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}
            else:
                value = {"data": {"repository": {"pullRequest": pull}}}
        elif args[:4] == ["gh", "api", "--method", "GET"]:
            value = {"login": "fleet"} if args[4].startswith("user") else [[]]
        else:
            writes.append({"argv": args})
            value = {}
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    return send


@pytest.fixture
def queue_publisher(monkeypatch, synthetic_opsec):
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {"public": {"github": "unit/public"}})
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)
    # Review qualification is tested separately. Keep the actual readiness and transport here.
    monkeypatch.setattr(publisher, "_refuse_incomplete_seats", lambda *args: None)


@pytest.mark.parametrize("priority", [False, True], ids=["plain", "jump"])
@pytest.mark.parametrize("state,draft,reason", [
    ("BLOCKED", False, "BLOCKED"), ("DIRTY", False, "DIRTY"),
    ("BEHIND", False, "BEHIND"), ("UNSTABLE", False, "UNSTABLE"),
    ("UNKNOWN", False, "UNKNOWN"), (None, False, "unverifiable"),
    ("CLEAN", True, "DRAFT"),
])
def test_non_ready_pr_cannot_arm_auto_merge(queue_publisher, capsys, priority, state, draft, reason):
    writes = []
    assert publisher.main(
        ["pr-merge", "--repo", "unit/public", "--number", "1", "--match-head", HEAD],
        runner=enqueue_transport(writes, state=state, draft=draft, priority=priority),
    ) == 2
    assert reason in capsys.readouterr().err
    assert writes == []


@pytest.mark.parametrize("jump", [False, True])
@pytest.mark.parametrize("state", ["CLEAN", "HAS_HOOKS"])
def test_ready_pr_enqueues_exact_head_without_gh_merge(queue_publisher, monkeypatch, jump, state):
    from lu_machinery.orchestration import integration_sweep

    monkeypatch.setattr(integration_sweep, "priority_jump", lambda *args: jump)
    writes = []
    assert publisher.main(
        ["pr-merge", "--repo", "unit/public", "--number", "1", "--match-head", HEAD],
        runner=enqueue_transport(writes, state=state, priority=jump),
    ) == 0
    assert len(writes) == 1
    assert "enqueuePullRequest" in writes[0]["query"]
    assert "expectedHeadOid:$head" in writes[0]["query"]
    assert f"jump:{str(jump).lower()}" in writes[0]["query"]
    assert writes[0]["variables"] == {"id": "PR_node", "head": HEAD}


def test_moved_head_refuses_enqueue(queue_publisher, capsys):
    writes = []
    assert publisher.main(
        ["pr-merge", "--repo", "unit/public", "--number", "1", "--match-head", HEAD],
        runner=enqueue_transport(writes, head="b" * 40),
    ) == 2
    assert "reviewed head changed" in capsys.readouterr().err
    assert writes == []


def test_already_queued_head_does_not_enqueue_twice(queue_publisher):
    writes = []
    assert publisher.main(
        ["pr-merge", "--repo", "unit/public", "--number", "1", "--match-head", HEAD],
        runner=enqueue_transport(writes, queued=True),
    ) == 0
    assert writes == []


@pytest.mark.parametrize("recorder", [True, False])
def test_head_without_a_recorded_verdict_is_refused_only_where_the_recorder_is_used(
    monkeypatch, low_review, recorder,
):
    _tasks, _comments, _statuses, files, admit = low_review
    files[:] = [{"filename": "lu_machinery/common/example.py", "status": "modified"}]
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {
        "public": {"github": "unit/public", "verdict_recorder": recorder},
    })
    if recorder:
        with pytest.raises(publisher.gate.PublishBlocked, match="no cross-family APPROVE"):
            admit()
    else:
        assert admit() is None


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param(_ABSENT, id="absent"),
        pytest.param("true", id="string-true"),
        pytest.param("false", id="string-false"),
        pytest.param(None, id="null"),
    ],
)
def test_absent_string_or_null_verdict_recorder_is_refused_before_publication(
    monkeypatch, tmp_path, setting,
):
    """An absent, string (including \"true\") or null recorder is unverifiable, never coerced."""
    row = {"github": "unit/public"}
    if setting is not _ABSENT:
        row["verdict_recorder"] = setting
    monkeypatch.setattr(publisher.gate, "catalog", lambda: {"public": row})
    calls = []

    def send(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    with pytest.raises(publisher.gate.PublishBlocked) as caught:
        publisher._refuse_incomplete_seats(
            "github.com/unit/public", 1, HEAD, send, tmp_path, {},
        )
    assert str(caught.value) == _UNVERIFIABLE_RECORDER
    assert calls == []
