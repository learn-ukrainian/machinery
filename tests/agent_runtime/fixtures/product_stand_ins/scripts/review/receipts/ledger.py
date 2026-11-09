"""STAND-IN for learn-ukrainian ``scripts/review/receipts/ledger.py`` (LU 5aba2adb).

Satisfies the machinery interface ``attempt_hooks().ledger()`` consumers read: ``REVIEW_TOOLS``
(the tool names are the product's, copied), ``review_tools()`` and ``LedgerError``. ``review_tools``
validates against the Sources server of the selected product root, as the product's does against its
own. ``records()`` always refuses. The real module imports several product modules.
"""

from __future__ import annotations

from pathlib import Path

from lu_machinery.agent_runtime.sources_read_only import SERVER_RELATIVE_PATH, _tool_sets, sources_tool_sets
from lu_machinery.common.repo_root import product_root

# Principle 4a of the review contract. A call to any other tool is refused.
REVIEW_TOOLS = frozenset(
    {
        "verify_words",
        "verify_word",
        "verify_lemma",
        "check_text",
        "inspect_word",
        "inspect_words",
        "verify_stress",
        "query_sum20",
        "query_ulif_records",
        "query_wikipedia_cached",
        "search_slovnyk_me",
        "search_esum",
        "search_grinchenko_1907",
        "search_definitions",
        "query_pravopys",
        "search_style_guide",
        "search_text",
        "query_r2u",
        "search_heritage",
        "check_russian_shadow",
        "search_ua_gec_errors",
        "query_grac",
        "query_cefr_level",
        "verify_quote",
    }
)
FULL_REVIEW_TOOLS = REVIEW_TOOLS | {"search_resources"}




def review_tools(review_access: str = "isolated", *, server_path: Path | None = None) -> frozenset[str]:
    if review_access not in {"isolated", "full"}:
        raise ValueError("review_access_invalid")
    if server_path is not None:
        try:
            launched = server_path.read_bytes()
            imported = (product_root() / SERVER_RELATIVE_PATH).read_bytes()
        except OSError as exc:
            raise ValueError("review_launched_sources_server_unreadable") from exc
        if launched != imported:
            raise ValueError("review_launched_sources_server_mismatch")
        readers = frozenset(_tool_sets.__wrapped__(server_path)[0])
    else:
        readers = frozenset(sources_tool_sets()[0])
    if not readers >= FULL_REVIEW_TOOLS:
        raise ValueError("review_contract_contains_non_read_only_sources_tool")
    return FULL_REVIEW_TOOLS if review_access == "full" else REVIEW_TOOLS


class LedgerError(Exception):
    """The ledger file, its sidecar, or the recording environment is unusable."""


def records(ledger_path: Path) -> list[dict]:
    raise LedgerError("the stand-in ledger holds no records")
