"""Execute admitted raw reads/private writes through the publishing guard and shared client.

The agent gh shim starts this module through the package bootstrap, so the
checkout is not put on the import path.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lu_machinery.opsec.gh_snapshot import admit
from lu_machinery.opsec.prepublish import PublishBlocked, internal_environment, real_gh


def main() -> int:
    real, _shim, *argv = sys.argv[1:]
    environment = internal_environment()
    environment["AGENT_REAL_GH"] = real
    frozen = None
    try:
        real = real_gh(environment)
        environment["AGENT_REAL_GH"] = real
        frozen = admit(argv, cwd=Path.cwd(), environment=environment)
        from lu_machinery.common import github_client

        return github_client.run(["gh", *frozen.argv], env=environment, native=True, timeout=None).returncode
    except PublishBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        if frozen is None:
            print("OPSEC: command admission failed; verify repository and flags.", file=sys.stderr)
            return 2
        if not frozen.write:
            print("OPSEC: read command failed; verify repository and flags.", file=sys.stderr)
            return 2
        print("OPSEC: publishing input unresolved; use explicit repository and --body-file.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
