"""Tests for delegate monitor API endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import lu_machinery.api.delegate_router as delegate_router
from lu_machinery.api.main import app

client = TestClient(app, raise_server_exceptions=False)


def _snapshot_client(ctx=None):
    snapshot_app = FastAPI()
    snapshot_app.state.ctx = ctx or app.state.ctx
    snapshot_app.include_router(delegate_router.router, prefix="/api/delegate")
    return TestClient(snapshot_app)


def _active_get():
    # These legacy clients omit ASGI startup. Exercise the real router lifespan.
    with _snapshot_client() as snapshot_client:
        return snapshot_client.get("/api/delegate/active")


def _pin_tasks_dir(monkeypatch, tasks_dir: Path):
    """Redirect delegate task reads to a disposable tasks directory."""
    tasks_dir = Path(tasks_dir)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    ctx = app.state.ctx.with_roots(batch_state_dir=tasks_dir.parent)
    monkeypatch.setattr(app.state, "ctx", ctx)
    # Patch the canonical factory (used by shared resolve_context) and the
    # router re-export kept for monkeypatch compatibility after #7496.
    import lu_machinery.api.monitor_context as monitor_context

    monkeypatch.setattr(monitor_context, "production_context", lambda: ctx)
    monkeypatch.setattr(delegate_router, "production_context", lambda: ctx)
    monkeypatch.setattr(delegate_router, "_tasks_dir", lambda ctx=None: tasks_dir)
    return ctx


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_task(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _task_payload(task_id: str, **overrides) -> dict:
    started = datetime.now(UTC) - timedelta(minutes=5)
    payload = {
        "task_id": task_id,
        "agent": "codex",
        "model": "gpt-5.5",
        "effort": "high",
        "cli_version": "0.123.0",
        "mode": "workspace-write",
        "cwd": "/tmp/repo",
        "pid": 12345,
        "status": "done",
        "started_at": _iso(started),
        "finished_at": _iso(started + timedelta(seconds=30)),
        "duration_s": 30.0,
        "prompt_chars": 10,
        "response_chars": 20,
        "result_file": None,
        "returncode": 0,
        "stderr_excerpt": None,
    }
    payload.update(overrides)
    return payload


def test_tasks_lists_state_files(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "first.json", _task_payload("first", started_at=_iso(datetime.now(UTC) - timedelta(minutes=10))))
    _write_task(tasks_dir / "second.json", _task_payload("second", started_at=_iso(datetime.now(UTC) - timedelta(minutes=1))))

    response = client.get("/api/delegate/tasks")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["tasks"][0]["task_id"] == "second"
    assert data["tasks"][1]["task_id"] == "first"
    assert data["tasks"][0]["model"] == "gpt-5.5"
    assert data["tasks"][0]["effort"] == "high"
    assert data["tasks"][0]["cli_version"] == "0.123.0"
    assert data["tasks"][0]["substitution"] is None


def test_tasks_status_filter(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "done.json", _task_payload("done", status="done"))
    _write_task(tasks_dir / "running.json", _task_payload("running", status="running"))

    response = client.get("/api/delegate/tasks?status=done")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["tasks"][0]["task_id"] == "done"
    assert data["tasks"][0]["status"] == "done"


def test_tasks_timeout_status_is_distinct_from_failed(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "timeout.json", _task_payload("timeout", status="timeout"))
    _write_task(tasks_dir / "failed.json", _task_payload("failed", status="failed"))

    timeout_response = client.get("/api/delegate/tasks?status=timeout")
    failed_response = client.get("/api/delegate/tasks?status=failed")

    assert timeout_response.status_code == 200
    assert failed_response.status_code == 200
    assert [task["task_id"] for task in timeout_response.json()["tasks"]] == ["timeout"]
    assert [task["task_id"] for task in failed_response.json()["tasks"]] == ["failed"]


def test_tasks_attention_status_filters_are_queryable(tmp_path, monkeypatch):
    """``needs_finalize``/``no_deliverable`` are real settle states — the API
    must not 422 when an orchestrator filters by them (#5800 review)."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "finalize.json", _task_payload("finalize", status="needs_finalize"))
    _write_task(tasks_dir / "nodeliv.json", _task_payload("nodeliv", status="no_deliverable"))

    finalize_response = client.get("/api/delegate/tasks?status=needs_finalize")
    nodeliv_response = client.get("/api/delegate/tasks?status=no_deliverable")

    assert finalize_response.status_code == 200
    assert nodeliv_response.status_code == 200
    assert [task["task_id"] for task in finalize_response.json()["tasks"]] == ["finalize"]
    assert [task["task_id"] for task in nodeliv_response.json()["tasks"]] == ["nodeliv"]


def test_active_lists_only_live_running_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)

    def fake_kill(pid: int, sig: int) -> None:
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(delegate_router.os, "kill", fake_kill)
    _write_task(tasks_dir / "running.json", _task_payload("running", status="running", pid=111))
    _write_task(tasks_dir / "zombie.json", _task_payload("zombie", status="running", pid=222))
    _write_task(tasks_dir / "done.json", _task_payload("done", status="done", pid=333))

    response = _active_get()

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["tasks"][0]["task_id"] == "running"


def test_task_state_path_stays_under_tasks_dir(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _pin_tasks_dir(monkeypatch, tasks_dir)

    for task_id in ("normal", "agent/task", "../../etc/passwd", r"..\..\windows"):
        path = Path(delegate_router._task_state_path(task_id))
        resolved = path.resolve()
        assert resolved == tasks_dir.resolve() / resolved.name
        assert resolved.is_relative_to(tasks_dir.resolve())


def test_task_detail_truncates_large_result(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _pin_tasks_dir(monkeypatch, tasks_dir)
    # Production writes ``.result`` siblings under TASKS_DIR; reads are
    # containment-checked there (CodeQL py/path-injection).
    result_file = tasks_dir / "large.result"
    result_file.write_text("x" * (70 * 1024), encoding="utf-8")
    _write_task(
        tasks_dir / "large.json",
        _task_payload("large", result_file=str(result_file), status="done"),
    )

    response = client.get("/api/delegate/tasks/large")

    assert response.status_code == 200
    data = response.json()
    assert data["result_truncated"] is True
    assert len(data["result"].encode("utf-8")) <= delegate_router.RESULT_BYTES_LIMIT


def test_task_detail_falls_back_to_archived_record_and_result(tmp_path, monkeypatch):
    """#8625: an archived record keeps its hot-directory result_file path."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    archive = tasks_dir / delegate_router.TASK_ARCHIVE_DIR_NAME
    _write_task(
        archive / "old.json",
        _task_payload("old", result_file=str(tasks_dir / "old.result"), status="done"),
    )
    (archive / "old.result").write_text("archived reply", encoding="utf-8")

    response = client.get("/api/delegate/tasks/old")

    assert response.status_code == 200
    data = response.json()
    assert data["task"]["task_id"] == "old"
    assert data["result"] == "archived reply"


def test_task_listing_keeps_archived_history_and_active_views_stay_hot(tmp_path, monkeypatch):
    """#8625: status=all and terminal filters read archive/; active queries read the hot directory only."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "hot-done.json", _task_payload("hot-done"))
    _write_task(tasks_dir / "hot-failed.json", _task_payload("hot-failed", status="failed"))
    _write_task(tasks_dir / "archive" / "old-done.json", _task_payload("old-done"))
    # Never produced by archive; proves active queries do not look there.
    _write_task(tasks_dir / "archive" / "parked.json", _task_payload("parked", status="needs_finalize"))

    assert client.get("/api/delegate/tasks?status=all").json()["total"] == 4
    done = client.get("/api/delegate/tasks?status=done").json()
    assert sorted(row["task_id"] for row in done["tasks"]) == ["hot-done", "old-done"]
    assert client.get("/api/delegate/tasks?status=needs_finalize").json()["total"] == 0
    assert _active_get().json()["total"] == 0


def test_task_cache_drops_records_that_left_the_hot_directory(tmp_path, monkeypatch):
    """#8625: archived records leave the in-memory and SQLite task cache."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "keep.json", _task_payload("keep"))
    _write_task(tasks_dir / "gone.json", _task_payload("gone"))
    assert client.get("/api/delegate/tasks?status=all").json()["total"] == 2
    gone_path = str(tasks_dir / "gone.json")
    assert gone_path in delegate_router._TASK_STATE_CACHE

    (tasks_dir / "archive").mkdir()
    (tasks_dir / "gone.json").rename(tasks_dir / "archive" / "gone.json")
    archived_path = str(tasks_dir / "archive" / "gone.json")

    # The archived record keeps counting under its new path; the hot path leaves the cache.
    assert client.get("/api/delegate/tasks?status=all").json()["total"] == 2
    assert gone_path not in delegate_router._TASK_STATE_CACHE
    assert archived_path in delegate_router._TASK_STATE_CACHE
    cached = delegate_router._load_task_cache_from_db(str(tasks_dir))
    assert gone_path not in cached
    assert {str(tasks_dir / "keep.json"), archived_path} <= set(cached)
    # A hot-only (active) scan never evicts the archive entries it did not read.
    _active_get()
    assert archived_path in delegate_router._TASK_STATE_CACHE
    (tasks_dir / "archive" / "gone.json").unlink()
    assert client.get("/api/delegate/tasks?status=all").json()["total"] == 1
    assert archived_path not in delegate_router._TASK_STATE_CACHE


def test_task_detail_rejects_result_outside_tasks_dir(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    escape = tmp_path / "outside.result"
    escape.write_text("secret", encoding="utf-8")
    _write_task(
        tasks_dir / "escape.json",
        _task_payload("escape", result_file=str(escape), status="done"),
    )

    response = client.get("/api/delegate/tasks/escape")

    assert response.status_code == 200
    data = response.json()
    assert data["result"] is None
    assert data["result_truncated"] is False


def test_zombie_detection_works_on_dead_pid(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)

    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(delegate_router.os, "kill", fake_kill)
    _write_task(
        tasks_dir / "zombie.json",
        _task_payload("zombie", status="running", pid=424242, finished_at=None, duration_s=None),
    )

    response = client.get("/api/delegate/tasks")

    assert response.status_code == 200
    task = response.json()["tasks"][0]
    assert task["task_id"] == "zombie"
    assert task["status"] == "zombie"
    assert task["alive"] is False


# Public summary keys include additive accounting projections, without repository attribution.
_LEGACY_DELEGATE_SUMMARY_KEYS = {
    "task_id",
    "agent",
    "model",
    "effort",
    "cli_version",
    "substitution",
    "status",
    "started_at",
    "duration_s",
    "age_s",
    "updated_at",
    "alive",
    "requested_selector",
    "observed_model",
    "identity_state",
    "observed_model_source",
    "observed_model_provenance",
}
_DELEGATE_SUMMARY_OBSERVATION_KEYS = {
    "run_nonce",
    "schema_version",
    "finished_at",
    "returncode",
    "head_sha",
    "remote_head_sha",
    "push_state",
    "commits_ahead",
    "worktree_dirty_on_exit",
    "claude_account",
    "codex_account",
    "delivery_pr",
    "research_role",
    "error",
}


def _assert_legacy_summary_shape(tasks: list) -> None:
    """Rows retain legacy keys, allow observations, and exclude repository identity."""
    for task in tasks:
        assert isinstance(task, dict)
        assert set(task) >= _LEGACY_DELEGATE_SUMMARY_KEYS
        assert set(task) <= _LEGACY_DELEGATE_SUMMARY_KEYS | _DELEGATE_SUMMARY_OBSERVATION_KEYS
        assert "repository" not in task
        assert "repository_id" not in task


def test_list_delegate_tasks_repository_filter_before_limit(tmp_path, monkeypatch):
    """Internal repository predicate filters before total/limit so public rows are not starved."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    public_repo = "learn-ukrainian/learn-ukrainian.github.io"
    private_repo = "other-org/other-private-repo"
    now = datetime.now(UTC)

    # >500 newer foreign/unclassified tasks that would consume the default page.
    for index in range(520):
        _write_task(
            tasks_dir / f"foreign-{index:04d}.json",
            _task_payload(
                f"foreign-{index:04d}",
                status="done",
                started_at=_iso(now - timedelta(seconds=index)),
                repository=private_repo,
                cwd=f"/Users/private/{private_repo}",
                worktree_path=f"/Users/private/.worktrees/dispatch/codex/foreign-{index}",
            ),
        )
    for index in range(30):
        _write_task(
            tasks_dir / f"unclassified-{index:04d}.json",
            _task_payload(
                f"unclassified-{index:04d}",
                status="done",
                started_at=_iso(now - timedelta(seconds=600 + index)),
                cwd=f"/Users/private/projects/{public_repo}",
                worktree_path=f"/Users/private/.worktrees/dispatch/codex/issue-{index}",
            ),
        )
    # Older public tasks that must still appear when the list is repo-scoped.
    public_ids = []
    for index in range(7):
        task_id = f"public-old-{index:04d}"
        public_ids.append(task_id)
        _write_task(
            tasks_dir / f"{task_id}.json",
            _task_payload(
                task_id,
                status="done",
                started_at=_iso(now - timedelta(days=2, minutes=index)),
                repository=public_repo,
            ),
        )
    _write_task(
        tasks_dir / "public-running.json",
        _task_payload(
            "public-running",
            status="running",
            pid=111,
            started_at=_iso(now - timedelta(days=3)),
            repository=public_repo,
            duration_s=None,
        ),
    )
    _write_task(
        tasks_dir / "public-spawning.json",
        _task_payload(
            "public-spawning",
            status="spawning",
            pid=None,
            started_at=_iso(now - timedelta(days=3, minutes=1)),
            repository_id=public_repo,
            duration_s=None,
        ),
    )
    _write_task(
        tasks_dir / "ambiguous.json",
        _task_payload(
            "ambiguous",
            status="done",
            started_at=_iso(now - timedelta(days=1)),
            repository=public_repo,
            repository_id=private_repo,
        ),
    )
    monkeypatch.setattr(delegate_router.os, "kill", lambda pid, sig: None)

    unscoped = delegate_router.list_delegate_tasks(status="all", limit=500)
    # Unscoped page is filled by newer foreign volume; older public rows drop off.
    assert unscoped["total"] > 500
    unscoped_ids = {t["task_id"] for t in unscoped["tasks"]}
    assert "foreign-0000" in unscoped_ids
    assert "public-old-0000" not in unscoped_ids
    # Privacy: raw private repository on disk must never appear in generic summaries.
    _assert_legacy_summary_shape(unscoped["tasks"])
    unscoped_blob = json.dumps(unscoped)
    assert "repository" not in unscoped_blob
    assert "repository_id" not in unscoped_blob
    assert private_repo not in unscoped_blob

    scoped = delegate_router.list_delegate_tasks(
        status="all", limit=500, repository=public_repo
    )
    assert scoped["total"] == 9  # 7 older + running + spawning; ambiguous omitted
    scoped_ids = {t["task_id"] for t in scoped["tasks"]}
    assert scoped_ids == set(public_ids) | {"public-running", "public-spawning"}
    # Scoped internal results still use the legacy public summary shape.
    _assert_legacy_summary_shape(scoped["tasks"])
    blob = json.dumps(scoped)
    assert "repository" not in blob
    assert "repository_id" not in blob
    assert private_repo not in blob
    assert public_repo not in blob
    assert "/Users/private/" not in blob
    assert "cwd" not in blob
    assert "worktree_path" not in blob

    active = delegate_router.active_delegate_tasks(repository=public_repo)
    assert active["total"] == 2
    assert {t["task_id"] for t in active["tasks"]} == {
        "public-running",
        "public-spawning",
    }
    _assert_legacy_summary_shape(active["tasks"])
    assert "repository" not in json.dumps(active)

    # Default HTTP surface stays unscoped and free of repository/path metadata.
    response = client.get("/api/delegate/tasks?status=all&limit=500")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == unscoped["total"]
    _assert_legacy_summary_shape(data["tasks"])
    http_blob = json.dumps(data)
    assert "repository" not in http_blob
    assert "repository_id" not in http_blob
    assert private_repo not in http_blob
    assert "/Users/private/" not in http_blob
    assert "worktree_path" not in http_blob

    active_http = _active_get()
    assert active_http.status_code == 200
    active_data = active_http.json()
    _assert_legacy_summary_shape(active_data["tasks"])
    active_http_blob = json.dumps(active_data)
    assert "repository" not in active_http_blob
    assert "repository_id" not in active_http_blob
    assert private_repo not in active_http_blob

    # repository query param is not a free-form public selector
    response_q = client.get(
        f"/api/delegate/tasks?status=all&limit=50&repository={public_repo}"
    )
    assert response_q.status_code == 200
    # Unknown query keys are ignored by FastAPI; response stays unscoped.
    assert response_q.json()["total"] == unscoped["total"]
    _assert_legacy_summary_shape(response_q.json()["tasks"])


def test_delegate_http_redacts_repository_even_when_task_state_has_private(
    tmp_path, monkeypatch
):
    """Unscoped /tasks and /active must never serialize repository/repository_id."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    private_repo = "secret-org/secret-private-infra"
    public_repo = "learn-ukrainian/learn-ukrainian.github.io"
    now = datetime.now(UTC)
    _write_task(
        tasks_dir / "private-running.json",
        _task_payload(
            "private-running",
            status="running",
            pid=222,
            started_at=_iso(now),
            repository=private_repo,
            repository_id=private_repo,
            delivery_pr=f"{private_repo}#42",
            cwd=f"/Users/private/{private_repo}",
        ),
    )
    _write_task(
        tasks_dir / "public-done.json",
        _task_payload(
            "public-done",
            status="done",
            started_at=_iso(now - timedelta(minutes=1)),
            repository=public_repo,
            delivery_pr=f"{public_repo}#43",
        ),
    )
    monkeypatch.setattr(delegate_router.os, "kill", lambda pid, sig: None)

    tasks_resp = client.get("/api/delegate/tasks?status=all&limit=50")
    active_resp = _active_get()
    assert tasks_resp.status_code == 200
    assert active_resp.status_code == 200
    tasks_body = tasks_resp.json()
    active_body = active_resp.json()
    recent_body = delegate_router.recent_finished_tasks()
    assert tasks_body["total"] == 2
    assert active_body["total"] == 1
    _assert_legacy_summary_shape(tasks_body["tasks"])
    _assert_legacy_summary_shape(active_body["tasks"])
    assert {row["delivery_pr"] for row in tasks_body["tasks"]} == {"#42", "#43"}
    for body in (tasks_body, active_body, recent_body):
        blob = json.dumps(body)
        assert "repository" not in blob
        assert "repository_id" not in blob
        assert private_repo not in blob
        assert public_repo not in blob
        assert "secret-org" not in blob
        assert "/Users/private/" not in blob


def test_task_detail_detects_stale_run_nonce_split_brain(tmp_path, monkeypatch):
    """#7168: Monitor API task detail returns 409 Conflict if expected run_nonce mismatches."""
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(
        tasks_dir / "split-task.json",
        _task_payload(
            "split-task",
            status="done",
            run_nonce="round-1-stale-nonce",
        ),
    )

    # Reader queries with stale nonce from round 1 -> 200 OK
    resp_match = client.get("/api/delegate/tasks/split-task?run_nonce=round-1-stale-nonce")
    assert resp_match.status_code == 200
    assert resp_match.json()["task"]["run_nonce"] == "round-1-stale-nonce"

    # Reader queries with live nonce from round 2 -> 409 Conflict
    resp_mismatch = client.get("/api/delegate/tasks/split-task?run_nonce=round-2-live-nonce")
    assert resp_mismatch.status_code == 409
    assert "Task run_nonce mismatch" in resp_mismatch.json()["detail"]


def test_accounting_projection_survives_task_cache_restart_and_upgrade(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    ctx = _pin_tasks_dir(monkeypatch, tasks_dir)
    expected = {
        "requested_selector": "requested-model", "observed_model": "observed-model",
        "identity_state": "observed", "observed_model_source": "provider-init",
        "observed_model_provenance": {"requested_model": "attempt-model", "invocation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
    }
    _write_task(tasks_dir / "observed.json", _task_payload("observed", model="accounting-model",
                codex_account="b-sub", research_role="reviewer", delivery_pr="unit/public#12", **expected))
    _write_task(tasks_dir / "legacy.json", _task_payload("legacy", model="legacy-model"))

    def assert_rows():
        response = client.get("/api/delegate/tasks")
        assert response.status_code == 200
        rows = {row["task_id"]: row for row in response.json()["tasks"]}
        assert rows["observed"]["model"] == "accounting-model"
        assert rows["observed"]["codex_account"] == "b-sub"
        assert rows["observed"]["research_role"] == "reviewer"
        assert rows["observed"]["delivery_pr"] == "#12"
        assert {key: rows["observed"][key] for key in expected} == expected
        assert {key: rows["legacy"][key] for key in expected} == {
            "requested_selector": None, "observed_model": None,
            "identity_state": "unknown", "observed_model_source": None, "observed_model_provenance": None,
        }

    assert_rows()

    delegate_router._TASK_STATE_CACHE.clear()
    reloaded = delegate_router._load_task_cache_from_db(str(tasks_dir), ctx)
    assert len(reloaded) == 2
    # A process restart uses only the persisted cache when task mtimes match.
    delegate_router._TASK_STATE_CACHE.update(reloaded)
    read_task = delegate_router._read_task_state
    monkeypatch.setattr(delegate_router, "_read_task_state", lambda *a: (_ for _ in ()).throw(AssertionError("unexpected reread")))
    assert_rows()
    monkeypatch.setattr(delegate_router, "_read_task_state", read_task)
    # Persisted pre-upgrade projections can be non-null but lack the added observations.
    conn = delegate_router._init_task_cache_db(delegate_router._task_cache_db_path(str(tasks_dir)), ctx)
    for path, fingerprint in conn.execute("SELECT path, fingerprint FROM task_cache").fetchall():
        old = json.loads(fingerprint)
        old[0] = old[0].replace("record-v3:", "record-v2:")
        conn.execute("UPDATE task_cache SET fingerprint=?, observations=? WHERE path=?",
                     (json.dumps(old), '{"schema_version": 2}', path))
    conn.commit()
    conn.close()
    delegate_router._TASK_STATE_CACHE.clear()
    assert_rows()
    # Entries from the old schema lack projections and must reload real tasks.
    conn = delegate_router._init_task_cache_db(delegate_router._task_cache_db_path(str(tasks_dir)), ctx)
    conn.execute("UPDATE task_cache SET model_accounting=NULL")
    conn.commit()
    conn.close()
    assert delegate_router._load_task_cache_from_db(str(tasks_dir), ctx) == {}
    delegate_router._TASK_STATE_CACHE.clear()
    assert_rows()


def test_recent_finished_tasks_orders_instants_and_excludes_unsettled(tmp_path, monkeypatch):
    """Defect class: lexical timestamps and nonterminal rows corrupt the recent list before limiting."""
    tasks = tmp_path / "tasks"
    ctx = _pin_tasks_dir(monkeypatch, tasks)
    for task_id, status, finished in (
        ("earlier", "done", "2026-11-12T04:05:00Z"),
        ("later", "done", "2026-11-12T04:05:00.500000+00:00"),
        ("finalize", "needs_finalize", "2026-11-12T04:10:00Z"),
        ("settling", "settling", "2026-11-12T04:11:00Z"),
        ("dead", "running", "2026-11-12T04:12:00Z"),
    ):
        _write_task(tasks / f"{task_id}.json", _task_payload(task_id, status=status, finished_at=finished))
    monkeypatch.setattr(delegate_router, "_pid_alive", lambda pid: False)
    recent = delegate_router.recent_finished_tasks(ctx=ctx, limit=1)
    assert recent["total"] == 2
    assert [t["task_id"] for t in recent["tasks"]] == ["later"]
    assert recent["needs_attention"]["total"] == 3
    assert len(recent["needs_attention"]["tasks"]) == 1
    all_recent = delegate_router.recent_finished_tasks(ctx=ctx)
    assert {t["status"] for t in all_recent["needs_attention"]["tasks"]} == {"needs_finalize", "settling", "zombie"}
    active = delegate_router.active_delegate_tasks(ctx=ctx)
    assert active["total"] == 0
    assert delegate_router.active_delegate_count(ctx=ctx) == 0


def test_accounting_conflict_is_unknown_in_list_and_detail(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    _pin_tasks_dir(monkeypatch, tasks_dir)
    _write_task(tasks_dir / "conflict.json", _task_payload(
        "conflict", model="accounting-model", requested_selector="requested-model",
        observed_model="observed-model", identity_state="observed", observed_model_source="provider-init",
        observed_model_provenance={
            "requested_model": "attempt-model", "invocation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
        model_identity={"actual_model_known": True, "actual_model": "conflicting-model", "source": "provider-init"},
    ))

    listed = client.get("/api/delegate/tasks")
    detail = client.get("/api/delegate/tasks/conflict")

    assert listed.status_code == detail.status_code == 200
    expected = {
        "requested_selector": "requested-model", "observed_model": None,
        "identity_state": "unknown", "observed_model_source": None, "observed_model_provenance": None,
    }
    for record in (listed.json()["tasks"][0], detail.json()["task"]):
        assert record["model"] == "accounting-model"
        assert {key: record[key] for key in expected} == expected


def test_active_snapshot_never_collects_on_requests(tmp_path, monkeypatch):
    """Warm reads survive refresh work; collector failure cannot swallow shutdown cancellation."""
    import asyncio
    import threading
    from contextlib import suppress

    from lu_machinery.api import state_helpers

    tasks = tmp_path / "tasks"
    ctx = _pin_tasks_dir(monkeypatch, tasks)
    _write_task(tasks / "live.json", _task_payload("live", status="spawning", pid=None))
    snapshot = delegate_router.active_snapshot(ctx)
    snapshot.interval = 0.01
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def collect():
        calls.append(True)
        if len(calls) == 1:
            return delegate_router.active_delegate_tasks(ctx=ctx)
        entered.set()
        assert release.wait(5), "refresh was never released"
        return {"total": 0, "tasks": []}
    snapshot.compute = collect
    with _snapshot_client(ctx) as running:
        expected, _ = snapshot.read()
        assert expected["total"] == 1
        def forbidden(*args, **kwargs):
            raise AssertionError("request collected task history")
        monkeypatch.setattr(delegate_router, "active_delegate_tasks", forbidden)
        monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: 1e12)
        try:
            assert entered.wait(5), "periodic refresh never ran"
            for _ in range(2):
                response = running.get("/api/delegate/active")
                assert response.status_code == 200
                assert response.json() == expected
        finally:
            release.set()
    # Shutdown joins an in-progress collector before its stores can close.
    assert snapshot.read()[0] == {"total": 0, "tasks": []}

    async def failing_collection_still_closes():
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def compute():
            calls.append(True)
            if len(calls) == 1:
                return {"retained": True}
            entered.set()
            assert release.wait(5), "shutdown never released the collector"
            raise OSError("collector fails while shutdown joins it")

        failing = state_helpers.PeriodicSnapshot(compute, 0.01)
        lifetime = failing.lifespan()
        await lifetime.__aenter__()
        assert await asyncio.to_thread(entered.wait, 5)
        failing.interval = 60
        closing = asyncio.create_task(lifetime.__aexit__(None, None, None))
        # Deliver shutdown cancellation before making the worker fail.
        await asyncio.sleep(0.01)
        release.set()
        done, _ = await asyncio.wait({closing}, timeout=1)
        try:
            assert closing in done, "collector error swallowed shutdown cancellation"
            assert failing.read()[0] == {"retained": True}
        finally:
            if not closing.done():
                closing.cancel()
            with suppress(asyncio.CancelledError):
                await closing

    asyncio.run(failing_collection_still_closes())


def test_snapshot_age_includes_collection_and_failed_refresh(tmp_path, monkeypatch):
    """Both endpoints expose advancing observation age, including refresh failures."""
    import asyncio

    import pytest
    from fastapi import HTTPException

    from lu_machinery.api import fleet_workers_router, state_helpers
    from lu_machinery.api.monitor_context import fixture_context

    ctx = fixture_context(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: clock[0])
    for snapshot, route, payload in (
        (delegate_router.active_snapshot(ctx), lambda: asyncio.run(delegate_router.delegate_active(ctx)), {"tasks": []}),
        (fleet_workers_router.workers_snapshot(ctx), lambda: fleet_workers_router.get_workers(None, ctx), {None: {}, "": {}}),
    ):
        def fail():
            raise OSError("fixture refresh failed")
        snapshot.compute = fail
        async def failed_startup():
            async with snapshot.lifespan():
                with pytest.raises(HTTPException) as unavailable:
                    snapshot.read()
                assert unavailable.value.status_code == 503
                assert unavailable.value.headers["X-Snapshot-Age-S"] == "unknown"
        asyncio.run(failed_startup())
        def compute():
            clock[0] += 3
            return payload
        snapshot.compute = compute
        snapshot.refresh()
        clock[0] += 7
        response = route()
        assert response.headers["X-Snapshot-Age-S"] == "10.000"
        snapshot.compute = fail
        with pytest.raises(OSError):
            snapshot.refresh()
        clock[0] += 2
        assert snapshot.read()[1] == 12.0
