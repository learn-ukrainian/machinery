"""The shared MCP tool-config refusal for the retired Kimi seat (kept from the Kimi adapter tests).

The rest of learn-ukrainian's ``tests/agent_runtime/adapters/test_kimi_adapter.py`` tested the Kimi
adapters removed by #141 and is dropped; this one calls only ``build_mcp_tool_config``, which still
refuses Kimi MCP requests (``lu_machinery/agent_runtime/tool_config.py``).
"""

from __future__ import annotations

import pytest

from lu_machinery.agent_runtime.tool_config import build_mcp_tool_config
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")


def test_kimi_mcp_request_fails_closed_without_per_call_selector():
    tool_config, diagnostics = build_mcp_tool_config("kimi", mcp_servers=["sources"])

    assert tool_config is None
    assert diagnostics["resolution_status"] == "servers_not_found"
    assert diagnostics["missing_server_names"] == ["sources"]
