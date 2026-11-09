"""Approved #12192 admission and mid-run record-damage defect classes."""

import json
import re
import signal
from types import SimpleNamespace
import pytest
from lu_machinery.dispatcher import store, worker
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.orchestration.task_record_store import read_task_record
from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review import review_lineage
from tests.dispatcher.conftest import run_git
from tests.helpers.product_tree import configured_roots  # noqa: F401





HEAD = "a" * 40


def admission_readers(root):
    return [
        lambda: review_lineage.review_records(root, repository="org/repo"),
        lambda: store.list_tasks(include_archive=True, strict=True),
        lambda: recorder.admitted_unknown_history(
            repository="org/repo", branch="topic", head_sha=HEAD, task_root=root, repo_root=root
        ),
        lambda: recorder.require_no_outside_review_in_flight(root, "org/repo", HEAD),
    ]


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("foreign", [
    b"[curriculum_manifest_canary] Manifest written successfully: 3901 files\n",
    b'{"schema":"agy-read-only.v1","task_id":"worker"}',
    b'{"schema_version":"code-review-findings.v1","findings":[]}',
    b'["a proof list"]',
])
def test_foreign_sidecar_does_not_block_review_admission(tmp_path, monkeypatch, caplog, foreign, archived):
    """A foreign sidecar must not deny an unrelated review or spend its attempt."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    archive = tmp_path / "archive"
    archive.mkdir()
    valid = dict(task_id="unrelated", schema_version=2, repository="org/repo", status="done", review_profile="infra")
    (tmp_path / "unrelated.json").write_text(json.dumps(valid))
    sidecar = (archive if archived else tmp_path) / "worker.manifest-canary.json"
    sidecar.write_bytes(foreign)
    assert [record["task_id"] for _, record in admission_readers(tmp_path)[0]()] == ["unrelated"]
    assert [record["task_id"] for record in admission_readers(tmp_path)[1]()] == ["unrelated"]
    assert admission_readers(tmp_path)[2]() == ()
    assert admission_readers(tmp_path)[3]() is None
    store.check_review_attempt(dict(review_id="unused", attempt_id="unused", manifest_sha256="b" * 64))
    assert str(sidecar) in caplog.text


def _plant_record_and_copy(root, copy_name, **copy_updates):
    """Plant ``worker.json`` and a misnamed copy; a None update omits the field from the copy."""
    record = dict(task_id="worker", schema_version=2, run_nonce="n1", repository="org/repo", status="done",
                  review_mode="independent")
    (root / "worker.json").write_text(json.dumps(record))
    copy = {**record, **copy_updates}
    (root / copy_name).write_text(json.dumps({key: value for key, value in copy.items() if value != "__omit__"}))


def test_exact_copy_of_a_named_record_does_not_block_admission(tmp_path, monkeypatch):
    """Defect class: a worker's saved copy of a task record refused every review admission read."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    _plant_record_and_copy(tmp_path, "worker.copy.json")
    readers = admission_readers(tmp_path)
    assert [path.name for path, _ in readers[0]()] in ([], ["worker.json"])
    assert [record["task_id"] for record in readers[1]()] == ["worker"]
    assert readers[2]() == ()
    assert readers[3]() is None


_READERS = [
    (0, "task record worker.COPY.json has invalid task identity"),
    (1, "unreadable task record .*worker.COPY"),
    (2, "task record .*worker.COPY.* unreadable"),
    (3, "RED_TEAM_REFUSED: task ledger unavailable"),
]
_READER_IDS = ["review_records", "list_tasks_strict", "admitted_unknown_history", "in_flight_guard"]
_MOVE_OUT = r"move it out of the task directory \(for example into <task-id>\.artifacts/\)"


@pytest.mark.parametrize("reader, message", _READERS, ids=_READER_IDS)
@pytest.mark.parametrize("copy_name, updates", [
    ("forged", {"run_nonce": "n2"}),
    ("claims", {"worktree_path": "/virtual/checkout", "review_profile": "infra", "review_head": HEAD,
                "review_attempt": 1}),
    ("status_nulled", {"status": None}),
    ("review_mode_omitted", {"review_mode": "__omit__"}),
    ("mid_run", {"status": "running"}),
])
def test_divergent_copy_still_blocks_admission(tmp_path, monkeypatch, reader, message, copy_name, updates):
    """Defect class: a copy differing from its named record in any field is skipped as if it were identical."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    errors = (ValueError, recorder.BranchFactsError, recorder.RecordError)
    _plant_record_and_copy(tmp_path, f"worker.{copy_name}.json", **updates)
    with pytest.raises(errors, match=message.replace("COPY", copy_name)) as caught:
        admission_readers(tmp_path)[reader]()
    assert re.search(_MOVE_OUT, str(caught.value))


@pytest.mark.parametrize("reader, message", _READERS, ids=_READER_IDS)
def test_orphaned_or_renamed_record_still_blocks_admission(tmp_path, monkeypatch, reader, message):
    """Defect class: a record with no correctly named twin (or an archived-only one) is skipped as a copy."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    errors = (ValueError, recorder.BranchFactsError, recorder.RecordError)
    _plant_record_and_copy(tmp_path, "worker.copy.json")
    (tmp_path / "worker.json").rename(tmp_path / "worker.renamed.json")
    with pytest.raises(errors, match=message.replace("COPY", "(copy|renamed)")):
        admission_readers(tmp_path)[reader]()
    (tmp_path / "worker.renamed.json").unlink()
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "worker.json").write_text((tmp_path / "worker.copy.json").read_text())
    with pytest.raises(errors, match=message.replace("COPY", "copy")):
        admission_readers(tmp_path)[reader]()


def test_receipt_marker_cannot_hide_legacy_review_claim(tmp_path, monkeypatch):
    """A receipt marker cannot remove a lifecycle claim from admission's inventory."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    record = dict(schema="agy-read-only.v1", task_id="legacy-review", agent="claude", status="done",
                  review_profile="infra", review_mode="cross_family", repository="org/repo", worktree_branch="topic")
    (tmp_path / "legacy-review.receipt.json").write_text(json.dumps(record))

    assert [row["task_id"] for _, row in review_lineage.review_records(tmp_path, repository="org/repo")] == [
        "legacy-review"
    ]
    assert [row["task_id"] for row in store.list_tasks(include_archive=True, strict=True)] == ["legacy-review"]


@pytest.mark.parametrize(("marker", "value"), [
    ("pr", 12192),
    ("review_attempt", {"review_id": "legacy"}),
    ("require_review_verdict", True),
    ("review_profile", "infra"),
    ("review_author_model", "gpt-6.1-sol"),
    ("review_risk", "high"),
    ("review_mode", "cross_family"),
    ("type", "review"),
])
def test_legacy_review_flags_remain_visible_to_lineage(tmp_path, marker, value):
    """Every established review flag retains a legacy row in lineage inventory."""
    (tmp_path / "legacy-review.json").write_text(json.dumps({"repository": "org/repo", marker: value}))

    assert [path.name for path, _ in review_lineage.review_records(tmp_path, repository="org/repo")] == [
        "legacy-review.json"
    ]


@pytest.mark.parametrize("damaged", [
    b'{"schema_version":2,"task_id":"damaged",',
    b'{"schema_version":3,"task_id":"damaged"}',
    b'{"schema_version":"2","task_id":"damaged"}',
    b'{"schema_version":2,"task_id":"forged"}',
    '{"schema_version":2,"task_id":"damaged"}'.encode("utf-16"),
    b'\xef\xbb\xbf{"schema_version":2,"task_id":"damaged"}',
    b'\xef\xbb\xbf\n{"schema_version":2,"task_id":"damaged"}',
    b'\xef\xbb\xbf\t  {"schema_version":2,"task_id":"damaged"}',
    '\ufeff\u00a0{"schema_version":2,"task_id":"damaged"}'.encode("utf-8"),
    b'\x00\x00{"schema_version":2,"task_id":"damaged"}',
])
def test_damaged_record_still_refuses_admission(tmp_path, monkeypatch, damaged):
    """Corruption or forged identity must not disappear from admission scans."""
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path))
    archive = tmp_path / "archive"
    archive.mkdir()
    legacy = archive / "legacy.20261030T000000Z.1.archived.json"
    legacy.write_text(json.dumps(dict(task_id="legacy", status="done", review_profile="infra", repository="org/repo")))
    assert read_task_record(legacy)["task_id"] == "legacy"
    assert [record["task_id"] for _, record in admission_readers(tmp_path)[0]()] == ["legacy"]
    (tmp_path / "damaged.json").write_bytes(damaged)
    for reader in admission_readers(tmp_path):
        with pytest.raises((ValueError, recorder.RecordError)):
            reader()


@pytest.mark.parametrize("phase", ["provider_exit", "settlement_cutoff", "terminal_write"])
@pytest.mark.parametrize("damage", ["deleted", "truncated", "non_object", "missing_nonce", "replaced_nonce", "recursion"])
def test_mid_run_record_damage_publishes_typed_terminal_failure(sandbox, monkeypatch, phase, damage):
    """Record loss/corruption cannot strand a supervisor or overwrite a replacement run."""
    from tests.dispatcher import stub_provider

    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setattr(worker.providers, "load", lambda _agent: stub_provider)
    released = []
    hold = SimpleNamespace(environment=lambda: {}, release=lambda: released.append(True))
    monkeypatch.setattr(worker.admission, "hold_review_test_slots", lambda **_kwargs: (hold, None))
    spec = TaskSpec("damage-probe", "codex", "echo", None, "read-only", sandbox.repo,
                    sandbox.repo / "damage-probe.prompt", sandbox.tasks / "damage-probe.result")
    spec.prompt_file.write_bytes(b"literal final response")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main",
                              run_git(sandbox.repo, "rev-parse", "HEAD"), {"research_owned_path": []})
    record["prompt_file"] = str(sandbox.tasks / "damage-probe.prompt")
    path = store.create(record, b"literal final response")
    (sandbox.tasks / "logs").mkdir()
    original = worker.communicate if phase == "provider_exit" else getattr(
        store, "begin_settlement" if phase == "settlement_cutoff" else "_write"
    )
    damaged = False

    def damage_record():
        nonlocal damaged
        if damaged:
            return
        damaged = True
        if damage == "deleted":
            path.unlink()
        elif damage == "truncated":
            path.write_text('{"schema_version":2,')
        elif damage == "non_object":
            path.write_text("[]")
        elif damage == "recursion":
            path.write_text("[" * 100000)
        elif damage == "missing_nonce":
            path.write_text("{}")
        else:
            path.write_text(json.dumps(dict(record, run_nonce="replacement")))

    def interrupted(*args, **kwargs):
        if phase == "terminal_write":
            if args[1].get("status") not in store.ACTIVE:
                damage_record()
            return original(*args, **kwargs)
        result = original(*args, **kwargs)
        damage_record()
        return result

    monkeypatch.setattr(worker if phase == "provider_exit" else store,
                        "communicate" if phase == "provider_exit" else (
                            "begin_settlement" if phase == "settlement_cutoff" else "_write"), interrupted)
    reaped = []
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: reaped.append(True))
    previous_term, previous_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    if damage == "replaced_nonce":
        with pytest.raises(store.TaskRecordError, match="TASK_RECORD_REPLACED"):
            worker.supervise(path, {}, 10)
        assert json.loads(path.read_text())["run_nonce"] == "replacement"
    else:
        worker.supervise(path, {}, 10)
        terminal = store.read("damage-probe", record["run_nonce"])
        assert terminal["status"] == "crashed"
        assert terminal["error"] == "TASK_RECORD_DAMAGED"
        assert terminal["finished_at"]
        assert terminal["worktree_disposition_reason"] == "task_record_damaged"
        assert worker._identity(terminal["provider_pgid"]) is None
    assert damaged
    assert released == [True]
    assert reaped == []
    assert signal.getsignal(signal.SIGTERM) is previous_term
    assert signal.getsignal(signal.SIGINT) is previous_int


def test_failed_record_recovery_types_os_errors_and_keeps_a_removed_tree(tmp_path, monkeypatch):
    """Defect class: an OSError subclass escaping without a typed outcome, or a removed disposition overwritten."""
    record = dict(schema_version=2, task_id="probe", run_nonce="n1", status="running")
    unwritable = tmp_path / "unwritable.json"
    unwritable.mkdir()
    with pytest.raises(store.TaskRecordError, match=r"^TASK_RECORD_IO_ERROR: IsADirectoryError$"):
        store.publish_failed_record(dict(record), unwritable)
    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text("{")
    read_text = type(unreadable).read_text

    def denied(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("test denial")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(unreadable), "read_text", denied)
    with pytest.raises(store.TaskRecordError, match=r"^TASK_RECORD_IO_ERROR: PermissionError$"):
        store.publish_failed_record(dict(record), unreadable)
    assert read_text(unreadable) == "{"
    reaped = tmp_path / "probe.json"
    reaped.write_text('{"schema_version":2,')
    store.publish_failed_record(dict(record, worktree_disposition="removed", worktree_disposition_reason="merged"), reaped)
    terminal = json.loads(reaped.read_text())
    assert (terminal["status"], terminal["error"]) == ("crashed", "TASK_RECORD_DAMAGED")
    assert (terminal["worktree_disposition"], terminal["worktree_disposition_reason"]) == ("removed", "merged")


pytestmark = pytest.mark.usefixtures("configured_roots")
