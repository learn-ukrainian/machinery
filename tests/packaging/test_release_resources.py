"""Release resources must work without a machinery checkout or PYTHONPATH."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARED = "agents_extensions/shared/"


def _run(command, *, cwd, env=None):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """Build once, then install the real wheel into an isolated consumer venv."""
    scratch = tmp_path_factory.mktemp("release-resources")
    source = scratch / "source"
    source.mkdir()
    for name in ("pyproject.toml", "MANIFEST.in"):
        shutil.copy2(REPO / name, source / name)
    for name in ("lu_machinery", "agents_extensions"):
        shutil.copytree(
            REPO / name,
            source / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    # The inherited execution guard owns sitecustomize, as does pip's build
    # isolation. A separate build venv keeps the guard and gives the backend
    # its declared dependencies without competing sitecustomize hooks.
    builder = scratch / "builder"
    venv.EnvBuilder(with_pip=True).create(builder)
    build_python = builder / "bin" / "python"
    requirements = tomllib.loads((source / "pyproject.toml").read_text())["build-system"]["requires"]
    prepared = _run(
        [
            str(build_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            *requirements,
        ],
        cwd=scratch,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    built = _run(
        [
            str(build_python),
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--disable-pip-version-check",
            "--no-deps",
            "--wheel-dir",
            str(scratch / "wheels"),
            str(source),
        ],
        cwd=scratch,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheel = next((scratch / "wheels").glob("lu_machinery-*.whl"))
    consumer = scratch / "consumer"
    venv.EnvBuilder(with_pip=True).create(consumer)
    executable = consumer / "bin" / "python"
    installed = _run(
        [
            str(executable),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-compile",
            "--no-deps",
            str(wheel),
        ],
        cwd=scratch,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    # The deployer uses its declared runtime YAML dependency to lint product skills.
    yaml_installed = _run(
        [str(executable), "-I", "-m", "pip", "install", "--disable-pip-version-check",
         "--only-binary=:all:", "PyYAML==6.0.3"], cwd=scratch,
    )
    assert yaml_installed.returncode == 0, yaml_installed.stdout + yaml_installed.stderr
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LU_", "LEARN_UKRAINIAN_"))}
    env["PYTHONPATH"] = ""
    return scratch, wheel, executable, env


def test_wheel_omission_or_product_leakage(release):
    """Every tracked machinery resource ships with its bytes; no product resource ships."""
    _, wheel, _, _ = release
    tracked = subprocess.check_output(["git", "ls-files", "agents_extensions/shared"], cwd=REPO, text=True).splitlines()
    ownership = json.loads((REPO / "lu_machinery/lint/rename-map.json").read_text())
    # The historical extraction map predates the approved move into fleet rules.
    moves = {
        SHARED + "rules/" + name: "docs/best-practices/" + name
        for name in ("fleet-shared-doctrine.md", "fleet-role-scorecard.md")
    }
    assert all(ownership.get(moves.get(name, name)) in {"machinery", "product"} for name in tracked)
    machinery = {name for name in tracked if ownership[moves.get(name, name)] == "machinery"}
    with zipfile.ZipFile(wheel) as archive:
        shipped = {name for name in archive.namelist() if name.startswith(SHARED)}
        assert shipped == machinery
        assert not any(name.endswith("/__init__.py") for name in shipped)
        for name in machinery:
            assert archive.read(name) == (REPO / name).read_bytes(), name


def test_checkout_dependent_resource_resolution(release):
    """An isolated installed consumer gets source-identical core, task and product bytes."""
    scratch, _, executable, env = release
    product = scratch / "product"
    product.mkdir()
    addendum = product / SHARED / "rules/core-curriculum.md"
    addendum.parent.mkdir(parents=True)
    addendum.write_text("# Selected product curriculum\n", encoding="utf-8")
    script = """
import hashlib, json, os, sys
from lu_machinery.lib import rules_core as r
scopes = ("core", "task:repo-change", "task:review", "task:driver")
result = {}
for scope in scopes:
    sources = r.scope_sources(scope)
    result[scope] = {"sha256": hashlib.sha256(r.assemble(sources).encode()).hexdigest(),
                     "origins": [str(r.source_path(s).resolve()) for s in sources]}
result["content"] = r.core_text("content", root=__import__("pathlib").Path(sys.argv[1]))
result["shared_without_product"] = str(r.source_path("agents_extensions/shared").resolve())
os.environ["LU_PRODUCT_ROOT"] = sys.argv[1]
result["shared_with_product"] = str(r.source_path("agents_extensions/shared").resolve())
result["core_with_product"] = r.core_text()
result["shell"] = str(__import__("pathlib").Path(r.__file__).with_suffix(".sh"))
os.environ.pop("LU_PRODUCT_ROOT")
try:
    r.core_text("content")
except r.RulesCoreMissing:
    result["no_product"] = "refused"
else:
    result["no_product"] = "accepted"
print(json.dumps(result))
"""
    ran = _run([str(executable), "-I", "-c", script, str(product)], cwd=scratch, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    data = json.loads(ran.stdout)
    source_sets = {
        "core": ["rules/core.md"],
        "task:repo-change": [
            "rules/critical-rules.md",
            "rules/dispatch-must-use-worktree.md",
            "rules/workflow.md",
        ],
        "task:review": ["skills/local-code-review/SKILL.md"],
        "task:driver": [
            "rules/fleet-comms-coordination.md",
            "rules/fleet-driver-routing.md",
            "rules/fleet-shared-doctrine.md",
            "rules/fleet-role-scorecard.md",
            "skills/drive-epic/SKILL.md",
        ],
    }
    for scope, names in source_sets.items():
        bodies = [(REPO / SHARED / name).read_text().rstrip() + "\n" for name in names]
        expected = "\n\n---\n\n".join(bodies).rstrip() + "\n"
        assert data[scope]["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        assert all(Path(origin).is_relative_to(executable.parent.parent) for origin in data[scope]["origins"])
    core = (REPO / SHARED / "rules/core.md").read_text().rstrip() + "\n"
    assert data["content"] == core + "\n\n---\n\n# Selected product curriculum\n"
    assert data["no_product"] == "refused"
    assert data["shared_with_product"] == data["shared_without_product"]
    assert Path(data["shared_without_product"]).is_relative_to(executable.parent.parent)
    assert data["core_with_product"] == core
    expected_block = (
        f'<rules-core seat="core" sha256="{hashlib.sha256(core.encode()).hexdigest()}">\n{core}</rules-core>'
    )
    shell_env = {**env, "TMPDIR": str(scratch / "shell-tmp")}
    Path(shell_env["TMPDIR"]).mkdir()
    helper = scratch / "launcher-helper"
    helper.mkdir()
    (helper / ".venv").symlink_to(executable.parent.parent, target_is_directory=True)
    shell_script = r"""if [ "$3" = launcher ]; then
  source "${1%rules_core.sh}launcher_core.sh"
  LC_DURABLE_HELPER_ROOT="$4"
  LC_MODE=worker LC_PROVIDER=claude LC_DRY_RUN=0
  launcher_load_rules_core
else
  source "$1"
  rules_core_load "$2" "" claude || exit 1
fi
case "$3" in
  block|launcher) printf '%s' "$LC_RULES_CORE" ;;
  toml) rules_core_toml "$2" ;;
esac
"""
    for rendering in ("block", "toml", "launcher"):
        shell = _run(
            ["bash", "-c", shell_script, "rules-shell", data["shell"], str(executable), rendering, str(helper)],
            cwd=scratch,
            env=shell_env,
        )
        assert shell.returncode == 0, shell.stdout + shell.stderr
        if rendering in {"block", "launcher"}:
            assert shell.stdout == expected_block
        elif rendering == "toml":
            assert tomllib.loads("value = " + shell.stdout)["value"] == expected_block


def test_missing_resource_admission_bypass(release):
    """A missing installed core refuses before a caller creates its side-effect marker."""
    scratch, _, executable, env = release
    locate = _run(
        [
            str(executable),
            "-I",
            "-c",
            "from lu_machinery.lib.rules_core import source_path, CORE_REL; print(source_path(CORE_REL))",
        ],
        cwd=scratch,
        env=env,
    )
    assert locate.returncode == 0, locate.stderr
    core = Path(locate.stdout.strip())
    saved = core.read_bytes()
    marker = scratch / "started"
    core.unlink()
    try:
        ran = _run(
            [
                str(executable),
                "-I",
                "-c",
                "from lu_machinery.lib.rules_core import require_core; require_core(); "
                "from pathlib import Path; Path('started').touch()",
            ],
            cwd=scratch,
            env=env,
        )
        assert ran.returncode != 0
        assert "RulesCoreMissing" in ran.stderr
        assert "core.md" in ran.stderr
        assert not marker.exists()
    finally:
        core.write_bytes(saved)


def test_wheel_only_deployer_isolation_idempotency_and_false_green(release):
    """A real installed wheel deploys a non-curriculum consumer and detects tampering."""
    scratch, wheel, executable, env = release
    consumer = scratch / "agent-consumer"
    consumer.mkdir()
    subprocess.run(["git", "init", "-q", str(consumer)], check=True)
    source = consumer / "agents_extensions/shared"
    skill = source / "skills/consumer/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: consumer\ndescription: Consumer example\n---\nRead this example.\n")
    with zipfile.ZipFile(wheel) as archive:
        inventory = json.loads(archive.read("lu_machinery/agent_deploy/release.v1.json"))
    descriptor = {"providers": ["claude", "codex", "agy", "gemini"], "sources": {
        "shared": {"path": "agents_extensions/shared", "required": True},
        "codex": {"path": "agents_extensions/codex", "required": True},
        "gemini": {"path": "gemini_extensions", "required": False}}, "preserve": []}
    (consumer / "agents_extensions/deployment.v1.json").write_text(json.dumps(descriptor))
    # Configurations are input fixtures. Independent assertions below never use
    # the inventory to derive expected output paths, bytes, modes, or counts.
    for provider, configs in inventory["required_hooks"].items():
        for destination, requirements in configs.items():
            data = {}
            for requirement in requirements:
                node = data
                for key in requirement["path"][:-1]:
                    node = node.setdefault(key, {})
                node.setdefault(requirement["path"][-1], []).append(
                    {**requirement["group"], "hooks": [
                        {**hook, "statusMessage": "Checking consumer session state"}
                        for hook in requirement["hooks"]]})
            path = source / "settings.json" if provider == "claude" else consumer / "agents_extensions/codex/hooks.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
    command = [str(executable), "-I", "-m", "lu_machinery.agent_deploy", "--project-root", str(consumer)]
    first = _run(command, cwd=scratch, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (consumer / ".agents/skills/consumer/SKILL.md").read_text() == "---\nname: consumer\ndescription: Consumer example\n---\nRead this example.\n"
    assert not (consumer / ".claude/rules/core.md").exists()
    assert not (consumer / ".codex/skills").exists()
    codex_hooks = json.loads((consumer / ".codex/hooks.json").read_text())["hooks"]
    assert all(hook["statusMessage"] == "Checking consumer session state"
               for groups in codex_hooks.values() for group in groups for hook in group["hooks"])
    hook = consumer / ".claude/hooks/enforce-venv.sh"
    assert hook.stat().st_mode & 0o777 == 0o755
    assert (consumer / ".agent/rules/core-part-01.md").read_text().startswith("---\ntrigger: always_on\n")
    def snapshot():
        return {str(p.relative_to(consumer)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in consumer.rglob("*") if p.is_file() and ".git" not in p.parts}
    before = snapshot()
    second = _run(command, cwd=scratch, env=env)
    assert second.returncode == 0 and "0 changes" in second.stdout, second.stdout + second.stderr
    checked = _run([*command, "--check"], cwd=scratch, env=env)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert snapshot() == before
    # Edit both the output AND the recorded manifest. Check must reconstruct
    # the expected bytes from the installed release instead of trusting state.
    hook.write_text("#!/bin/sh\nexit 0\n")
    state = consumer / ".deploy-state/agent-deploy.v1.json"
    data = json.loads(state.read_text())
    data["files"][".claude/hooks/enforce-venv.sh"]["sha256"] = hashlib.sha256(hook.read_bytes()).hexdigest()
    state.write_text(json.dumps(data))
    tampered = snapshot()
    drift = _run([*command, "--check"], cwd=scratch, env=env)
    assert drift.returncode == 1, drift.stdout + drift.stderr
    assert snapshot() == tampered
    # No namespace/product copy can supply a missing installed resource.
    locate = _run([str(executable), "-I", "-c",
        "from importlib.resources import files; print(files('lu_machinery').joinpath('..','agents_extensions','shared','rules','core.md'))"], cwd=scratch, env=env)
    assert locate.returncode == 0, locate.stderr
    core = Path(locate.stdout.strip())
    blob = core.read_bytes()
    core.unlink()
    try:
        missing = _run(command, cwd=scratch, env=env)
        assert missing.returncode == 2, missing.stdout + missing.stderr
        assert snapshot() == tampered
    finally:
        core.write_bytes(blob)
