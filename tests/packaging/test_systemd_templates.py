"""Linux-native systemd templates must supervise listeners, not oneshot services.sh."""

from __future__ import annotations

from pathlib import Path

from tests._host_path_guard import assert_no_host_facts

PACKAGING = Path(__file__).resolve().parents[2] / "packaging" / "systemd"
def test_systemd_templates_have_no_host_facts() -> None:
    for path in PACKAGING.iterdir():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert_no_host_facts(text, path.name)
    # Units that launch npm or node through /usr/bin/env must prefer the pinned Node
    # over the host Node. Without this line the Astro unit crash-loops on the older /usr/bin/node.
    pinned_node_path = "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
    for path in sorted(PACKAGING.glob("*.service")):
        lines = path.read_text(encoding="utf-8").splitlines()
        launches_host_node = [
            line
            for line in lines
            if line.startswith(("ExecStart=", "ExecStartPre=", "ExecStartPost="))
            and ("/usr/bin/env npm" in line or "/usr/bin/env node" in line)
        ]
        if launches_host_node:
            assert pinned_node_path in lines, path.name


def test_git_maintenance_limits_and_schedule() -> None:
    from lu_machinery.orchestration.scheduled_worktree_cleanup import GC_DEADLINE_SECONDS, GC_MEMORY_MAX

    service = (PACKAGING / "learn-ukrainian-git-maintenance.service").read_text()
    assert GC_MEMORY_MAX == 2 * 1024**3
    assert {"MemoryMax=2G", "MemorySwapMax=0", "CPUQuota=100%", "TasksMax=32", "KillMode=control-group"} <= set(service.splitlines())
    timeout = int(next(line.split("=", 1)[1] for line in service.splitlines() if line.startswith("TimeoutStartSec=")))
    # Both repositories run sequentially; each may consume its full deadline.
    assert timeout > 2 * GC_DEADLINE_SECONDS + 20
    timer = (PACKAGING / "learn-ukrainian-git-maintenance.timer").read_text()
    assert {"OnCalendar=*-*-* 03:30:00 UTC", "Persistent=false"} <= set(timer.splitlines())
