"""Control-plane storage seam (Phase 0b — sqlite default, real pg adapter)."""

from lu_machinery.control_plane.storage import (
    Authority,
    ControlPlaneError,
    ControlPlanePgConnectError,
    ControlPlanePgDsnMissingError,
    ControlPlaneSqliteRefusedError,
    ControlPlaneStoreUnavailableError,
    StoreId,
    connect,
    resolve_authority,
    sqlite_path,
)

__all__ = [
    "Authority",
    "ControlPlaneError",
    "ControlPlanePgConnectError",
    "ControlPlanePgDsnMissingError",
    "ControlPlaneSqliteRefusedError",
    "ControlPlaneStoreUnavailableError",
    "StoreId",
    "connect",
    "resolve_authority",
    "sqlite_path",
]
