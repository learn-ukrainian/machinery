"""STAND-IN for learn-ukrainian ``scripts/curriculum/review_attempt_hooks.py`` (LU 5aba2adb).

Satisfies the machinery interface ``lu_machinery.review.product_hooks.attempt_hooks().ledger()`` only.
The real module imports about six product modules and cannot be byte-copied; the other hooks it
defines name product modules this stand-in does not have.
"""

from __future__ import annotations

from types import ModuleType


def ledger() -> ModuleType:
    from scripts.review.receipts import ledger as receipts_ledger

    return receipts_ledger
