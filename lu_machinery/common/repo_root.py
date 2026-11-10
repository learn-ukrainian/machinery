"""Repository root and project-interpreter resolvers, and the one machinery root configuration.

Execution modes (C-4843, root contract step 1). This module runs either as the
**in-tree source copy** (the package directory that sits inside a product checkout
under the product's own package name) or as **lu_machinery** (the installed package,
or a standalone machinery checkout, whose package directory is named
``INSTALLED_PACKAGE_NAME``). :func:`in_tree_source` tells them apart from this file's
unresolved package directory name alone: no working directory, ancestor, sibling, home
directory or ``site-packages`` lookup takes part, so a decoy file cannot change the mode.

- :func:`machinery_root` and :func:`machinery_path`: the package directory, in both
  modes; never configured, and resource paths stay in the package or its shipped shared resource tree.
- :func:`product_root`: an explicit argument, else ``LU_PRODUCT_ROOT``; with neither,
  lu_machinery refuses, and only the in-tree copy derives the checkout holding it.
- :func:`state_root` and :func:`primary_root`: ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``;
  without it, lu_machinery refuses, and only the in-tree copy derives the primary of its
  own checkout (unchanged).

Refusals raise :class:`ProductRootError` or :class:`StateRootError` (both
:class:`MachineryConfigError`, a ``ValueError``) at the call, never at import, so
package-only imports and ``--help`` work with nothing configured. Resolution only
reads: it creates no directory, file, database or connection. An invalid selection
never falls through to a later candidate.

Machinery roots (#12681). Five roots are kept distinct, and the four that can live
outside this checkout come from one configuration: environment variables read at the
call (a systemd ``EnvironmentFile=``, a launcher or a test can supply them). In the
in-tree source copy an unset or empty variable keeps today's value, so the defaults
are the behaviour before the configuration existed.

========================  ====================================  =================================================
Root                      Variable                              Default (today's behaviour)
========================  ====================================  =================================================
runtime (machinery code)  none: the checkout this file is in    ``Path(__file__).parents[2]``
primary checkout          ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``  ``main_checkout_root`` of the runtime checkout
GitHub slug               ``LEARN_UKRAINIAN_GITHUB_SLUG``        ``learn-ukrainian/learn-ukrainian.github.io``
state root                ``LEARN_UKRAINIAN_STATE_ROOT``         the primary checkout (holds ``batch_state/``)
rules directory (policy)  none: installed release                ``agents_extensions/shared/rules`` beside the package
interpreter               ``LEARN_UKRAINIAN_PYTHON``             :func:`project_interpreter`'s resolution order
========================  ====================================  =================================================

``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT`` and ``LEARN_UKRAINIAN_PYTHON`` are the names the
project-state reporter wrapper already reads; the configuration reuses them so one
setting means one thing everywhere.

Path values must be absolute; a relative value raises :class:`MachineryConfigError`
(a ``ValueError``) rather than resolving against the working directory. The state root
must name the primary checkout: moving state to another store is the operator's state
cutover (machinery move step 9), so any other value is refused, which also keeps
modules that still anchor ``batch_state/`` on the primary from splitting from those
that read the state root.

The runtime root is never configured. It is the code that is running, and a re-exec
from a linked worktree (``scripts/dispatcher/primary.py``) still resolves that code's own
primary from Git metadata, not from this configuration.

Machinery code finds its own files through :func:`machinery_root` (the package directory
this module is in, one level below the runtime root) and :func:`machinery_path` (a
contained, package-relative code or shipped resource path), never by spelling the package's name.

Machinery code finds the product's files (curriculum data, product configuration, product
modules named by product data) through :func:`product_root` (C-4746), never through its
own location: once the machinery is its own repository, the checkout it runs from is not
the product. The selection order is an explicit argument, then ``LU_PRODUCT_ROOT``, then,
in the in-tree source copy only, the checkout that holds this package (from the unresolved
module location) when it is a product checkout. The first candidate that is given is the
only one considered: an invalid one raises :class:`ProductRootError` and never falls
through to the next. Every candidate must be absolute. An explicit or configured product
must be an existing directory and need not be the Ukrainian curriculum (kube-dojo is a
product too): the operation that reads it validates the files it needs. Only the derived
in-tree default must carry the product marker ``PRODUCT_MARKER``, which proves the
layout, not authorization or completeness: loaders keep their own origin checks, and a
reader whose required file is missing fails through its own error. Machinery-owned
resources (shared hooks, guard executables) never come from this root. The installed
package has no implicit product: every service, timer, hook or command that reads
product files names the product checkout with ``LU_PRODUCT_ROOT`` (a systemd
``EnvironmentFile=``, a launcher) or an explicit root; without one it refuses. The Monitor
API selects it at startup (``scripts.api.config.PRODUCT_ROOT``): served from a release
snapshot, that is the snapshot itself, never the live checkout.

Shared runtime state is anchored on ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``, never on the
selected product: kube-dojo's worktrees and the shared dispatch state live in different
places. The installed package refuses state-dependent use without that anchor.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _install_redacted_environ() -> None:
    """Hide env values without importing the scripts package.

    Callers load this module as ``common.repo_root`` with ``scripts/`` on
    ``sys.path``. A package import of ``scripts`` fails on that path.
    A partial checkout that copied this module without ``scripts/lib`` still
    imports: the formatter is optional there.
    """
    name = "lu_machinery.lib.redacted_environ"
    module = sys.modules.get(name)
    if module is None:
        path = Path(__file__).resolve().parents[1] / "lib" / "redacted_environ.py"
        if not path.is_file():
            return
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    module.install_redacted_environ_repr()


_install_redacted_environ()

PRIMARY_ROOT_ENV = "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"
GITHUB_SLUG_ENV = "LEARN_UKRAINIAN_GITHUB_SLUG"
STATE_ROOT_ENV = "LEARN_UKRAINIAN_STATE_ROOT"
INTERPRETER_ENV = "LEARN_UKRAINIAN_PYTHON"
MACHINERY_ENV_VARS = (
    PRIMARY_ROOT_ENV,
    GITHUB_SLUG_ENV,
    STATE_ROOT_ENV,
    INTERPRETER_ENV,
)
DEFAULT_GITHUB_SLUG = "learn-ukrainian/learn-ukrainian.github.io"
RULES_DIR_REL = "agents_extensions/shared/rules"
RUNTIME_ROOT = Path(__file__).resolve().parents[2]
_MACHINERY_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT_ENV = "LU_PRODUCT_ROOT"
# Product Python code and CI census data keep this tree after extraction.
_PRODUCT_TREE = "scripts"
# A file only the product repository has (Sol, C-4746): it proves a checkout's layout.
PRODUCT_MARKER = "curriculum/l2-uk-en/curriculum.yaml"
# The checkout that holds this package, from the unresolved module location (design-v1
# amendment 1): with the package directory symlinked into a product checkout, this names
# the product checkout, not the machinery checkout the link points at.
_PACKAGE_CHECKOUT = Path(os.path.abspath(__file__)).parents[2]
# The import name of the installed (and standalone-checkout) machinery package. A package
# directory with any other name is the in-tree source copy inside a product checkout.
INSTALLED_PACKAGE_NAME = "lu_machinery"
# Decided from the unresolved module location once, at import, without touching the file
# system: a symlinked in-tree package directory keeps the in-tree name.
_IN_TREE_SOURCE = Path(os.path.abspath(__file__)).parents[1].name != INSTALLED_PACKAGE_NAME
# GitHub owner (alphanumerics and inner hyphens) / repository name.
_GITHUB_SLUG_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}")


class MachineryConfigError(ValueError):
    """A machinery root variable is set to a value that cannot be used; the message names it."""


class StateRootError(MachineryConfigError):
    """No shared-state anchor is configured where one is required; the message names the variable."""


def in_tree_source() -> bool:
    """True when this module runs as the in-tree source copy inside a product checkout.

    False for the installed ``lu_machinery`` package and for a standalone machinery
    checkout: there no product and no state anchor is ever derived from the code's own
    location (C-4843).
    """
    return _IN_TREE_SOURCE


def _require_in_tree_default(error: type[MachineryConfigError], what: str, remedy: str) -> None:
    """Refuse the location-derived default of ``what`` outside the in-tree source copy."""
    if not _IN_TREE_SOURCE:
        raise error(f"no {what} is configured, and the {INSTALLED_PACKAGE_NAME} package derives none: {remedy}")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _configured_path(name: str, env: Mapping[str, str] | None) -> Path | None:
    """Absolute path named by ``name``, ``None`` when unset or empty; refuse a relative path."""
    value = _env(env).get(name, "")
    if not value:
        return None
    if not os.path.isabs(value):
        raise MachineryConfigError(f"{name} must be an absolute path, got {value!r}")
    return Path(value)


def configured_primary_root(env: Mapping[str, str] | None = None) -> Path | None:
    """The primary checkout named by the configuration, or ``None`` for the Git-derived default.

    Only a checkout whose ``.git`` is a real directory (not a symlink) holding no
    ``commondir`` file is accepted: that is the one shape whose Git common directory is
    its own ``.git``. Anything else (a gitfile, a ``.git`` symlink such as one to a
    linked worktree's administrative directory, a copied worktree gitdir, or no ``.git``)
    is refused, since anchoring state there would split task records from the locks
    closeout reads (#5171 class).
    """
    configured = _configured_path(PRIMARY_ROOT_ENV, env)
    if configured is not None and not _is_primary_git_layout(configured):
        raise MachineryConfigError(
            f"{PRIMARY_ROOT_ENV} must name a primary checkout, not a linked worktree: "
            f"{configured}/.git is not a real Git directory of its own"
        )
    return configured


def _is_primary_git_layout(checkout: Path) -> bool:
    """``checkout/.git`` is a non-symlink directory with no ``commondir`` file."""
    git_dir = checkout / ".git"
    try:
        if not stat.S_ISDIR(os.lstat(git_dir).st_mode):
            return False
    except OSError:
        return False
    return not os.path.lexists(git_dir / "commondir")


def runtime_primary_root(start: Path | None = None) -> Path:
    """The running code's own primary checkout, from its Git metadata; never configured.

    ``start`` is the caller's own checkout (``Path(__file__).resolve().parents[2]``),
    default this module's. Only the in-tree source copy has runtime Git provenance;
    installed machinery raises StateRootError even with a configured product primary.
    A product checkout cannot identify the code of the installed distribution.
    """
    _require_in_tree_default(
        StateRootError, "runtime source checkout", "installed machinery has no Git source checkout",
    )
    return main_checkout_root(RUNTIME_ROOT if start is None else start)


def primary_root(start: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The configured primary checkout, else the primary that owns ``start`` (default: the runtime).

    The fallback exists only in the in-tree source copy. The installed package requires
    ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT`` and raises :class:`StateRootError` without it,
    whatever ``start`` is: callers pass their own install location there, and a
    ``site-packages`` directory is never a state anchor.
    """
    configured = configured_primary_root(env)
    if configured is not None:
        return configured
    _require_in_tree_default(
        StateRootError,
        "shared-state anchor",
        f"set {PRIMARY_ROOT_ENV} to the primary checkout that holds batch_state/",
    )
    return main_checkout_root(RUNTIME_ROOT if start is None else start)


def configured_github_slug(env: Mapping[str, str] | None = None) -> str | None:
    """The configured ``owner/name`` GitHub slug, or ``None`` when unset or empty."""
    value = _env(env).get(GITHUB_SLUG_ENV, "")
    if not value:
        return None
    if not _GITHUB_SLUG_RE.fullmatch(value) or value.split("/")[1] in (".", ".."):
        raise MachineryConfigError(f"{GITHUB_SLUG_ENV} must be an owner/name GitHub slug, got {value!r}")
    return value


def github_slug(env: Mapping[str, str] | None = None) -> str:
    """The configured ``owner/name`` GitHub slug, else this repository's."""
    return configured_github_slug(env) or DEFAULT_GITHUB_SLUG


def state_root(start: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The checkout whose ``batch_state/`` holds dispatch state: the primary checkout.

    A configured state root must be that same directory (compared after ``realpath``);
    any other value would point state at a new store, which only the operator's state
    cutover may do, so it is refused. The installed package anchors only on a configured
    ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT`` (see :func:`primary_root`): a configured state
    root alone does not stand in for it.
    """
    primary = primary_root(start, env)
    configured = _configured_path(STATE_ROOT_ENV, env)
    if configured is None:
        return primary
    if _realpath(configured) != _realpath(primary):
        raise MachineryConfigError(
            f"{STATE_ROOT_ENV} ({configured}) must be the primary checkout ({primary}); "
            "moving dispatch state to another store is the operator's state cutover"
        )
    return configured


def rules_dir() -> Path:
    """Fleet rules shipped beside this machinery package; never configured."""
    return machinery_path(RULES_DIR_REL)


def configured_interpreter(env: Mapping[str, str] | None = None) -> Path | None:
    """The interpreter named by the configuration, or ``None`` for :func:`project_interpreter`'s order."""
    return _configured_path(INTERPRETER_ENV, env)


@dataclass(frozen=True)
class MachineryRoots:
    """The five distinct roots and the GitHub slug, as one resolved view."""

    runtime: Path
    primary: Path
    github_slug: str
    state: Path
    rules_dir: Path
    interpreter: Path


def machinery_roots(env: Mapping[str, str] | None = None) -> MachineryRoots:
    """Resolve every machinery root from the configuration and its defaults.

    Raises :class:`MachineryConfigError` for an unusable or (installed package) missing
    state anchor, :class:`StateRootError` being the missing case, and ``FileNotFoundError``
    when no interpreter can be resolved.
    """
    return MachineryRoots(
        runtime=RUNTIME_ROOT,
        primary=primary_root(env=env),
        github_slug=github_slug(env),
        state=state_root(env=env),
        rules_dir=rules_dir(),
        interpreter=project_interpreter(env=env),
    )


def machinery_root() -> Path:
    """The machinery package directory: the resolved directory that holds ``common/``.

    It is found from this file's own location, so it names the package wherever the code
    runs from and whatever the package is called. It is never configured, and it is not
    the checkout-level runtime root of :func:`machinery_roots`.
    """
    return _MACHINERY_ROOT


def machinery_path(relative: str) -> Path:
    """The machinery file or directory at package-relative ``relative``, which need not exist for code; shared resources must exist.

    ``relative`` is a trusted, package-relative path such as ``"config/model_catalog.yaml"``.
    The checks are ``path_safety.safe_join``'s, made stricter: an empty, absolute or
    NUL-bearing value, and any empty, ``.``, ``..`` or ``:``/``\\``-bearing component, raise
    ``ValueError``; so does a path whose resolved location leaves :func:`machinery_root`,
    such as one through a symlink that points outside the package, or one that cannot be
    resolved (a symlink loop). Unlike ``safe_join`` there is no release live-data exception:
    a machinery file never resolves outside the package. The returned path is logical
    (symlinks inside the package are not expanded). This module must load without the
    package (hooks load it by file; the AGY store contract projects it without
    ``path_safety.py``), so the checks are inlined rather than imported.
    """
    if not isinstance(relative, str):
        raise TypeError(f"machinery_path takes a str, got {type(relative).__name__}")
    if not relative or "\x00" in relative:
        raise ValueError("machinery path must be a non-empty relative path without NUL")
    if os.path.isabs(relative):
        raise ValueError(f"machinery path must be package-relative, got absolute {relative!r}")
    components = relative.split("/")
    if relative.endswith("/"):
        components.pop()
    for component in components:
        if component in ("", ".", "..") or ":" in component or "\\" in component:
            raise ValueError(f"machinery path has an invalid component {component!r}: {relative!r}")
    root = machinery_root()
    if components[:2] == ["agents_extensions", "shared"]:
        # The wheel preserves this sibling layout. No source/install branch or
        # import search participates, even when product namespaces overlap.
        root = root.parent / "agents_extensions" / "shared"
        if root.resolve() != root:
            raise ValueError("machinery resource tree must not be a symlink")
        components = components[2:]
    logical = root.joinpath(*components)
    try:
        resolved = logical.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"machinery path {relative!r} cannot be resolved: {exc}") from exc
    if os.path.commonpath([root, resolved]) != str(root):
        raise ValueError(f"machinery path {relative!r} resolves outside the machinery package: {resolved}")
    if (relative == "agents_extensions/shared" or relative.startswith("agents_extensions/shared/")) and not logical.exists():
        raise FileNotFoundError(f"machinery release resource unavailable: {relative}")
    return logical


class ProductRootError(MachineryConfigError):
    """No product checkout can be selected; the message names the candidate and the rule it broke."""


def product_tree_path(*parts: str, root: Path | None = None) -> Path:
    """A CI input or Python tree in the selected checkout, never in the wheel.

    Products keep ``scripts``. Machinery's own checkout uses its package name
    when that directory exists there. Missing product inputs keep their product
    spelling and fail at the reader; they never select a packaged copy.
    """
    selected = product_root(root)
    tree = _PRODUCT_TREE
    if not (selected / tree).is_dir() and (selected / _MACHINERY_ROOT.name).is_dir():
        tree = _MACHINERY_ROOT.name
    return selected.joinpath(tree, *parts)


def _carries_product_marker(checkout: Path, *, through_primary: bool) -> bool:
    """``checkout`` has ``PRODUCT_MARKER``; with ``through_primary``, or its primary checkout has it.

    Dispatch worktrees are sparse checkouts without ``curriculum/``. For the derived default only,
    a linked worktree whose primary checkout (found from Git metadata alone, by
    :func:`main_checkout_root`, never from the configured primary root) has the marker qualifies;
    the worktree itself stays the selected root, and nothing is read from the primary. A release
    snapshot has no Git metadata; its marker comes through its declared ``curriculum`` live-data
    link (``scripts/common/release_layout.py``). Qualifying proves the layout, not that every
    product file is present: a reader whose required file is missing fails through its own
    missing-input error.
    """
    # An unreadable candidate, or unreadable, malformed or looping Git metadata, does not qualify:
    # OSError covers permissions, ValueError covers NUL bytes and UnicodeDecodeError, and
    # RuntimeError is what Path.resolve() raises for a symlink loop before Python 3.13.
    try:
        if (checkout / PRODUCT_MARKER).is_file():
            return True
        if not through_primary:
            return False
        primary = main_checkout_root(checkout)
        return primary != checkout and (primary / PRODUCT_MARKER).is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def product_root(explicit: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """The product checkout whose files machinery reads, or raise :class:`ProductRootError`.

    The candidate is ``explicit`` when given, else ``LU_PRODUCT_ROOT`` when set and not
    empty. An explicit or configured candidate must be an absolute path to an existing
    directory; it need not carry ``PRODUCT_MARKER``, since a product other than the
    Ukrainian curriculum is still a product, and the reading operation validates its own
    inputs. With neither given, the installed package refuses: it has no implicit product.
    Only the in-tree source copy then derives the checkout that holds this package from the
    unresolved module location; that derived candidate must carry ``PRODUCT_MARKER``, or be
    a linked worktree of a checkout that carries it (see :func:`_carries_product_marker`).
    Only the one selected candidate is checked: one that fails raises at once instead of
    falling through to a later one. The path is returned as given (symlinks are not
    resolved), so a release snapshot stays the snapshot.

    Call it when a feature runs or an application starts, never at import, and resolve it
    before a cache lookup so the cache is keyed by the selected root.
    """
    derived = False
    if explicit is not None:
        source, value = "the explicit product root", os.fspath(explicit)
    elif _env(env).get(PRODUCT_ROOT_ENV, ""):
        source, value = PRODUCT_ROOT_ENV, _env(env)[PRODUCT_ROOT_ENV]
    else:
        _require_in_tree_default(
            ProductRootError,
            "product",
            f"name the product checkout with {PRODUCT_ROOT_ENV} or an explicit product root",
        )
        source, value, derived = "the checkout holding the machinery package", os.fspath(_PACKAGE_CHECKOUT), True
    if not isinstance(value, str) or "\x00" in value or not os.path.isabs(value):
        raise ProductRootError(f"{source} must be an absolute path, got {value!r}")
    candidate = Path(value)
    if not derived:
        if not os.path.isdir(candidate):
            raise ProductRootError(f"{source} ({candidate}) is not an existing directory")
        return candidate
    if not _carries_product_marker(candidate, through_primary=True):
        raise ProductRootError(
            f"{source} ({candidate}) is not a product checkout: it has no {PRODUCT_MARKER}; "
            f"name the product checkout with {PRODUCT_ROOT_ENV} or an explicit product root"
        )
    return candidate


def main_checkout_root(repo_root: Path) -> Path:
    """Return the primary checkout root that owns the shared ``.git`` dir.

    Only a ``gitdir:`` target with Git's exact ``.git/worktrees/<name>`` shape
    redirects a path. Primary checkouts, release snapshots, and every other
    malformed or non-Git root remain anchored to themselves.
    """
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return repo_root
    if not git_path.is_file():
        return repo_root

    try:
        first_line = git_path.read_text().splitlines()[0]
    except (IndexError, OSError):
        return repo_root
    prefix = "gitdir:"
    if not first_line.startswith(prefix):
        return repo_root

    git_dir = Path(first_line[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    git_dir = git_dir.resolve()
    if git_dir.parent.name != "worktrees":
        return repo_root
    common_git_dir = git_dir.parent.parent
    if common_git_dir.name != ".git":
        return repo_root
    return common_git_dir.parent


def _absolute_text(path: Path) -> str:
    text = os.fspath(path)
    if os.path.isabs(text):
        return text
    return os.path.join(os.getcwd(), text)


def _realpath(path: Path) -> Path:
    """``os.path.realpath`` of ``path``, or a normalized absolute path if that fails."""
    text = _absolute_text(path)
    try:
        resolved = os.path.realpath(text)
    except OSError:
        resolved = os.path.normpath(text)
    return Path(resolved)


def _venv_python(checkout: Path) -> Path:
    """Path of ``checkout``'s project interpreter, whether or not the file exists."""
    # Resolver definition: this join names that checkout's interpreter.
    return checkout / ".venv" / "bin" / "python"


def _executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def project_interpreter(root: Path | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """Return the project interpreter for ``root`` or this checkout.

    Called when a spawn needs the interpreter, not while this module is
    imported. Import therefore succeeds when no project interpreter can be
    found; this function raises ``FileNotFoundError`` at the call.

    A configured ``LEARN_UKRAINIAN_PYTHON`` wins for every checkout. It must be an
    absolute path to an executable file. A relative path raises
    ``MachineryConfigError``. A missing or non-executable file raises
    ``FileNotFoundError``. There is no fallback. The operator who sets it
    vouches for it, as for ``PATH``. Unset, the interpreter is the validated
    primary checkout's ``.venv/bin/python`` when that file is executable. Without an
    explicit checkout, the configured primary supplies that root; installed code
    never searches beside the package for an interpreter. The
    primary is the git common-dir checkout ``main_checkout_root`` finds for a
    linked worktree, and the checkout itself when ``.git`` is a directory.
    A gitfile that is not Git's ``.git/worktrees/<name>`` shape is not a
    primary, so its ``.venv`` is refused. A worktree-local ``.venv`` and
    ``sys.executable`` are not used. Anything else raises
    ``FileNotFoundError``.
    """
    configured = configured_interpreter(env)
    if configured is not None:
        if not _executable_file(configured):
            raise FileNotFoundError(
                f"project interpreter not found: {INTERPRETER_ENV}={configured} is not an executable file"
            )
        return configured
    repo = primary_root(env=env) if root is None else Path(root)
    primary = main_checkout_root(repo)
    # main_checkout_root anchors a malformed gitfile to the directory itself.
    # That directory is a linked worktree, not the primary, so its .venv is refused.
    if (repo / ".git").is_file() and primary.resolve() == repo.resolve():
        refused = repo / ".venv" / "bin" / "python"
        raise FileNotFoundError(
            f"project interpreter not found: {refused} (a linked worktree .venv is not used)"
        )
    executable = _venv_python(primary)
    if _executable_file(executable):
        return executable
    raise FileNotFoundError(f"project interpreter not found: {executable} is not an executable file")


def resolve_repo_root(script_path: Path, parents: int) -> Path:
    """Compatibility spelling for the shared primary/state anchor.

    The in-tree source copy may derive its own checkout from ``script_path``.
    Installed machinery requires the configured primary and never treats the
    supplied script's ancestor as a repository or state root.
    """
    return state_root(script_path.resolve().parents[parents] if in_tree_source() else None)
