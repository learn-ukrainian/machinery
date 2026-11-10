"""Installed CI must not substitute wheel census data for the selected product.

Defect classes: cross-product defaults, missing-input success, and a stack report
publishing names from the machinery release instead of the selected product.
"""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from tests.helpers.wheel import install_release, release_wheel

REPO = Path(__file__).resolve().parents[2]
FILES = [f"tests/test_{name}.py" for name in ("alpha", "beta", "gamma", "delta")]
CENSUS = (
    "ci/history-tests.txt",
    "ci/large_files_allowlist.txt",
    "ci/pr-affected-guards.txt",
    "ci/pytest-file-durations.json",
    "config/package_boundary.yaml",
    "lint/boundary_baseline.json",
    "lint/mypy_baseline.json",
)


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    scratch = tmp_path_factory.mktemp("product-ci-wheel")
    wheel = release_wheel(REPO, scratch)
    return install_release(wheel, scratch / "consumer", dependencies=True), wheel


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT, timeout=30
    ).strip()


def _product(scratch: Path, variant: str, *, missing: str | None = None) -> tuple[Path, dict[str, str]]:
    root = scratch / variant
    root.mkdir()
    guard = FILES[0 if variant == "alpha" else 1]
    weights = (100, 1, 50, 2) if variant == "alpha" else (1, 100, 2, 50)
    data = {
        "scripts/__init__.py": "",
        "scripts/runner.py": "VALUE: str = 1\n",
        "scripts/ci/components.json": json.dumps({"product": variant}),
        "scripts/ci/pr-affected-guards.txt": guard + "\n",
        "scripts/ci/pytest-file-durations.json": json.dumps(dict(zip(FILES, weights, strict=True))),
        "scripts/ci/history-tests.txt": guard + "\n",
        "scripts/ci/large_files_allowlist.txt": f"data/{variant}.bin\t5242881\tfixture product\n",
        "scripts/lint/mypy_baseline.json": json.dumps({"schema": "mypy-ratchet-baseline.v1", "entries": [{
            "path": "scripts/runner.py", "code": "assignment",
            "message": 'Incompatible types in assignment (expression has type "int", variable has type "str")',
        }]}),
        "scripts/lint/boundary_baseline.json": json.dumps({"schema": "boundary-ratchet-baseline.v1", "entries": []}),
        "scripts/config/package_boundary.yaml": (
            "schema: package-boundary.v1\npackages: {}\nmodules:\n"
            "  __init__.py: {role: machinery}\n  runner.py: {role: machinery}\n"
        ),
        "scripts/config/stack_pins.yaml": (
            f"pins:\n  - name: product-{variant}\n    pinned: 1.2.3\n    source: fixture\n"
        ),
        "pyproject.toml": '[tool.mypy]\npython_version = "3.12"\n',
        **{name: "def test_ok():\n    assert True\n" for name in FILES},
    }
    for name, content in data.items():
        if name == f"scripts/{missing}":
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "CI fixture")
    _git(root, "config", "user.email", "ci@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.hooksPath", "/dev/null")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "product inputs")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-b", "feature")
    (root / "scripts/runner.py").write_text("VALUE: str = 2\n", encoding="utf-8")
    _git(root, "commit", "-am", "product change")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "main")
    _git(root, "merge", "--no-ff", "feature", "-m", "PR candidate")
    event = scratch / f"{variant}-event.json"
    event.write_text(json.dumps({
        "action": "opened", "repository": {"default_branch": "main"},
        "pull_request": {"number": 1, "base": {"sha": base}, "head": {"sha": head}, "body": ""},
    }), encoding="utf-8")
    return root, {
        "LU_PRODUCT_ROOT": str(root), "EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": _git(root, "rev-parse", "HEAD"),
        "GITHUB_REF": "refs/pull/1/merge",
    }


def _run(python: Path, scratch: Path, env: dict[str, str], *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LU_", "LEARN_UKRAINIAN_", "GITHUB_"))}
    return subprocess.run(
        [str(python), "-I", "-B", *args], cwd=scratch, env={**clean, **env},
        input=stdin, text=True, capture_output=True, timeout=90,
    )


def test_wheel_cannot_supply_checkout_ci_census(installed: tuple[Path, Path]) -> None:
    """Bundled defaults must not silently replace deleted product census inputs."""
    with zipfile.ZipFile(installed[1]) as wheel:
        names = set(wheel.namelist())
    assert not {f"lu_machinery/{name}" for name in CENSUS} & names
    # Host drift probes still need release-owned pins. CI must ignore this copy.
    assert "lu_machinery/config/stack_pins.yaml" in names
    assert "lu_machinery/ci/components.json" in names
    assert "lu_machinery/lint/rename-map.json" in names
    assert "agents_extensions/shared/rules/workflow.md" in names


@pytest.mark.parametrize("variant", ["alpha", "beta"])
def test_product_defaults_control_shards_guards_and_stack_names(
    tmp_path: Path, installed: tuple[Path, Path], variant: str,
) -> None:
    """Wrong product defaults or rejected relative root flags change CI behavior."""
    python, _wheel = installed
    root, env = _product(tmp_path, variant)
    expected = (
        [["alpha"], ["gamma"], ["beta", "delta"]] if variant == "alpha"
        else [["beta"], ["delta"], ["alpha", "gamma"]]
    )
    for shard, members in enumerate(expected, 1):
        result = _run(python, tmp_path, env, "-m", "lu_machinery.ci.split_tests", "split",
                      "--shard", str(shard), "--of", "3", stdin="\n".join(FILES) + "\n")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "".join(f"tests/test_{name}.py\n" for name in members)
    plan = _run(python, tmp_path, env, "-c",
                "import json; from lu_machinery.ci.pr_affected_tests import plan; "
                "print(json.dumps(plan('HEAD^1', 'HEAD', importers=False)))")
    assert plan.returncode == 0, plan.stderr
    assert json.loads(plan.stdout)["run_files"] == [f"tests/test_{variant}.py"]
    collect = _run(python, tmp_path, env, "-m", "lu_machinery.ci.stack_currency_report", "collect",
                   "--out", str(tmp_path / "report.json"))
    assert collect.returncode == 0, collect.stdout + collect.stderr
    assert [row["name"] for row in json.loads((tmp_path / "report.json").read_text())["rows"]] == [f"product-{variant}"]
    # Include a second product and a real bundled component as hostile report names.
    (tmp_path / "report.json").write_text(json.dumps({"rows": [
        {"name": name, "pinned": "1.2.3", "latest": "2.0.0", "source": "1.0.0"}
        for name in ("product-alpha", "product-beta", "python")
    ]}), encoding="utf-8")
    rendered = _run(python, tmp_path, env, "-m", "lu_machinery.ci.stack_currency_report", "render",
                    "--report", str(tmp_path / "report.json"), "--body-out", str(tmp_path / "body.md"))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    body = (tmp_path / "body.md").read_text()
    assert f"| product-{variant} |" in body
    assert f"| product-{'beta' if variant == 'alpha' else 'alpha'} |" not in body
    assert "| python |" not in body
    unavailable = str(tmp_path / "absent")
    refused = _run(python, tmp_path, {**env, "LU_PRODUCT_ROOT": unavailable},
                   "-m", "lu_machinery.ci.stack_currency_report", "collect", "--out", "absent-report.json")
    assert refused.returncode == 1
    assert unavailable not in refused.stdout + refused.stderr
    for module in ("mypy_ratchet", "boundary_ratchet"):
        result = _run(python, tmp_path, env, "-m", f"lu_machinery.lint.{module}")
        assert result.returncode == 0, result.stdout + result.stderr
        explicit = _run(python, tmp_path, {**env, "LU_PRODUCT_ROOT": str(tmp_path / "absent")},
                        "-m", f"lu_machinery.lint.{module}", "--root", variant)
        assert explicit.returncode == 0, explicit.stdout + explicit.stderr
    guard = _run(python, tmp_path, env, "-m", "lu_machinery.ci.check_large_files", "--changed-vs-base", "HEAD^1")
    assert guard.returncode == 0, guard.stdout + guard.stderr
    guard = _run(python, tmp_path, {**env, "LU_PRODUCT_ROOT": str(tmp_path / "absent")},
                 "-m", "lu_machinery.ci.check_large_files", "--changed-vs-base", "HEAD^1", "--repo-root", variant)
    assert guard.returncode == 0, guard.stdout + guard.stderr
    size = _run(python, tmp_path, env, "-m", "lu_machinery.ci.pr_size_warning",
                "--base", _git(root, "rev-parse", "HEAD^1"), "--head", env["GITHUB_SHA"])
    assert size.returncode == 0, size.stderr
    assert json.loads(size.stdout.splitlines()[0])["status"] == "measured"
    assert json.loads(size.stdout.splitlines()[0])["changed_non_test_lines"] == 2
    size = _run(python, tmp_path, {**env, "LU_PRODUCT_ROOT": str(tmp_path / "absent")},
                "-m", "lu_machinery.ci.pr_size_warning", "--repo", variant)
    assert size.returncode == 0, size.stderr
    assert json.loads(size.stdout.splitlines()[0])["status"] == "measured"
    size = _run(python, tmp_path, env, "-m", "lu_machinery.ci.pr_size_warning",
                "--event-path", str(tmp_path / "absent-event.json"))
    assert size.returncode == 0, size.stderr
    assert json.loads(size.stdout.splitlines()[0])["status"] == "unknown"


@pytest.mark.parametrize("variant", ["alpha", "beta"])
@pytest.mark.parametrize(("missing", "module", "arguments", "diagnostic"), [
    ("ci/pr-affected-guards.txt", "ci.pr_affected_tests", [], "stage=test_plan"),
    ("ci/pytest-file-durations.json", "ci.pr_affected_tests", [], "stage=test_plan"),
    ("ci/pytest-file-durations.json", "ci.split_tests", ["split", "--shard", "1", "--of", "3"], "FileNotFoundError"),
    ("ci/history-tests.txt", "ci.split_tests", ["split", "--shard", "1", "--of", "3"], "FileNotFoundError"),
    ("ci/large_files_allowlist.txt", "ci.check_large_files", ["--changed-vs-base", "HEAD^1"], "Allowlist file not found"),
    ("lint/mypy_baseline.json", "lint.mypy_ratchet", [], "mypy ratchet could not run"),
    ("lint/boundary_baseline.json", "lint.boundary_ratchet", [], "boundary ratchet could not run"),
    ("config/package_boundary.yaml", "lint.boundary_ratchet", [], "boundary ratchet could not run"),
    ("config/stack_pins.yaml", "ci.stack_currency_report", ["collect", "--out", "missing-report.json"], "stack pins file could not be read"),
    ("config/stack_pins.yaml", "ci.stack_currency_report", ["render", "--report", "report.json", "--body-out", "missing-body.md"], "stack pins file could not be read"),
])
def test_deleted_product_data_never_uses_wheel_defaults(
    tmp_path: Path, installed: tuple[Path, Path], variant: str,
    missing: str, module: str, arguments: list[str], diagnostic: str,
) -> None:
    """Missing required census inputs must fail even with a real installed wheel."""
    _root, env = _product(tmp_path, variant, missing=missing)
    (tmp_path / "report.json").write_text('{"rows": []}', encoding="utf-8")
    result = _run(installed[0], tmp_path, env, "-m", f"lu_machinery.{module}", *arguments,
                  stdin="\n".join(FILES) + "\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert diagnostic in result.stdout + result.stderr
