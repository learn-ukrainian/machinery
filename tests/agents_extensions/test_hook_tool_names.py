"""Shared shell tool recognition is exact and leaves other tools untouched."""

import importlib.util

from lu_machinery.common.repo_root import machinery_path


def test_issue_10337_shell_names_are_exact():
    source = machinery_path('agents_extensions/shared/hooks/tool_names.py')
    spec = importlib.util.spec_from_file_location("hook_tool_names", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name in ("Bash", "Shell"):
        assert module.is_shell_tool(name)
    for name in ("bash", "shell", "Shell ", "Write", "Edit", "MultiEdit", "WriteShellStdin", "unknown", "", None, [], {}):
        assert not module.is_shell_tool(name)
