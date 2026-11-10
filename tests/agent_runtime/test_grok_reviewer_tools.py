"""Opt-in Grok reviewer shell and hook boundary."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from lu_machinery.agent_runtime.adapters.grok_build import GrokBuildAdapter
from lu_machinery.agent_runtime.env_sanitize import build_agent_env
from lu_machinery.common.repo_root import machinery_path
from tests.agent_runtime.product_roots import (  # noqa: F401
    agent_runtime_roots,
    configured_roots,
)

pytestmark = pytest.mark.usefixtures("agent_runtime_roots")

ROOT = Path(__file__).resolve().parents[2]


def _plan(tmp_path: Path, config: dict | None):
    with patch("lu_machinery.agent_runtime.adapters.grok_build.shutil.which", return_value="/usr/bin/grok"):
        return GrokBuildAdapter().build_invocation(
            prompt="inspect",
            mode="read-only",
            cwd=tmp_path,
            model=None,
            task_id=None,
            session_id=None,
            tool_config=config,
            effort="low",
        )


def test_reviewer_opt_in_denies_bash_and_guards_all_tracked_reads(tmp_path: Path) -> None:
    adapter = GrokBuildAdapter()
    baseline = _plan(tmp_path, None)
    reviewer = _plan(tmp_path, {"reviewer_tools": True})
    try:
        agent_path = Path(reviewer.cmd[reviewer.cmd.index("--agent") + 1])
        definition = agent_path.read_text(encoding="utf-8")
        assert definition.count("type: command") == 11
        for name in (
            "enforce-venv.sh",
            "heal-core-bare.py",
            "guard-branch-switch-in-main.py",
            "guard-admin-merge.py",
            "guard-pr-merge.py",
            "guard-secret-print.py",
            "guard-primary-checkout-write.py",
            "guard-public-github-text.py",
            "guard-reviewer-publish.py",
        ):
            assert str(machinery_path("agents_extensions/shared/hooks") / name) in definition
        assert "-I -m lu_machinery.agent_runtime.grok_hook_bridge" in definition
        assert reviewer.cmd[reviewer.cmd.index("--permission-mode") + 1] == "auto"
        assert "--always-approve" not in reviewer.cmd
        allows = [reviewer.cmd[i + 1] for i, arg in enumerate(reviewer.cmd) if arg == "--allow"]
        assert "Bash" not in allows
        tools = reviewer.cmd[reviewer.cmd.index("--tools") + 1].split(",")
        assert tools == ["read_file", "list_dir", "grep"]
        assert allows == ["Read", "Grep"]
        assert "grok_reviewer_permissions.py" in definition
        assert "Approval-requiring tools are denied without prompting" in definition
        assert "using tracked-file reads only" in definition
        assert "Report any execution evidence you could not obtain" in definition
        assert "literal read commands" not in definition
        from lu_machinery.agent_runtime.grok_reviewer_permissions import (
            GROK_REVIEWER_READ_COMMANDS,
        )

        assert all(command not in definition for command in GROK_REVIEWER_READ_COMMANDS)
        assert "--no-subagents" in reviewer.cmd and "--disable-web-search" in reviewer.cmd
        denies = [reviewer.cmd[i + 1] for i, arg in enumerate(reviewer.cmd) if arg == "--deny"]
        assert set(denies) == {"Write", "Edit", "Bash", "MCPTool", "WebFetch", "WebSearch"}
        assert "search_replace" in reviewer.cmd[reviewer.cmd.index("--disallowed-tools") + 1].split(",")
        assert baseline.cmd[baseline.cmd.index("--permission-mode") + 1] == "auto"
        assert reviewer.env_overrides == {"LU_CLAUDE_READ_ONLY_GIT_PUSH_BLOCK": "1"}
        env = build_agent_env(provider="grok", overrides=reviewer.env_overrides)
        assert any(
            value == "url.file:///dev/null/claude-read-only/.pushInsteadOf"
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_KEY_")
        )
    finally:
        adapter.cleanup_invocation(reviewer)
    assert not agent_path.exists()


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("gh pr view 8912 --json number", False),
        ("git push --dry-run", True),
        ("python -m lu_machinery.publish issue-comment --number 1 --body unit", True),
        ("python -m lu_machinery.publish --help", False),
        ("python -m lu_machinery.publish pr-comment --help", False),
        # Every entry form of both publisher names, the installed package and the product's
        # in-tree copy, must be refused: interpreter flags or clusters before -m, an attached
        # module, the package's __main__ module, and its __main__.py or directory as a path.
        ("python -m scripts.publish issue-comment --number 1 --body unit", True),
        ("python -I -m lu_machinery.publish issue-comment --number 1", True),
        ("python -Im scripts.publish issue-comment --number 1", True),
        ("python -W ignore -mlu_machinery.publish issue-comment --number 1", True),
        ("python -u -Es -m scripts.publish.__main__ pr-comment --number 1", True),
        ("python -m lu_machinery.publish.__main__ issue-comment --number 1", True),
        ("python /x/site-packages/lu_machinery/publish/__main__.py issue-comment --number 1", True),
        ("python scripts/publish/__main__.py issue-comment --number 1", True),
        ("python ./lu_machinery/publish/ issue-comment --number 1", True),
        ("python -X dev scripts/publish issue-comment --number 1", True),
        # The guard cannot know the working directory, so a relative tail of a publisher path
        # or module, a dots-only directory and the cached bytecode all count as the publisher.
        ("cd lu_machinery/publish && python __main__.py issue-comment --number 1", True),
        ("cd lu_machinery/publish/__pycache__ && python .. issue-comment --number 1", True),
        ("cd lu_machinery && python -m publish issue-comment --number 1", True),
        ("python scripts/publish/__pycache__/__main__.cpython-312.pyc issue-comment --number 1", True),
        ("python -m scripts.foo bar", False),
        # A launcher runs the publisher too: uv run's command, its -m module and a script or
        # directory it hands to Python, a wrapper spelled as a path, and the time keyword's -p.
        ("uv run python -m scripts.publish issue-comment --number 1 --body unit", True),
        ("uv run --no-sync python -m lu_machinery.publish issue-comment --number 1", True),
        ("uv run -m scripts.publish issue-comment --number 1", True),
        ("uv run scripts/publish/__main__.py issue-comment --number 1", True),
        ("/usr/bin/timeout 5 python -m scripts.publish issue-comment --number 1", True),
        ("time -p python -m scripts.publish issue-comment --number 1", True),
        ("/usr/bin/time -p python -m scripts.publish issue-comment --number 1", True),
        # A wrapper option outside the guard's allowlist, or an unknown expansion between a
        # wrapper and its command, may hide the command it runs: each such class is refused.
        ("env -S 'uv run' -m scripts.publish issue-comment --number 1", True),
        ("/usr/bin/time -pf %e python -m scripts.publish issue-comment --number 1", True),
        ("nice --adj 5 python -m scripts.publish issue-comment --number 1", True),
        ("uv run -qm scripts.publish issue-comment --number 1", True),
        ("nohup $UNKNOWN python -m scripts.publish issue-comment --number 1", True),
        # env's lone -, a quoted time (the executable), an empty expansion and a brace pattern.
        ("env - python -m scripts.publish issue-comment --number 1", True),
        ('"time" -f %e python -m scripts.publish issue-comment --number 1', True),
        ("X=; nohup $X python -m scripts.publish issue-comment --number 1", True),
        ("nohup {python,python} -m scripts.publish issue-comment --number 1", True),
        # A word after a quoted time (the executable) that expands to an option.
        ('X=-f; "time" $X %e python -m scripts.publish issue-comment --number 1', True),
        ("uv run pytest -q", False),
        ("uv run python -m scripts.foo bar", False),
        ("gh pr comment 8912 --body test", True),
    ],
)
def test_grok_hook_bridge_runs_publish_guard(command: str, blocked: bool) -> None:
    event = {
        "hook_event_name": "PreToolUse",
        "toolName": "run_terminal_command",
        "toolInput": {"command": command},
        "cwd": str(ROOT),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
            str(machinery_path("agents_extensions/shared/hooks/guard-reviewer-publish.py")),
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 2) is blocked, (command, result.stderr)


def test_grok_hook_bridge_denies_unreadable_event() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
            str(machinery_path("agents_extensions/shared/hooks/guard-reviewer-publish.py")),
        ],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2


_PINNED_GUARDS = ("guard-pr-merge.py", "guard-admin-merge.py", "guard-branch-switch-in-main.py")
_HOOKS = machinery_path("agents_extensions/shared/hooks")


def _pinned(name: str) -> str:
    return shlex.quote(str(_HOOKS / name))


def _bridge(guard_command: str, shell_command: str) -> subprocess.CompletedProcess[str]:
    event = {
        "hook_event_name": "PreToolUse",
        "toolName": "run_terminal_command",
        "toolInput": {"command": shell_command},
        "cwd": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge", guard_command],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("name", _PINNED_GUARDS)
def test_grok_bridge_runs_the_adapters_pinned_invocation(name: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import guard_argv
    command = _pinned(name)
    assert guard_argv(command) == [sys.executable, "-I", str(_HOOKS / name)]
    allowed = _bridge(command, "echo 'git checkout -b fixture; gh pr merge 5 --admin'")
    assert allowed.returncode == 0, allowed.stderr


def test_grok_bridge_pinned_guard_still_blocks() -> None:
    result = _bridge(_pinned("guard-pr-merge.py"), "gh pr merge 5 --auto")
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("name", ["guard-pr-merge.py", "guard-secret-print.py", "enforce-venv.sh"])
def test_grok_bridge_keeps_the_plain_guard_path(name: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import guard_argv

    guard = str(_HOOKS / name)
    assert guard_argv(guard) == ([sys.executable, "-I", guard] if name.endswith(".py") else [guard])


@pytest.mark.parametrize(
    "command",
    [
        "'{hooks}/guard-pr-'merge.py",
        '"{hooks}/guard-pr-merge.py"',
        "{hooks}/guard\\-pr-merge.py",
    ],
)
def test_grok_bridge_classifies_the_pinned_invocation_after_tokenization(command: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import guard_argv
    python = sys.executable
    assert guard_argv(command.format(python=python, hooks=_HOOKS)) == [str(python), "-I", str(_HOOKS / "guard-pr-merge.py")]


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        # Extra arguments, behind split quoting and backslash spellings too.
        ("{python} '{hooks}/guard-pr-'merge.py --extra", "one tracked fleet guard path required"),
        ("{python} {hooks}/guard\\-pr-merge.py extra", "one tracked fleet guard path required"),
        ("'{hooks}/guard-secret-'print.py --extra", "one tracked fleet guard path required"),
        # Missing arguments: the interpreter or nothing at all.
        ("{python}", "only tracked fleet guards may run"),
        ("'{python}'", "only tracked fleet guards may run"),
        ("", "one tracked fleet guard path required"),
        # The wrapper, an untracked path, or a tracked path in another spelling of the directory.
        ("{hooks}/run-project-python-hook.sh", "only tracked fleet guards may run"),
        ("{python} '{hooks}/run-project-python-'hook.sh", "one tracked fleet guard path required"),
        ("/bin/true", "only tracked fleet guards may run"),
        ("{hooks}/../hooks/guard-secret-print.py", "only tracked fleet guards may run"),
    ],
)
def test_grok_bridge_refuses_noncanonical_spellings(command: str, reason: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import (
        GuardInvocationError,
        guard_argv,
    )
    command = command.format(python=sys.executable, hooks=_HOOKS)
    with pytest.raises(GuardInvocationError, match=reason):
        guard_argv(command)
    result = _bridge(command, "echo hello")
    assert result.returncode == 2
    assert reason in result.stderr


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["/usr/bin/python3", "-I", "{hooks}/guard-pr-merge.py"], "one tracked fleet guard path required"),
        (["{python}", "{hooks}/guard-secret-print.py"], "one tracked fleet guard path required"),
        (["{python}", "/tmp/elsewhere/guard-pr-merge.py"], "one tracked fleet guard path required"),
        (["{python}", "{hooks}/guard-pr-merge.py", "--extra"], "one tracked fleet guard path required"),
        (["{python}", "-E", "{hooks}/guard-pr-merge.py"], "one tracked fleet guard path required"),
        (["/tmp/elsewhere/guard-pr-merge.py"], "only tracked fleet guards may run"),
    ],
)
def test_grok_bridge_refuses_other_invocations(argv: list[str], reason: str) -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import (
        GuardInvocationError,
        guard_argv,
    )
    command = shlex.join(arg.format(python=sys.executable, hooks=_HOOKS) for arg in argv)
    with pytest.raises(GuardInvocationError, match=reason):
        guard_argv(command)
    result = _bridge(command, "echo hello")
    assert result.returncode == 2
    assert reason in result.stderr


def test_grok_bridge_refuses_an_unreadable_invocation() -> None:
    from lu_machinery.agent_runtime.grok_hook_bridge import (
        GuardInvocationError,
        guard_argv,
    )

    with pytest.raises(GuardInvocationError, match="unreadable"):
        guard_argv(f"python '{_HOOKS / 'guard-pr-merge.py'}")


@pytest.mark.parametrize("quoted", [True, False])
def test_grok_bridge_refuses_a_missing_guard(quoted: bool) -> None:
    from lu_machinery.agent_runtime import grok_hook_bridge

    vanished = "guard-vanished.py"
    guard = str(_HOOKS / vanished)
    command = shlex.quote(guard) if quoted else guard
    with pytest.raises(grok_hook_bridge.GuardInvocationError, match="fleet guard unavailable"):
        grok_hook_bridge.guard_argv(command)


_FLEET_GUARD_NAMES = (
    "enforce-venv.sh",
    "heal-core-bare.py",
    "guard-branch-switch-in-main.py",
    "guard-admin-merge.py",
    "guard-pr-merge.py",
    "guard-secret-print.py",
    "guard-primary-checkout-write.py",
    "guard-public-github-text.py",
)


@pytest.mark.parametrize("mode", ["workspace-write", "danger"])
def test_write_mode_installs_fleet_guards_without_reviewer_publish_or_push_rewrite(tmp_path: Path, mode: str) -> None:
    """Write sessions load the fleet PreToolUse guards and can still push (#8965).

    Grok 1.0.41 leaves yolo off when ``--permission-mode auto`` is set, and the
    auto classifier then refuses ``git push`` before execution. Write mode uses
    ``bypassPermissions`` plus the tracked worker guards. The agent is the write
    worker, not the read-only reviewer: no publish guard and no push rewrite.
    """
    adapter = GrokBuildAdapter()
    agent_path: Path | None = None
    with patch("lu_machinery.agent_runtime.adapters.grok_build.shutil.which", return_value="/usr/bin/grok"):
        write_plan = adapter.build_invocation(
            prompt="push the dispatch branch",
            mode=mode,
            cwd=tmp_path,
            model=None,
            task_id="impl-8965",
            session_id=None,
            tool_config={"reviewer_tools": True},
            effort="low",
        )
    try:
        assert write_plan.cmd[write_plan.cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--always-approve" in write_plan.cmd
        assert "--deny" not in write_plan.cmd
        assert "--tools" not in write_plan.cmd
        assert "reviewer_agent_file" not in write_plan.metadata
        agent_path = Path(write_plan.cmd[write_plan.cmd.index("--agent") + 1])
        assert agent_path.name.endswith(".grok-write-agent.md")
        definition = agent_path.read_text(encoding="utf-8")
        assert "name: lu-write-worker" in definition
        assert "lu-read-only-reviewer" not in definition
        assert definition.count("type: command") == 9
        for name in _FLEET_GUARD_NAMES:
            assert str(machinery_path("agents_extensions/shared/hooks") / name) in definition
        assert "guard-reviewer-publish.py" not in definition
        assert "run_terminal_command" in definition
        assert "search_replace" in definition
        assert "-I -m lu_machinery.agent_runtime.grok_hook_bridge" in definition
        assert write_plan.env_overrides == {}
        env = build_agent_env(provider="grok", overrides=write_plan.env_overrides)
        assert not any(
            value == "url.file:///dev/null/claude-read-only/.pushInsteadOf"
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_KEY_")
        )
    finally:
        adapter.cleanup_invocation(write_plan)
        if agent_path is not None:
            assert not agent_path.exists()

    event = {
        "hook_event_name": "PreToolUse",
        "toolName": "run_terminal_command",
        "toolInput": {"command": "git push -u origin HEAD"},
        "cwd": str(ROOT),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
            str(machinery_path("agents_extensions/shared/hooks/guard-reviewer-publish.py")),
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "git push" in result.stderr


def test_grok_hook_bridge_primary_checkout_write_guard(agent_runtime_roots, monkeypatch) -> None:  # noqa: F811
    """The bridge lets the fleet guard classify Grok write and shell tools.

    The hermetic primary stands in for the product's primary checkout on ``main``; the writer runs in
    a dispatch worktree of it, as in the product.
    """
    primary = agent_runtime_roots.primary
    worktree = primary / ".worktrees" / "dispatch" / "grok" / "guard"
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "guard", str(worktree)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(primary))
    probe = primary / "probe.txt"
    allow = worktree / "probe-allow.txt"
    cases = [
        ("write", {"file_path": str(probe), "content": "probe"}, True),
        (
            "search_replace",
            {"file_path": str(probe), "old_string": "a", "new_string": "b"},
            True,
        ),
        ("run_terminal_command", {"command": f"printf probe > {probe}"}, True),
        # uv runs the wrapped writer, and --directory moves it into the primary; the refusal
        # names a relative target as written.
        ("run_terminal_command", {"command": f"uv run --no-sync tee {probe}"}, True),
        ("run_terminal_command", {"command": f"uv run --directory {primary} tee {probe.name}"}, True, f"'{probe.name}'"),
        # A builtin after a redirect binds in this shell; a quoted name still sets uv's directory.
        ("run_terminal_command", {"command": f"D=/tmp/; < /usr/bin/env export D={primary}/; tee ${{D}}{probe.name}"}, True),
        (
            "run_terminal_command",
            {"command": f'export UV_WORKING_""DIR={primary}; uv run tee {probe.name}'},
            True,
            f"'{probe.name}'",
        ),
        # An exec or env option the guard does not model hides the writer; it is refused anywhere.
        ("run_terminal_command", {"command": f"exec -c tee {probe}"}, True, "undecidable_wrapper_option"),
        ("run_terminal_command", {"command": f"env --argv0 x tee {probe}"}, True, "undecidable_wrapper_option"),
        # A builtin named by an expansion, an agent-set UV_ENV_FILE and a ~ value make uv's
        # directory unknown, so a relative target refuses.
        (
            "run_terminal_command",
            {"command": f"E=export; command $E UV_WORKING_DIR={primary}; uv run tee {probe.name}"},
            True,
            f"'{probe.name}'",
        ),
        (
            "run_terminal_command",
            {"command": f"UV_ENV_FILE=/dev/stdin uv run bash -c 'uv run tee {probe.name}'"},
            True,
            f"'{probe.name}'",
        ),
        ("run_terminal_command", {"command": f"UV_WORKING_DIR=~ uv run tee {probe.name}"}, True, f"'{probe.name}'"),
        # After time, an empty word or a builtin that changes this shell, which only the keyword
        # runs here; and a dotenv source that unsetting UV_WORKING_DIR does not clear.
        ("run_terminal_command", {"command": f"X=; time $X tee {probe}"}, True, "undecidable_wrapper_option"),
        (
            "run_terminal_command",
            {"command": f'D={primary}/; "time" export D=/tmp/; tee ${{D}}{probe.name}'},
            True,
            "undecidable_wrapper_option",
        ),
        (
            "run_terminal_command",
            {"command": f"UV_ENV_FILE=/dev/stdin env -u UV_WORKING_DIR uv run bash -c 'uv run tee {probe.name}'"},
            True,
            f"'{probe.name}'",
        ),
        ("write", {"file_path": str(allow), "content": "ok"}, False),
    ]
    for tool_name, tool_input, blocked, *shown in cases:
        event = {
            "hook_event_name": "PreToolUse",
            "toolName": tool_name,
            "toolInput": tool_input,
            "cwd": str(worktree),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-I", "-m", "lu_machinery.agent_runtime.grok_hook_bridge",
                str(machinery_path("agents_extensions/shared/hooks/guard-primary-checkout-write.py")),
            ],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode == 2) is blocked, (tool_name, result.returncode, result.stderr)
        if blocked:
            assert "BLOCKED by guard-primary-checkout-write" in result.stderr
            assert (shown[0] if shown else str(probe)) in result.stderr


@pytest.mark.parametrize("tool", ["run_terminal_command", "run_terminal_cmd", "Bash"])
def test_literal_read_commands_pass_guard_but_keep_native_bash_backstop(tmp_path, tool):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        GROK_REVIEWER_READ_COMMANDS,
        reviewer_command_allowed,
    )

    plan = _plan(tmp_path, {"reviewer_tools": True})
    try:
        allows = [plan.cmd[i + 1] for i, arg in enumerate(plan.cmd) if arg == "--allow"]
        for command in GROK_REVIEWER_READ_COMMANDS:
            assert reviewer_command_allowed({
                "hook_event_name": "PreToolUse", "toolName": tool, "toolInput": {"command": command},
            })
            assert f"Bash({command})" not in allows
        denies = [plan.cmd[i + 1] for i, arg in enumerate(plan.cmd) if arg == "--deny"]
        assert "Bash" in denies
    finally:
        GrokBuildAdapter().cleanup_invocation(plan)


@pytest.mark.parametrize("command", [
    "touch output.txt", "git push", "curl https://example.invalid", "wget https://example.invalid",
    "git -c color.ui=false diff", "python - <<'PY'\nprint('probe')\nPY",
    "cat ~/.grok/auth.json", "git diff --output=out", "git diff --ext-diff",
    "git diff --textconv", "env git diff", "git diff; touch output.txt",
])
def test_disallowed_shell_actions_return_typed_refusal_before_native_permissions(command):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import REFUSAL_CODE

    result = subprocess.run(
        [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")), "--review-root", str(ROOT)],
        input=json.dumps({
            "hook_event_name": "PreToolUse", "toolName": "run_terminal_command", "toolInput": {"command": command},
        }), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert result.stderr.startswith(REFUSAL_CODE + ":")
    assert command not in result.stderr
    assert "Continue the review" in result.stderr
    assert "use tracked-file reads only" in result.stderr
    assert "report any execution evidence you could not obtain" in result.stderr
    assert "literal Git inspection commands" not in result.stderr


@pytest.mark.parametrize("payload", [None, [], {},
    {"hook_event_name": "PreToolUse", "toolName": "run_terminal_command", "toolInput": {}},
    {"hook_event_name": "PreToolUse", "toolName": "write", "toolInput": {"command": "pwd"}},
    {"hook_event_name": "PostToolUse", "toolName": "Bash", "toolInput": {"command": "pwd"}},
])
def test_permission_guard_fails_closed_on_unreadable_or_unknown_tools(payload):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        reviewer_command_allowed,
    )

    assert not reviewer_command_allowed(payload)


def test_literal_permissions_cannot_be_extended_or_wrapped():
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        GROK_REVIEWER_READ_COMMANDS,
        reviewer_command_allowed,
    )

    for command in GROK_REVIEWER_READ_COMMANDS:
        for altered in (command + " > out", command + " && touch out", "env " + command,
                        command + " --output=out", command + " --ext-diff", command + "\n"):
            assert not reviewer_command_allowed({
                "hook_event_name": "PreToolUse", "toolName": "Bash", "toolInput": {"command": altered},
            })


def test_permission_guard_cli_admits_read_command_and_denies_bad_json():
    from lu_machinery.agent_runtime.grok_reviewer_permissions import REFUSAL_CODE

    for event, expected in [(json.dumps({
        "hook_event_name": "PreToolUse", "toolName": "run_terminal_command", "toolInput": {"command": "pwd"},
    }), 0), ("broken json", 2)]:
        result = subprocess.run(
            [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")), "--review-root", str(ROOT)],
            input=event, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == expected
        assert result.stderr.startswith(REFUSAL_CODE) if expected else result.stderr == ""


@pytest.fixture
def tracked_review_root(tmp_path):
    root = tmp_path / "review"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    (root / "tracked.txt").write_text("public fixture", encoding="utf-8")
    (root / "untracked.txt").write_text("private fixture", encoding="utf-8")
    (root / "tracked[1].txt").write_text("private fixture", encoding="utf-8")
    (root / "tracked1.txt").write_text("public fixture", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt", "tracked1.txt"], cwd=root, check=True, timeout=30)
    return root


@pytest.mark.parametrize("tool,inputs,allowed", [
    ("read_file", {"target_file": "tracked.txt"}, True),
    ("grep", {"path": "tracked.txt", "pattern": "fixture"}, True),
    ("list_dir", {"target_directory": "."}, True),
    ("read_file", {"target_file": "untracked.txt"}, False),
    ("read_file", {"target_file": "tracked[1].txt"}, False),
    ("read_file", {"target_file": "missing.txt"}, False),
    ("read_file", {"target_file": ".git/config"}, False),
    ("read_file", {"target_file": "."}, False),
    ("grep", {"path": ".", "pattern": "fixture"}, False),
    ("grep", {"pattern": "fixture"}, False),
    ("list_dir", {"target_directory": ".git"}, False),
    ("read_file", {"target_file": ""}, False),
    ("read_file", {"target_file": "a\x00b"}, False),
    ("unknown", {}, False),
    ([], {}, False),
])
def test_review_reads_remain_within_tracked_checkout(tracked_review_root, tool, inputs, allowed):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        reviewer_tool_allowed,
    )

    assert reviewer_tool_allowed({
        "hook_event_name": "PreToolUse", "toolName": tool, "toolInput": inputs,
    }, tracked_review_root) is allowed


def test_read_guard_denies_absolute_and_symlink_escape(tracked_review_root):
    from lu_machinery.agent_runtime.grok_reviewer_permissions import (
        reviewer_tool_allowed,
    )

    outside = tracked_review_root.parent / "outside.txt"
    outside.write_text("private fixture", encoding="utf-8")
    (tracked_review_root / "escape.txt").symlink_to(outside)
    subprocess.run(["git", "add", "escape.txt"], cwd=tracked_review_root, check=True, timeout=30)
    for path in (str(outside), "../outside.txt", "escape.txt"):
        assert not reviewer_tool_allowed({
            "hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": path},
        }, tracked_review_root)


def test_read_guard_denies_unavailable_git(tracked_review_root, monkeypatch):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(policy.subprocess, "run", unavailable)
    assert not policy.reviewer_tool_allowed({
        "hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "tracked.txt"},
    }, tracked_review_root)


@pytest.mark.parametrize("event,expected", [
    (json.dumps({"hook_event_name": "PreToolUse", "toolName": "Bash", "toolInput": {"command": "pwd"}}), 0),
    ("broken json", 2),
    ("{}", 2),
])
def test_permission_cli_main_returns_typed_disposition(monkeypatch, capsys, tracked_review_root, event, expected):
    import io

    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    monkeypatch.setattr(sys, "argv", ["guard", "--review-root", str(tracked_review_root)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    assert policy.main() == expected
    captured = capsys.readouterr()
    assert policy.REFUSAL_CODE in captured.err if expected else captured.err == ""


def test_tracked_symlink_loop_is_a_typed_denial(tracked_review_root):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    (tracked_review_root / "loop").symlink_to("loop")
    subprocess.run(["git", "add", "loop"], cwd=tracked_review_root, check=True, timeout=30)
    event = {"hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "loop"}}
    assert not policy.reviewer_tool_allowed(event, tracked_review_root)
    result = subprocess.run(
        [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")),
         "--review-root", str(tracked_review_root)],
        input=json.dumps(event), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert result.stderr.startswith(policy.REFUSAL_CODE + ":")
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("error", [RuntimeError, PermissionError, MemoryError, KeyboardInterrupt, SystemExit])
def test_permission_boundary_denies_every_evaluation_exception(tracked_review_root, monkeypatch, capsys, error):
    import io

    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    def fail(*args, **kwargs):
        raise error("private error detail")

    event = {"hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "tracked.txt"}}
    monkeypatch.setattr(Path, "open", fail)
    assert not policy.reviewer_tool_allowed(event, tracked_review_root)
    monkeypatch.setattr(sys, "argv", ["guard", "--review-root", str(tracked_review_root)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert policy.main() == 2
    captured = capsys.readouterr()
    assert captured.err.startswith(policy.REFUSAL_CODE + ":")
    assert "private error detail" not in captured.err


@pytest.mark.parametrize("error", [RuntimeError, PermissionError, MemoryError, KeyboardInterrupt])
def test_main_denies_interpreter_level_input_errors(tracked_review_root, monkeypatch, capsys, error):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    def fail(*args, **kwargs):
        raise error("private input error")

    monkeypatch.setattr(policy.json, "load", fail)
    monkeypatch.setattr(sys, "argv", ["guard", "--review-root", str(tracked_review_root)])
    assert policy.main() == 2
    assert capsys.readouterr().err.startswith(policy.REFUSAL_CODE + ":")


def test_guard_git_never_lazily_fetches(tracked_review_root, monkeypatch):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    calls = []
    real_run = policy.subprocess.run

    def record(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(policy.subprocess, "run", record)
    assert policy.reviewer_tool_allowed({
        "hook_event_name": "PreToolUse", "toolName": "read_file", "toolInput": {"target_file": "tracked.txt"},
    }, tracked_review_root)
    assert calls and all("--no-lazy-fetch" in argv for argv in calls)
    assert all("--no-lazy-fetch" in command for command in policy.GROK_REVIEWER_READ_COMMANDS if command.startswith("git "))


def test_unreadable_tracked_file_is_denied(tracked_review_root):
    from lu_machinery.agent_runtime import grok_reviewer_permissions as policy

    target = tracked_review_root / "tracked.txt"
    target.chmod(0)
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")),
             "--review-root", str(tracked_review_root)],
            input=json.dumps({"hook_event_name": "PreToolUse", "toolName": "read_file",
                              "toolInput": {"target_file": "tracked.txt"}}),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2
        assert result.stderr.startswith(policy.REFUSAL_CODE + ":")
    finally:
        target.chmod(0o600)


def test_guard_help_does_not_require_a_review_event():
    result = subprocess.run(
        [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "Exit codes:" in result.stdout


@pytest.mark.parametrize("event", ["{}", "broken json"])
def test_refusal_exits_two_with_real_closed_stderr_pipe(event):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(machinery_path("agent_runtime/grok_reviewer_permissions.py")),
             "--review-root", str(ROOT)],
            input=event, text=True, stdout=subprocess.PIPE, stderr=write_fd, timeout=30,
        )
        assert result.returncode == 2
        assert result.stdout == ""
    finally:
        os.close(write_fd)
