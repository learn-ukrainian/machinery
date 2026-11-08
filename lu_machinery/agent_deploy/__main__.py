"""Apply or verify release-owned agent extensions in an explicitly selected consumer."""
from __future__ import annotations

import argparse
import os
import sys

from .plan import Drift, Invalid, deploy, selected_tree


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy release fleet resources and consumer agent extensions.\n"
                    "Use in an enrolled Git checkout; never targets the installation or home implicitly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  .venv/bin/python -I -m lu_machinery.agent_deploy --project-root /work/project
  .venv/bin/python -I -m lu_machinery.agent_deploy --project-root /work/project --check
Outputs: enabled provider files and .deploy-state/agent-deploy.v1.json; check writes nothing.
Exit codes: 0 verified/applied; 1 drift or application failure; 2 invalid/unsafe input.
Related: agents_extensions/deployment.v1.json; machinery #100.""",
    )
    parser.add_argument("--project-root", default=None, help="Absolute consumer Git root, e.g. /work/project; default: LU_PRODUCT_ROOT, otherwise refuse")
    parser.add_argument("--check", action="store_true", help="Verify recomposed outputs without writing anything; default: apply")
    args = parser.parse_args()
    try:
        selected = args.project_root if args.project_root is not None else os.environ.get("LU_PRODUCT_ROOT")
        with selected_tree(selected, args.check) as tree:
            return deploy(tree, check=args.check)
    except Invalid as exc:
        print(f"invalid deployment: {exc}", file=sys.stderr)
        return 2
    except Drift as exc:
        print(f"deployment drift: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"invalid or unsafe deployment input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
