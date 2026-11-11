"""Contract gates recognize changes in both repository layouts."""

from lu_machinery.review.contract_canary import CONTRACT_PATHS, touches_contract


def test_contract_paths_match_product_layout() -> None:
    """Prevent installed keepers from missing product review-contract changes."""
    package_paths = [path for path in CONTRACT_PATHS if path.startswith("lu_machinery/")]
    assert package_paths
    for path in package_paths:
        assert touches_contract([path.replace("lu_machinery/", "scripts/", 1)])
    assert touches_contract(["scripts/review/record_cf_verdict.py"])
    assert touches_contract(["schemas/code-review-findings.v1.schema.json"])
    assert not touches_contract(["scripts/unrelated.py"])
