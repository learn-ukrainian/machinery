"""Installed shell resources must never mistake site-packages for a product checkout.

External execution boundaries are probes: no provider, Docker daemon, systemd
manager or real data volume is touched. The wheel itself is never rewritten.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.wheel import install_release, release_wheel

REPO = Path(__file__).resolve().parents[1]
PRODUCT_MEMBERS = (
    "storage/data_volume_guard.sh",
    "orchestration/run_scheduled_backup.sh",
    "orchestration/run_project_state_reporter.sh",
    "ops/lu_slice_apply.sh",
    "ops/smoketest_bridge_stdout_only.sh",
    "ci/linux_fs_parity.sh",
)
UUID = "11111111-2222-3333-4444-555555555555"


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    scratch = tmp_path_factory.mktemp("installed-shell-roots")
    python = install_release(release_wheel(REPO, scratch), scratch / "venv", dependencies=True)
    result = subprocess.run(
        [str(python), "-I", "-c", "import lu_machinery; print(lu_machinery.__path__[0])"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    package = Path(result.stdout.strip())
    assert package.is_relative_to(scratch / "venv")
    return package


@dataclass
class Probe:
    root: Path
    env: dict[str, str]
    log: Path

    def run(self, installed: Path, member: str) -> subprocess.CompletedProcess[str]:
        resource = str(installed / member)
        args = ["bash", "--noprofile", "--norc", resource]
        args += {
            "storage/data_volume_guard.sh": ["--status"],
            "orchestration/run_scheduled_backup.sh": ["retention"],
            "ops/lu_slice_apply.sh": ["apply"],
        }.get(member, [])
        return subprocess.run(
            args, cwd=self.root.parent, env=self.env, input="{}", capture_output=True,
            text=True, timeout=30,
        )


@pytest.fixture
def probe(tmp_path: Path, installed: Path) -> Probe:
    root = tmp_path / "product checkout"
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith((
        "GIT_", "LU_", "LEARN_UKRAINIAN_", "LC_", "CLAUDE_", "SESSION_",
    )) and k not in {"BASH_ENV", "PROJECT_DIR", "PYTHONPATH"}}
    env.update(LU_PRODUCT_ROOT=str(root), TMPDIR=str(tmp_path), SHELL_PROBE_LOG=str(tmp_path / "calls"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], env=env, check=True, capture_output=True)
    log = Path(env["SHELL_PROBE_LOG"])
    # Log the interpreter and cwd without running a provider or reporter.
    executable(root / ".venv/bin/python", f'''#!{sys.executable}
import json, os, sys
with open(os.environ["SHELL_PROBE_LOG"], "a") as out:
    out.write(json.dumps({{"python": sys.argv[0], "cwd": os.getcwd(), "args": sys.argv[1:]}}) + "\\n")
print(os.environ.get("SHELL_PROBE_REPLY", "{{}}"))
''')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    executable(root / "scripts/backup-data.sh", '#!/bin/bash\nprintf "backup=%s args=%s\\n" "$0" "$*" >> "$SHELL_PROBE_LOG"\n')
    # Redirect only the host-specific UUID file constant at the shell boundary.
    # DEBUG runs the original, installed guard unchanged, including its real
    # UUID parsing and findmnt target selection.
    uuid_file = tmp_path / "volume.uuid"
    uuid_file.write_text(UUID + "\n")
    bash_env = tmp_path / "bash-env"
    bash_env.write_text('trap \'if [[ ${UUID_FILE:-} == /etc/learn-ukrainian/data-volume.uuid ]]; then UUID_FILE="$TEST_UUID_FILE"; fi\' DEBUG\n')
    env.update(BASH_ENV=str(bash_env), TEST_UUID_FILE=str(uuid_file))
    executable(bindir / "findmnt", f'#!/bin/bash\nprintf "mount=%s\\n" "${{@: -1}}" >> "$SHELL_PROBE_LOG"\necho "{UUID} /dev/probe"\n')
    executable(bindir / "docker", '#!/bin/bash\nprintf "docker=%s\\n" "$*" >> "$SHELL_PROBE_LOG"\n')
    # Give the slice helper a fake manager and kernel readback, all in scratch.
    cg = tmp_path / "cgroup"
    cg.mkdir()
    for name, value in {"current": 1, "high": 10, "max": 20, "swap.max": 0}.items():
        (cg / f"memory.{name}").write_text(str(value))
    units = tmp_path / "units"
    (units / "lu.slice.d").mkdir(parents=True)
    (units / "lu.slice.d/10-limits.conf").write_text("probe")
    source = root / "packaging/systemd/lu.slice"
    source.parent.mkdir(parents=True)
    source.write_text("[Slice]\nDescription=selected product checkout\n")
    env.update(LU_SLICE_CGROUP=str(cg), LU_SLICE_UNIT_DIR=str(units),
               LU_SLICE_CONTROL_DIR=str(tmp_path / "control"), LU_SLICE_RUNTIME_CONTROL_DIR=str(tmp_path / "runtime"))
    executable(bindir / "systemctl", '#!/bin/bash\nif [[ $2 == show ]]; then\nprintf "MemoryHigh=10\\nMemoryMax=20\\nMemorySwapMax=0\\nDropInPaths=%s/lu.slice.d/10-limits.conf\\n" "$LU_SLICE_UNIT_DIR"\nfi\n')
    return Probe(root, env, log)


@pytest.mark.parametrize("member", PRODUCT_MEMBERS)
@pytest.mark.parametrize("configuration", ["unset", "missing-directory"])
def test_installed_product_root_refusal_prevents_site_packages_fallback(
    installed: Path, probe: Probe, member: str, configuration: str,
) -> None:
    if configuration == "unset":
        probe.env.pop("LU_PRODUCT_ROOT")
    else:
        probe.env["LU_PRODUCT_ROOT"] = str(probe.root / "missing")
    result = probe.run(installed, member)
    assert result.returncode != 0, result.stdout
    assert result.stderr.splitlines() == [
        "Error: product root requires an absolute existing directory (set LU_PRODUCT_ROOT or pass an explicit root)"
    ]
    assert not probe.log.exists(), "unconfigured consumer executed a downstream operation"


@pytest.mark.parametrize("member", PRODUCT_MEMBERS)
def test_installed_product_root_targets_configured_checkout(
    installed: Path, probe: Probe, member: str,
) -> None:
    if member == "ops/smoketest_bridge_stdout_only.sh":
        probe.env["SHELL_PROBE_REPLY"] = "PARSER_TEST_OK 9.5/10"
    result = probe.run(installed, member)
    assert result.returncode == 0, result.stdout + result.stderr
    if member == "storage/data_volume_guard.sh":
        assert probe.log.read_text() == f"mount={probe.root}/data\n"
        assert result.stdout.strip() == f"data: volume {UUID}"
    elif member == "orchestration/run_scheduled_backup.sh":
        assert probe.log.read_text() == f"backup={probe.root}/scripts/backup-data.sh args=retention --execute\n"
    elif member == "ops/lu_slice_apply.sh":
        assert (Path(probe.env["LU_SLICE_UNIT_DIR"]) / "lu.slice").read_text() == "[Slice]\nDescription=selected product checkout\n"
    elif member == "ci/linux_fs_parity.sh":
        assert f"type=bind,src={probe.root},dst=/workspace,readonly" in probe.log.read_text()
    else:
        calls = [json.loads(line) for line in probe.log.read_text().splitlines()]
        assert calls
        assert all(call["python"] == str(probe.root / ".venv/bin/python") for call in calls)
        assert calls[0]["cwd"] == str(probe.root)
        assert "-I" in calls[0]["args"]


@pytest.mark.parametrize("value", ["", "relative", "/does-not-exist-shell-product-root"])
def test_product_selector_rejects_invalid_configuration(installed: Path, probe: Probe, value: str) -> None:
    probe.env["LU_PRODUCT_ROOT"] = value
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; project_product_root_resolve', "selector", str(installed / "lib/project_interpreter.sh")],
        env=probe.env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1


def test_explicit_caller_product_root_wins_over_environment(installed: Path, probe: Probe) -> None:
    probe.env["LU_PRODUCT_ROOT"] = str(probe.root / "missing")
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; project_product_root_resolve "$2"',
         "selector", str(installed / "lib/project_interpreter.sh"), str(probe.root)],
        env=probe.env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{probe.root}\n"
    assert result.stderr == ""
