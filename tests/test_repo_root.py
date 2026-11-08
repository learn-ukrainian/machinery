"""Unit and integration tests for repository root resolver and primary checkout anchoring."""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common import repo_root as rr
from lu_machinery.common.repo_root import main_checkout_root, project_interpreter
from lu_machinery.orchestration import worktree_claims

REPO = Path(__file__).resolve().parents[1]


def test_main_checkout_root_resolves_primary_checkout_from_worktree(tmp_path):
    """Verify that main_checkout_root correctly identifies primary repo root from a worktree."""
    main_repo = tmp_path / "main"
    worktree = main_repo / ".worktrees" / "dispatch" / "agy" / "task-123"
    git_dir = main_repo / ".git" / "worktrees" / "task-123"

    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    assert main_checkout_root(worktree) == main_repo


@pytest.fixture
def wt_layout(tmp_path) -> tuple[Path, Path]:
    """Sets up a mock primary repo and a mock worktree layout.

    Returns a tuple of (mock_primary_root, mock_worktree_root).
    """
    main_repo = tmp_path / "main"
    worktree = main_repo / ".worktrees" / "dispatch" / "agy" / "task-123"
    git_dir = main_repo / ".git" / "worktrees" / "task-123"

    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    # Copy the bridge and common module code files to the mock worktree
    src_root = Path(__file__).resolve().parents[1]

    # Create empty init for the bridge package to avoid loading other bridge modules
    bridge_init = worktree / "lu_machinery" / "ai_agent_bridge" / "__init__.py"
    bridge_init.parent.mkdir(parents=True, exist_ok=True)
    bridge_init.write_text("", encoding="utf-8")

    files_to_copy = [
        "lu_machinery/common/__init__.py",
        "lu_machinery/common/bridge_paths.py",
        "lu_machinery/common/repo_root.py",
        "lu_machinery/common/github_client.py",
        "lu_machinery/ai_agent_bridge/_config.py",
        "lu_machinery/ai_agent_bridge/_env.py",
        "lu_machinery/ai_agent_bridge/_dispatch_wrappers.py",
        "lu_machinery/ai_agent_bridge/_monitor_cache.py",
        "lu_machinery/review/__init__.py",
        "lu_machinery/review/verdict_parser.py",
    ]
    for f in files_to_copy:
        dest = worktree / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((src_root / f).read_text(encoding="utf-8"), encoding="utf-8")

    return main_repo, worktree


def test_bridge_config_state_paths_resolve_to_primary_in_worktree(wt_layout):
    """Verify that _config.py run from a worktree layout resolves state paths to the primary root.

    lu_machinery derives no state anchor (C-4843), so the primary is configured, as installed
    machinery has it; the worktree stays the working directory.
    """
    main_repo, worktree = wt_layout

    # Run subprocess to check DB_PATH and PID_DIR
    script = "from lu_machinery.ai_agent_bridge._config import DB_PATH, PID_DIR; print(f'{DB_PATH};{PID_DIR}')"
    env = os.environ.copy()
    env.pop("AB_DB_PATH", None)  # Exercise the default path, not the suite's isolated override.
    env["LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"] = str(main_repo)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(worktree),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    db_path_str, pid_dir_str = proc.stdout.strip().split(";")

    expected_db_path = main_repo / ".mcp" / "servers" / "message-broker" / "messages.db"
    expected_pid_dir = main_repo / ".mcp" / "servers" / "message-broker" / "pids"

    assert Path(db_path_str).resolve() == expected_db_path.resolve()
    assert Path(pid_dir_str).resolve() == expected_pid_dir.resolve()


def _write_venv_python(checkout: Path, name: str = "python") -> Path:
    interpreter = checkout / ".venv" / "bin" / name
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    return interpreter


def _link_worktree(primary: Path, worktree: Path) -> None:
    git_dir = primary / ".git" / "worktrees" / "task"
    git_dir.mkdir(parents=True, exist_ok=True)
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")


def test_project_interpreter_rejects_another_checkouts_venv(tmp_path, monkeypatch):
    """An explicit root must not inherit a different checkout's interpreter.

    ``scripts.project_api.batch_router`` passes ``live_repo_root`` to
    ``project_interpreter`` (the dispatcher scan command). A missing checkout
    used to accept any running ``sys.executable`` ending in ``.venv/bin/python``,
    so ``project_interpreter(Path("/no-such-checkout"))`` returned this repo's
    interpreter.
    """
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))

    with pytest.raises(FileNotFoundError, match="not the requested checkout"):
        project_interpreter(Path("/no-such-checkout"))


def test_project_interpreter_accepts_running_python3_of_the_primary_checkout(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    python3 = _write_venv_python(primary, "python3")
    monkeypatch.setattr(sys, "executable", str(python3))

    assert project_interpreter(worktree) == python3


def _toolchain_python(tmp_path: Path) -> Path:
    toolchain = tmp_path / "toolchain" / "python3.12"
    toolchain.parent.mkdir(parents=True, exist_ok=True)
    toolchain.write_text("", encoding="utf-8")
    return toolchain


def _symlink_venv_python(checkout: Path, target: Path, name: str = "python3.12") -> Path:
    interpreter = checkout / ".venv" / "bin" / name
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.symlink_to(target)
    return interpreter


def _symlink_checkout_venv_to_store(checkout: Path, store: Path, toolchain: Path) -> Path:
    """Point ``checkout/.venv`` at ``store`` and return the ``python3.12`` entry.

    The store has no ``bin/python``, so resolution cannot stop at the early
    ``.venv/bin/python`` match and has to classify ``sys.executable``.
    """
    entry = store / "bin" / "python3.12"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(toolchain)
    checkout.mkdir(parents=True, exist_ok=True)
    venv = checkout / ".venv"
    venv.symlink_to(os.path.relpath(store, checkout))
    executable = venv / "bin" / "python3.12"
    assert venv.is_symlink()
    assert executable.parent.parent.name == ".venv"
    assert not (store / "bin" / "python").exists()
    assert Path(os.path.realpath(executable.parent)).parent == store.resolve()
    return executable


@pytest.mark.parametrize("shape", ["direct", "alias-to-venv", "venv-to-store", "alias-to-venv-to-store"])
@pytest.mark.parametrize("own_venv", [False, True], ids=["foreign-refused", "own-accepted"])
def test_project_interpreter_classifies_each_venv_symlink_shape(tmp_path, monkeypatch, shape, own_venv):
    """Every intermediate .venv owner counts, including one hidden by a second hop."""
    requested = tmp_path / "requested"
    owner = requested if own_venv else tmp_path / "foreign"
    toolchain = _toolchain_python(tmp_path)
    if shape in {"venv-to-store", "alias-to-venv-to-store"}:
        executable = _symlink_checkout_venv_to_store(owner, tmp_path / "store", toolchain)
    else:
        executable = _symlink_venv_python(owner, toolchain)
    if shape in {"alias-to-venv", "alias-to-venv-to-store"}:
        alias = tmp_path / "venv-alias"
        alias.symlink_to(owner / ".venv")
        executable = alias / "bin" / "python3.12"
    monkeypatch.setattr(sys, "executable", str(executable))

    if own_venv:
        assert project_interpreter(requested) == executable
    else:
        with pytest.raises(FileNotFoundError, match="not the requested checkout"):
            project_interpreter(requested)


def test_project_interpreter_refuses_directory_symlink_loop(tmp_path, monkeypatch):
    """A loop cannot be mistaken for hosted Python, even without a named owner."""
    alias_a = tmp_path / "alias-a"
    alias_b = tmp_path / "alias-b"
    alias_a.symlink_to(alias_b)
    alias_b.symlink_to(alias_a)
    monkeypatch.setattr(sys, "executable", str(alias_a / "bin" / "python3.12"))

    with pytest.raises(FileNotFoundError, match=r"^project interpreter not found: .* is a symlink loop$"):
        project_interpreter(tmp_path / "requested")


def test_project_interpreter_refuses_an_unreadable_path_component(tmp_path, monkeypatch):
    """A PermissionError in the walk reaches callers as FileNotFoundError (#9201)."""
    blocked = tmp_path / "blocked"
    executable = blocked / "bin" / "python3.12"
    executable.parent.mkdir(parents=True)
    executable.touch()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(FileNotFoundError, match=r"^project interpreter not found: .* cannot be inspected: ") as excinfo:
        project_interpreter(tmp_path / "requested")
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_project_interpreter_refuses_a_final_link_that_vanishes_before_readlink(tmp_path, monkeypatch):
    """A race on the final readlink fails closed instead of reading as hosted Python (#9201)."""
    toolchain = _toolchain_python(tmp_path)
    alias = tmp_path / "hosted" / "python3.12"
    alias.parent.mkdir()
    alias.symlink_to(toolchain)
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        if Path(path) == alias:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", readlink)
    monkeypatch.setattr(sys, "executable", str(alias))

    with pytest.raises(FileNotFoundError, match=r"^project interpreter not found: .* cannot be inspected: "):
        project_interpreter(tmp_path / "requested")


def test_project_interpreter_refuses_a_final_path_that_vanishes_before_the_link_check(tmp_path, monkeypatch):
    """A path gone before the link check is refused, not accepted as hosted Python (#9201)."""
    vanished = tmp_path / "hosted" / "python3.12"
    vanished.parent.mkdir()
    vanished.symlink_to(_toolchain_python(tmp_path))
    vanished.unlink()
    monkeypatch.setattr(sys, "executable", str(vanished))

    with pytest.raises(FileNotFoundError, match=r"^project interpreter not found: .* cannot be inspected: ") as excinfo:
        project_interpreter(tmp_path / "requested")
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)


def test_project_interpreter_rejects_another_checkouts_versioned_python(tmp_path, monkeypatch):
    """A missing checkout must not inherit another checkout's ``python3.12``.

    The ownership check used to allow only ``python`` and ``python3``, so
    ``.venv/bin/python3.12`` was returned as if it were a hosted interpreter.
    """
    foreign = _write_venv_python(tmp_path / "other-checkout", "python3.12")
    monkeypatch.setattr(sys, "executable", str(foreign))

    with pytest.raises(FileNotFoundError, match="not the requested checkout"):
        project_interpreter(Path("/no-such-checkout"))


def test_project_interpreter_rejects_a_versioned_symlink_out_of_another_checkouts_venv(tmp_path, monkeypatch):
    """``python3.12`` still belongs to its venv when the file is a symlink to the toolchain."""
    foreign = _symlink_venv_python(tmp_path / "other-checkout", _toolchain_python(tmp_path))
    monkeypatch.setattr(sys, "executable", str(foreign))

    with pytest.raises(FileNotFoundError, match="not the requested checkout"):
        project_interpreter(Path("/no-such-checkout"))


def test_project_interpreter_rejects_a_symlink_that_resolves_into_another_checkouts_venv(tmp_path, monkeypatch):
    """A symlink outside ``.venv/bin`` counts when its target is that entrypoint."""
    foreign = _write_venv_python(tmp_path / "other-checkout", "python3.12")
    alias = tmp_path / "runner"
    alias.symlink_to(Path("other-checkout") / ".venv" / "bin" / "python3.12")
    assert alias.resolve() == foreign.resolve()
    monkeypatch.setattr(sys, "executable", str(alias))

    with pytest.raises(FileNotFoundError, match="not the requested checkout"):
        project_interpreter(Path("/no-such-checkout"))


def test_project_interpreter_rejects_when_parent_resolution_names_another_checkout(tmp_path, monkeypatch):
    """View U may name the requested checkout while view R names a foreign one.

    ``requested/.venv`` points at ``foreign/.venv``. The unresolved path names
    the requested checkout; parent resolution names the foreign checkout. Any
    foreign owner is refused.
    """
    foreign = tmp_path / "foreign-checkout"
    _symlink_venv_python(foreign, _toolchain_python(tmp_path))
    requested = tmp_path / "requested"
    requested.mkdir()
    (requested / ".venv").symlink_to(Path("..") / "foreign-checkout" / ".venv")
    executable = requested / ".venv" / "bin" / "python3.12"
    assert executable.parent.parent.name == ".venv"
    assert Path(os.path.realpath(executable.parent)) == (foreign / ".venv" / "bin").resolve()
    assert not (requested / ".venv" / "bin" / "python").exists()
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(FileNotFoundError, match="not the requested checkout"):
        project_interpreter(requested)


def test_project_interpreter_accepts_a_symlink_into_the_requested_checkouts_venv(tmp_path, monkeypatch):
    """A symlink that resolves into the requested checkout's ``.venv/bin`` is accepted."""
    requested = tmp_path / "requested"
    python312 = _write_venv_python(requested, "python3.12")
    alias = tmp_path / "runner"
    alias.symlink_to(python312)
    monkeypatch.setattr(sys, "executable", str(alias))

    assert project_interpreter(requested) == alias


def test_project_interpreter_accepts_hosted_python3_12(tmp_path, monkeypatch):
    """A versioned interpreter outside any checkout ``.venv/bin`` is the CI fallback."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    hosted = tmp_path / "hostedtoolcache" / "Python" / "3.12.15" / "x64" / "bin" / "python3.12"
    hosted.parent.mkdir(parents=True)
    hosted.write_text("", encoding="utf-8")
    alias = tmp_path / "hosted-alias"
    alias.symlink_to(hosted)
    monkeypatch.setattr(sys, "executable", str(alias))

    assert project_interpreter(checkout) == alias


def test_project_interpreter_accepts_the_requested_worktree_venv_when_primary_has_none(tmp_path, monkeypatch):
    """(b) When the primary checkout has no ``.venv``, the worktree's own interpreter is used."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    local = _write_venv_python(worktree)
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))

    assert project_interpreter(worktree) == local


def test_project_interpreter_prefers_the_primary_checkout_venv(tmp_path, monkeypatch):
    """(a) The primary checkout's ``.venv/bin/python`` wins when the worktree also has one."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    primary_python = _write_venv_python(primary)
    _write_venv_python(worktree)
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))

    assert project_interpreter(worktree) == primary_python


def test_project_interpreter_uses_the_primary_venv_when_the_checkout_has_none(tmp_path, monkeypatch):
    """(a) A linked worktree with no ``.venv`` uses the primary checkout's interpreter."""
    primary = tmp_path / "primary"
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "task"
    _link_worktree(primary, worktree)
    primary_python = _write_venv_python(primary)
    foreign = _write_venv_python(tmp_path / "other-checkout")
    monkeypatch.setattr(sys, "executable", str(foreign))

    assert project_interpreter(worktree) == primary_python


# --------------------------------------------------------------------------- C-4843 root contract, step 1
#
# Installed lu_machinery has no implicit product and no implicit state anchor; only the
# in-tree source copy keeps its checkout defaults. Explicit selection needs no curriculum
# marker, an invalid selection never falls through, decoys in the working directory are
# never consulted, and resolution creates nothing.

_ROOT_ENV = (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV)
_copy_ids = itertools.count()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ROOT_ENV:
        monkeypatch.delenv(name, raising=False)


def _primary_checkout(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _decoy_cwd(path: Path) -> Path:
    """A directory that looks like a product primary holding dispatch state."""
    marker = path / rr.PRODUCT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("decoy: true\n", encoding="utf-8")
    (path / "batch_state" / "tasks").mkdir(parents=True)
    (path / "batch_state" / "fleet-comms" / "v1").mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _tree(root: Path) -> list[tuple[str, str]]:
    """Every entry under ``root`` (not following symlinks), with its kind."""
    entries = []
    for directory, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(directory, name)
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
            entries.append((path.relative_to(root).as_posix(), kind))
    return sorted(entries)


def _load_copy(monkeypatch: pytest.MonkeyPatch, package_dir: Path):
    """Load a copy of this resolver from ``package_dir/common/repo_root.py`` (as hooks load it, by file).

    The copy is registered in ``sys.modules`` for the test only (dataclasses look the module up).
    """
    target = package_dir / "common" / "repo_root.py"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rr.__file__, target)
    spec = importlib.util.spec_from_file_location(f"_repo_root_copy_{next(_copy_ids)}", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_installed_package_refuses_product_and_state_without_anchors(unconfigured, tmp_path, monkeypatch):
    """Defect class: installed machinery inventing a product or a state anchor from its own location or cwd."""
    assert rr.in_tree_source() is False
    assert rr.machinery_root().name == rr.INSTALLED_PACKAGE_NAME
    decoy = _decoy_cwd(tmp_path / "decoy")
    monkeypatch.chdir(decoy)
    with pytest.raises(rr.ProductRootError, match=rr.PRODUCT_ROOT_ENV):
        rr.product_root()
    for resolve in (
        rr.state_root,
        rr.primary_root,
        rr.machinery_roots,
        lambda: rr.state_root(decoy),
        lambda: rr.primary_root(decoy),
        lambda: rr.state_root(REPO),
    ):
        with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
            resolve()
    # Consumers that catch the resolver's ValueError keep refusing.
    with pytest.raises(worktree_claims.ControlPlaneError, match=rr.PRIMARY_ROOT_ENV):
        worktree_claims.public_primary_root()
    # A state root alone does not stand in for the primary anchor.
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(_primary_checkout(tmp_path / "primary")))
    with pytest.raises(rr.StateRootError, match=rr.PRIMARY_ROOT_ENV):
        rr.state_root()


def test_the_installed_package_resolves_its_configured_anchors(unconfigured, tmp_path, monkeypatch):
    """Defect class: a configured anchor ignored, or replaced by a cwd decoy, in the installed package."""
    monkeypatch.chdir(_decoy_cwd(tmp_path / "decoy"))
    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "kube-dojo"
    product.mkdir()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    assert rr.product_root() == product
    assert rr.state_root() == rr.primary_root() == primary
    assert worktree_claims.public_primary_root() == primary
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary))
    assert rr.state_root() == primary
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(tmp_path / "decoy"))
    with pytest.raises(rr.MachineryConfigError, match="state cutover"):
        rr.state_root()


@pytest.mark.parametrize("in_tree", [False, True], ids=["installed", "in-tree"])
def test_an_explicit_product_needs_no_curriculum_marker(unconfigured, tmp_path, monkeypatch, in_tree):
    """Defect class: a non-curriculum product (kube-dojo) refused because it lacks Ukrainian curriculum data."""
    monkeypatch.setattr(rr, "_IN_TREE_SOURCE", in_tree)
    product = tmp_path / "kube-dojo"
    product.mkdir()
    assert not (product / rr.PRODUCT_MARKER).exists()
    assert rr.product_root(product) == product
    assert rr.product_root(str(product)) == product
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    assert rr.product_root() == product
    # Returned as given: a release snapshot link stays the snapshot path.
    snapshot = tmp_path / "current"
    snapshot.symlink_to(product, target_is_directory=True)
    assert rr.product_root(snapshot) == snapshot


@pytest.mark.parametrize("bad", ["relative", "missing", "file", "nul", "empty", "bytes"])
def test_an_invalid_explicit_product_never_falls_through(unconfigured, tmp_path, monkeypatch, bad):
    """Defect class: a mistyped selection silently replaced by the environment or the checkout default."""
    fallback = tmp_path / "configured"
    fallback.mkdir()
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    value = {
        "relative": "kube-dojo",
        "missing": str(tmp_path / "missing"),
        "file": str(tmp_path / "a-file"),
        "nul": f"{tmp_path}/x\x00y",
        "empty": "",
        "bytes": os.fsencode(fallback),
    }[bad]
    # An in-tree copy inside a real product checkout: the derived default would succeed.
    product = tmp_path / "product"
    (product / rr.PRODUCT_MARKER).parent.mkdir(parents=True)
    (product / rr.PRODUCT_MARKER).write_text("", encoding="utf-8")
    in_tree = _load_copy(monkeypatch, product / "scripts")
    assert in_tree.in_tree_source() is True
    assert in_tree.product_root() == product
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(fallback))
    for module in (rr, in_tree):
        with pytest.raises(module.ProductRootError):
            module.product_root(value)
    if bad not in ("empty", "bytes", "nul"):  # empty means unset; the OS refuses NUL and bytes in os.environ
        monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, value)
        for module in (rr, in_tree):
            with pytest.raises(module.ProductRootError, match=rr.PRODUCT_ROOT_ENV):
                module.product_root()


def test_the_mode_comes_from_the_package_directory_name_not_the_surroundings(unconfigured, tmp_path, monkeypatch):
    """Defect class: a copy inside a product checkout losing its default, or a decoy enabling one."""
    monkeypatch.chdir(_decoy_cwd(tmp_path / "decoy"))
    # Installed-name copy inside a directory that carries every product and state marker.
    staged = _decoy_cwd(tmp_path / "site-packages-ish")
    installed = _load_copy(monkeypatch, staged / rr.INSTALLED_PACKAGE_NAME)
    assert installed.in_tree_source() is False
    with pytest.raises(installed.ProductRootError):
        installed.product_root()
    with pytest.raises(installed.StateRootError):
        installed.state_root()
    # The in-tree copy keeps its established checkout defaults.
    product = _decoy_cwd(tmp_path / "product")
    in_tree = _load_copy(monkeypatch, product / "scripts")
    assert in_tree.in_tree_source() is True
    assert in_tree.product_root() == product
    assert in_tree.state_root() == in_tree.primary_root() == product
    # A package directory symlinked into the product keeps the in-tree name it is loaded under.
    linked = _decoy_cwd(tmp_path / "linked-product")
    (linked / "scripts").symlink_to(staged / rr.INSTALLED_PACKAGE_NAME, target_is_directory=True)
    assert _load_copy(monkeypatch, linked / "scripts").in_tree_source() is True
    # An in-tree copy outside any product still refuses rather than consulting cwd.
    stray = _load_copy(monkeypatch, tmp_path / "stray" / "scripts")
    with pytest.raises(stray.ProductRootError, match="not a product checkout"):
        stray.product_root()


def test_resolution_creates_nothing(unconfigured, tmp_path, monkeypatch):
    """Defect class: a resolver that creates state directories, files or databases as a side effect."""
    decoy = _decoy_cwd(tmp_path / "decoy")
    primary = _primary_checkout(tmp_path / "primary")
    product = tmp_path / "product"
    product.mkdir()
    # An in-tree product checkout without state yet: its defaults must not create any.
    in_tree_product = _primary_checkout(tmp_path / "in-tree-product")
    (in_tree_product / rr.PRODUCT_MARKER).parent.mkdir(parents=True)
    (in_tree_product / rr.PRODUCT_MARKER).write_text("", encoding="utf-8")
    in_tree = _load_copy(monkeypatch, in_tree_product / "scripts")
    monkeypatch.chdir(decoy)
    watched = (tmp_path, REPO / rr.INSTALLED_PACKAGE_NAME / "common")
    before = [_tree(root) for root in watched]
    repo_top = sorted(os.listdir(REPO))

    def every_resolution() -> None:
        for module in (rr, in_tree):
            for resolve, args in (
                (module.product_root, ()),
                (module.product_root, (product,)),
                (module.product_root, (tmp_path / "missing",)),
                (module.state_root, ()),
                (module.primary_root, ()),
                (module.machinery_roots, ()),
                (module.machinery_path, ("config/model_catalog.yaml",)),
            ):
                with contextlib.suppress(ValueError, FileNotFoundError):
                    resolve(*args)

    every_resolution()
    monkeypatch.setenv(rr.PRIMARY_ROOT_ENV, str(primary))
    monkeypatch.setenv(rr.PRODUCT_ROOT_ENV, str(product))
    every_resolution()
    monkeypatch.setenv(rr.STATE_ROOT_ENV, str(primary))
    every_resolution()
    assert [_tree(root) for root in watched] == before
    assert sorted(os.listdir(REPO)) == repo_top
    for root in (primary, product, in_tree_product):
        assert not (root / "batch_state").exists()


def test_package_imports_and_help_work_with_nothing_configured(tmp_path):
    """Defect class: a missing anchor breaking import or ``--help`` instead of the operation that needs it."""
    decoy = _decoy_cwd(tmp_path / "decoy")
    env = {key: value for key, value in os.environ.items() if key not in (*_ROOT_ENV, "LU_TASKS_DIR", "PYTHONPATH")}
    env["PYTHONPATH"] = str(REPO)
    before = _tree(decoy)
    probe = (
        "from lu_machinery.common import repo_root as rr\n"
        "for resolve in (rr.product_root, rr.state_root):\n"
        "    try:\n"
        "        resolve()\n"
        "    except rr.MachineryConfigError as exc:\n"
        "        print(type(exc).__name__)\n"
    )
    imported = subprocess.run(
        [sys.executable, "-P", "-c", probe], cwd=decoy, env=env, capture_output=True, text=True, timeout=120
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.split() == ["ProductRootError", "StateRootError"]
    helped = subprocess.run(
        [sys.executable, "-P", "-m", "lu_machinery.dispatch", "--help"],
        cwd=decoy,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert helped.returncode == 0, helped.stderr
    assert "usage" in helped.stdout.lower()
    assert _tree(decoy) == before


def test_gate_probe_deliberate_failure() -> None:
    """Scratch probe for PR 158: CI Gate must fail when the called pytest job fails."""
    assert False
