"""C5 defect classes, with literal independent output expectations for a tiny release.

The fixture supplies input resources only. Expected output paths and contents below
are literals, never constructed by the deployer's inventory or renderer.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

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
    ".claude/memory/MEMORY.md", ".codex/memory/MEMORY.md", ".agent/memory/MEMORY.md",
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
    inventory = {"version": 1, "provider_replacements": {"codex": ["config.toml", "hooks.json", "memory"]}, "claude_autoload_excludes": ["rules/core.md", "rules/non-negotiable-rules.md", "rules/core-curriculum.md"], "files": {
        "rules/core.md": {"mode": 420, "owner": "fleet", "targets": {
            "codex": [".codex/rules/core.md"], "agy": [".agent/rules/core.md"], "gemini": [".gemini/rules/core.md"]}},
        "rules/fleet.md": {"mode": 420, "owner": "fleet", "targets": {
            "claude": [".claude/rules/fleet.md"], "codex": [".codex/rules/fleet.md"], "agy": [".agent/rules/fleet.md"], "gemini": [".gemini/rules/fleet.md"]}},
        "hooks/fleet.sh": {"mode": 493, "owner": "fleet", "targets": {
            "claude": [".claude/hooks/fleet.sh"], "codex": [".codex/hooks/fleet.sh"], "agy": [".agent/hooks/fleet.sh"]}},
        "skills/fleet/SKILL.md": {"mode": 420, "owner": "fleet", "targets": {
            "claude": [".claude/skills/fleet/SKILL.md"], "codex": [".agents/skills/fleet/SKILL.md"],
            "agy": [".agent/skills/fleet/SKILL.md", ".agents/skills/fleet/SKILL.md"], "gemini": [".gemini/skills/fleet/SKILL.md"]}},
    }, "required_hooks": {"claude": {".claude/settings.json": [
        {"path": ["hooks", "SessionStart"], "group": {"matcher": "startup"}, "hooks": [
            {"type": "command", "command": ".claude/hooks/fleet.sh", "timeout": 10}]}]},
        "codex": {".codex/hooks.json": [
            {"path": ["hooks", "SessionStart"], "group": {}, "hooks": [
                {"type": "command", "command": ".codex/hooks/fleet.sh", "timeout": 10, "statusMessage": "Checking learn-ukrainian session state"}]}]}}}
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
    write(root, "agents_extensions/shared/memory/MEMORY.md", "shared memory\n")
    write(root, "agents_extensions/codex/memory/MEMORY.md", "codex memory\n")
    write(root, "agents_extensions/shared/settings.json", json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": ".claude/hooks/fleet.sh", "timeout": 10}]}]}}))
    write(root, "agents_extensions/codex/hooks.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": ".codex/hooks/fleet.sh", "timeout": 10, "statusMessage": "Checking consumer session state"}]}]}}))
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


@pytest.mark.parametrize("defect", ["missing", "namespace"])
def test_missing_release_resource_cannot_fall_back(consumer, release, monkeypatch, defect):
    """Missing release resources or an alternate namespace fail before mutation."""
    if defect == "missing":
        (release / "rules/core.md").unlink()
    else:
        (release / "rules/core.md").unlink()
        write(consumer, "rules/core.md", CORE)
        original = plan.resources.files
        monkeypatch.setattr(plan.resources, "files", lambda name: consumer if name == "agents_extensions.shared" else original(name))
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("defect", ["logical", "skill", "delegator", "disabled", "preserve-policy", "prefix", "skill-name"])
def test_silent_policy_override_refused_before_writes(consumer, monkeypatch, capsys, defect):
    """Duplicate names/paths or absent/disabled mandatory hooks cannot override fleet policy."""
    if defect == "logical":
        write(consumer, "agents_extensions/shared/rules/core.md", CORE)
    elif defect == "skill-name":
        write(consumer, "agents_extensions/shared/skills/different/SKILL.md", SKILL)
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
    if defect == "skill-name":
        assert "duplicate skill name: fleet" in capsys.readouterr().err


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
    unknown = write(consumer, ".claude/skills/unknown", "keep")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 1
    assert snapshot(consumer) == before
    unknown.unlink()
    assert invoke(monkeypatch, consumer) == 0
    assert not victim.exists()
    assert not victim.parent.exists()
    assert (consumer / ".agent/tmp/scratch").read_text() == "keep"


@pytest.mark.parametrize("root", [".claude", ".agent", ".agents", ".codex", ".gemini", ".cursor"])
@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling", "fifo"])
@pytest.mark.parametrize("name", ["sessions", "cache\\key", "session\u200bid"])
def test_runtime_siblings_do_not_block_check_or_apply(consumer, tmp_path, monkeypatch, root, kind, name):
    """Runtime state outside owned top-levels must never block deployment."""
    if root == ".cursor":
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        descriptor(consumer, providers=data["providers"] + ["cursor"])
    outside = write(tmp_path, "outside-runtime", "keep outside\n")
    runtime = consumer / root / name
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        write(consumer, root + "/" + name + "/active", "keep runtime\n")
    elif kind == "file":
        runtime.write_text("keep runtime\n")
    elif kind == "symlink":
        runtime.symlink_to(outside)
    elif kind == "dangling":
        runtime.symlink_to(tmp_path / "absent")
    else:
        os.mkfifo(runtime)
    before = runtime.lstat()
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert invoke(monkeypatch, consumer) == 0
    assert runtime.lstat() == before
    assert outside.read_text() == "keep outside\n"


@pytest.mark.parametrize("path,providers", [(path, None) for path in [
    ".claude/commands/planted.md", ".claude/agents/planted.md",
    ".claude/output-styles/planted.md", ".claude/plugins/planted.md",
    ".claude/skills/old/SKILL.md", ".codex/agents/planted.toml",
    ".codex/config.toml", ".agents/plugins/planted/plugin.json",
    ".agents/rules/planted.md", ".agent/workflows/planted.md",
    ".gemini/commands/planted.toml", ".claude/workflows/planted.js",
    ".claude/agent-memory/reviewer/MEMORY.md", ".claude/agent-memory-local/reviewer/MEMORY.md",
    ".claude/AGENTS.md", ".gemini/sandbox.Dockerfile",
    ".cursor/cli.json", ".cursor/environment.json", ".cursor/worktrees.json",
    ".gemini/sandbox-macos-team.sb", ".claude/Skills/old/SKILL.md",
    ".gemini/policies/allow-all.toml", ".gemini/sandbox.bashrc",
    ".gemini/config.yaml", ".gemini/styleguide.md", ".cursor/BUGBOT.md",
    ".cursor/sandbox.json", ".cursor/permissions.json", ".cursor/config/bugbot.yaml",
]] + [
    # Every provider that discovers the shared .agents tree inventories it on its own,
    # from a fresh apply too (no earlier deployment state names .agents).
    (path, providers) for providers in (["codex"], ["agy"], ["gemini"], ["cursor"], ["gemini", "cursor"])
    for path in (".agents/skills/planted/SKILL.md", ".agents/rules/planted.md")
])
@pytest.mark.parametrize("check", [False, True], ids=["apply", "check"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_provider_discovery_content_cannot_hide_as_runtime(consumer, tmp_path, monkeypatch, capsys, path, providers, check, kind):
    """Never-planned discovery content and stale skills must still report drift."""
    if providers is not None:
        descriptor(consumer, providers=providers, sources={
            "shared": {"path": "agents_extensions/shared", "required": True},
            **({"codex": {"path": "agents_extensions/codex", "required": True}} if "codex" in providers else {}),
        }, preserve=[])
    elif path.startswith(".cursor/"):
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        descriptor(consumer, providers=data["providers"] + ["cursor"])
    if providers is None:
        assert invoke(monkeypatch, consumer) == 0
    if kind == "file":
        write(consumer, path, 'model = "planted"\n' if path.endswith(".toml") else "planted\n")
    else:
        outside = write(tmp_path, "outside-discovery", "untouched\n")
        planted = consumer / path
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.symlink_to(outside)
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer, check=check) == (1 if kind == "file" else 2)
    captured = capsys.readouterr()
    if kind == "file":
        assert "unknown unowned output: " + path in captured.out + captured.err
    else:
        if path == ".codex/config.toml":
            assert "non-regular deployment output or parent: " + path in captured.err
        else:
            assert "symlink or special file" in captured.err
        assert planted.is_symlink()
        assert outside.read_text() == "untouched\n"
    assert snapshot(consumer) == before


@pytest.mark.parametrize("root,names", [
    (".claude", "agents agent-memory agent-memory-local commands output-styles plugins skills rules hooks workflows settings.json settings.local.json CLAUDE.md AGENTS.md"),
    (".codex", "agents prompts plugins skills rules hooks config.toml hooks.json"),
    (".agent", "agents plugins skills rules workflows hooks hooks.json mcp_config.json settings.json rules.json skills.json agents.json plugins.json AGENTS.md GEMINI.md"),
    (".agents", "agents plugins skills rules workflows hooks hooks.json mcp_config.json rules.json skills.json agents.json plugins.json AGENTS.md GEMINI.md"),
    (".gemini", "agents commands extensions skills rules hooks settings.json config.json GEMINI.md AGENTS.md system.md .env sandbox.Dockerfile sandbox.bashrc policies config.yaml styleguide.md"),
    (".cursor", "agents commands skills rules hooks hooks.json mcp.json cli.json environment.json worktrees.json BUGBOT.md sandbox.json permissions.json config"),
])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_every_discovery_name_refuses_unowned_content(tmp_path, root, names, kind):
    """An omitted discovery entry must fail even without planned or recorded siblings."""
    assert plan.DISCOVERY[root] == set(names.split())
    outside = write(tmp_path, "outside-discovery", "untouched\n")
    empty_plan = plan.Plan(files={}, preserve=[], roots={root}, release="release", configuration="config")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        tree = plan.Tree(fd)
        for name in names.split():
            path = tmp_path / root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "file":
                path.write_text("planted\n")
                if (root, name) == (".codex", "skills"):
                    with pytest.raises(plan.Invalid, match="non-directory deployment path: .codex/skills"):
                        plan.preflight(tree, empty_plan, {"files": {}}, False)
                else:
                    assert plan.preflight(tree, empty_plan, {"files": {}}, False) == [
                        "unknown unowned output: " + root + "/" + name]
            else:
                path.symlink_to(outside)
                with pytest.raises(plan.Invalid, match="symlink or special file"):
                    plan.preflight(tree, empty_plan, {"files": {}}, False)
            path.unlink()
        assert outside.read_text() == "untouched\n"
    finally:
        os.close(fd)


@pytest.mark.parametrize("name", ["cache\\key", "session\u200bid"])
def test_unsafe_names_inside_owned_discovery_still_fail_closed(tmp_path, name):
    """Ignoring runtime siblings must not bypass validation inside discovery trees."""
    write(tmp_path, ".claude/rules/" + name, "planted\n")
    empty_plan = plan.Plan(files={}, preserve=[], roots={".claude"}, release="release", configuration="config")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(plan.Invalid):
            plan.preflight(plan.Tree(fd), empty_plan, {"files": {}}, False)
    finally:
        os.close(fd)


@pytest.mark.parametrize("check", [False, True], ids=["apply", "check"])
@pytest.mark.parametrize("kind", ["nested-file", "parent-file", "leaf-directory", "symlink", "dangling", "fifo"])
def test_future_planned_output_refuses_runtime_collision(consumer, tmp_path, monkeypatch, capsys, check, kind):
    """New ownership refuses differing bytes and cross-type collisions without crashing."""
    assert invoke(monkeypatch, consumer) == 0
    target = consumer / ".claude/sessions/active"
    if kind == "parent-file":
        write(consumer, ".claude/sessions", "runtime bytes\n")
    else:
        target.parent.mkdir(parents=True)
        if kind == "nested-file":
            target.write_text("runtime bytes\n")
        elif kind == "leaf-directory":
            target.mkdir()
        elif kind in {"symlink", "dangling"}:
            outside = tmp_path / "outside-collision"
            if kind == "symlink":
                outside.write_text("untouched\n")
            target.symlink_to(outside)
        else:
            os.mkfifo(target)
    write(consumer, "agents_extensions/shared/sessions/active", "planned bytes\n")
    before = snapshot(consumer)
    with plan.selected_tree(str(consumer), check=check) as tree:
        with pytest.raises(plan.Invalid):
            plan.deploy(tree, check=check)
    assert invoke(monkeypatch, consumer, check=check) == 2
    error = capsys.readouterr().err
    if kind == "nested-file":
        assert "differing unowned output: .claude/sessions/active" in error
    elif kind == "parent-file":
        assert "non-directory deployment path: .claude/sessions" in error
    elif kind == "leaf-directory":
        assert "non-regular deployment output or parent: .claude/sessions/active" in error
    else:
        assert "symlink or special file" in error
    assert snapshot(consumer) == before


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
    assert (consumer / ".codex/memory/MEMORY.md").read_text() == "codex memory\n"
    assert (consumer / ".claude/memory/MEMORY.md").read_text() == "shared memory\n"
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


@pytest.mark.parametrize("defect", ["interruption", "late-discovery", "partial_crash_then_source_change"])
def test_interrupted_apply_cannot_publish_false_success_and_recovers(consumer, monkeypatch, capsys, defect):
    """Interrupted outputs recover after source edits using the documented input restoration."""
    original = plan.Tree.replace
    calls = []
    def interrupted(self, path, blob, mode):
        calls.append(path)
        if defect == "partial_crash_then_source_change" and path == ".agent/skills/product/SKILL.md":
            original(self, path, blob, mode)
            raise OSError("injected interruption after product output")
        if len(calls) == 3 and defect != "partial_crash_then_source_change":
            if defect == "interruption":
                raise OSError("injected interruption")
            (consumer / ".codex/skills").mkdir(parents=True)
        return original(self, path, blob, mode)
    monkeypatch.setattr(plan.Tree, "replace", interrupted)
    assert invoke(monkeypatch, consumer) != 0
    assert not (consumer / plan.STATE).exists()
    assert invoke(monkeypatch, consumer, check=True) != 0
    source = consumer / "agents_extensions/shared/skills/product/SKILL.md"
    desired = PRODUCT.replace("Run the product example.", "Run the updated product example.")
    if defect == "partial_crash_then_source_change":
        source.write_text(desired)
        before = snapshot(consumer)
        assert invoke(monkeypatch, consumer) == 2
        assert "see --help section: Recovery after an interrupted fresh apply" in capsys.readouterr().err
        assert snapshot(consumer) == before
        monkeypatch.setattr(sys, "argv", ["agent_deploy", "--help"])
        with pytest.raises(SystemExit) as exited:
            main()
        assert exited.value.code == 0
        help_text = capsys.readouterr().out
        assert "Restore the product sources and installed" in help_text
        assert "apply again to establish ownership" in help_text
        assert "Do not delete provider directories or the ownership manifest" in help_text
        # Follow the documented recovery, without deleting any output or state.
        source.write_text(PRODUCT)
    monkeypatch.setattr(plan.Tree, "replace", original)
    if defect == "late-discovery":
        (consumer / ".codex/skills").rmdir()
    assert invoke(monkeypatch, consumer) == 0
    if defect == "partial_crash_then_source_change":
        source.write_text(desired)
        assert invoke(monkeypatch, consumer) == 0
        assert (consumer / ".agents/skills/product/SKILL.md").read_text() == desired
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


def test_sigkill_after_first_write_recovers_without_deleting_unknown_files(consumer, release, monkeypatch):
    """SIGKILL bypasses finally cleanup; the next apply must recover its temporary file."""
    script = """
import os, signal, sys
from pathlib import Path
from lu_machinery.agent_deploy import plan
from lu_machinery.agent_deploy.__main__ import main
install = Path(sys.argv[1])
locations = {"lu_machinery": install / "lu_machinery",
             "lu_machinery.agent_deploy": install / "lu_machinery/agent_deploy",
             "agents_extensions.shared": install / "agents_extensions/shared"}
plan.resources.files = lambda name: locations[name]
def kill_after_write(fd, mode):
    os.kill(os.getpid(), signal.SIGKILL)
plan.os.fchmod = kill_after_write
sys.argv = ["agent_deploy", "--project-root", sys.argv[2]]
raise SystemExit(main())
"""
    killed = subprocess.run([sys.executable, "-c", script, str(release.parents[1]), str(consumer)],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert not (consumer / plan.STATE).exists()
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer, check=True) == 1
    assert snapshot(consumer) == before
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert not any(p.name.startswith(".agent-deploy-") for p in consumer.rglob("*"))
    assert not (consumer / ".deploy-state/agent-deploy.tmp").exists()


def test_unknown_staging_content_is_preserved_before_first_apply(consumer, monkeypatch):
    """A reserved staging filename never licenses deleting unrelated unowned bytes."""
    write(consumer, ".deploy-state/agent-deploy.tmp", "unrelated user content", 0o600)
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert invoke(monkeypatch, consumer, check=True) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("defect", ["global", "never_matcher", "local_source", "local_preserved", "codex_feature", "codex_alias"])
def test_mandatory_hooks_cannot_be_globally_disabled_or_made_unreachable(consumer, monkeypatch, defect):
    """F1: actual provider disable switches and nonmatching groups cannot neutralize fleet hooks."""
    if defect == "never_matcher":
        path = consumer / "agents_extensions/codex/hooks.json"
        data = json.loads(path.read_text())
        data["hooks"]["SessionStart"][0]["matcher"] = "no-such-source-ever"
        path.write_text(json.dumps(data))
    elif defect in {"codex_feature", "codex_alias"}:
        key = "hooks" if defect == "codex_feature" else "codex_hooks"
        write(consumer, "agents_extensions/codex/config.toml", f"[features]\n{key} = false\n")
    elif defect == "global":
        path = consumer / "agents_extensions/shared/settings.json"
        data = json.loads(path.read_text())
        data["disableAllHooks"] = True
        path.write_text(json.dumps(data))
    elif defect == "local_source":
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        data["preserve"] = [".agent/tmp"]
        descriptor(consumer, **data)
        write(consumer, "agents_extensions/shared/settings.local.json", '{"disableAllHooks": true}')
    else:
        write(consumer, ".claude/settings.local.json", '{"disableAllHooks": true}')
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert invoke(monkeypatch, consumer, check=True) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("kind", ["skill", "file"])
def test_codex_provider_never_plans_retired_skill_discovery(consumer, monkeypatch, kind):
    """F2: provider skills use .agents; an invalid skills leaf refuses before any write."""
    body = "---\nname: codexonly\ndescription: Codex example\n---\nUse this example.\n"
    if kind == "skill":
        write(consumer, "agents_extensions/codex/skills/codexonly/SKILL.md", body)
        assert invoke(monkeypatch, consumer) == 0
        assert (consumer / ".agents/skills/codexonly/SKILL.md").read_text() == body
        assert not (consumer / ".codex/skills").exists()
        assert invoke(monkeypatch, consumer, check=True) == 0
    else:
        write(consumer, "agents_extensions/codex/skills", "not a skill directory")
        before = snapshot(consumer)
        assert invoke(monkeypatch, consumer) == 2
        assert snapshot(consumer) == before


@pytest.mark.parametrize("kind", ["symlink", "fifo", "unreadable"])
def test_preserved_runtime_content_is_not_opened_or_traversed(consumer, tmp_path, monkeypatch, kind):
    """F3: sockets, links and unreadable files in excluded runtime paths cannot block deploy."""
    runtime = consumer / ".agent/tmp"
    runtime.mkdir(parents=True)
    path = runtime / "active"
    outside = write(tmp_path, "outside-runtime", "external runtime\n")
    outside_before = (outside.read_bytes(), outside.stat().st_mtime_ns)
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("private runtime\n")
        path.chmod(0)
    mode_before = path.lstat().st_mode
    try:
        assert invoke(monkeypatch, consumer) == 0
        assert invoke(monkeypatch, consumer, check=True) == 0
        assert path.lstat().st_mode == mode_before
        assert (outside.read_bytes(), outside.stat().st_mtime_ns) == outside_before
    finally:
        if kind == "unreadable":
            path.chmod(0o600)
    if kind == "unreadable":
        assert path.read_text() == "private runtime\n"


def test_packaged_resource_edits_load_without_stale_pins_and_change_identity(tmp_path, monkeypatch):
    """Every listed resource can change without stale pins or an unchanged release identity."""
    repo = Path(__file__).resolve().parents[2]
    installed = tmp_path / "installed"
    package = installed / "lu_machinery"
    package.mkdir(parents=True)
    shutil.copytree(Path(plan.__file__).resolve().parent, package / "agent_deploy")
    fleet = installed / "agents_extensions/shared"
    shutil.copytree(repo / "agents_extensions/shared", fleet)
    locations = {"lu_machinery": package, "lu_machinery.agent_deploy": package / "agent_deploy"}
    monkeypatch.setattr(plan.resources, "files", lambda name: locations[name])
    original, inventory, identity = plan.release_inputs()
    assert plan.release_inputs()[2] == identity
    for name in inventory["files"]:
        path = fleet / name
        before = path.read_bytes()
        edited = before + b"\n# scratch release edit\n"
        path.write_bytes(edited)
        try:
            outputs, _, changed = plan.release_inputs()
            assert outputs[name].blob == edited
            assert outputs[name].mode == original[name].mode
            assert changed != identity, name
            assert plan.release_inputs()[2] == changed
        finally:
            path.write_bytes(before)
    assert plan.release_inputs()[2] == identity
    assert all("sha256" not in record for record in inventory["files"].values())
    # Modes and routing metadata remain release identity inputs as well.
    inventory_path = package / "agent_deploy/release.v1.json"
    inventory["files"]["rules/core.md"]["mode"] = 493
    inventory_path.write_bytes(plan.encoded(inventory))
    assert plan.release_inputs()[2] != identity
    inventory["files"]["rules/core.md"]["mode"] = original["rules/core.md"].mode
    inventory_path.write_bytes(plan.encoded(inventory))
    assert plan.release_inputs()[2] == identity
    inventory["provider_replacements"]["codex"].append("scratch-metadata")
    inventory_path.write_bytes(plan.encoded(inventory))
    assert plan.release_inputs()[2] != identity
