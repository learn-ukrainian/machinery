"""Installed-layout regression: package ancestors are never operational roots (Refs #100).

Each entry point runs in a clean process with the package copied outside Git.
Process spies stop paid dispatches and host probes at their boundary; the alert
publisher and publishing gh shim also run real child processes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_root

_PROBE = r'''
import importlib, json, os, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
site, product, primary, scenario = map(str, sys.argv[1:])
sys.path.insert(0, site)
product, primary = Path(product), Path(primary)
from lu_machinery.common import repo_root as rr
assert not rr.in_tree_source()
assert rr.machinery_root() == Path(site) / 'lu_machinery'
assert not (Path(site) / '.git').exists()
from lu_machinery.ai_agent_bridge import _dispatch_wrappers as wrappers
from lu_machinery.dispatcher import worker, git, primary as dispatcher_primary
from lu_machinery.orchestration import dispatch_admission as admission

if scenario == 'text':
    # This generic helper has no repository operation; the caller owns its cwd.
    for key in (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV):
        os.environ.pop(key, None)
    output = wrappers._run_text_command([sys.executable, '-c', 'from pathlib import Path; print(Path.cwd())'])
    assert output.strip() == str(product.parent), output
elif scenario == 'dispatch':
    with patch.object(wrappers.github_client, 'run', return_value=SimpleNamespace(returncode=0)) as run:
        assert wrappers._run_dispatch(['probe', '--task-id', 'root-probe'], False, product / 'prompt') == 0
    assert run.call_args.kwargs['cwd'] == primary
elif scenario in ('review_dispatch', 'review_wait'):
    prompt_dir = product / 'prompts'
    prompt_dir.mkdir()
    os.environ['LU_RUNTIME_TMP_ROOT'] = str(prompt_dir)
    reply = primary / 'reply'
    reply.write_text('VERDICT: APPROVED\n')
    target = SimpleNamespace(agent='claude', model='unused')
    with patch('lu_machinery.agent_runtime.target_admission.resolve_and_admit', return_value=(target,)), \
         patch.object(wrappers, 'build_ask_review_dispatch_command', return_value=['dispatch']), \
         patch.object(wrappers, 'build_ask_review_wait_command', return_value=['wait']), \
         patch.object(subprocess, 'run', side_effect=[
             SimpleNamespace(returncode=0),
             SimpleNamespace(returncode=0, stdout=json.dumps({'status':'done', 'result_file':str(reply)}))
         ]) as run:
        result = wrappers.run_ask_review_dispatch('claude', 'review', task_id='root-probe')
    assert result['ok'] is True, result
    index = 0 if scenario == 'review_dispatch' else 1
    assert run.call_args_list[index].kwargs['cwd'] == primary
elif scenario == 'supervisor':
    record_path = primary / 'batch_state/tasks/root-probe.json'
    record_path.parent.mkdir(parents=True)
    spec = SimpleNamespace(agent='claude', task_id='root-probe', options={})
    record = {'run_nonce':'nonce', 'admission_hold':{'held':True}}
    process = SimpleNamespace(pid=1234, stdin=Mock())
    with patch.object(worker.providers, 'load', return_value=SimpleNamespace()), \
         patch.object(worker.store, 'record_path', return_value=record_path), \
         patch.object(worker.store, 'create'), patch.object(worker.store, 'replace'), \
         patch.object(worker, '_identity', return_value='identity'), \
         patch.object(worker.admission, 'spawn_supervisor', return_value=(process, {})) as spawn:
        worker.start(spec, record, b'prompt', 30)
    assert spawn.call_args.kwargs['cwd'] == primary
    process.stdin.write.assert_called_once()
elif scenario == 'provenance':
    # Give the configured primary real metadata: it still does not identify the wheel.
    subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=primary, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=primary, check=True)
    subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'fixture'], cwd=primary, check=True)
    assert worker._dispatcher_code() == {'root':str(primary), 'head':None, 'clean':False}
elif scenario == 'primary':
    # A git probe in site-packages is wrong even if it happens to return that directory.
    with patch.object(dispatcher_primary, 'main_checkout_root', side_effect=AssertionError('package Git probe')), \
         patch.object(dispatcher_primary.os, 'execv', side_effect=AssertionError('product re-exec')):
        assert dispatcher_primary.exec_primary(['list']) is None
elif scenario == 'hooks':
    tree = product / 'tree'
    hooks = tree / '.git/dispatch-hooks'
    hooks.parent.mkdir(parents=True)
    chain = tree / 'chain'
    chain.mkdir()
    registry = primary / 'registry'
    def fake_git(path, *args, **kwargs):
        if args[:4] == ('config', '--path', '--get', 'core.hooksPath'):
            return str(chain)
        if '--git-path' in args:
            return str(hooks)
        return ''
    with patch.object(git, '_git', side_effect=fake_git):
        git._install_refresh_hooks(tree, registry)
    # Run every generated hook against an inert timeout executable; observe its cwd.
    binaries = product / 'bin'
    binaries.mkdir()
    timeout = binaries / 'timeout'
    timeout.write_text('#!/bin/sh\npwd > "$ROOT_PROOF"\n')
    timeout.chmod(0o755)
    for name in git.REFRESH_HOOKS:
        proof = product / name
        subprocess.run([str(hooks / name)], cwd=product, check=True,
                       env={**os.environ, 'PATH':str(binaries)+':'+os.defpath, 'ROOT_PROOF':str(proof)})
        assert proof.read_text().strip() == str(primary)
elif scenario == 'alert':
    # The real Fleet child selects its plane from cwd; site-packages is refused.
    from lu_machinery.fleet_comms.authority import AuthorityService
    with AuthorityService(root=primary / 'batch_state/fleet-comms/v1') as service:
        service.create_channel(admission.DISK_ALERT_CHANNEL)
    admission.post_disk_alert('installed layout proof', 'installed-root-proof')
    assert (primary / 'batch_state/fleet-comms/v1/comms.sqlite3').is_file()
    assert not (Path(site) / 'batch_state').exists()
elif scenario == 'drift':
    from lu_machinery.ops import host_stack_drift as drift
    with patch.object(drift, 'write_report') as write:
        assert drift.main([]) == 0
    write.assert_called_once_with(product)
    with patch.object(drift, 'write_report') as write:
        assert drift.main(['--repo-root', product.relative_to(Path.cwd()).as_posix()]) == 0
    write.assert_called_once_with(product)
    report = drift.write_report(product, env={'XDG_STATE_HOME':str(product.parent / 'private-state')},
                                read=lambda name: drift.Reading('unknown', 'unknown'))
    payload = json.loads(report.read_text())
    assert payload['schema'] == 'stack-drift.v1'
    assert payload['rows'] and 'error' not in payload, payload
    assert not report.is_relative_to(product)
    try:
        drift.write_report(product, env={'XDG_STATE_HOME':str(rr.machinery_root())})
    except drift.DriftError:
        pass
    else:
        raise AssertionError('report wrote into package code')
elif scenario == 'interpreter':
    os.environ.pop(rr.INTERPRETER_ENV, None)
    python = primary / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\n')
    python.chmod(0o755)
    assert rr.project_interpreter() == python
elif scenario == 'diagnostic':
    from lu_machinery.orchestration import task_record_causes as causes
    record = primary / 'batch_state/tasks/root-probe.json'
    record.parent.mkdir(parents=True)
    record.write_text('{}')
    with patch.object(causes, '_diagnostic_line', return_value=b'root proof\n'):
        result = causes._append_diagnostics(record, [('field', object())], source='test')
    assert result == 'batch_state/tasks/root-probe.diag', result
    assert record.with_suffix('.diag').read_text() == 'root proof\n'
elif scenario in ('gh_shim', 'guarded_gh_shim'):
    from lu_machinery.agent_runtime.env_sanitize import build_agent_env
    from lu_machinery.agent_runtime.runner import _apply_merge_guard
    real = product / 'gh'
    real.write_text('#!/bin/sh\nprintf "gh-root-proof\\n"\n')
    real.chmod(0o755)
    os.environ['PATH'] = str(product) + ':' + os.defpath
    home = product / 'home'
    home.mkdir()
    os.environ['HOME'] = str(home)
    os.environ.pop('GH_CONFIG_DIR', None)
    os.environ.pop('AGENT_ALLOW_MERGE', None)
    os.environ.pop('AGENT_NO_MERGE', None)
    python = primary / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    # Exercise the production sanitizer boundary: these operator settings are
    # intentionally absent from the child environment, but still select the guard.
    if scenario == 'guarded_gh_shim':
        os.environ.pop(rr.INTERPRETER_ENV, None)
    else:
        os.environ[rr.INTERPRETER_ENV] = str(python)
    with patch('lu_machinery.agent_runtime.agent_github_identity.resolve_agent_github_identity', return_value=None):
        source = build_agent_env(provider='claude', model='claude-opus-5-5')
    assert rr.PRIMARY_ROOT_ENV not in source and rr.INTERPRETER_ENV not in source
    environment = _apply_merge_guard(mode='danger' if scenario == 'guarded_gh_shim' else 'safe', env=source)
    assert environment[rr.INTERPRETER_ENV] == str(python)
    result = subprocess.run(['gh', 'auth', 'status'], cwd=product, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'gh-root-proof\n', result.stdout
    if scenario == 'guarded_gh_shim':
        denied = subprocess.run(['gh', 'pr', 'merge', '1'], cwd=product, env=environment,
                                capture_output=True, text=True, timeout=30)
        assert denied.returncode != 0 and 'AGENT_NO_MERGE=1' in denied.stderr, denied
elif scenario == 'npm_shim':
    from lu_machinery.dispatcher.environment import child_env
    python = primary / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    npm = product / 'npm'
    npm.write_text('#!/bin/sh\nprintf "npm-root-proof\\n"\n')
    npm.chmod(0o755)
    os.environ.pop(rr.INTERPRETER_ENV, None)
    os.environ['PATH'] = str(product) + os.pathsep + os.defpath
    spec = SimpleNamespace(agent='muse', provider=None, worktree=product,
                           task_id='root-probe', mode='workspace-write')
    environment = child_env(spec)
    assert environment[rr.INTERPRETER_ENV] == str(python)
    result = subprocess.run(['npm', '--version'], cwd=product, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'npm-root-proof\n', result.stdout
elif scenario == 'compatibility_resolvers':
    assert rr.resolve_repo_root(Path(rr.__file__), 2) == primary
    for key in rr.MACHINERY_ENV_VARS:
        os.environ.pop(key, None)
    try:
        rr.resolve_repo_root(Path(rr.__file__), 2)
    except rr.StateRootError:
        pass
    else:
        raise AssertionError('compatibility resolver derived an installed state root')
    os.environ[rr.PRIMARY_ROOT_ENV] = str(primary)
    try:
        rr.runtime_primary_root()
    except rr.StateRootError:
        pass
    else:
        raise AssertionError('installed code claimed a runtime Git checkout')
elif scenario == 'source_attestation':
    from lu_machinery.ci import pr20_measurement as pr20
    import io
    with patch.object(subprocess, 'check_output', side_effect=AssertionError('package Git probe')), \
         patch.object(sys, 'stderr', new_callable=io.StringIO) as error:
        assert pr20.main(['anchor', '--inputs', str(product / 'inputs'), '--output', str(product / 'out'),
                          '--switch-pr', str(product / 'switch'), '--observed-at', '2026-11-09T00:00:00Z']) == 2
    assert '(ProductRootError)' in error.getvalue(), error.getvalue()
elif scenario == 'unconfigured':
    for key in (*rr.MACHINERY_ENV_VARS, rr.PRODUCT_ROOT_ENV):
        os.environ.pop(key, None)
    for call in (lambda: admission.post_disk_alert('refuse', 'refuse'),
                 lambda: dispatcher_primary.exec_primary(['list']), worker._dispatcher_code,
                 rr.project_interpreter):
        try:
            call()
        except rr.StateRootError:
            pass
        else:
            raise AssertionError('unconfigured state did not refuse')
    from lu_machinery.ops import host_stack_drift as drift
    try:
        drift.main([])
    except rr.ProductRootError:
        pass
    else:
        raise AssertionError('unconfigured product did not refuse')
    assert not (Path(site) / 'batch_state').exists()
else:
    raise AssertionError(scenario)
print('root contract verified: ' + scenario)
'''


@pytest.fixture(scope="module")
def installed_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site = tmp_path_factory.mktemp("installed-root") / "site-packages"
    package = machinery_root()
    shutil.copytree(package, site / "lu_machinery", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return site


@pytest.mark.parametrize("scenario", [
    "text", "dispatch", "review_dispatch", "review_wait", "supervisor", "provenance",
    "primary", "hooks", "alert", "drift", "interpreter", "diagnostic", "gh_shim", "guarded_gh_shim", "npm_shim", "compatibility_resolvers", "source_attestation", "unconfigured",
])
def test_installed_entry_points_never_use_package_ancestors_as_roots(installed_site, tmp_path, scenario):
    """Defect class: installed package location used as cwd, repository or state anchor."""
    product, primary = tmp_path / "product", tmp_path / "primary"
    product.mkdir()
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    environment = {key: value for key, value in os.environ.items() if key not in (
        "LEARN_UKRAINIAN_STATE_ROOT", "LU_TEST_DISPATCH_HEALTHY_HOST", "FLEET_COMMS_ROOT", "LU_TASKS_DIR",
    ) and not key.startswith("GIT_")}
    environment.update(LU_PRODUCT_ROOT=str(product), LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=str(primary),
                       LEARN_UKRAINIAN_PYTHON=sys.executable, PYTHONPATH=str(installed_site))
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(installed_site), str(product), str(primary), scenario],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("root contract verified: " + scenario)
