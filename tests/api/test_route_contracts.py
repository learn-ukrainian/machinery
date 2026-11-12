"""Fleet-comms observer routes keep their observer contract."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from lu_machinery.api.route_contracts import contract_for_route


@pytest.mark.parametrize("path", ["/api/fleet", "/api/fleet/requests", "/api/fleet/v10/now"])
def test_fleet_comms_routes_keep_observer_contract(path: str) -> None:
    contract = contract_for_route(path)

    assert contract is not None
    assert contract.pattern == "/api/fleet"
    assert contract.locality == "cluster_authoritative"
    assert contract.response_schema_version == "comms.v2"


def test_product_home_replaces_machinery_index_and_contract(tmp_path, monkeypatch):
    """Defect class: a packaged fallback shadows a declared product home and duplicates its contract."""
    from lu_machinery.api import main, route_contracts
    from lu_machinery.api.product_contribution import EMPTY, ProductContribution

    monkeypatch.setattr(main.config, "PRODUCT_ROOT", tmp_path)
    product_index = tmp_path / "product.html"
    product_index.write_text("product home")
    machinery_index = tmp_path / "index.html"
    machinery_index.write_text("machinery home")
    page = replace(route_contracts.PAGE_CONTRACTS[0], purpose="Product home")
    product = ProductContribution(static_files={"index.html": product_index}, page_contracts=(page,))
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=product))})
    context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=tmp_path))
    for path in ("", "index.html"):
        response = asyncio.run(main.serve_static(path, request, context))
        assert response.path == product_index
    contracts = [c for c in route_contracts.all_page_contracts(product) if c.file == "index.html"]
    assert contracts == [page]
    request.app.state.product = EMPTY
    assert asyncio.run(main.serve_static("", request, context)).path == machinery_index


def test_machinery_path_serves_machinery_home_when_product_owns_root(tmp_path, monkeypatch):
    """Defect class: a product that declares its own home at ``/`` hides the machinery page."""
    from lu_machinery.api import main
    from lu_machinery.api.product_contribution import ProductContribution

    monkeypatch.setattr(main.config, "PRODUCT_ROOT", tmp_path)
    product_index = tmp_path / "product.html"
    product_index.write_text("product home")
    machinery_index = tmp_path / "index.html"
    machinery_index.write_text("machinery home")
    product = ProductContribution(static_files={"index.html": product_index})
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=product))})
    context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=tmp_path))
    assert asyncio.run(main.serve_static("", request, context)).path == product_index
    for path in ("machinery", "machinery/"):
        assert asyncio.run(main.serve_static(path, request, context)).path == machinery_index
