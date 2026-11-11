"""Exercise provider commands with local stubs and captured stream fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from lu_machinery.dispatcher.providers import cursor, load, muse, opencode
from lu_machinery.dispatcher.types import TaskSpec
from lu_machinery.review.verdict_parser import recognized_verdicts
from tests.helpers.product_tree import configured_roots  # noqa: F401





MODELS = ("muse-spark-1.3", "muse-wonder-1.0", "muse-wonder-full-1.0")
PROMPT = '--leading-option "quoted" $literal\nReply with exactly: ready\n'


@pytest.fixture
def spec(tmp_path):
    prompt = tmp_path / "prompt with spaces.txt"
    prompt.write_text(PROMPT, encoding="utf-8")
    return TaskSpec("providers-c", "muse", MODELS[0], None, "read-only", tmp_path, prompt, tmp_path / "final")


@pytest.fixture
def stub_env(tmp_path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    source = (
        f"#!{sys.executable}\n"
        + """import json, pathlib, sys
args = sys.argv[1:]
if "--prompt-file" in args:
    prompt = pathlib.Path(args[args.index("--prompt-file") + 1]).read_text(encoding="utf-8")
else:
    prompt = args[args.index("--") + 1]
print(json.dumps({"argv": args, "prompt": prompt, "stdin": sys.stdin.read(), "cwd": str(pathlib.Path.cwd())}))
"""
    )
    for name in ("muse", "opencode"):
        executable = binary_dir / name
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o755)
    return {"PATH": str(binary_dir) + os.pathsep + os.defpath, "STUB_SENTINEL": "preserved"}


def run_stub(invocation, spec):
    result = subprocess.run(
        invocation.argv,
        input=invocation.stdin or b"",
        env=invocation.env,
        cwd=spec.worktree,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert invocation.stdin is None
    assert invocation.final_file is None
    observed = json.loads(result.stdout)
    assert observed["prompt"] == PROMPT
    assert observed["stdin"] == ""
    assert observed["cwd"] == str(spec.worktree)
    assert invocation.env["STUB_SENTINEL"] == "preserved"
    return observed["argv"]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mode", ("read-only", "workspace-write"))
@pytest.mark.parametrize("effort", (None, "high"))
@pytest.mark.parametrize("contributor", (None, "on", "off"))
def test_muse_argv(spec, stub_env, model, mode, effort, contributor, monkeypatch):
    # Transport flags are independent of the file sandbox, exercised below.
    monkeypatch.setattr(muse, "confined_muse_argv", lambda argv, *args: argv)
    options = {} if contributor is None else {"contributor_mode": contributor}
    spec = replace(spec, model=model, mode=mode, effort=effort, options=options)
    invocation = muse.build(spec, stub_env)
    expected = [
        "exec",
        "--json",
        "--provider",
        "meta",
        "--model",
        model,
        "--disable-web-tools",
        "--workspace",
        str(spec.worktree),
        "--worktree",
        "off",
        "--prompt-file",
        str(spec.prompt_file),
        "--contributor-mode",
        "off",
    ]
    if effort:
        expected += ["--reasoning-effort", effort]
    if mode == "workspace-write":
        expected += ["--permission-profile", ":ask-me", "--disable-shell",
                     "--disable-reminders", "--disable-request-user-input", "--no-foreign-personal-context",
                     "--config", 'run.toolset=["read_file", "write_file", "edit_file"]']
    else:
        expected += ["--permission-profile", ":read-only", "--disable-shell",
                     "--disable-reminders", "--disable-request-user-input", "--no-foreign-personal-context",
                     "--config", 'run.toolset=["read_file"]', "--disable-write"]
    assert run_stub(invocation, spec) == expected
    assert invocation.env["MUSE_NO_AUTO_UPDATE"] == "1"
    assert "MUSE_NO_AUTO_UPDATE" not in stub_env


@pytest.mark.parametrize("mode", ("read-only", "workspace-write"))
@pytest.mark.parametrize("model", ("deepseek-v4.1-flash", "deepseek/deepseek-flash"))
def test_opencode_argv(spec, stub_env, mode, model):
    spec = replace(spec, agent="opencode", model=model, mode=mode, effort="high")
    expected = ["run", "--model", "deepseek/deepseek-flash", "--format", "json", "--variant", "high"]
    if mode == "workspace-write":
        expected += ["--auto"]
    assert run_stub(opencode.build(spec, stub_env), spec) == [*expected, "--", PROMPT]


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize(
    "models,expected",
    [
        ([], None),
        ([None, "", "  ", 42, [], {}], None),
        (["Grok 4.7 256K High"], "Grok 4.7 256K High"),
        ([" Grok 4.7 256K High "], " Grok 4.7 256K High "),
        (["Grok 4.7 256K High", None, "Grok 4.7 256K High"], "Grok 4.7 256K High"),
        (["Grok 4.7 256K High", "different", "Grok 4.7 256K High"], None),
    ],
)
def test_cursor_never_attests_a_model_the_init_events_did_not_report(models, expected):
    events = [{"type": "system", "subtype": "init", "model": model} for model in models]
    events += [
        {"type": "system", "subtype": "other", "model": "forged"},
        {"type": "assistant", "model": "forged", "content": "model: forged"},
        {"type": "result", "subtype": "success", "model": "forged", "result": "model: forged"},
    ]
    parsed = cursor.parse(stream(*events), "model: forged", 0, "model: forged")
    assert (parsed.text, parsed.error, parsed.resolved_model) == ("model: forged", None, expected)


def muse_event(kind, **payload):
    return {"payload_type": kind, "payload": payload}


MUSE_READY = muse_event("run.terminal.completed", terminal="completed", text="ready")
OC_TEXT = {"type": "text", "part": {"text": "ready"}}
OC_STOP = {"type": "step_finish", "part": {"reason": "stop"}}


@pytest.mark.parametrize("usage", (None, {"models": [], "calls": 1}))
def test_muse_success_unknown_events_and_optional_usage(usage):
    events = [muse_event("future.informational"), ["unknown shape"]]
    if usage is not None:
        events.append(muse_event("run.usage", **usage))
    events.append(MUSE_READY)
    result = muse.parse("startup notice\n" + stream(*events), "", 0, "ignored sidecar")
    assert (result.text, result.usage, result.error) == ("ready", usage, None)


@pytest.mark.parametrize("kind", ("failed", "cancelled", "error", "future_outcome"))
@pytest.mark.parametrize("failure_first", (False, True))
def test_muse_terminal_failure_is_sticky(kind, failure_first):
    failure = muse_event("run.terminal." + kind, text="ready")
    events = [failure, MUSE_READY] if failure_first else [MUSE_READY, failure]
    assert muse.parse(stream(*events), "", 0, None).error


def test_muse_completed_with_failure_reason():
    assert muse.parse(stream(muse_event("run.terminal.completed", text="ready", reason="failed")), "", 0, None).error


@pytest.mark.parametrize("terminal", ("step_finish", "step-finish"))
@pytest.mark.parametrize("usage", (None, {"input": 2, "output": 1}))
def test_opencode_final_turn(terminal, usage):
    stop = {"type": terminal, "part": {"reason": "stop"}}
    if usage is not None:
        stop["part"]["tokens"] = usage
    output = stream(
        {"type": "text", "part": {"text": "Preamble"}},
        {"type": "tool", "part": {"state": {"status": "completed"}}},
        {"type": "step_start"},
        {"type": "future.info"},
        ["unknown"],
        {"type": "text", "part": {"text": "rea"}, "error": None},
        {"type": "text", "part": {"text": "dy"}},
        stop,
    )
    result = opencode.parse("notice\n" + output, "", 0, None)
    assert (result.text, result.usage, result.error) == ("ready", usage, None)


@pytest.mark.parametrize(
    "reason",
    (
        "abort",
        "cancelled",
        "permission_denied",
        "permission_rejected",
        "error",
        "length",
        "future_outcome",
        "tool-calls",
    ),
)
def test_opencode_bad_terminal_reason(reason):
    result = opencode.parse(stream(OC_TEXT, {"type": "step_finish", "part": {"reason": reason}}), "", 0, None)
    assert result.error


@pytest.mark.parametrize(
    "failure",
    (
        {"type": "error", "error": {"name": "APIError"}},
        {"type": "future", "error": "failed"},
        {"type": "tool", "part": {"state": {"status": "rejected"}}},
    ),
)
def test_opencode_explicit_failure_is_sticky(failure):
    assert opencode.parse(stream(failure, OC_TEXT, OC_STOP), "", 0, None).error


@pytest.mark.parametrize(
    "provider,stdout", ((muse, stream(MUSE_READY)), (opencode, stream(OC_TEXT, OC_STOP)))
)
def test_nonzero_exit_cannot_succeed(provider, stdout):
    result = provider.parse(stdout, "private diagnostic must not become public error", 2, "ready")
    assert result.error
    assert "private diagnostic" not in result.error


@pytest.mark.parametrize("provider", (muse, opencode))
def test_missing_response(provider):
    result = provider.parse("", "", 0, "sidecar is not this provider's response")
    assert result.text is None
    assert result.usage is None
    assert result.error


@pytest.mark.parametrize("provider", (muse, opencode))
def test_unknown_events_are_not_a_response(provider):
    result = provider.parse(stream({"type": "future", "payload_type": "future", "payload": {}}), "", 0, None)
    assert result.text is None
    assert result.error


@pytest.mark.parametrize("name", ("muse", "opencode"))
def test_registry(name):
    assert load(name).__name__ == f"lu_machinery.dispatcher.providers.{name}"


@pytest.mark.parametrize("payload", (None, "failure", []))
def test_muse_malformed_terminal_is_not_success(payload):
    output = stream(MUSE_READY, {"payload_type": "run.terminal.failed", "payload": payload})
    assert muse.parse(output, "", 0, None).error


def test_opencode_missing_terminal_reason_is_not_success():
    assert opencode.parse(stream(OC_TEXT, {"type": "step_finish", "part": {}}), "", 0, None).error


def test_opencode_tool_step_before_final_completion():
    output = stream(
        {"type": "step_finish", "part": {"reason": "tool-calls"}},
        OC_TEXT,
        OC_STOP,
    )
    result = opencode.parse(output, "", 0, None)
    assert (result.text, result.error) == ("ready", None)


def test_opencode_unknown_terminal_before_completion_stays_failed():
    output = stream({"type": "step_finish", "part": {"reason": "future_outcome"}}, OC_TEXT, OC_STOP)
    assert opencode.parse(output, "", 0, None).error


def _assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_cursor_keeps_the_verdict_on_its_own_line_when_result_concatenates_messages():
    """Real Grok stream: the terminal result glues the assistant messages together unseparated."""
    messages = [
        "I'll review the delta on the exact head before giving a verdict.",
        "The stat is wider than the brief claimed, so I'll check the same cases.",
        "VERDICT: APPROVE\n\nNo new findings.",
    ]
    events = [
        {"type": "system", "subtype": "init", "model": "Grok 4.7 256K High"},
        *map(_assistant, messages),
        {"type": "result", "subtype": "success", "is_error": False, "result": "".join(messages)},
    ]
    parsed = cursor.parse(stream(*events), "", 0, None)
    assert parsed.error is None
    assert parsed.text == "\n\n".join(messages)
    assert recognized_verdicts(parsed.text) == ["APPROVE"]


def test_cursor_reply_is_the_final_turn_after_the_last_tool_call():
    """#12390 stream shape: narration, tool calls, then the findings object as the final message."""
    narration = "I'll match the findings schema and return one JSON object with the same verdict."
    reply = '{"schema_version":"code-review-findings.v1","findings":[]}'
    events = [
        {"type": "system", "subtype": "init", "model": "Grok 4.7 256K High"},
        _assistant(narration),
        {"type": "tool_call", "subtype": "started"},
        {"type": "tool_call", "subtype": "completed"},
        _assistant(reply),
        {"type": "result", "subtype": "success", "is_error": False, "result": narration + reply},
    ]
    parsed = cursor.parse(stream(*events), "", 0, None)
    assert (parsed.text, parsed.error) == (reply, None)


def test_cursor_falls_back_to_the_result_when_no_assistant_text_was_captured():
    events = [{"type": "result", "subtype": "success", "result": "Finished.\n\nVERDICT: APPROVE"}]
    parsed = cursor.parse(stream(*events), "", 0, None)
    assert (parsed.text, parsed.error) == ("Finished.\n\nVERDICT: APPROVE", None)
    assert recognized_verdicts(parsed.text) == ["APPROVE"]


def test_cursor_text_deltas_do_not_displace_the_result_or_duplicate_assistant_text():
    deltas = [{"type": "text", "content": "Checking."}]
    result = {"type": "result", "subtype": "success", "result": "Checking.\n\nVERDICT: APPROVE"}
    assert cursor.parse(stream(*deltas, result), "", 0, None).text == "Checking.\n\nVERDICT: APPROVE"
    mixed = [*deltas, _assistant("VERDICT: APPROVE"), result]
    assert cursor.parse(stream(*mixed), "", 0, None).text == "VERDICT: APPROVE"


def test_muse_provider_cannot_bypass_file_sandbox(tmp_path, monkeypatch):
    """Defect class: Muse provider bypasses file sandbox and reads host files."""
    import shutil

    from lu_machinery.agent_runtime.adapters.muse import confined_muse_argv
    from lu_machinery.common.repo_root import project_interpreter
    from lu_machinery.guardrails.worktree_containment import NotAGitRepositoryError

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required for the real file boundary")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    workspace = tmp_path / ".worktrees/dispatch/muse/containment"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "inside.txt").write_text("inside")
    (workspace / "escape").symlink_to(outside)
    install = tmp_path / "install"
    install.mkdir()
    launcher = install / "muse"
    launcher.write_text('#!/bin/sh\nexec "$(dirname "$0")/muse-bin-1.0.0-R1" "$@"\n')
    launcher.chmod(0o700)
    (install / ".muse-version").write_text("1.0.0-R1")
    native = install / "muse-bin-1.0.0-R1"
    native.write_text(
        '#!/bin/sh\n'
        'test "$(cat "$HOME/.config/muse/auth.json")" = synthetic-auth-only || exit 19\n'
        'cat inside.txt\n'
        f'if cat "{outside}" 2>/dev/null; then exit 20; fi\n'
        'if cat ../outside.txt 2>/dev/null; then exit 21; fi\n'
        'if cat escape 2>/dev/null; then exit 22; fi\n'
        'if printf changed > inside.txt 2>/dev/null; then exit 23; fi\n'
        f'if printf bad > "{outside}" 2>/dev/null; then exit 24; fi\n'
        'if printf bad > ../outside.txt 2>/dev/null; then exit 25; fi\n'
        'if printf bad > escape 2>/dev/null; then exit 26; fi\n'
        'if printf bad > "$HOME/.config/muse/auth.json" 2>/dev/null; then exit 27; fi\n'
    )
    native.chmod(0o700)
    source_home = tmp_path / "empty-home"
    source_home.mkdir()
    auth = source_home / ".config/muse/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("synthetic-auth-only")
    auth.chmod(0o600)
    monkeypatch.setenv("HOME", str(source_home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(scratch))
    prompt = tmp_path / "brief.txt"
    prompt.write_text("fixture")
    spec = TaskSpec("muse-containment", "muse", MODELS[0], None, "read-only", workspace, prompt, tmp_path / "final")
    # Defect class: direct sandbox callers mount a primary or symlink target writable.
    for rejected in (tmp_path, tmp_path / ".worktrees", tmp_path / "install"):
        with pytest.raises(ValueError, match="muse_dispatch_worktree_required"):
            muse.build(replace(spec, mode="workspace-write", worktree=rejected), {})
    linked = tmp_path / "linked-workspace"
    linked.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(NotAGitRepositoryError, match="symlink or not a directory"):
        muse.build(replace(spec, mode="workspace-write", worktree=linked), {})
    # child_env forwards the configured project interpreter to the read-only launcher child.
    invocation = muse.build(spec, {
        "PATH": str(install) + os.pathsep + os.defpath, "HOME": str(source_home),
        "LEARN_UKRAINIAN_PYTHON": str(project_interpreter()),
    })
    assert invocation.argv[:6] == confined_muse_argv([], workspace, "read-only", spec.task_id)[:6]
    result = subprocess.run(invocation.argv, cwd=workspace, env=invocation.env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "inside"
    assert (workspace / "inside.txt").read_text() == "inside"
    assert not list(scratch.glob("task-scratch/*/scratch"))

    # The same OS boundary permits only task-worktree writes in write mode.
    host_git = tmp_path / "host-git"
    host_git.mkdir()
    (host_git / "private-marker").write_text("host-only")
    original_pointer = f"gitdir: {host_git}\n"
    # Defect class: invalid Git markers permit a workspace-write launch.
    marker = workspace / ".git"
    for invalid in ("directory", "symlink"):
        if invalid == "directory":
            marker.mkdir()
        else:
            marker.symlink_to(host_git, target_is_directory=True)
        invocation = muse.build(replace(spec, mode="workspace-write"),
                                {"PATH": str(install) + os.pathsep + os.defpath, "HOME": str(source_home)})
        result = subprocess.run(invocation.argv, cwd=workspace, env=invocation.env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert "muse_worktree_git_pointer_refused" in result.stderr
        marker.rmdir() if invalid == "directory" else marker.unlink()
    (workspace / ".git").write_text(original_pointer)
    native.write_text(
        '#!/bin/sh\n'
        'test "$(cat "$HOME/.config/muse/auth.json")" = synthetic-auth-only || exit 19\n'
        'pointer=$(cat .git)\n'
        'gitdir=${pointer#gitdir: }\n'
        'test -d "$gitdir" || exit 29\n'
        'test ! -e "$gitdir/private-marker" || exit 30\n'
        'if printf bad > .git 2>/dev/null; then exit 31; fi\n'
        'if printf bad > "$gitdir/config" 2>/dev/null; then exit 32; fi\n'
        'printf changed > inside.txt || exit 23\n'
        f'if printf bad > "{outside}" 2>/dev/null; then exit 24; fi\n'
        'if printf bad > ../outside.txt 2>/dev/null; then exit 25; fi\n'
        'if printf bad > escape 2>/dev/null; then exit 26; fi\n'
        'if printf bad > "$HOME/.config/muse/auth.json" 2>/dev/null; then exit 27; fi\n'
        'if rm "$HOME/.config/muse/auth.json" 2>/dev/null; then exit 28; fi\n'
        'cat inside.txt\n'
    )
    invocation = muse.build(replace(spec, mode="workspace-write"),
                            {"PATH": str(install) + os.pathsep + os.defpath, "HOME": str(source_home)})
    result = subprocess.run(invocation.argv, cwd=workspace, env=invocation.env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "changed"
    assert (workspace / "inside.txt").read_text() == "changed"
    assert outside.read_text() == "outside"
    assert auth.read_text() == "synthetic-auth-only"
    assert (workspace / ".git").read_text() == original_pointer
    assert (host_git / "private-marker").read_text() == "host-only"
    assert not list(scratch.glob("task-scratch/*/scratch"))


def _muse_stream_fixture(tmp_path, model="muse-wonder-full-1.0", *, observations=False, write_observation=False):
    """Native envelope shared only by the granted Muse protocol cases."""
    from lu_machinery.agent_runtime.adapters.base import InvocationPlan

    session = "11111111-1111-4111-8111-111111111111"
    command = "22222222-2222-4222-8222-222222222222"
    task = "33333333-3333-4333-8333-333333333333"
    plan = InvocationPlan(cmd=["fixture-muse"], cwd=tmp_path, metadata={
        "task_id": "muse-interface", "run_nonce": "0123456789abcdef",
        "provider_session_id": session, "model": model, "workspace": str(tmp_path),
        "mode": "read-only", "permission_profile": ":read-only", "toolset": ["read_file"],
        "cli_version": "Muse Code 1.4.11 (1.4.11-R6667.1)",
        "cli_sha256": "a" * 64, "prompt_sha256": "b" * 64, "preview": False,
    })
    records = []
    tool_name = "edit_file" if write_observation else "read_file"
    if write_observation:
        plan.metadata.update(mode="workspace-write", permission_profile=":ask-me",
                             toolset=["read_file", "write_file", "edit_file"])

    def event(kind, payload, stream="session"):
        number = len(records) + 1
        records.append({
            "schema_version": 1, "payload_schema_version": 1,
            "id": f"00000000-0000-4000-8000-{number:012d}",
            "stream": {"kind": stream, "id": session},
            "sequence": 1 + sum(r["stream"]["kind"] == stream for r in records),
            "causation_id": command, "payload_type": kind,
            "payload": {"command_id": command, "run_stream": {"kind": "run", "id": command}, **payload},
        })

    event("runtime.command.accepted", {"kind": "command_accepted", "command_kind": "turn.submit"})
    event("session.run.linked", {})
    if observations:
        event("run.model.configured", {"kind": "run_model_configured", "model_id": model, "provider_id": "meta"})
    event("session.workspace_branch.observed", {
        "record": {"command_id": command, "workspace_root": str(tmp_path)},
    })
    event("run.lifecycle.started", {})
    event("task.stream.linked", {"task_id": task, "task_stream": {"kind": "task", "id": task}})
    for stage in ("proposed", "accepted", "scheduled", "side_effect_intent", "started", "completed"):
        task_event = {"task_id": task, "kind": stage}
        if observations and stage == "side_effect_intent":
            task_event.update(operation="tool:" + tool_name, idempotency_key="tool:call_fixture")
        event(f"task.lifecycle.{stage}", {
            "task_id": task, "task_stream": {"kind": "task", "id": task}, "event": task_event,
        })
        if observations and stage == "started":
            for kind, detail in (("status", {"message": "reading"}),
                                 ("output", {"chunk": "fixture file text", "final_result": True})):
                event(f"task.lifecycle.{kind}", {
                    "kind": "task_lifecycle", "task_id": task, "task_stream": {"kind": "task", "id": task},
                    "event": {"task_id": task, "kind": kind, **detail},
                })
            if write_observation:
                event("task.lifecycle.tool_output_ref", {
                    "kind": "task_lifecycle", "task_id": task, "task_stream": {"kind": "task", "id": task},
                    "event": {"task_id": task, "kind": "tool_output_ref", "availability": "available",
                              "output_ref": {"kind": "tool_patch", "media_type": "application/json",
                                             "id": "tool_patch-fixture", "path": "/synthetic/patch.json",
                                             "uri": "tool-output://local/fixture", "digest": None,
                                             "byte_len": 240, "patch_summary": {"added": 1, "files": 1, "removed": 1}}},
                })
    if observations:
        event("tool.result", {"kind": "tool_result", "call_id": "call_fixture", "text": "fixture file text",
                              "correlation_facts": {"outcome": "success", "tool_name": tool_name}})
    event("run.usage", {
        "kind": "run_usage", "children_included": False, "failed_calls": 0, "cancelled_calls": 0,
        "calls": 1, "completed_calls": 1,
        "models": [{"model": model, "calls": 1, "input_tokens": 100, "output_tokens": 20,
                    "cached_tokens": 30, "reasoning_tokens": 5}],
    }, "exec.usage")
    event("run.terminal.completed", {"kind": "run_terminal", "terminal": "completed", "text": "READY\n"})
    return plan, records


@pytest.mark.parametrize("model", ["muse-wonder-full-1.0", "muse-spark-1.3"])
def test_muse_complete_stream_preserves_bound_token_accounting(tmp_path, model):
    """M01: retain usage and reject forged or cross-session write observations."""
    import hashlib

    from lu_machinery.agent_runtime.adapters.muse import MuseAdapter

    # Legacy and Muse 1.4.15 observation shapes, qualified from the synthetic
    # native exec capture. MSP notifications are not the exec contract.
    for observations, write_observation in ((False, False), (True, False), (True, True)):
        plan, records = _muse_stream_fixture(tmp_path, model, observations=observations,
                                            write_observation=write_observation)
        result = MuseAdapter().parse_response(
            stdout="\n".join(json.dumps(record) for record in records),
            stderr="", returncode=0, output_file=None, plan=plan,
        )
        assert result.ok is True
        assert result.response == "READY\n"
        assert result.tokens == 120
    import copy

    for kind, field, value in (
        ("run.model.configured", "model_id", "foreign-model"),
        ("task.lifecycle.status", "message", None),
        ("task.lifecycle.output", "final_result", 1),
        ("tool.result", "call_id", "foreign-call"),
    ):
        invalid = copy.deepcopy(records)
        payload = next(r["payload"] for r in invalid if r["payload_type"] == kind)
        (payload["event"] if kind.startswith("task.") else payload)[field] = value
        rejected = MuseAdapter().parse_response(
            stdout="\n".join(json.dumps(record) for record in invalid),
            stderr="", returncode=0, output_file=None, plan=plan,
        )
        assert rejected.ok is False
        assert rejected.tokens is None
    # Defect class: forged or cross-session write observation accepted.
    for field, value in (("task_id", "44444444-4444-4444-8444-444444444444"), ("output_ref", {})):
        invalid = copy.deepcopy(records)
        payload = next(r["payload"] for r in invalid if r["payload_type"] == "task.lifecycle.tool_output_ref")
        payload["event"][field] = value
        rejected = MuseAdapter().parse_response(
            stdout="\n".join(json.dumps(record) for record in invalid),
            stderr="", returncode=0, output_file=None, plan=plan,
        )
        assert rejected.ok is False
        assert rejected.tokens is None
    # Defect class: read-only or unbound tasks attest write-patch observations.
    for mismatch in ("read-only", "read-intent", "foreign-session"):
        invalid = copy.deepcopy(records)
        invalid_plan = copy.deepcopy(plan)
        if mismatch == "read-only":
            invalid_plan.metadata.update(mode="read-only", permission_profile=":read-only", toolset=["read_file"])
        if mismatch in {"read-only", "read-intent"}:
            next(r["payload"]["event"] for r in invalid
                 if r["payload_type"] == "task.lifecycle.side_effect_intent")["operation"] = "tool:read_file"
            next(r["payload"] for r in invalid if r["payload_type"] == "tool.result")[
                "correlation_facts"]["tool_name"] = "read_file"
        else:
            next(r for r in invalid if r["payload_type"] == "task.lifecycle.tool_output_ref")[
                "stream"]["id"] = "44444444-4444-4444-8444-444444444444"
        assert MuseAdapter().parse_response(
            stdout="\n".join(json.dumps(record) for record in invalid),
            stderr="", returncode=0, output_file=None, plan=invalid_plan,
        ).ok is False
    unknown = copy.deepcopy(records)
    unknown[2]["payload_type"] = "future.unknown"
    assert MuseAdapter().parse_response(
        stdout="\n".join(json.dumps(record) for record in unknown),
        stderr="", returncode=0, output_file=None, plan=plan,
    ).ok is False

    assert result.ok is True
    assert result.response == "READY\n"
    assert result.tokens == 120
    assert result.session_id == "11111111-1111-4111-8111-111111111111"
    companion = result.muse_run.projection()
    assert companion["task_id"] == "muse-interface"
    assert companion["run_nonce"] == "0123456789abcdef"
    assert companion["provider_session_id"] == "11111111-1111-4111-8111-111111111111"
    assert companion["provider_run_id"] == "22222222-2222-4222-8222-222222222222"
    assert companion["requested_selector"] == model
    assert companion["usage_model"] == model
    assert companion["input_tokens"] == 100
    assert companion["output_tokens"] == 20
    assert companion["cached_tokens"] == 30
    assert companion["reasoning_tokens"] == 5
    assert companion["total_tokens"] == 120
    assert companion["observed_model"] is None
    assert companion["identity_state"] == "unknown"
    assert companion["effective_permissions"] == "unknown"
    assert companion["cli_sha256"] == [170] * 32
    assert companion["prompt_sha256"] == [187] * 32
    assert bytes(companion["cli_sha256"]).hex() == plan.metadata["cli_sha256"]
    assert bytes(companion["prompt_sha256"]).hex() == plan.metadata["prompt_sha256"]
    fields = {key: value for key, value in companion.items() if key not in {"receipt_ref", "receipt_sha256"}}
    assert bytes(companion["receipt_sha256"]) == hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode(),
    ).digest()
    assert companion["receipt_ref"] == (
        "muse-run:11111111-1111-4111-8111-111111111111:22222222-2222-4222-8222-222222222222"
    )


def _muse_planning_fixture(tmp_path, monkeypatch, version):
    from lu_machinery.agent_runtime.adapters import muse

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    workspace = tmp_path / ".worktrees" / "dispatch" / "codex" / "muse-interface"
    workspace.mkdir(parents=True)
    scratch = tmp_path / "private-scratch"
    scratch.mkdir()
    binary = tmp_path / "fixture-muse"
    binary.write_text("fixture only\n")
    monkeypatch.setattr(muse, "require_muse_execution_model", lambda model: model)
    monkeypatch.setattr(muse, "validate_read_only_tmp_root", lambda *a, **kw: scratch)
    monkeypatch.setattr(muse.shutil, "which", lambda name: str(binary))
    probes = []

    def probe(cmd, **kwargs):
        probes.append(cmd)
        assert cmd == [str(binary), "--version"]
        return SimpleNamespace(stdout=version)

    monkeypatch.setattr(muse, "subprocess", SimpleNamespace(run=probe))
    return muse.MuseAdapter(), workspace, scratch, probes


@pytest.mark.parametrize("mode,role", [("read-only", "investigation"), ("workspace-write", "implementation")])
def test_muse_installed_cli_preserves_explicit_permissions(tmp_path, monkeypatch, mode, role):
    """M05: preserve permissions and prevent symlink-package bootstrap or interpreter-anchor loss."""
    adapter, workspace, scratch, probes = _muse_planning_fixture(
        tmp_path, monkeypatch, "Muse Code 1.4.12 (1.4.12-R6668.1)",
    )
    plan = adapter.build_invocation(
        prompt="fixture", mode=mode, cwd=workspace, model="muse-wonder-full-1.0",
        task_id="muse-interface", session_id=None, effort="high",
        tool_config={"muse_worker": True, "muse_role": role,
                     "run_nonce": "0123456789abcdef", "muse_tmp_root": str(scratch)},
    )
    assert len(probes) == 1
    assert plan.metadata["cli_version"] == "Muse Code 1.4.12 (1.4.12-R6668.1)"
    assert plan.cmd[plan.cmd.index("--permission-profile") + 1] == (
        ":read-only" if mode == "read-only" else ":ask-me"
    )
    assert ("--disable-write" in plan.cmd) is (mode == "read-only")
    assert plan.cmd[plan.cmd.index("--workspace") + 1] == str(workspace)
    assert plan.cmd[plan.cmd.index("--worktree") + 1] == "off"
    assert plan.cmd[plan.cmd.index("--contributor-mode") + 1] == "off"
    assert plan.cmd[plan.cmd.index("--config") + 1] == (
        'run.toolset=["read_file"]' if mode == "read-only"
        else 'run.toolset=["read_file", "write_file", "edit_file"]'
    )
    assert {"--disable-shell", "--disable-web-tools", "--disable-reminders",
            "--disable-request-user-input", "--no-foreign-personal-context"} <= set(plan.cmd)
    assert plan.env_overrides == {"MUSE_NO_AUTO_UPDATE": "1"}
    assert "GH_TOKEN" in plan.env_unsets
    assert Path(plan.cmd[-1]).read_text() == "fixture"
    from lu_machinery.agent_runtime.adapters import muse as muse_module

    package = tmp_path / "machinery/lu_machinery"
    (package / "agent_runtime/adapters").mkdir(parents=True)
    for directory in (package, package / "agent_runtime", package / "agent_runtime/adapters"):
        (directory / "__init__.py").write_text("")
    (package / "agent_runtime/adapters/muse.py").write_text(
        'import os\ndef sandbox_main(args):\n'
        '    return 0 if os.environ.get("LEARN_UKRAINIAN_PYTHON") else 23\n'
    )
    product = tmp_path / "product"
    product.mkdir()
    (product / "scripts").symlink_to(package, target_is_directory=True)
    monkeypatch.setattr(muse_module, "__file__", str(product / "scripts/agent_runtime/adapters/muse.py"))
    bootstrap = muse_module.confined_muse_argv(["fixture"], workspace, mode, "symlink-bootstrap", scratch)
    completed = subprocess.run(bootstrap, cwd=workspace, env={"PATH": os.defpath},
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


pytestmark = pytest.mark.usefixtures("configured_roots")
