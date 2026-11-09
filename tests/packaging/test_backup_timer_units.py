"""Backup systemd user units carry the required contract and pass verify."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from lu_machinery.orchestration import install_backup_timer
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")


def test_installer_accepts_primary_checkout_and_rejects_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../primary/.git/worktrees/linked\n", encoding="utf-8")
    monkeypatch.setattr(install_backup_timer, "verify_units", lambda *_args, **_kwargs: "verified")
    monkeypatch.setattr(install_backup_timer, "preview", lambda *_args: 0)

    with pytest.raises(install_backup_timer.InstallError, match="must be the primary checkout"):
        install_backup_timer.main(["--repo-root", str(linked), "--unit-dir", str(tmp_path / "units")])

    # Retirement refusal must precede rendering, writes, verification and service actions.
    boundary = Mock(side_effect=AssertionError("enable request reached a side effect"))
    monkeypatch.setattr(install_backup_timer, "render_units", boundary)
    monkeypatch.setattr(install_backup_timer, "verify_units", boundary)
    monkeypatch.setattr(install_backup_timer, "open_unit_dir", boundary)
    monkeypatch.setattr(install_backup_timer, "systemctl_user", boundary)
    with pytest.raises(install_backup_timer.InstallError, match=r"retired.*refused"):
        install_backup_timer.main(["--repo-root", str(primary), "--apply", "--enable"])
    with pytest.raises(install_backup_timer.InstallError, match=r"retired.*refused"):
        install_backup_timer.apply({}, tmp_path / "units", enable=True)
    boundary.assert_not_called()
    assert not (tmp_path / "units").exists()


def test_installer_writes_owner_only_units_and_repairs_existing_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    existing = unit_dir / "learn-ukrainian-backup.service"
    existing.write_text("unchanged", encoding="utf-8")
    existing.chmod(0o644)
    systemctl = Mock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(install_backup_timer, "systemctl_user", systemctl)

    assert install_backup_timer.apply({existing.name: "unchanged"}, unit_dir, enable=False) == 0
    assert existing.stat().st_mode & 0o777 == 0o600

    assert install_backup_timer.apply({"learn-ukrainian-backup.timer": "new"}, unit_dir, enable=False) == 0
    assert (unit_dir / "learn-ukrainian-backup.timer").stat().st_mode & 0o777 == 0o600
    assert existing.read_text() == "unchanged"
    assert (unit_dir / "learn-ukrainian-backup.timer").read_text() == "new"
    assert systemctl.call_args_list == [
        call("daemon-reload"),
        call("disable", "--now", "learn-ukrainian-backup.timer", "learn-ukrainian-backup-retention.timer"),
        call("daemon-reload"),
        call("disable", "--now", "learn-ukrainian-backup.timer", "learn-ukrainian-backup-retention.timer"),
    ]
    assert "next step: systemctl --user enable" not in capsys.readouterr().out

    systemctl.reset_mock()
    systemctl.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="disable denied"),
    ]
    with pytest.raises(install_backup_timer.InstallError, match="systemctl --user disable failed: disable denied"):
        install_backup_timer.apply({existing.name: "unchanged"}, unit_dir, enable=False)
    assert systemctl.call_args_list == [
        call("daemon-reload"),
        call("disable", "--now", "learn-ukrainian-backup.timer", "learn-ukrainian-backup-retention.timer"),
    ]
    assert existing.read_text() == "unchanged"
    assert existing.stat().st_mode & 0o777 == 0o600

