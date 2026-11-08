"""Load machinery for a hook without putting a parent directory on sys.path.

This is the package-only loader #161 used in audit-primary-checkout-dirt.py.
Hooks call it; they do not keep a second copy. On a machinery tree (a checkout
or the wheel, where this file sits in agents_extensions/shared/hooks) only
lu_machinery is loaded from that tree, by spec, so the tree root never goes on
sys.path and no other name can resolve there. A deployed copy does not match
that layout, so it imports the release installed for its interpreter. The hook
directory is then added for sibling modules such as shell_shlex. Neither step
inserts the consumer's parent directory.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def install_machinery_package() -> None:
    hook_dir = Path(__file__).resolve().parent
    tree = hook_dir.parents[2]
    if hook_dir.as_posix().endswith("/agents_extensions/shared/hooks") and (tree / "lu_machinery" / "__init__.py").is_file():
        spec = importlib.util.spec_from_file_location(
            "lu_machinery",
            tree / "lu_machinery" / "__init__.py",
            submodule_search_locations=[str(tree / "lu_machinery")],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"lu_machinery package has no loader at {tree / 'lu_machinery'}")
        sys.modules["lu_machinery"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["lu_machinery"])
    hook_dir_text = str(hook_dir)
    if hook_dir_text not in sys.path:
        sys.path.insert(0, hook_dir_text)


install_machinery_package()
