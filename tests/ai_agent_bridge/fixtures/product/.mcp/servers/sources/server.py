"""Stand-in for the product's Sources server tool declarations (never imported).

``lu_machinery.agent_runtime.sources_read_only`` reads these declarations with ``ast``
to split read-only from persisting tools; the real server is product-owned (C-4843).
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True)  # noqa: F821
PERSISTING = ToolAnnotations(readOnlyHint=False)  # noqa: F821


def _tool(**fields):
    fields.setdefault("annotations", READ_ONLY)
    return fields


async def list_tools():
    return [_tool(name="verify_word"), _tool(name="record_receipt", annotations=PERSISTING)]
