"""ACPX launcher-copy cleanup logs must not retain caller-controlled names (#10751)."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import time

import pytest

from lu_machinery.agent_runtime.adapters import acpx as acpx_module

_SECRET_COPY = "launch-secret12"
_SECRET_LABEL = "secret-looking-adapter-label"
_SECRET_ERRNO_TEXT = "secret-looking-os-error-text"
_SECRET_REFUSAL = "secret-looking-refusal-text"
_PRIVATE_VALUES = (_SECRET_COPY, "secret12", _SECRET_LABEL, _SECRET_ERRNO_TEXT, _SECRET_REFUSAL)


@pytest.fixture
def copy_base(tmp_path, monkeypatch):
    """A caller-owned copy base holding one secret-named copy directory."""
    base = tmp_path / "copies"
    base.mkdir(mode=0o700)
    (base / _SECRET_COPY).mkdir(mode=0o700)
    lease = os.open(base / _SECRET_COPY / acpx_module._CLAUDE_ACP_LAUNCHER_COPY_LEASE, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(lease)
    monkeypatch.setattr(acpx_module, "_CLAUDE_ACP_LAUNCHER_COPY_BASE", base)
    # The ancestor-trust walk is covered elsewhere; these tests exercise the log calls after it.
    monkeypatch.setattr(
        acpx_module,
        "_open_claude_acp_copy_base",
        lambda path, *, adapter_label: os.open(path, os.O_RDONLY | os.O_DIRECTORY),
    )
    return base


def _only_record(caplog, level):
    records = [
        record
        for record in caplog.records
        if record.name == acpx_module.__name__ and record.levelno == level
    ]
    assert len(records) == 1
    return records[0]


def _assert_constant(record, expected_message):
    assert record.getMessage() == expected_message
    assert record.args == ()
    assert record.exc_info is None
    for value in _PRIVATE_VALUES:
        assert value not in record.getMessage()
        assert value not in repr(vars(record))


def _cleanup(copy_base, caplog):
    with caplog.at_level(logging.DEBUG):
        return acpx_module._remove_claude_acp_copy_dir(_SECRET_COPY, adapter_label=_SECRET_LABEL)


def _sweep(copy_base):
    old = time.time() - acpx_module._CLAUDE_ACP_LAUNCHER_COPY_MAX_AGE_S - 60
    os.utime(copy_base / _SECRET_COPY, (old, old))
    base_fd = os.open(copy_base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        acpx_module._sweep_stale_claude_acp_launcher_copies(base_fd)
    finally:
        os.close(base_fd)


@contextlib.contextmanager
def _leased(copy_base):
    """Another launch holds the secret copy's lease shared."""
    fd = os.open(copy_base / _SECRET_COPY / acpx_module._CLAUDE_ACP_LAUNCHER_COPY_LEASE, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def _fail_removal(monkeypatch):
    def refuse(_fd, _name):
        raise OSError(errno.EACCES, _SECRET_ERRNO_TEXT)

    monkeypatch.setattr(acpx_module, "_remove_claude_acp_copy_entry", refuse)


def test_sweep_keeping_a_leased_copy_logs_a_constant_message(copy_base, caplog):
    with _leased(copy_base), caplog.at_level(logging.DEBUG):
        _sweep(copy_base)
    _assert_constant(
        _only_record(caplog, logging.INFO),
        "Claude ACP launcher sweep kept a stale copy: a launch still holds its lease",
    )
    assert (copy_base / _SECRET_COPY).exists()


def test_sweep_failing_to_remove_a_copy_logs_a_constant_message(copy_base, monkeypatch, caplog):
    (copy_base / _SECRET_COPY / "agent_runtime").mkdir(mode=0o700)
    _fail_removal(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        _sweep(copy_base)
    _assert_constant(
        _only_record(caplog, logging.INFO),
        "Claude ACP launcher sweep could not remove a stale copy under its lease",
    )


_BUSY = (
    "Claude ACP launcher cleanup kept the private launcher copy: a launch still holds its lease; "
    "a later sweep removes it once the lease is free"
)
_REFUSED = (
    "Claude ACP launcher cleanup refused the private launcher copy: it is not provably a leased copy "
    "this adapter made, or its lease could not be locked"
)
_FAILED = "Claude ACP launcher cleanup could not remove the private launcher copy under its lease"


def test_cleanup_refusing_the_copy_base_logs_a_constant_message(copy_base, monkeypatch, caplog):
    def refuse(_base, *, adapter_label):
        raise acpx_module.ClaudeCodeExecutableRefusal(
            f"{adapter_label} {_SECRET_COPY} {_SECRET_REFUSAL}", reason="untrusted_path"
        )

    monkeypatch.setattr(acpx_module, "_open_claude_acp_copy_base", refuse)
    assert _cleanup(copy_base, caplog) == "refused"
    _assert_constant(_only_record(caplog, logging.ERROR), _REFUSED)
    assert (copy_base / _SECRET_COPY).exists()


def test_cleanup_failing_to_open_the_copy_base_logs_a_constant_message(copy_base, monkeypatch, caplog):
    def refuse(_base, *, adapter_label):
        raise OSError(errno.EACCES, _SECRET_ERRNO_TEXT)

    monkeypatch.setattr(acpx_module, "_open_claude_acp_copy_base", refuse)
    assert _cleanup(copy_base, caplog) == "failed"
    _assert_constant(_only_record(caplog, logging.ERROR), _FAILED)


def test_cleanup_leaving_a_leased_copy_in_place_logs_a_constant_message(copy_base, caplog):
    with _leased(copy_base):
        assert _cleanup(copy_base, caplog) == "busy"
    _assert_constant(_only_record(caplog, logging.WARNING), _BUSY)
    assert (copy_base / _SECRET_COPY).exists()


def test_cleanup_of_a_copy_the_caller_does_not_own_logs_a_constant_message(copy_base, monkeypatch, caplog):
    monkeypatch.setattr(acpx_module, "_claude_acp_copy_owned", lambda _stat: False)
    assert _cleanup(copy_base, caplog) == "refused"
    _assert_constant(_only_record(caplog, logging.ERROR), _REFUSED)


def test_cleanup_failing_to_remove_a_copy_logs_a_constant_message(copy_base, monkeypatch, caplog):
    (copy_base / _SECRET_COPY / "agent_runtime").mkdir(mode=0o700)
    _fail_removal(monkeypatch)
    assert _cleanup(copy_base, caplog) == "failed"
    _assert_constant(_only_record(caplog, logging.ERROR), _FAILED)
    assert (copy_base / _SECRET_COPY / acpx_module._CLAUDE_ACP_LAUNCHER_COPY_LEASE).exists()
