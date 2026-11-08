"""C5 defect classes, with literal independent output expectations for a tiny release.

The fixture supplies input resources only. Expected output paths and contents below
are literals, never constructed by the deployer's inventory or renderer.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from lu_machinery.agent_deploy import plan
from lu_machinery.agent_deploy.__main__ import main

CORE = b"# Fleet core\n"
SKILL = b"---\nname: fleet\ndescription: Fleet example\n---\nRun the fleet example.\n"
PRODUCT = "---\nname: product\ndescription: Product example\n---\nRun the product example.\n"
HOOK = b"#!/bin/sh\nprintf 'fleet hook works\\n'\n"
# Literal evaluator assertions, independent of the release input mappings.
EXPECTED = {
    ".claude/hooks/fleet.sh", ".claude/rules/fleet.md", ".claude/skills/fleet/SKILL.md",
    ".claude/skills/product/SKILL.md", ".claude/settings.json",
    ".codex/hooks/fleet.sh", ".codex/rules/core.md", ".codex/rules/fleet.md",
    ".agents/skills/fleet/SKILL.md", ".agents/skills/product/SKILL.md",
    ".agent/hooks/fleet.sh", ".agent/rules/core.md", ".agent/rules/fleet.md",
    ".agent/skills/fleet/SKILL.md", ".agent/skills/product/SKILL.md", ".agent/settings.json",
    ".agent/rules.json", ".agent/rules/core-part-01.md",
    ".gemini/rules/core.md", ".gemini/rules/fleet.md",
    ".gemini/skills/fleet/SKILL.md", ".gemini/skills/product/SKILL.md",
    ".codex/settings.json", ".codex/hooks.json",
}


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode & 0o777, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def write(root, relative, content, mode=0o644):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path


def descriptor(root, **changes):
    data = {"providers": ["claude", "codex", "agy", "gemini"], "sources": {
        "shared": {"path": "agents_extensions/shared", "required": True},
        "codex": {"path": "agents_extensions/codex", "required": True},
        "gemini": {"path": "gemini_extensions", "required": False},
    }, "preserve": [".agent/tmp", ".claude/settings.local.json"]}
    data.update(changes)
    write(root, plan.DESCRIPTOR, json.dumps(data))
    return data


@pytest.fixture
def release(tmp_path, monkeypatch):
    install = tmp_path / "installed"
    fleet = install / "agents_extensions/shared"
    package = install / "lu_machinery"
    write(fleet, "rules/core.md", CORE)
    write(fleet, "rules/fleet.md", "# Fleet rule\n")
    write(fleet, "hooks/fleet.sh", HOOK, 0o755)
    write(fleet, "skills/fleet/SKILL.md", SKILL)
    # Input mappings are intentionally explicit, including omissions.
    inventory = {"version": 1, "claude_autoload_excludes": ["rules/core.md", "rules/non-negotiable-rules.md", "rules/core-curriculum.md"], "files": {
        "rules/core.md": {"sha256": hashlib.sha256(CORE).hexdigest(), "mode": 420, "owner": "fleet", "targets": {
            "codex": [".codex/rules/core.md"], "agy": [".agent/rules/core.md"], "gemini": [".gemini/rules/core.md"]}},
        "rules/fleet.md": {"sha256": hashlib.sha256(b"# Fleet rule\n").hexdigest(), "mode": 420, "owner": "fleet", "targets": {
            "claude": [".claude/rules/fleet.md"], "codex": [".codex/rules/fleet.md"], "agy": [".agent/rules/fleet.md"], "gemini": [".gemini/rules/fleet.md"]}},
        "hooks/fleet.sh": {"sha256": hashlib.sha256(HOOK).hexdigest(), "mode": 493, "owner": "fleet", "targets": {
            "claude": [".claude/hooks/fleet.sh"], "codex": [".codex/hooks/fleet.sh"], "agy": [".agent/hooks/fleet.sh"]}},
        "skills/fleet/SKILL.md": {"sha256": hashlib.sha256(SKILL).hexdigest(), "mode": 420, "owner": "fleet", "targets": {
            "claude": [".claude/skills/fleet/SKILL.md"], "codex": [".agents/skills/fleet/SKILL.md"],
            "agy": [".agent/skills/fleet/SKILL.md", ".agents/skills/fleet/SKILL.md"], "gemini": [".gemini/skills/fleet/SKILL.md"]}},
    }, "required_hooks": {"claude": {".claude/settings.json": [
        {"path": ["hooks", "SessionStart"], "group": {"matcher": "startup"}, "hooks": [
            {"type": "command", "command": ".claude/hooks/fleet.sh", "timeout": 10}]}]},
        "codex": {".codex/hooks.json": [
            {"path": ["hooks", "SessionStart"], "group": {}, "hooks": [
                {"type": "command", "command": ".codex/hooks/fleet.sh", "timeout": 10}]}]}}}
    write(package, "agent_deploy/release.v1.json", json.dumps(inventory))
    locations = {"lu_machinery": package, "lu_machinery.agent_deploy": package / "agent_deploy", "agents_extensions.shared": fleet}
    monkeypatch.setattr(plan.resources, "files", lambda name: locations[name])
    return fleet


@pytest.fixture
def consumer(tmp_path, release):
    root = tmp_path / "consumer"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    descriptor(root)
    write(root, "agents_extensions/shared/skills/product/SKILL.md", PRODUCT)
    write(root, "agents_extensions/shared/settings.json", json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": ".claude/hooks/fleet.sh", "timeout": 10}]}]}}))
    write(root, "agents_extensions/codex/hooks.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": ".codex/hooks/fleet.sh", "timeout": 10}]}]}}))
    return root


def invoke(monkeypatch, root=None, check=False):
    args = ["agent_deploy"]
    if root is not None:
        args += ["--project-root", str(root)]
    if check:
        args += ["--check"]
    monkeypatch.setattr(sys, "argv", args)
    return main()


def test_wrong_project_selection_never_falls_back(consumer, tmp_path, monkeypatch):
    """An invalid explicit target must never deploy to LU_PRODUCT_ROOT or cwd."""
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    before = snapshot(consumer)
    monkeypatch.chdir(unrelated)
    assert invoke(monkeypatch, tmp_path / "absent") == 2
    assert snapshot(consumer) == before
    assert list(unrelated.iterdir()) == []
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, check=True) == 0
    monkeypatch.delenv("LU_PRODUCT_ROOT")
    assert invoke(monkeypatch) == 2


def test_non_curriculum_consumer_lints_its_own_sources(consumer, monkeypatch):
    """A consumer with no curriculum marker must lint and deploy its own skills."""
    path = consumer / "agents_extensions/shared/skills/product/SKILL.md"
    path.write_text("---\nname: wrong\ndescription: Example\n---\n")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before
    path.write_text(PRODUCT)
    assert invoke(monkeypatch, consumer) == 0
    from lu_machinery.lint.lint_agent_skills import lint_skills
    assert lint_skills(consumer) == []
    path.write_text("broken")
    assert lint_skills(consumer) != []


@pytest.mark.parametrize("defect", ["missing", "changed", "namespace"])
def test_missing_release_resource_cannot_fall_back(consumer, release, monkeypatch, defect):
    """Missing/changed release resources or an alternate namespace fail before mutation."""
    if defect == "missing":
        (release / "rules/core.md").unlink()
    elif defect == "changed":
        (release / "rules/core.md").write_text("tampered")
    else:
        (release / "rules/core.md").unlink()
        write(consumer, "rules/core.md", CORE)
        original = plan.resources.files
        monkeypatch.setattr(plan.resources, "files", lambda name: consumer if name == "agents_extensions.shared" else original(name))
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("defect", ["logical", "skill", "delegator", "disabled", "preserve-policy", "prefix"])
def test_silent_policy_override_refused_before_writes(consumer, monkeypatch, defect):
    """Duplicate names/paths or absent/disabled mandatory hooks cannot override fleet policy."""
    if defect == "logical":
        write(consumer, "agents_extensions/shared/rules/core.md", CORE)
    elif defect == "prefix":
        write(consumer, "agents_extensions/shared/rules", "file instead of directory")
    elif defect == "skill":
        write(consumer, "agents_extensions/shared/skills/fleet/SKILL.md", SKILL)
    elif defect in {"delegator", "disabled"}:
        config = consumer / "agents_extensions/shared/settings.json"
        data = json.loads(config.read_text())
        if defect == "delegator":
            data["hooks"]["SessionStart"][0]["hooks"] = []
        else:
            data["hooks"]["SessionStart"][0]["disabled"] = True
        config.write_text(json.dumps(data))
    else:
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        data["preserve"].append(".claude/hooks")
        descriptor(consumer, **data)
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before


def test_unsafe_retirement_preserves_modified_unknown_and_runtime(consumer, monkeypatch):
    """Only recorded, unchanged outputs may retire; unknown files and scratch survive."""
    write(consumer, ".agent/tmp/scratch", "keep")
    assert invoke(monkeypatch, consumer) == 0
    source = consumer / "agents_extensions/shared/skills/product/SKILL.md"
    source.unlink()
    source.parent.rmdir()
    victim = consumer / ".agent/skills/product/SKILL.md"
    victim.write_text("local change")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 1
    assert snapshot(consumer) == before
    victim.write_text(PRODUCT)
    unknown = write(consumer, ".claude/unknown", "keep")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 1
    assert snapshot(consumer) == before
    unknown.unlink()
    assert invoke(monkeypatch, consumer) == 0
    assert not victim.exists()
    assert not victim.parent.exists()
    assert (consumer / ".agent/tmp/scratch").read_text() == "keep"


@pytest.mark.parametrize("path", [".agent", ".deploy-state", "agents_extensions/shared", ".claude/hooks"])
def test_symlink_escape_never_mutates_external_target(consumer, tmp_path, monkeypatch, path):
    """Source, output and state symlinks cannot redirect either apply or check."""
    import shutil
    external = tmp_path / "outside"
    external.mkdir()
    write(external, "sentinel", "untouched")
    target = consumer / path
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external, target_is_directory=True)
    before = snapshot(external)
    assert invoke(monkeypatch, consumer) == 2
    assert invoke(monkeypatch, consumer, check=True) == 2
    assert snapshot(external) == before


@pytest.mark.parametrize("defect", ["bytes", "mode", "hook", "agy", "codex"])
def test_false_green_check_recomposes_without_mutation(consumer, monkeypatch, defect):
    """Bytes, modes, hook references, generated parts and discovery paths all fail check."""
    assert invoke(monkeypatch, consumer) == 0
    if defect == "bytes":
        (consumer / ".claude/rules/fleet.md").write_text("wrong bytes")
    elif defect == "mode":
        (consumer / ".claude/hooks/fleet.sh").chmod(0o644)
    elif defect == "hook":
        (consumer / ".codex/hooks.json").write_text('{"hooks":{}}')
    elif defect == "agy":
        (consumer / ".agent/rules/core-part-01.md").write_text("wrong part")
    else:
        (consumer / ".codex/skills").mkdir()
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer, check=True) == 1
    assert snapshot(consumer) == before


def test_non_idempotency_literal_inventory_and_modes(consumer, monkeypatch):
    """Fresh apply and repeat apply must yield literal paths/bytes/modes and no rewrites."""
    assert invoke(monkeypatch, consumer) == 0
    paths = {p for p in snapshot(consumer) if p.startswith((".claude/", ".codex/", ".agents/", ".agent/", ".gemini/"))}
    assert paths == EXPECTED
    assert (consumer / ".agent/rules/core-part-01.md").read_bytes() == b"---\ntrigger: always_on\ndescription: Binding rules core, part 1 of 1.\n---\n# Fleet core\n"
    assert (consumer / ".agent/rules.json").read_text() == '{\n  "exclude": [\n    "core.md",\n    "model-assignment.md",\n    "workflow.md"\n  ]\n}\n'
    assert (consumer / ".claude/hooks/fleet.sh").stat().st_mode & 0o777 == 0o755
    assert (consumer / ".claude/rules/fleet.md").stat().st_mode & 0o777 == 0o644
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert snapshot(consumer) == before
    assert subprocess.check_output([str(consumer / ".claude/hooks/fleet.sh")], text=True) == "fleet hook works\n"
    assert (consumer / ".agents/skills/product/SKILL.md").read_text() == PRODUCT


@pytest.mark.parametrize("defect", ["interruption", "late-discovery"])
def test_interrupted_apply_cannot_publish_false_success_and_recovers(consumer, monkeypatch, defect):
    """A failed replacement cannot publish a success manifest; same inputs recover."""
    original = plan.Tree.replace
    calls = []
    def interrupted(self, path, blob, mode):
        calls.append(path)
        if len(calls) == 3:
            if defect == "interruption":
                raise OSError("injected interruption")
            (consumer / ".codex/skills").mkdir(parents=True)
        return original(self, path, blob, mode)
    monkeypatch.setattr(plan.Tree, "replace", interrupted)
    assert invoke(monkeypatch, consumer) != 0
    assert not (consumer / plan.STATE).exists()
    assert invoke(monkeypatch, consumer, check=True) != 0
    monkeypatch.setattr(plan.Tree, "replace", original)
    if defect == "late-discovery":
        (consumer / ".codex/skills").rmdir()
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0


def test_concurrent_deployer_cannot_interleave(consumer, monkeypatch):
    """A second process holding the target inode lock must block deployment without writes."""
    with plan.selected_tree(str(consumer), False):
        before = snapshot(consumer)
        assert invoke(monkeypatch, consumer) == 1
        assert snapshot(consumer) == before


@pytest.mark.parametrize("selected", ["relative", "../escape", "bad\npath"])
def test_wrong_project_selection_invalid_input_has_no_side_effects(consumer, monkeypatch, selected):
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))
    before = snapshot(consumer)
    assert invoke(monkeypatch, selected) == 2
    assert snapshot(consumer) == before
