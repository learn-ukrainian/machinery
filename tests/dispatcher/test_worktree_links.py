"""Dispatch installs stay local while shared databases remain available."""

import errno
import json
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from lu_machinery.common.repo_root import product_root
from lu_machinery.dispatcher import agy_sandbox, environment, git
from lu_machinery.dispatcher import site_deps
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.orchestration.worktree_artifacts import is_disposable_path
from lu_machinery.storage.topology import STORE_FILES
from tests.helpers.product_tree import configured_roots  # noqa: F401





LINKS = (*(f"data/{STORE_FILES[store][0]}" for store in ("sources", "vesum")), "node_modules", "site/node_modules")
SOURCES_LINK = LINKS[0]


@pytest.mark.parametrize("operation", ["mkdir", "link", "copy2", "symlink", "rename"])
@pytest.mark.parametrize("after", [False, True])
def test_killed_dependency_materialization_does_not_retain_worktree(layout, operation, after):
    """A SIGKILL during dependency provisioning must not strand an unknown staging tree (#490)."""
    from lu_machinery.orchestration.worktree_claims import remove_unclaimed_worktree

    primary, worktree = layout
    site = worktree / "site"
    site.mkdir()
    (site / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    run_git(worktree, "add", "site/package-lock.json")
    run_git(worktree, "commit", "-m", "fixture dependency lock")
    store = site_deps.store_root(primary)
    blob = store / "blobs" / "ab" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_text("dependency")
    manifest = store / "trees" / "fixture.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"entries": [
        ["d", "pkg"], ["f", "pkg/file", "abc"],
        ["c", ".package-lock.json", "abc"], ["l", "alias", "pkg/file"],
    ]}))
    probe = '''
import os, shutil, signal, sys
from pathlib import Path
from lu_machinery.dispatcher import site_deps
operation, after = sys.argv[3], sys.argv[4] == "True"
module = shutil if operation == "copy2" else os
original = getattr(module, operation)
def fault(*args, **kwargs):
    if not after:
        os.kill(os.getpid(), signal.SIGKILL)
    result = original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
    return result
setattr(module, operation, fault)
site_deps.materialize(Path(sys.argv[1]), Path(sys.argv[2]), "fixture")
'''
    result = subprocess.run(
        [sys.executable, "-c", probe, str(primary), str(site), operation, str(after)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    assert set(site.iterdir()) <= {site / "node_modules", site / "package-lock.json"}
    if (site / "node_modules").exists():
        assert (site / "node_modules/pkg/file").read_text() == "dependency"
        assert (site / "node_modules/.package-lock.json").read_text() == "dependency"
        assert (site / "node_modules/alias").read_text() == "dependency"
    assert run_git(worktree, "status", "--porcelain") == ""
    tasks = primary / "batch_state/tasks"
    tasks.mkdir(parents=True)
    removal = remove_unclaimed_worktree(
        worktree, repo_root=primary, control_root=primary, tasks_dir=tasks, owner_task_id=None,
        reason="crashed provisioning",
    )
    assert removal.action == "removed", removal
    assert not worktree.exists()


@pytest.mark.parametrize("external_store", [False, True])
def test_dependency_materialization_publishes_complete_private_tree(layout, monkeypatch, external_store):
    """External staging must still publish hard links, private copies and relative links (#490)."""
    primary, worktree = layout
    site = worktree / "site"
    site.mkdir()
    if external_store:
        monkeypatch.setenv(site_deps.STORE_ENV, str(primary.parent / "external-dep-store"))
    store = site_deps.store_root(primary)
    blob = store / "blobs" / "ab" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_text("dependency")
    blob.chmod(0o444)
    manifest = store / "trees" / "fixture.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"entries": [
        ["d", "pkg"], ["f", "pkg/file", "abc"],
        ["c", ".package-lock.json", "abc"], ["l", "alias", "pkg/file"],
    ]}))
    if external_store:
        # Model two devices without mounting anything or changing host configuration.
        link, rename = os.link, os.rename
        def cross_device(operation, source, destination, *args, **kwargs):
            if Path(source).is_relative_to(store) != Path(destination).is_relative_to(store):
                raise OSError(errno.EXDEV, "injected cross-device operation")
            return operation(source, destination, *args, **kwargs)
        monkeypatch.setattr(os, "link", lambda *a, **k: cross_device(link, *a, **k))
        monkeypatch.setattr(os, "rename", lambda *a, **k: cross_device(rename, *a, **k))
    assert site_deps.materialize(primary, site, "fixture") == {
        "state": "installed", "linked": 0 if external_store else 1, "copied": 2 if external_store else 1,
    }
    assert ((site / "node_modules/pkg/file").stat().st_ino == blob.stat().st_ino) is (not external_store)
    assert (site / "node_modules/.package-lock.json").stat().st_ino != blob.stat().st_ino
    assert (site / "node_modules/alias").read_text() == "dependency"
    assert not list((primary / ".git").rglob("materialize-*"))


@pytest.mark.parametrize("relative", ["node_modules", "site/node_modules"])
def test_real_npm_ci_cannot_corrupt_primary_install(layout, tmp_path, relative):
    """A normal install must clean only its own dependencies, even without the shim."""
    primary, worktree = layout
    shim_dirs = {("agent_runtime", "shims"), ("dispatcher", "npm_shims")}
    tool_path = os.pathsep.join(
        entry for entry in os.environ.get("AGENT_ORIGINAL_PATH", os.environ.get("PATH", os.defpath)).split(os.pathsep)
        if Path(entry).resolve().parts[-2:] not in shim_dirs
    )
    npm = shutil.which("npm", path=tool_path)
    assert npm is not None, "real npm is required for the corruption regression"
    target = primary / relative
    (target / "sentinel").write_text("primary dependency")
    before = target.stat().st_ino
    local = worktree / relative
    local.mkdir(parents=True, exist_ok=True)
    (local / "obsolete").write_text("stale worktree dependency")
    package = {"name": "isolation-regression", "version": "1.0.0"}
    (local.parent / "package.json").write_text(json.dumps(package))
    (local.parent / "package-lock.json").write_text(json.dumps({
        **package, "lockfileVersion": 3, "packages": {"": package},
    }))
    result = subprocess.run(
        [npm, "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=local.parent,
        env={"PATH": tool_path, "HOME": str(tmp_path), "npm_config_cache": str(tmp_path / "npm-cache"),
             "npm_config_userconfig": str(tmp_path / "user.npmrc"),
             "npm_config_globalconfig": str(tmp_path / "global.npmrc")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (local / "obsolete").exists(), "npm must actually clean the local installation"
    assert target.stat().st_ino == before
    assert (target / "sentinel").read_text() == "primary dependency"
    assert sorted(path.name for path in target.iterdir()) == ["sentinel"]
    assert not local.is_symlink()


@pytest.fixture
def layout(tmp_path):
    primary, worktree = tmp_path / "primary", tmp_path / "worktree"
    primary.mkdir()
    for relative in LINKS:
        target = primary / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".db"):
            target.write_text("database sentinel")
        else:
            target.mkdir()
    run_git(primary, "init", "-b", "main")
    run_git(primary, "config", "user.name", "Fixture")
    run_git(primary, "config", "user.email", "fixture@example.invalid")
    (primary / ".gitignore").write_text("data/\nnode_modules/\nsite/node_modules/\n.worktrees/\n")
    run_git(primary, "add", ".gitignore")
    run_git(primary, "commit", "-m", "fixture")
    git.create_worktree(primary, worktree, "codex/layout", "main")
    return primary, worktree


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.mark.parametrize("full_checkout", [False, True])
@pytest.mark.parametrize("selector", [False, True])
def test_creation_and_recreation_provision_clean_reapable_links(layout, full_checkout, selector):
    """Database links and dependency isolation must survive creation and fix rounds."""
    primary, _unused = layout
    if selector:
        generations = primary / "data/sources-generations"
        generations.mkdir()
        generation = generations / "sources.20261030T210000Z-0123456789ab.db"
        (primary / SOURCES_LINK).rename(generation)
        (primary / SOURCES_LINK).symlink_to(generation.relative_to(primary / "data"))
    run_git(primary, "init", "-b", "main")
    run_git(primary, "config", "user.name", "Fixture")
    run_git(primary, "config", "user.email", "fixture@example.invalid")
    ignore = product_root() / ".gitignore"
    (primary / ".gitignore").write_bytes(ignore.read_bytes())
    run_git(primary, "add", ".gitignore")
    run_git(primary, "commit", "-m", "fixture")
    sha = run_git(primary, "rev-parse", "HEAD")
    worktree = primary / ".worktrees/dispatch/codex/fixture"
    for round_number in range(2):
        git.create_worktree(primary, worktree, f"codex/fixture-{round_number}", sha, full_checkout=full_checkout)
        for relative in git.DATABASE_LINKS:
            link = worktree / relative
            assert not link.exists()
            assert not link.is_symlink()
            assert run_git(worktree, "check-ignore", relative) == relative
            assert not is_disposable_path(Path(relative), worktree=worktree, primary=primary)
        for relative in git.NODE_LINKS:
            assert not (worktree / relative).exists()
            assert not (worktree / relative).is_symlink()
        assert run_git(worktree, "status", "--porcelain") == ""
        run_git(primary, "worktree", "remove", str(worktree))
        assert (primary / "data/sources.db").read_text() == "database sentinel"
        assert (primary / "node_modules").is_dir()
        assert not (primary / "node_modules").is_symlink()


@pytest.mark.parametrize("case", [
    "selected", "legacy", "stale", "direct-generation", "foreign", "tracked",
    "malformed", "invalid-name", "missing", "generation-link", "generation-directory",
    "directory-link", "vesum-link",
])
def test_branch_content_cannot_choose_agy_host_database_mount(layout, case):
    """#12174: only the primary's selected generation or legacy file may become a host mount."""
    from lu_machinery.dispatcher.agy_sandbox import Refused, _trusted_database

    primary, worktree = layout
    run_git(worktree, "init", "-b", "main")
    selector = primary / SOURCES_LINK
    generations = primary / "data/sources-generations"
    generations.mkdir()
    selected = generations / "sources.20261030T210000Z-0123456789ab.db"
    stale = generations / "sources.20261030T200000Z-abcdef012345.db"
    stale.write_text("old generation")
    if case not in {"legacy", "vesum-link"}:
        selector.rename(selected)
        selector.symlink_to(selected.relative_to(selector.parent))
    if case == "malformed":
        selector.unlink()
        selector.symlink_to(f"./sources-generations/{selected.name}")
    elif case == "invalid-name":
        selected.rename(generations / "sources.invalid.db")
        selector.unlink()
        selector.symlink_to("sources-generations/sources.invalid.db")
    elif case == "missing":
        selected.unlink()
    elif case == "generation-link":
        selected.unlink()
        selected.symlink_to(stale)
    elif case == "generation-directory":
        selected.unlink()
        selected.mkdir()
    elif case == "directory-link":
        saved = generations.with_name("saved-generations")
        generations.rename(saved)
        generations.symlink_to(saved, target_is_directory=True)
    elif case == "vesum-link":
        vesum = primary / "data/vesum.db"
        saved = vesum.with_suffix(".saved")
        vesum.rename(saved)
        vesum.symlink_to(saved)

    relative = "data/vesum.db" if case == "vesum-link" else SOURCES_LINK
    link = worktree / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    assert not link.exists() and not link.is_symlink()
    if case in {"stale", "direct-generation", "foreign", "tracked"}:
        link.symlink_to({"stale": stale, "direct-generation": selected,
                         "foreign": primary / "data/vesum.db", "tracked": selector}[case])
        if case == "tracked":
            run_git(worktree, "add", "-f", relative)
    if case in {"selected", "legacy"}:
        expected = selected if case == "selected" else selector
        assert _trusted_database(worktree, primary, relative) == expected
        assert expected.read_text() == "database sentinel"
        assert not link.exists() and not link.is_symlink()
        if case == "selected":
            replacement = generations / "sources.20261030T220000Z-111111111111.db"
            replacement.write_text("new generation")
            selector.unlink()
            selector.symlink_to(replacement.relative_to(selector.parent))
            assert _trusted_database(worktree, primary, relative) == replacement
    else:
        with pytest.raises(Refused):
            _trusted_database(worktree, primary, relative)


@pytest.mark.parametrize("kind", ["file", "directory", "link", "dangling"])
def test_existing_destination_is_never_overwritten(layout, kind):
    """Provisioning must preserve real entries and both live and dangling links."""
    primary, worktree = layout
    entry = worktree / "data/sources.db"
    entry.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        entry.write_text("keep")
    elif kind == "directory":
        entry.mkdir()
    else:
        entry.symlink_to(primary / ("data/sources.db" if kind == "link" else "absent"))
    before = entry.lstat()
    with pytest.raises(agy_sandbox.Refused, match="worktree_local_store"):
        agy_sandbox._trusted_database(worktree, primary, SOURCES_LINK)
    assert entry.lstat() == before


def test_missing_targets_are_not_linked(tmp_path):
    """Absent primary dependencies must not become dangling worktree links."""
    primary, worktree = tmp_path / "primary", tmp_path / "worktree"
    primary.mkdir()
    worktree.mkdir()
    with pytest.raises(agy_sandbox.Refused):
        agy_sandbox._trusted_database(worktree, primary, SOURCES_LINK)
    assert list(worktree.iterdir()) == []


@pytest.mark.parametrize("side,relative", [("source", SOURCES_LINK), ("source", "data"), ("destination", "data")])
def test_symlink_escape_is_refused(layout, tmp_path, side, relative):
    """Source symlinks and destination ancestor links must not escape checkout boundaries."""
    primary, worktree = layout
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sources.db").write_text("outside sentinel")
    root = primary if side == "source" else worktree
    entry = root / relative
    if entry.exists():
        entry.rename(root / "saved")
    entry.symlink_to(outside)
    with pytest.raises(agy_sandbox.Refused):
        agy_sandbox._trusted_database(worktree, primary, SOURCES_LINK)
    assert (outside / "sources.db").read_text() == "outside sentinel"
    assert sorted(p.name for p in outside.iterdir()) == ["sources.db"]
    if side == "source":
        assert not (worktree / relative).exists()


def test_atomic_creation_preserves_racing_entry(layout, monkeypatch):
    """No provisioning or trust check may replace a concurrently created store slot."""
    primary, worktree = layout
    racing = worktree / SOURCES_LINK
    racing.parent.mkdir(parents=True, exist_ok=True)
    racing.write_text("racing writer")

    def forbidden(*args, **kwargs):
        pytest.fail("store provisioning attempted to create a symlink")
    monkeypatch.setattr(git.os, "symlink", forbidden)
    with pytest.raises(agy_sandbox.Refused, match="worktree_local_store"):
        agy_sandbox._trusted_database(worktree, primary, SOURCES_LINK)
    assert racing.read_text() == "racing writer"


def test_unexpected_io_failure_is_reported(layout, monkeypatch):
    """Permission and I/O failures refuse the store boundary rather than a partial launch."""
    primary, worktree = layout

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(git.os, "open", denied)
    with pytest.raises(agy_sandbox.Refused, match="worktree_local_store"):
        agy_sandbox._trusted_database(worktree, primary, SOURCES_LINK)


@pytest.mark.parametrize("relative", ["node_modules", "site/node_modules"])
@pytest.mark.parametrize("tool,args", [("npm", ["install"]), ("npx", ["npm", "install"])])
def test_child_env_refuses_shared_install_without_changing_git_gh(layout, monkeypatch, relative, tool, args):
    """Dispatch PATH must guard shared installs without enabling legacy git/gh policies."""
    primary, worktree = layout
    for legacy in git.NODE_LINKS:
        link = worktree / legacy
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(primary / legacy)
    target = primary / relative / "sentinel"
    target.write_text("keep")
    binaries = worktree.parent / "bin"
    binaries.mkdir()
    for name in ("npm", "npx"):
        binary = binaries / name
        binary.write_text(
            '#!/bin/sh\ncase "$*" in *install*) printf corrupted > node_modules/sentinel;; *) echo real-tool;; esac\n'
        )
        binary.chmod(0o755)
    original_path = os.pathsep.join(
        [str(binaries), *(entry for entry in os.get_exec_path() if not entry.endswith("/lu_machinery/agent_runtime/shims"))]
    )
    monkeypatch.setenv("PATH", original_path)
    spec = TaskSpec("fixture", "codex", "stub", None, "workspace-write", worktree, worktree / "prompt", worktree / "final")
    env = environment.child_env(spec)
    result = subprocess.run(
        [tool, *args], cwd=worktree / Path(relative).parent, env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1, result.stderr
    assert "shim refused" in result.stderr
    assert target.read_text() == "keep"
    assert sorted(path.name for path in target.parent.iterdir()) == ["sentinel"]
    shim_dir = Path(env["PATH"].split(os.pathsep)[0])
    assert sorted(path.name for path in shim_dir.iterdir()) == ["npm", "npx"]
    for name in ("git", "gh"):
        real = shutil.which(name, path=original_path)
        assert real is not None
        assert shutil.which(name, path=env["PATH"]) == real
    monkeypatch.setenv("PATH", env["PATH"])
    repeated = environment.child_env(spec)
    passthrough = subprocess.run(
        [tool, "--version"], cwd=worktree, env=repeated, capture_output=True, text=True, timeout=10
    )
    assert passthrough.returncode == 0, passthrough.stderr
    assert passthrough.stdout == "real-tool\n"


@pytest.mark.parametrize("missing", ["directory", "npm", "npx", "npm_guard.py", "interpreter"])
def test_unavailable_guard_keeps_new_installs_isolated_and_refuses_legacy_links(layout, monkeypatch, missing):
    """Missing npm/npx protection must not expose the primary install to children."""
    primary, worktree = layout
    if missing == "directory":
        monkeypatch.setattr(environment, "_NPM_SHIMS", worktree / "missing-shims")
    else:
        runtime = Path(environment.__file__).resolve().parents[1] / "agent_runtime"
        unavailable = Path(sys.executable) if missing == "interpreter" else runtime / (
            f"shims/{missing}" if missing in ("npm", "npx") else missing
        )
        access = os.access
        monkeypatch.setattr(os, "access", lambda path, mode: path != unavailable and access(path, mode))
    for relative in LINKS:
        assert not (worktree / relative).is_symlink()
    (worktree / "node_modules").symlink_to(primary / "node_modules")
    spec = TaskSpec("fixture", "codex", "stub", None, "workspace-write", worktree, worktree / "prompt", worktree / "final")
    with pytest.raises(ValueError, match="npm/npx guard unavailable"):
        environment.child_env(spec)


@pytest.mark.parametrize("tool", ["npm", "npx"])
@pytest.mark.parametrize("route", ["child", "legacy_path", "legacy_pinned"])
def test_foreign_dispatcher_shims_cannot_recurse(layout, monkeypatch, tool, route):
    """Nested dispatch and legacy lookup must reach real tools without wrapper recursion."""
    _primary, worktree = layout
    foreign = worktree.parent / "other/lu_machinery/dispatcher/npm_shims"
    shutil.copytree(environment._NPM_SHIMS, foreign)
    runtime = Path(environment.__file__).resolve().parents[1] / "agent_runtime"
    (foreign.parents[1] / "agent_runtime").symlink_to(runtime, target_is_directory=True)
    binaries = worktree.parent / "bin"
    binaries.mkdir()
    binary = binaries / tool
    binary.write_text('#!/bin/sh\nprintf "real-tool\\n"\n')
    binary.chmod(0o755)
    original_path = os.pathsep.join([f"{foreign}/.", str(binaries), "/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", original_path)
    spec = TaskSpec("fixture", "codex", "stub", None, "workspace-write", worktree, worktree / "prompt", worktree / "final")
    env = environment.child_env(spec)
    if route == "child":
        assert str(foreign) not in env["PATH"]
        assert str(foreign) not in env["AGENT_ORIGINAL_PATH"]
    else:
        env["AGENT_ORIGINAL_PATH"] = original_path
        if route == "legacy_pinned":
            env[f"AGENT_REAL_{tool.upper()}"] = str(foreign / tool)
    result = subprocess.run(
        [tool, "--version"], cwd=worktree, env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "real-tool\n"


def test_product_and_release_shims_on_one_path_reach_real_npm(tmp_path):
    """Mutual shim recursion: a product checkout's npm shims (under `scripts`) and the release's on one PATH.

    A shim that skips only its own package's shim directories execs the other copy, which execs it back.
    """
    package = Path(environment.__file__).resolve().parents[1]
    product = tmp_path / "product" / "scripts"
    for relative in ("agent_runtime/shims", "dispatcher/npm_shims"):
        shutil.copytree(package / relative, product / relative)
    shutil.copy2(package / "agent_runtime" / "npm_guard.py", product / "agent_runtime" / "npm_guard.py")
    (product / "lib").mkdir()
    shutil.copy2(package / "lib" / "project_interpreter.sh", product / "lib" / "project_interpreter.sh")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for tool in ("npm", "npx"):
        binary = binaries / tool
        binary.write_text(f'#!/bin/sh\nprintf "real-{tool}\\n"\n')
        binary.chmod(0o755)
    release = [str(package / "dispatcher" / "npm_shims"), str(package / "agent_runtime" / "shims")]
    foreign = [str(product / "dispatcher" / "npm_shims"), str(product / "agent_runtime" / "shims")]
    for first, second in ((release, foreign), (foreign, release)):
        path = os.pathsep.join([*first, *second, str(binaries), "/usr/bin", "/bin"])
        env = {"PATH": path, "HOME": str(tmp_path), "LEARN_UKRAINIAN_PYTHON": sys.executable}
        for tool in ("npm", "npx"):
            result = subprocess.run(
                [tool, "--version"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10
            )
            assert result.returncode == 0, (first[0], tool, result.stderr)
            assert result.stdout == f"real-{tool}\n"


pytestmark = pytest.mark.usefixtures("configured_roots")
