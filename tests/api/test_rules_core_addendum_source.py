"""Rules loaders keep fleet policy and product curriculum in their own trees (#12819).

Fleet rules come only from the installed release (machinery #126, which removed the
rules-directory selector), so CORE is the shipped core text, and the decoy curriculum file
sits in the directory the callers name as their machinery or API project root.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.common import repo_root
from lu_machinery.lib import rules_core
from lu_machinery.orchestration import curriculum_lifecycle_pilot, prompt_contracts

CORE = rules_core.core_text("core")
ADDENDUM = "# PRODUCT CURRICULUM\n"
DECOY = "# MACHINERY DECOY\n"


def _product(root: Path, addendum: str | None = ADDENDUM) -> Path:
    marker = root / repo_root.PRODUCT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("product: fixture\n", encoding="utf-8")
    target = root / rules_core.CONTENT_ADDENDUM_REL
    target.parent.mkdir(parents=True)
    if addendum is not None:
        target.write_text(addendum, encoding="utf-8")
    return root


@pytest.fixture
def split(tmp_path: Path, monkeypatch):
    machinery = tmp_path / "machinery"
    rules = machinery / rules_core.RULES_DIR_REL
    rules.mkdir(parents=True)
    (rules / "core-curriculum.md").write_text(DECOY, encoding="utf-8")
    product = _product(tmp_path / "product")
    monkeypatch.setenv(repo_root.PRODUCT_ROOT_ENV, str(product))
    monkeypatch.setenv("AGENT_NO_TELEMETRY_FOOTER", "1")
    monkeypatch.setenv("LU_RULES_SEAT", "content")
    return machinery, product


def _cli(*args: str):
    return subprocess.run(
        [sys.executable, str(repo_root.machinery_path("lib/rules_core.py")), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _shell(machinery: Path, form: str):
    # #126: the shell loader takes no rules root (rules_core_load PYTHON LANE PROVIDER).
    script = """source "$1/lib/rules_core.sh"
    rules_core_load "$2" '' codex || exit 1
    case "$4" in
      block) printf '%s' "$LC_RULES_CORE" ;;
      toml) rules_core_toml "$2" ;;
    esac"""
    return subprocess.run(
        ["bash", "-c", script, "bash", str(repo_root.machinery_root()), sys.executable, str(machinery), form],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _client(machinery: Path):
    from lu_machinery.api import rules_router
    from lu_machinery.api.monitor_context import get_ctx

    app = FastAPI()
    app.include_router(rules_router.router, prefix="/api/rules")
    app.dependency_overrides[get_ctx] = lambda: SimpleNamespace(roots=SimpleNamespace(project_root=machinery))
    return TestClient(app)


def test_python_uses_product_not_machinery(split):
    """Wrong-source defect: a decoy machinery addendum must never enter a content seat."""
    assert rules_core.core_text("content") == CORE + rules_core.FILE_SEP + ADDENDUM
    assert rules_core.source_path(rules_core.CONTENT_ADDENDUM_REL) == split[1] / rules_core.CONTENT_ADDENDUM_REL


def test_explicit_product_wins(split, tmp_path):
    """Precedence defect: a caller's explicit product overrides LU_PRODUCT_ROOT."""
    explicit = _product(tmp_path / "explicit", "# EXPLICIT PRODUCT\n")
    expected = CORE + rules_core.FILE_SEP + "# EXPLICIT PRODUCT\n"
    assert rules_core.core_text("content", explicit) == expected
    result = _cli("--root", str(explicit), "--seat", "content", "--format", "text")
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize("form", ["text", "block", "toml", "json"])
def test_cli_default_keeps_configured_product(split, form):
    """Default-shadowing defect: CLI machinery defaults must not override LU_PRODUCT_ROOT."""
    result = _cli("--seat", "content", "--format", form)
    assert result.returncode == 0, result.stderr
    if form == "json":
        assert (
            json.loads(result.stdout)["sha256"]
            == hashlib.sha256((CORE + rules_core.FILE_SEP + ADDENDUM).encode()).hexdigest()
        )
    else:
        assert "PRODUCT CURRICULUM" in result.stdout
        assert "MACHINERY DECOY" not in result.stdout


@pytest.mark.parametrize("form", ["block", "toml"])
def test_shell_default_keeps_configured_product(split, form):
    """Default-shadowing defect: all shell renders preserve the configured product."""
    result = _shell(split[0], form)
    assert result.returncode == 0, result.stderr
    assert "PRODUCT CURRICULUM" in result.stdout
    assert "MACHINERY DECOY" not in result.stdout


@pytest.mark.parametrize("form", ["markdown", "json"])
def test_http_default_keeps_configured_product(split, form):
    """Default-shadowing defect: the API context's machinery root is not product selection."""
    response = _client(split[0]).get(f"/api/rules?scope=content&format={form}")
    assert response.status_code == 200, response.text
    body = response.text if form == "markdown" else response.json()["markdown"]
    assert body == CORE + rules_core.FILE_SEP + ADDENDUM


@pytest.mark.parametrize("damage", ["missing", "empty", "blank", "directory", "encoding"])
def test_product_addendum_refuses_despite_decoy(split, damage):
    """Fallback defect: missing or unreadable product content refuses despite a readable decoy."""
    target = split[1] / rules_core.CONTENT_ADDENDUM_REL
    target.unlink()
    if damage == "directory":
        target.mkdir()
    elif damage != "missing":
        target.write_bytes({"empty": b"", "blank": b" \n\t", "encoding": b"\xff"}[damage])
    with pytest.raises(rules_core.RulesCoreMissing, match=rules_core.CONTENT_ADDENDUM_REL):
        rules_core.require_core("content")
    assert rules_core.core_text("core") == CORE
    output = split[1] / "side-effect" / "rules.txt"
    result = _cli("--seat", "content", "--format", "block", "--output", str(output))
    assert result.returncode == 3, result.stderr
    assert rules_core.CONTENT_ADDENDUM_REL in result.stderr
    assert not output.parent.exists()
    for form in ("block", "toml"):
        result = _shell(split[0], form)
        assert result.returncode == 1, result.stderr
        assert rules_core.CONTENT_ADDENDUM_REL in result.stderr
        assert not result.stdout
    for form in ("markdown", "json"):
        response = _client(split[0]).get(f"/api/rules?scope=content&format={form}")
        assert response.status_code == 503, response.text
        assert rules_core.CONTENT_ADDENDUM_REL in response.json()["detail"]


def test_core_never_resolves_product(split, monkeypatch):
    """Machinery-only regression: core seats must succeed even when product resolution raises."""

    def unavailable(*args, **kwargs):
        raise repo_root.ProductRootError("no product")

    monkeypatch.setattr(repo_root, "product_root", unavailable)
    if hasattr(rules_core, "product_root"):
        monkeypatch.setattr(rules_core, "product_root", unavailable)
    assert rules_core.core_text("core") == CORE
    assert rules_core.require_core("core") == "core"
    assert _client(split[0]).get("/api/rules?scope=core").text == CORE
    monkeypatch.setenv(repo_root.PRODUCT_ROOT_ENV, str(split[0] / "missing-product"))
    assert _cli("--seat", "core", "--format", "text").stdout == CORE
    monkeypatch.setenv("LU_RULES_SEAT", "core")
    assert _shell(split[0], "block").returncode == 0


def test_invalid_product_is_named_refusal(split, monkeypatch):
    """Resolver-refusal defect: invalid product selection must refuse through RulesCoreMissing."""
    # #70 (C-4843 step 1): a configured product need not carry PRODUCT_MARKER, so the invalid
    # selection is a product directory that does not exist.
    monkeypatch.setenv(repo_root.PRODUCT_ROOT_ENV, str(split[0] / "missing-product"))
    with pytest.raises(rules_core.RulesCoreMissing, match=rules_core.CONTENT_ADDENDUM_REL):
        rules_core.core_text("content")
    result = _cli("--seat", "content", "--format", "text")
    assert result.returncode == 3
    assert rules_core.CONTENT_ADDENDUM_REL in result.stderr
    assert _client(split[0]).get("/api/rules?scope=content").status_code == 503


def test_default_binding_bundle_is_the_bounded_core(split) -> None:
    """Default-bundle defect: an omitted scope must serve the bounded core, not a wider bundle."""
    api_client = _client(split[0])
    body = api_client.get("/api/rules?format=json").json()
    assert body == api_client.get("/api/rules?scope=core&format=json").json()
    assert body["scope"] == "core"
    assert body["sources"] == [rules_core.CORE_REL]
    assert body["markdown"] == rules_core.core_text()
    assert body["bytes"] == len(body["markdown"].encode("utf-8")) <= 40_960
    plain = api_client.get("/api/rules")
    assert plain.text == body["markdown"]
    assert plain.headers["x-rules-scope"] == "core"
    assert api_client.get("/api/rules", headers={"If-None-Match": plain.headers["etag"]}).status_code == 304


# Prompt-contract reader: fleet rules come from the release, product files stay in the product (#100).
_RULES = rules_core.RULES_DIR_REL
_WORKFLOW = f"{_RULES}/workflow.md"


def _contract_product(root: Path) -> Path:
    """A product tree carrying a decoy of a fleet rule, a product-owned rule and its own rule."""
    rules = root / _RULES
    rules.mkdir(parents=True)
    (rules / "workflow.md").write_text("# PRODUCT DECOY\n", encoding="utf-8")
    (rules / "core-curriculum.md").write_text("# product rule\n", encoding="utf-8")
    (rules / "custom-product-policy.md").write_text("# product-own rule\n", encoding="utf-8")
    fragments = root / "agents_extensions/shared/prompt-contracts/fragments"
    fragments.mkdir(parents=True)
    return root


def _release_workflow() -> bytes:
    return (repo_root.machinery_path(_RULES) / "workflow.md").read_bytes()


@pytest.mark.parametrize(
    "declared",
    [
        _WORKFLOW,
        f"./{_WORKFLOW}",
        _WORKFLOW.replace("agents_extensions/", "agents_extensions/./", 1),
        _WORKFLOW.replace("agents_extensions/", "agents_extensions//", 1),
    ],
)
def test_noncanonical_fleet_rule_path_never_reads_product_decoy(tmp_path, declared):
    """Prefix-bypass defect: a non-canonical spelling of a fleet rule must still select the release copy."""
    product = _contract_product(tmp_path / "product")
    selected = prompt_contracts._shared_selection(product, declared, "policy")
    assert selected.path.read_bytes() == _release_workflow()
    assert selected.logical == _WORKFLOW


def test_symlink_out_of_shared_tree_is_refused_not_given_a_forged_identity(tmp_path):
    """Shared-tree escape defect: a shared file symlinked to a non-shared product file must be refused."""
    product = _contract_product(tmp_path / "product")
    secret = product / "docs/secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("SECRET\n", encoding="utf-8")
    fragment = product / "agents_extensions/shared/prompt-contracts/fragments/core.md"
    fragment.symlink_to(secret)
    with pytest.raises(prompt_contracts.PromptContractError, match="must live under agents_extensions/shared"):
        prompt_contracts._shared_selection(product, fragment.relative_to(product).as_posix(), "fragment")


def test_product_symlink_out_of_the_product_root_is_refused(tmp_path):
    """Containment defect: a product-owned rule symlinked to a file outside the product stays refused."""
    product = _contract_product(tmp_path / "product")
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    owned = product / _RULES / "core-curriculum.md"
    owned.unlink()
    owned.symlink_to(outside)
    with pytest.raises(prompt_contracts.PromptContractError, match="escapes repository root"):
        prompt_contracts._shared_selection(product, f"{_RULES}/core-curriculum.md", "policy")


def test_product_own_rule_and_relative_product_root_still_resolve(tmp_path, monkeypatch):
    """Regression defect: a product-own rule absent from the release and a relative root must keep resolving."""
    product = _contract_product(tmp_path / "product")
    own = prompt_contracts._shared_selection(product, f"{_RULES}/custom-product-policy.md", "policy")
    assert own.path.read_text(encoding="utf-8") == "# product-own rule\n"
    monkeypatch.chdir(tmp_path)
    owned = prompt_contracts._shared_selection(Path("product"), f"{_RULES}/core-curriculum.md", "policy")
    assert owned.path.read_text(encoding="utf-8") == "# product rule\n"
    assert owned.logical == f"{_RULES}/core-curriculum.md"


@pytest.mark.parametrize("failure", ["unavailable", "unexpected"])
def test_release_selection_failure_never_falls_back_to_product_decoy(tmp_path, monkeypatch, failure):
    """Fail-open defect: when the release carries a rule but cannot select it, a product copy must not be read."""
    product = _contract_product(tmp_path / "product")
    decoy = product / _WORKFLOW

    def broken(rel, root=None):
        if failure == "unavailable":
            raise rules_core.RulesCoreMissing("release unavailable")
        return decoy

    monkeypatch.setattr(prompt_contracts, "source_path", broken)
    with pytest.raises(prompt_contracts.PromptContractError):
        prompt_contracts._shared_selection(product, _WORKFLOW, "policy")


def test_pilot_identity_inputs_and_owner_paths_take_fleet_rules_from_the_release(tmp_path):
    """Owner-path defect: identity inputs naming a fleet rule must not require a product copy of it."""
    product = tmp_path / "product"
    product.mkdir()
    assert curriculum_lifecycle_pilot._identity_file(product, _WORKFLOW).read_bytes() == _release_workflow()
    with pytest.raises(curriculum_lifecycle_pilot.PilotError, match="missing"):
        curriculum_lifecycle_pilot._identity_file(product, "docs/absent.md")
