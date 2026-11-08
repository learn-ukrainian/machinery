"""Retired Kimi input refusal and shared content/tree readers."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .deepseek_admission import refuse_deepseek_if_disallowed
from .redacted_env import RedactedEnv

KIMI_AGENT_IDS = frozenset({"kimi", "kimicc"})
# Activity labels retained by public refusal callers.
ACP_MODE = "acp"
REVIEW_MODE = "review"
BRIDGE_MODE = "bridge"

# Shared mechanical-admission content readers use these character checks.
CYRILLIC = re.compile("[Ѐ-ӿԀ-ԯ]")
# Tab, LF, and CR are the only admitted control characters.
_KEPT_CONTROLS = frozenset((0x09, 0x0A, 0x0D))
_GLOB_CHARS = frozenset("*?[")
# Build and dependency output skipped when a directory or glob scope is expanded.
_SCOPE_SKIPPED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache", ".astro", "dist"})
_GIT_TIMEOUT_S = 60
_GIT_REDIRECT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_COMMON_DIR",
    }
)


class KimiAdmissionRefused(ValueError):
    """A retired Kimi seat or model was requested.

    ``read_errors`` holds the exceptions of tree readers that failed: the
    refusal text names only their class, and a caller that keeps a local
    diagnostic record reads their messages here (#9878).
    """

    def __init__(self, message: str = "", *, read_errors: Sequence[BaseException] = ()) -> None:
        super().__init__(message)
        self.read_errors = tuple(read_errors)


def is_kimi_model(model: str | None) -> bool:
    """True for any Kimi model id or alias spelling (``kimi-code/*``, ``kimi-k3*``, ``k3``...)."""
    text = str(model or "").strip().casefold()
    if not text:
        return False
    if text.startswith(("kimi", "acpx-kimi")):
        return True
    try:
        from lu_machinery.review.model_catalog import kimi_historical_labels

        return text in kimi_historical_labels()
    except Exception:  # catalog unavailable: the prefix rule above still applies
        return False


def is_kimi_seat(agent: str | None, *, model: str | None = None) -> bool:
    """True when ``agent`` is a Kimi seat (``kimi``, ``kimicc``, ``kimi-<lane>`` slots, ``acpx-kimi*``) or ``model`` names a Kimi model."""
    name = str(agent or "").strip().casefold()
    return name in KIMI_AGENT_IDS or is_kimi_model(name) or is_kimi_model(model)


def _walk_files(repo_root: Path, base: str) -> Iterable[str]:
    for directory, subdirs, files in os.walk(repo_root / base):
        subdirs[:] = sorted(name for name in subdirs if name not in _SCOPE_SKIPPED_DIRS)
        rel_dir = Path(directory).relative_to(repo_root).as_posix()
        for name in sorted(files):
            yield f"{rel_dir}/{name}" if rel_dir != "." else name


def _literal_base(glob: str) -> str:
    """The directory part of ``glob`` before its first wildcard segment."""
    literal: list[str] = []
    for segment in glob.split("/"):
        if _GLOB_CHARS.intersection(segment):
            break
        literal.append(segment)
    return "/".join(literal)


def scope_files(normalized: str, *, repo_root: Path) -> tuple[bool, list[str]]:
    """Whether ``normalized`` is a directory or glob scope, and the existing files it owns on disk.

    Ownership is read as dispatch reads it: a directory owns its subtree and a
    glob is a case-sensitive ``fnmatch`` pattern whose ``*`` also crosses
    ``/``. A single file owns itself; a path that does not exist yet owns no
    existing file.
    """
    if _GLOB_CHARS.intersection(normalized):
        base = _literal_base(normalized)
        if not base or not (repo_root / base).is_dir():
            return True, []
        return True, [rel for rel in _walk_files(repo_root, base) if fnmatch.fnmatchcase(rel, normalized)]
    target = repo_root / normalized
    if target.is_dir():
        return True, list(_walk_files(repo_root, normalized))
    return False, [normalized] if target.is_file() else []


class ContentTree(Protocol):
    """A tree of files checked by shared mechanical admission."""

    def owned_files(self, normalized: str) -> tuple[bool, dict[str, bytes | None]]:
        """Whether ``normalized`` is a directory or glob scope, and each file it owns with its content.

        Content is None for an entry that holds no file content (a submodule).
        Raises ``OSError`` or ``RuntimeError`` when the tree cannot be read.
        """
        ...


@dataclass(frozen=True)
class DirectoryTree:
    """The files on disk under ``root``: a worktree as the worker sees it."""

    root: Path

    def __str__(self) -> str:
        # A refusal names where it read, never the host path (#9878).
        return "in the working tree"

    def owned_files(self, normalized: str) -> tuple[bool, dict[str, bytes | None]]:
        is_scope, files = scope_files(normalized, repo_root=self.root)
        return is_scope, {rel: (self.root / rel).read_bytes() for rel in files}


@dataclass(frozen=True)
class CommitTree:
    """The files of ``commit`` in the repository at ``repo``, read with git plumbing and no checkout."""

    repo: Path
    commit: str
    env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.env is not None:
            object.__setattr__(self, "env", RedactedEnv(self.env))

    def __str__(self) -> str:
        return f"in commit {self.commit[:12]}"

    def _git(self, *args: str, stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            input=stdin,
            capture_output=True,
            check=False,
            env=None if self.env is None else dict(self.env),
            timeout=_GIT_TIMEOUT_S,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"git {args[0]} {self.commit[:12]} failed: {detail}")
        return proc.stdout

    def _entries(self, base: str) -> dict[str, tuple[str, str]]:
        """``path -> (object type, object id)`` for every entry at or under ``base``."""
        entries: dict[str, tuple[str, str]] = {}
        for record in self._git("ls-tree", "-r", "-z", "--full-tree", self.commit, "--", base).split(b"\0"):
            if record:
                meta, _, path = record.partition(b"\t")
                _mode, kind, oid = meta.decode("ascii").split()
                entries[path.decode("utf-8", "surrogateescape")] = (kind, oid)
        return entries

    def _blobs(self, oids: Iterable[str]) -> dict[str, bytes]:
        wanted = sorted(set(oids))
        if not wanted:
            return {}
        out = self._git("cat-file", "--batch", stdin="".join(f"{oid}\n" for oid in wanted).encode("ascii"))
        blobs: dict[str, bytes] = {}
        pos = 0
        while pos < len(out):
            header_end = out.index(b"\n", pos)
            fields = out[pos:header_end].decode("ascii").split()
            if len(fields) != 3:
                raise RuntimeError(f"git cat-file cannot read {fields[0] if fields else 'an object'}")
            start = header_end + 1
            blobs[fields[0]] = out[start : start + int(fields[2])]
            pos = start + int(fields[2]) + 1
        return blobs

    def owned_files(self, normalized: str) -> tuple[bool, dict[str, bytes | None]]:
        if _GLOB_CHARS.intersection(normalized):
            base = _literal_base(normalized)
            entries = self._entries(base) if base else {}
            entries = {path: entry for path, entry in entries.items() if fnmatch.fnmatchcase(path, normalized)}
            is_scope = True
        else:
            entries = self._entries(normalized)
            is_scope = bool(entries) and normalized not in entries
        blobs = self._blobs(oid for kind, oid in entries.values() if kind == "blob")
        return is_scope, {path: blobs.get(oid) if kind == "blob" else None for path, (kind, oid) in entries.items()}


_CYRILLIC_TEXT = "Cyrillic text"


def _has_disallowed_control(text: str) -> bool:
    """True for a C0 control other than tab, LF, and CR, or for DEL and the C1 controls."""
    for code in map(ord, text):
        if code in _KEPT_CONTROLS:
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return True
    return False


def content_problem(data: bytes | None, *, allow_cyrillic: bool = False) -> str | None:
    """Why ``data`` is not admissible plain text, or None when it is.

    Admissible content is UTF-8 text with no control characters other than
    tab, LF and CR, and, by default, no Cyrillic character. Qualified opaque
    recon may allow Cyrillic. None (no file content) is not admissible either.
    """
    if data is None:
        return "no file content"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    if _has_disallowed_control(text):
        return "control characters"
    if not allow_cyrillic and CYRILLIC.search(text):
        return _CYRILLIC_TEXT
    return None


# Metadata keys that can name the model or the recipient of a request. A stored
# ``data`` payload is merged into a message's metadata, so the gate reads these
# keys from every attachment, not just the explicit arguments.
MODEL_SELECTOR_KEYS = ("to_model", "model", "target_model", "requested_model")
SEAT_SELECTOR_KEYS = (
    "to",
    "to_llm",
    "to_agent",
    "to_agents",
    "agent",
    "agents",
    "target",
    "route",
    "recipient",
    "recipients",
    "participant",
    "participants",
)


def attachment_metadata(data: Any) -> dict[str, Any]:
    """The metadata a ``data`` attachment carries.

    A JSON-object string (or a mapping) is the metadata; any other text is
    stored as ``{"raw": text}``, exactly as ``send_message`` merges it.
    """
    if isinstance(data, Mapping):
        return dict(data)
    if not data or not isinstance(data, str):
        return {}
    if data.startswith("{"):
        try:
            loaded = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return {"raw": data}
        if isinstance(loaded, dict):
            return loaded
    return {"raw": data}


def _selector_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [name for item in value for name in _selector_names(item)]
    return []


def effective_request_targets(
    recipients: Iterable[str | None],
    model: str | None = None,
    *attachments: Any,
) -> tuple[list[str | None], list[str | None]]:
    """The final ``(seats, models)`` a request addresses after its attachments are merged.

    A resolver: called only by ``target_admission.resolve_and_admit``.

    Precedence is the one ``send_message`` stores: an explicit ``model`` replaces
    an attachment's ``to_model``; every other model or recipient key an
    attachment carries (``MODEL_SELECTOR_KEYS``, ``SEAT_SELECTOR_KEYS``) is kept
    as written. Attachments may be JSON strings, mappings or lists of mappings.
    A selector value is read as both a seat and a model, so an alias under any
    key is caught.
    """
    seats: list[str | None] = list(recipients)
    models: list[str | None] = [model]
    for attachment in attachments:
        items = attachment if isinstance(attachment, (list, tuple)) else (attachment,)
        for item in items:
            metadata = attachment_metadata(item)
            for key in MODEL_SELECTOR_KEYS:
                if key == "to_model" and model:
                    continue  # the explicit model overrides the attached one
                models.extend(_selector_names(metadata.get(key)))
            for key in SEAT_SELECTOR_KEYS:
                names = _selector_names(metadata.get(key))
                seats.extend(names)
                models.extend(names)
    return seats, models


def _kimi_seat_name(participants: Iterable[str | None], models: Iterable[str | None]) -> str | None:
    for participant in participants:
        if is_kimi_seat(participant):
            return str(participant).strip()
    for model in models:
        if is_kimi_model(model):
            return str(model).strip()
    return None


def refuse_kimi_if_disallowed(
    effective_participants: Iterable[str | None],
    effective_models: Iterable[str | None] = (),
    *,
    mode: str,
    paths: Iterable[str] = (),
    repo: str | None = None,
    review: bool = False,
    tool_config: Mapping[str, Any] | None = None,
    language_lane: bool = False,
    research_track: str | None = None,
    prompt_file: str | None = None,
    repo_root: Path | None = None,
    trees: Sequence[ContentTree] | Callable[[], Sequence[ContentTree]] = (),
    declared_paths: Iterable[str] | None = None,
) -> None:
    """Reject every retired Kimi target, after the shared DeepSeek admission check."""
    participants, models = tuple(effective_participants), tuple(effective_models)
    refuse_deepseek_if_disallowed(participants, models, repo=repo, prompt_file=prompt_file)
    seat = _kimi_seat_name(participants, models)
    if seat is not None:
        raise KimiAdmissionRefused(format_refusal(seat, ["Kimi models and seats are retired"]))


def _git_env() -> dict[str, str]:
    """The environment without repo-redirecting Git variables, so ``cwd`` names the repository."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_REDIRECT_ENV and not key.startswith("PRE_COMMIT")
    }


def worktree_trees(worktree: Path, *, env: Mapping[str, str] | None = None) -> list[ContentTree]:
    """A worktree content snapshot: its files on disk and the commit checked out there.

    Raises ``RuntimeError`` when the checked-out commit cannot be resolved.
    """
    git_env = _git_env() if env is None else env
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            env=dict(git_env),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot resolve the commit checked out in {worktree} ({exc})") from exc
    head = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not head:
        raise RuntimeError(f"cannot resolve the commit checked out in {worktree}")
    return [DirectoryTree(worktree), CommitTree(worktree, head, env=git_env)]


def refuse_kimi_execution(
    participants: Iterable[str | None],
    models: Iterable[str | None] = (),
    *,
    mode: str,
    cwd: Path | None,
    tool_config: Mapping[str, Any] | None,
) -> None:
    """Reject retired execution targets without resolving ownership or reading trees."""
    refuse_kimi_if_disallowed(participants, models, mode=mode, tool_config=tool_config)


def format_refusal(agent: str, reasons: Iterable[Any]) -> str:
    """Format the retained retired-seat refusal without obsolete admission instructions."""
    joined = "; ".join(str(reason) for reason in reasons)
    return f"ROUTING REFUSED: Kimi is retired; {agent} cannot execute. Refused: {joined}."
