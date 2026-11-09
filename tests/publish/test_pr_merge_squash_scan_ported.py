"""pr-merge scans the squash subject and body GitHub will publish (#9339); synthetic data, no network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest
from lu_machinery.opsec import prepublish as gate
from lu_machinery.publish import github as pub
from tests.opsec_fixtures import CATALOG, TOKEN, recorded_approval
from tests.helpers.product_tree import configured_roots  # noqa: F401





HEAD = "a" * 40


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)


def transport(calls, *, subject="clean (#1)", body="* clean", queue=True, head=HEAD):
    """Answer readiness and squash-text reads; record only the queue mutation."""
    def send(args, **kwargs):
        pull = {
            "id": "PR_node", "number": 1, "isDraft": False, "headRefOid": HEAD, "state": "OPEN",
            "baseRefName": "main", "isCrossRepository": False, "headRefName": "codex/fixture",
            "statusCheckRollup": [], "mergeStateStatus": "CLEAN", "labels": [],
            "isInMergeQueue": False, "isMergeQueueEnabled": queue,
            "viewerMergeHeadlineText": subject, "viewerMergeBodyText": body,
        }
        if args[:3] == ["gh", "pr", "view"]:
            value = pull
        elif args[:3] == ["gh", "pr", "checks"]:
            value = []
        elif args[:4] == ["gh", "api", "--method", "GET"]:
            value = ([[{"filename": "scripts/example.py"}]] if "/pulls/1/files" in args[4]
                     else [[recorded_approval(HEAD)]] if "/issues/1/comments" in args[4]
                     else {"login": "unit"})
        elif args[:5] == ["gh", "api", "--method", "POST", "graphql"]:
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            if payload["query"].startswith("mutation"):
                calls.append({"argv": args, "payload": payload})
                value = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}
            else:
                value = {"data": {"repository": {"pullRequest": {
                    **pull, "headRefOid": head if "viewerMergeHeadlineText" in payload["query"] else HEAD
                }}}}
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
    return send


@pytest.mark.parametrize(
    "field,defaults", [("subject", {"subject": TOKEN + " (#1)"}), ("body", {"body": "* " + TOKEN})]
)
def test_default_squash_text_hit_is_refused_before_merge(synthetic_opsec, field, defaults):
    calls = []
    with pytest.raises(gate.PublishBlocked) as error:
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls, **defaults))
    assert not calls and TOKEN not in str(error.value)
    assert f"rule=synthetic-rule class=1 field={field} line=1" in str(error.value)


@pytest.mark.parametrize("field", ["subject", "body"])
def test_explicit_squash_text_hit_is_refused_before_merge(synthetic_opsec, field):
    calls = []
    with pytest.raises(gate.PublishBlocked, match=f"field={field} "):
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls), **{field: "x " + TOKEN})
    assert not calls


def test_clean_defaults_are_sent_explicitly_as_scanned(synthetic_opsec):
    calls = []
    pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls, body="* clean\n* also clean"))
    assert len(calls) == 1
    assert "enqueuePullRequest" in calls[0]["payload"]["query"]
    assert calls[0]["payload"]["variables"] == {"id": "PR_node", "head": HEAD}


def test_merge_queue_defaults_are_scanned_even_with_explicit_text(synthetic_opsec):
    calls = []
    with pytest.raises(gate.PublishBlocked, match="field=default_body "):
        pub.publish(
            "pr-merge",
            repo="unit/public",
            number=1,
            subject="clean",
            body="clean",
            runner=transport(calls, body="* " + TOKEN, queue=True),
        )
    assert not calls






@pytest.mark.parametrize("recorder", [True, False])
def test_head_without_a_recorded_verdict_is_refused_only_where_the_recorder_is_used(
    synthetic_opsec, monkeypatch, recorder
):
    # #12031: zero verdict comments; the registry's per-repository setting decides.
    row = {**CATALOG["public"], "verdict_recorder": recorder}
    monkeypatch.setattr(gate, "catalog", lambda: {**CATALOG, "public": row})
    calls = []
    base = transport(calls)

    def send(args, **kwargs):
        if args[:4] == ["gh", "api", "--method", "GET"] and "/issues/1/comments" in args[4]:
            return subprocess.CompletedProcess(args, 0, "[[]]", "")
        return base(args, **kwargs)

    if recorder:
        with pytest.raises(gate.PublishBlocked, match=f"no cross-family APPROVE from .* is recorded at {HEAD}"):
            pub.publish("pr-merge", repo="unit/public", number=1, runner=send)
        assert not calls
    else:
        pub.publish("pr-merge", repo="unit/public", number=1, runner=send)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [{"head": "b" * 40}, {"subject": None}, {"body": 7}, {"queue": "yes"}],
)
def test_unverifiable_squash_text_is_refused(synthetic_opsec, overrides):
    calls = []
    with pytest.raises(gate.PublishBlocked, match="squash text unverifiable"):
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls, **overrides))
    assert not calls


def test_missing_matcher_fails_closed_for_default_text(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "private_tooling", lambda: tmp_path / "absent")
    calls = []
    with pytest.raises(gate.PublishBlocked, match="unavailable"):
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls))
    assert not calls


def test_override_is_logged_and_single_use(synthetic_opsec, tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "primary_root", lambda cwd=None: tmp_path)
    calls = []
    env = {"LU_OPSEC_OVERRIDE": "synthetic false positive"}
    pub.publish(
        "pr-merge", repo="unit/public", number=1, env=dict(env), runner=transport(calls, subject=TOKEN + " (#1)")
    )
    assert len(calls) == 1
    log = tmp_path / "batch_state/opsec/overrides.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["rule_ids"] == ["synthetic-rule"] and TOKEN not in log.read_text()
    with pytest.raises(gate.PublishBlocked, match="override already consumed"):
        pub.publish(
            "pr-merge", repo="unit/public", number=1, env=dict(env), runner=transport(calls, subject=TOKEN + " (#1)")
        )
    assert len(calls) == 1


def priority_transport(calls, *, labels, timeline):
    """A queue repository with one recorded approval of HEAD; records either enqueue write."""
    base = transport(calls, queue=True)
    approval = recorded_approval(HEAD, "fleet")
    pages = {"/comments": [[approval]], "/files": [[{"filename": "scripts/example.py"}]], "/timeline": [timeline]}

    def send(args, **kwargs):
        if args[:3] == ["gh", "pr", "view"] and "labels" in args[args.index("--json") + 1]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "id": "PR_node", "headRefOid": HEAD, "isCrossRepository": False, "baseRefName": "main",
                "number": 1, "isDraft": False, "mergeStateStatus": "CLEAN",
                "labels": [{"name": name} for name in labels],
            }), "")
        if args[:4] == ["gh", "api", "--method", "GET"]:
            if args[4].startswith("user"):
                return subprocess.CompletedProcess(args, 0, json.dumps({"login": "fleet"}), "")
            page = next(value for key, value in pages.items() if key + "?" in args[4])
            return subprocess.CompletedProcess(args, 0, json.dumps(page), "")
        if args[:5] == ["gh", "api", "--method", "POST", "graphql"]:
            document = json.loads(Path(args[6]).read_text())
            if "enqueuePullRequest" in document["query"]:
                calls.append({"argv": args, "jump": "jump:true" in document["query"], "variables": document["variables"], "payload": document})
                return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}), "")
        return base(args, **kwargs)

    return send


def labeled(at):
    return {"event": "labeled", "label": {"name": "merge-priority"}, "actor": {"login": "fleet"}, "created_at": at}


def test_labelled_approved_pr_is_enqueued_with_jump(synthetic_opsec):
    calls = []
    result = pub.publish("pr-merge", repo="unit/public", number=1, match_head=HEAD,
                         runner=priority_transport(calls, labels=["merge-priority"],
                                                   timeline=[labeled("2026-10-29T10:10:00Z")]))
    assert len(calls) == 1 and calls[0]["jump"] is True
    assert calls[0]["variables"] == {"id": "PR_node", "head": HEAD}
    assert result.queue_jump is True


def test_unlabelled_pr_is_enqueued_without_jump(synthetic_opsec):
    calls = []
    result = pub.publish("pr-merge", repo="unit/public", number=1, match_head=HEAD,
                         runner=priority_transport(calls, labels=[], timeline=[]))
    assert len(calls) == 1 and "jump:false" in calls[0]["payload"]["query"]
    assert not getattr(result, "queue_jump", False)


def test_label_applied_after_an_earlier_heads_approval_does_not_jump(synthetic_opsec):
    """Labelled after approving an old head; this head's approval came later, so the label predates it."""
    calls = []
    old_head_label = labeled("2026-10-29T10:03:00Z")
    result = pub.publish("pr-merge", repo="unit/public", number=1, match_head=HEAD,
                         runner=priority_transport(calls, labels=["merge-priority"], timeline=[old_head_label]))
    assert len(calls) == 1 and "jump:false" in calls[0]["payload"]["query"]
    assert not getattr(result, "queue_jump", False)


pytestmark = pytest.mark.usefixtures("configured_roots")
