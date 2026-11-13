"""Only the Monitor API's claude-account scheduler probes Claude accounts or writes the samples file (#12542)."""

from __future__ import annotations

import threading

import pytest

from lu_machinery.api import claude_account_usage as cau
from lu_machinery.api import subscription_usage


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cau, "_routing_budget_state_dir", lambda: tmp_path)
    # The scheduler gate in force outside pytest, so only ownership decides.
    monkeypatch.setenv("CODEXBAR_PERIODIC_REFRESH", "1")
    return tmp_path


@pytest.fixture
def probes(monkeypatch):
    calls: list[str] = []

    def probe(label, *, timeout_s):
        calls.append(label)
        raise cau._ProbeFailed("probe_error")

    monkeypatch.setattr(cau, "probe_account", probe)
    return calls


def test_non_scheduler_caller_never_probes_or_writes_samples(state_dir, probes, monkeypatch):
    # An exporter or one-liner process: no scheduler thread here.
    monkeypatch.setattr(subscription_usage, "_claude_account_scheduler_thread", None)

    cau.refresh_claude_account_usage()

    assert probes == []
    assert not (state_dir / cau._SAMPLES_FILE).exists()
    assert list(state_dir.iterdir()) == []


def test_non_scheduler_caller_never_persists_a_sample(state_dir, monkeypatch):
    monkeypatch.setattr(subscription_usage, "_claude_account_scheduler_thread", None)

    cau._persist_sample("claude:a", object())  # refused before the sample is read

    assert list(state_dir.iterdir()) == []


def test_scheduler_process_probes_every_label(state_dir, probes, monkeypatch):
    stop = threading.Event()
    owner = threading.Thread(target=stop.wait, daemon=True)
    owner.start()
    monkeypatch.setattr(subscription_usage, "_claude_account_scheduler_thread", owner)
    try:
        cau.refresh_claude_account_usage()
    finally:
        stop.set()
        owner.join(timeout=5)

    assert sorted(probes) == sorted(cau.CLAUDE_SUBSCRIPTION_ACCOUNT_LABELS)



def test_account_scheduler_owns_both_accounts_beside_other_lanes(
    state_dir, probes, monkeypatch
):
    """Other provider schedulers must not suppress the sole account-a reader."""
    stop = threading.Event()
    lane = threading.Thread(target=stop.wait, daemon=True)
    owner = threading.Thread(target=stop.wait, daemon=True)
    lane.start()
    owner.start()
    monkeypatch.setattr(subscription_usage, "_scheduler_thread", lane)
    monkeypatch.setattr(subscription_usage, "_claude_account_scheduler_thread", owner)
    try:
        cau.refresh_claude_account_usage()
        assert sorted(probes) == ["a", "b"]
        assert not (state_dir / cau._SAMPLES_FILE).exists()

        # A restored default-account row belongs to this same scheduler.
        monkeypatch.setattr(
            cau,
            "claude_accounts_snapshot",
            lambda **_kwargs: {
                "claude:a": {"stale": True, "status": "stale"},
                "claude:b": {"stale": False, "status": "ok"},
            },
        )
        assert subscription_usage._claude_account_reading_past_ttl() is True

        class _Restored:
            source_at = 1.0

        cau._reset_for_tests()
        monkeypatch.setattr(
            cau, "_restored_sample", lambda label, entry, moment, limit_s: _Restored()
        )
        monkeypatch.setattr(cau, "_read_samples", lambda _path: {"a": {}, "b": {}})
        assert cau.restore_claude_account_samples(now=1.0) == 0.0
        assert set(cau._last_good) == {"a", "b"}
    finally:
        cau._reset_for_tests()
        stop.set()
        lane.join(timeout=5)
        owner.join(timeout=5)


def test_stale_account_does_not_start_duplicate_scheduler_probe(state_dir, monkeypatch):
    """An HTTP refresh cannot amplify polling while the account scheduler owns the cadence."""

    class Owner:
        def is_alive(self):
            return True

    monkeypatch.setattr(subscription_usage, "_claude_account_scheduler_thread", Owner())
    monkeypatch.setattr(subscription_usage, "_claude_account_startup_pending", False)
    monkeypatch.setattr(subscription_usage, "_claude_account_probe_started_at", None)
    monkeypatch.setattr(subscription_usage, "_claude_account_oneshot_thread", None)
    monkeypatch.setattr(
        subscription_usage, "_claude_account_reading_past_ttl", lambda: True
    )
    subscription_usage.trigger_claude_account_refresh()
    assert subscription_usage._claude_account_oneshot_thread is None
