#!/usr/bin/env python3
"""Fail when the canonical fleet model catalog is invalid or stale."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lu_machinery.review.model_catalog import (
    CATALOG_PATH,
    ModelCatalogError,
    catalog_age_days,
    catalog_is_stale,
    load_model_catalog,
    validate_glm_alias_consumers,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Validate catalog structure, alias consumers and freshness.\n"
                     "Use for the model-catalog gate; provider health is checked separately."),
        epilog=("Examples: .venv/bin/python -m lu_machinery.lint.lint_model_catalog --as-of 2026-11-08\n"
                "Outputs: validation JSON on stdout; no writes.\n"
                "Exit codes: 0 valid and fresh; 1 invalid or stale; 2 invalid arguments.\n"
                "Related: lu_machinery/config/model_catalog.yaml."),
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH, help="Catalog YAML path, default: canonical machinery catalog.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="Freshness date YYYY-MM-DD, default: today.")
    args = parser.parse_args()
    try:
        load_model_catalog.cache_clear()
        catalog = load_model_catalog(args.catalog.resolve())
        validate_glm_alias_consumers()
        age_days = catalog_age_days(catalog, as_of=args.as_of)
        stale = catalog_is_stale(catalog, as_of=args.as_of)
    except ModelCatalogError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1

    result = {
        "ok": not stale,
        "schema_version": catalog["schema_version"],
        "reviewed_on": catalog["reviewed_on"],
        "age_days": age_days,
        "refresh_after_days": catalog["refresh_after_days"],
        "model_count": len(catalog["models"]),
        "review_candidate_count": len(catalog["review_candidates"]),
    }
    if stale:
        result["error"] = (
            "model catalog is stale; re-enumerate live CLI/provider catalogs, "
            "verify official sources and CodexBar health, then update reviewed_on"
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
