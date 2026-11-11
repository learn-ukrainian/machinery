"""Fleet-comms observer routes keep their observer contract."""

import pytest

from lu_machinery.api.route_contracts import contract_for_route


@pytest.mark.parametrize("path", ["/api/fleet", "/api/fleet/requests", "/api/fleet/v10/now"])
def test_fleet_comms_routes_keep_observer_contract(path: str) -> None:
    contract = contract_for_route(path)

    assert contract is not None
    assert contract.pattern == "/api/fleet"
    assert contract.locality == "cluster_authoritative"
    assert contract.response_schema_version == "comms.v2"
