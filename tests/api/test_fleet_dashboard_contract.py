"""Route-registration contract for the fleet observer routes."""

from __future__ import annotations

from fastapi import FastAPI

from lu_machinery.api.fleet_router import router as fleet_router
from lu_machinery.api.monitor_context import production_context
from lu_machinery.api.route_contracts import contract_for_route


def test_fleet_routes_are_registered_get_only_and_contracted() -> None:
    observer_app = FastAPI()
    observer_app.state.ctx = production_context()
    observer_app.include_router(fleet_router, prefix="/api/fleet")
    openapi = observer_app.openapi()["paths"]
    paths = {path for path in openapi if path.startswith("/api/fleet")}

    expected = {
        "/api/fleet/health",
        "/api/fleet/overview",
        "/api/fleet/operations",
        "/api/fleet/agents",
        "/api/fleet/endpoints",
        "/api/fleet/requests",
        "/api/fleet/authority/jobs",
        "/api/fleet/messages",
        "/api/fleet/messages/{message_id}",
        "/api/fleet/discussions",
        "/api/fleet/discussions/{conversation_id}",
        "/api/fleet/reviews",
        "/api/fleet/reviews/{review_id}",
        "/api/fleet/dead-letters",
        "/api/fleet/migrations",
        "/api/fleet/acp/conversations",
        "/api/fleet/acp/conversations/{conversation_id}",
        "/api/fleet/activity",
    }
    assert expected <= paths
    assert all(set(openapi[path]) == {"get"} for path in paths)
    assert contract_for_route("/api/fleet/messages", "http") is not None
    operations_contract = contract_for_route("/api/fleet/operations", "http")
    assert operations_contract is not None
    assert operations_contract.pattern == "/api/fleet/operations"

