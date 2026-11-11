"""Shared dispatcher fixtures."""

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import venv
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import yaml
from lu_machinery.dispatcher import git, worker
from tests.dispatcher.worker_reap import Reaper, reap_root
from lu_machinery.common.repo_root import machinery_root





DOCTOR_NOT_FETCHED = "Managed settings (remote): not fetched — requires an Enterprise or Team subscription"


@pytest.fixture
def sources_product_root(configured_roots, tmp_path_factory, monkeypatch):
    """Keep Sources MCP data local to these dispatcher cases, away from other tranches."""
    product = tmp_path_factory.getbasetemp() / "t5-sources-product"
    if not product.exists():
        shutil.copytree(configured_roots.product, product)
        source = Path(__file__).parent / "fixtures" / "product.mcp.json"
        (product / ".mcp.json").write_bytes(source.read_bytes())
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    return product


@pytest.fixture(scope="session", autouse=True)
def dispatcher_worker_reaper() -> Iterator[Reaper]:
    """Fail the session if a dispatcher test left a detached worker alive.

    Defect class: a test leaks a detached worker process. The failure names that
    test and the pid. Per-test teardown kills the worker's group; this check is
    what remains at session end.
    """
    reaper = Reaper.start()
    try:
        yield reaper
    finally:
        reaper.finish()


@pytest.fixture(autouse=True)
def end_detached_dispatcher_workers(request: pytest.FixtureRequest, tmp_path: Path, dispatcher_worker_reaper: Reaper):
    """End workers this test detached, including when pytest-timeout uses ``os._exit``.

    The reaper is armed before the test body. Fixture teardown kills the groups.
    ``os._exit`` skips that teardown; the reaper's parent-death pipe does not.
    """
    dispatcher_worker_reaper.activate(tmp_path, request.node.nodeid)
    try:
        yield
    finally:
        dispatcher_worker_reaper.reap(tmp_path)


@pytest.fixture(autouse=True)
def claude_doctor(tmp_path_factory, monkeypatch):
    """A read-only Claude dispatch asks ``claude doctor`` for server-managed settings; answer as a consumer login.

    CI has no Claude Code, and a developer host's own login must not decide a test. Tests that start a
    provider ``claude`` put their own stub first on ``PATH``.
    """
    return install_claude_doctor_stub(tmp_path_factory.mktemp("claude-doctor"), monkeypatch)


def install_claude_doctor_stub(bin_dir, monkeypatch):
    """Put a ``claude`` that answers only ``claude doctor``, as a consumer login, first on ``PATH``."""
    stub = bin_dir / "claude"
    stub.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"if sys.argv[1:] != ['doctor']:\n    sys.exit('claude doctor stub: ' + ' '.join(sys.argv[1:]))\n"
        f"print({DOCTOR_NOT_FETCHED!r})\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    return stub



SOURCE = machinery_root().parent

# The stub provider switches behavior on the model string. These names are not
# catalog ids. The sandbox copy aliases them to the sol seat so the unknown-pin
# guard admits the fixture and the provider still receives the original string.
# The production catalog does not gain them.
STUB_MODELS = (
    "echo", "blocked", "tree", "governed", "escape", "pushed", "unpushed", "dirty",
    "pushed-dirty", "pushed-nested", "capacity", "capacity-dirty", "file", "crash",
    "error", "empty", "fail", "cancel", "vendor/model",
    "requested-model-is-not-proof", "explicit-model", "stub", "approved-model",
    "unknown-model",
)


@pytest.fixture(autouse=True)
def admit_stub_models(monkeypatch):
    """Fixture model strings are not catalog ids. Waive only the unknown-pin guard."""
    from lu_machinery.dispatcher import checks

    real_admission = checks.check_model_admission

    def admission(agent, model, mode, options, prompt):
        if str(model or "").strip().split("[", 1)[0] in STUB_MODELS:
            return None
        return real_admission(agent, model, mode, options, prompt)

    monkeypatch.setattr(checks, "check_model_admission", admission)


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True, timeout=30
    ).stdout.strip()


@pytest.fixture
def sandbox(tmp_path, hermetic_monitor, configured_roots, sources_product_root):
    repo, tasks = tmp_path / "public", tmp_path / "tasks"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.email", "stub@example.invalid")
    run_git(repo, "config", "user.name", "Stub")
    # Test doubles are overlaid on bytes from the installed wheel, never checkout code.
    for package in ("dispatcher", "common", "review", "storage", "lib", "fleet", "config", "schemas", "sql"):
        shutil.copytree(SOURCE / "lu_machinery" / package, repo / "lu_machinery" / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(SOURCE / "lu_machinery/dispatch.py", repo / "lu_machinery/dispatch.py")
    (repo / "lu_machinery/__init__.py").write_text(
        f"__path__.append({str(SOURCE / 'lu_machinery')!r})\n"
    )
    shutil.copytree(SOURCE / "agents_extensions", repo / "agents_extensions", ignore=shutil.ignore_patterns("__pycache__"))
    config = repo / "lu_machinery/config"
    (config / "fleet_repos.yaml").write_text(
        yaml.safe_dump(
            {
                "repos": {
                    "public": {
                        "default": True,
                        "local_name": "public",
                        "github": str(remote),
                        "transport": "file",
                        "role": "public-monorepo",
                    },
                    "private": {"local_name": "private", "github": "example/private", "role": "private-product"},
                }
            }
        )
    )
    shutil.copy(SOURCE / "lu_machinery/config/model_catalog.yaml", config / "model_catalog.yaml")
    catalog_copy = yaml.safe_load((config / "model_catalog.yaml").read_text())
    sol = catalog_copy["models"]["gpt-6.1-sol"]
    sol["aliases"] = [*(sol.get("aliases") or []), *STUB_MODELS]
    (config / "model_catalog.yaml").write_text(yaml.safe_dump(catalog_copy, sort_keys=False))
    shutil.copy(SOURCE / "lu_machinery/config/credit_lanes.yaml", config / "credit_lanes.yaml")
    # This checkout is the product checkout of the copied machinery (product_root, C-4746): it
    # carries the product marker, and the product files the dispatcher reads come from it, with
    # the review hooks module the hooks file names.
    marker = repo / "curriculum/l2-uk-en/curriculum.yaml"
    marker.parent.mkdir(parents=True)
    marker.write_text("levels: {}\n")
    (repo / "scripts/curriculum").mkdir(parents=True)
    for name in ("review_hooks.v1.json", "admission_policy.v1.json"):
        shutil.copy(configured_roots.product / "scripts/curriculum" / name, repo / "scripts/curriculum")
    shutil.copytree(sources_product_root, repo, dirs_exist_ok=True)
    schema_dir = repo / "schemas"
    schema_dir.mkdir()
    shutil.copy(SOURCE / "lu_machinery/schemas/code-review-findings.v1.schema.json", schema_dir)
    shutil.copy(Path(__file__).with_name("stub_provider.py"), repo / "lu_machinery/dispatcher/providers/codex.py")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "fixture")
    # Origin advertises main as its default branch: review authorship is read from it (C-2902).
    run_git(repo, "init", "--bare", "--initial-branch=main", str(remote))
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(repo, "push", "-u", "origin", "main")
    # The dispatcher runs as a subprocess here, out of reach of the in-process probe stub
    # (conftest ``dispatch_slice_probe``); without the forced fallback it asks the host user
    # manager and starts every stub worker in a real ``lu-worker-*`` scope.
    env = {**os.environ, "LU_TASKS_DIR": str(tasks), "LU_DISPATCH_ISOLATION": "fallback", "LU_PRODUCT_ROOT": str(repo), "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": str(repo)}
    env.pop("PYTHONPATH", None)
    # The dispatcher launches its supervisor and hooks as `sys.executable -I -m lu_machinery...`, so neither
    # the working directory nor PYTHONPATH reaches them. This interpreter's site imports the overlay first,
    # then the installed wheel, so those children run the same test doubles as the dispatcher itself.
    interpreter = tmp_path / "interpreter"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(interpreter)
    site = Path(subprocess.run(
        [str(interpreter / "bin/python"), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        text=True, capture_output=True, check=True, timeout=30,
    ).stdout.strip())
    (site / "dispatcher-overlay.pth").write_text(f"{repo}\n{sysconfig.get_paths()['purelib']}\n")
    python = str(interpreter / "bin/python")

    def cli(*args, expected=0, stdin=None):
        result = subprocess.run(
            [python, "lu_machinery/dispatch.py", *args],
            cwd=repo,
            env=env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == expected, (result.stdout, result.stderr)
        return result

    def dispatch(model="echo", *args):
        result = cli("dispatch", "--agent", "sol", "--model", model, "--prompt", "literal final response", *args)
        task_id, nonce = result.stdout.splitlines()
        return task_id, nonce

    def settle(task, expected=0):
        deadline = time.monotonic() + 15
        record = json.loads(
            cli(
                "wait", task[0], "--run-nonce", task[1], "--timeout", "15", "--poll-interval", "0.02", expected=expected
            ).stdout
        )
        # A terminal record precedes supervisor exit; recovery must see its claim released (#12121).
        while worker._identity(record["pid"]) == record["process_start"]:
            assert time.monotonic() < deadline, "supervisor still alive after settlement"
            time.sleep(0.02)
        return record

    def sweep():
        result = subprocess.run(
            [sys.executable, "-c", "from lu_machinery.dispatcher.worker import settle_left_worktrees; settle_left_worktrees()"],
            cwd=repo, env=env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    def review_models(*models):
        """File the stub reviewer ``models`` under OpenAI in the sandbox catalog copy only."""
        catalog = yaml.safe_load((config / "model_catalog.yaml").read_text())
        catalog["models"]["gpt-6.1-sol"].setdefault("runtime_model_ids", []).extend(models)
        (config / "model_catalog.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False))

    def review_target(*models):
        """Arguments for a read-only review of one Anthropic-authored commit by the stub ``models``.

        A review needs a reviewed range and a reviewer family outside its authors (C-2902).
        """
        review_models(*models)
        head = run_git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m",
                       "authored change\n\nX-Agent: claude/claude-opus-5-5")
        return "--base", "main", "--pinned-head", head

    box = SimpleNamespace(repo=repo, tasks=tasks, remote=remote, env=env, python=python, cli=cli, dispatch=dispatch,
                          settle=settle, sweep=sweep, review_models=review_models, review_target=review_target)
    try:
        yield box
    finally:
        # cancel() only signals a supervisor that is still the recorded process. The provider
        # is its own session, and pytest-timeout's os._exit never reaches this fixture.
        reap_root(tmp_path)
        shutil.rmtree(repo / "lu_machinery")
        shutil.rmtree(repo / "agents_extensions")


def await_file(path, *, ready=lambda content: True):
    deadline = time.monotonic() + 10
    while not path.exists() or not (content := path.read_text()) or not ready(content):
        assert time.monotonic() < deadline, f"stub did not populate {path.name}"
        time.sleep(0.02)
    return content


def prior_worktree(sandbox, name="prior-round", start="origin/main"):
    """An earlier round's worktree; a task's own clean worktree is removed when it settles."""
    tree = sandbox.repo / ".worktrees/dispatch/codex" / name
    git.create_worktree(sandbox.repo, tree, f"codex/{name}", run_git(sandbox.repo, "rev-parse", start))
    return tree


def local_branches(repo):
    return run_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def in_process_repo(sandbox, monkeypatch):
    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(sandbox.repo))
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(sandbox.repo))
    monkeypatch.setattr(git, "machinery_path", lambda _rel: sandbox.repo / "lu_machinery/config/fleet_repos.yaml")


@pytest.fixture
def delivery_settle(sandbox):
    run_git(sandbox.repo, "push", "origin", "main:release")
    # The probes reuse one worktree; a borrowed (--cwd) worktree survives each settlement.
    tree = prior_worktree(sandbox, "delivery-probe", "origin/release")
    record = sandbox.settle(sandbox.dispatch("echo", "--cwd", str(tree), "--base", "origin/release"))
    # Synthetic write records need the trusted remote omitted from read-only dispatches.
    record["worktree_remote_url"] = str(sandbox.remote)
    # Existing settle probes also cover records created before the start-SHA field.
    record.pop("worktree_start_sha", None)
    def settle(model="echo", owned=("delivered.txt",)):
        record.update(mode="workspace-write", model=model, status="running", owned_paths=list(owned or []))
        if owned is None:
            record.pop("owned_paths")
        path = sandbox.tasks / f"{record['task_id']}.json"
        path.write_text(json.dumps(record))
        subprocess.run([
            sys.executable, "-c", "import sys; from pathlib import Path; "
            "from lu_machinery.dispatcher.worker import supervise; "
            "supervise(Path(sys.argv[1]), {}, 0.2 if sys.argv[2] == 'blocked' else 10)", str(path), model,
        ], cwd=sandbox.repo, env=sandbox.env, check=True, timeout=30)
        return json.loads(path.read_text())
    return record, settle


@pytest.fixture
def merged_delivery_world(tmp_path, monkeypatch):
    """A write task whose branch cannot show its delivery, with a scripted GitHub and ownership ledger."""
    from lu_machinery.common import github_client
    from lu_machinery.orchestration import pr_ownership

    world = SimpleNamespace(
        prs={}, files={}, listed=[], published=(None, True), lookup_error=None,
        record={
            "task_id": "merge-probe", "agent": "claude", "mode": "workspace-write", "status": "done", "error": None,
            "owned_paths": ["docs/owned.md"], "worktree_remote_url": "https://github.com/org/repo.git",
            "worktree_base_ref": "origin/main", "worktree_branch": "claude/merge-probe", "publish_branch": None,
            "started_at": "2026-10-29T08:00:00+00:00", "head_sha": "a" * 40, "commits_ahead": 0,
            "push_state": "unpushed", "worktree_dirty_on_exit": False,
        },
    )

    def gh(args, **_):
        if world.lookup_error:
            raise world.lookup_error
        words = args[1:]
        out = (json.dumps([{"number": pr["number"],
                            "head": {"sha": pr["headRefOid"], "repo": {"full_name": "org/repo"}},
                            "base": {"repo": {"full_name": "other/repo" if pr["isCrossRepository"] else "org/repo"}}}
                           for pr in world.listed if "headRefOid" in pr])
               if words[0] == "api" and words[2].endswith("/pulls")
               else json.dumps("deliver\n\nX-Agent: claude/merge-probe")
               if words[0] == "api" and words[2].endswith("/commits")
               else json.dumps(world.listed) if words[:2] == ["pr", "list"]
               else json.dumps(world.prs[int(words[2])]) if words[:2] == ["pr", "view"]
               else "\n".join(json.dumps(name) for name in world.files[int(words[2].split("/")[4])]))
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    state = tmp_path / "state.json"
    monkeypatch.setattr(pr_ownership, "state_path", lambda: state)
    monkeypatch.setattr(github_client, "run", gh)
    monkeypatch.setattr(git, "_canonical_publication", lambda *_: world.published)
    monkeypatch.setattr(git, "ownership_git", Mock(side_effect=subprocess.CalledProcessError(
        128, ["git", "fetch"], stderr="branch gone")))

    def accept(*numbers):
        history = [{"event": "accepted", "pr": f"org/repo#{n}", "successor": "merge-probe"} for n in numbers]
        state.write_text(json.dumps({"schema": 1, "last_tick": None, "episodes": {}, "handoffs": {},
                                     "history": history, "outbox": []}))

    world.accept = accept
    return world


MERGED = {"state": "MERGED", "baseRefName": "main", "mergedAt": "2026-10-29T09:00:00Z",
          "mergeCommit": {"oid": "c" * 40}, "isCrossRepository": False}
