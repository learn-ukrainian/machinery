"""Release/product composition and descriptor-bound deployment.

The descriptor has exactly three fields: providers (names), sources (a mapping
from shared/provider names to {path, required}), and preserve (relative target
subtrees). Product enrollment, including legacy migration, is separate work.
The Python >=3.12 runtime floor guarantees importlib.resources availability.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import importlib.resources as resources  # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2
import json
import os
import stat
import subprocess
import tomllib
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from lu_machinery.common.safe_open import safe_open_below
from lu_machinery.lint.lint_agent_skills import _parse_frontmatter, metadata_errors

DESCRIPTOR = "agents_extensions/deployment.v1.json"
STATE = ".deploy-state/agent-deploy.v1.json"
STAGING = ".deploy-state/agent-deploy.tmp"
TARGETS = {"claude": ".claude", "codex": ".codex", "agy": ".agent", "gemini": ".gemini", "cursor": ".cursor"}



class Invalid(ValueError):
    """Invalid or unsafe input, exit 2."""


class Drift(ValueError):
    """Deployment conflict or failed verification, exit 1."""


def relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Invalid("expected a nonempty relative POSIX path")
    if any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in value):
        raise Invalid("control/format characters in path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in value.split("/")):
        raise Invalid("absolute, traversal or noncanonical path")
    return value


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def decode(blob: bytes) -> Any:
    def members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Invalid("duplicate JSON member")
            result[key] = value
        return result
    try:
        return json.loads(blob, object_pairs_hook=members)
    except (ValueError, UnicodeError) as exc:
        raise Invalid("invalid JSON") from exc


class Tree:
    """All filesystem operations remain below a held, no-follow root fd."""

    def __init__(self, fd: int):
        self.fd = fd

    @contextlib.contextmanager
    def directory(self, path: str, *, create: bool = False) -> Iterator[int]:
        fd = os.dup(self.fd)
        try:
            for part in relative(path).split("/"):
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def parent(self, path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = relative(path).rsplit("/", 1)
        if len(parts) == 1:
            yield self.fd, parts[0]
        else:
            with self.directory(parts[0], create=create) as fd:
                yield fd, parts[1]

    def read(self, path: str) -> tuple[bytes, int] | None:
        try:
            with self.parent(path) as (parent, name):
                fd = safe_open_below(parent, name, os.O_RDONLY)
                with os.fdopen(fd, "rb") as file:
                    mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode)
                    return file.read(), mode
        except FileNotFoundError:
            return None

    def inventory(self, path: str, *, exclude: Callable[[str], bool] | None = None) -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        def visit(fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(fd)):
                relative(name)
                key = prefix + name
                if exclude is not None and exclude(path + "/" + key):
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, key + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    child = safe_open_below(fd, name, os.O_RDONLY)
                    with os.fdopen(child, "rb") as file:
                        found[key] = (file.read(), stat.S_IMODE(os.fstat(file.fileno()).st_mode))
                else:
                    raise Invalid("symlink or special file in deployment tree")
        try:
            with self.directory(path) as fd:
                visit(fd, "")
        except FileNotFoundError:
            pass
        return found

    def replace(self, path: str, blob: bytes, mode: int) -> None:
        # The per-target lock serializes all writes, so one reserved staging
        # slot suffices. SIGKILL can leave it behind without polluting a provider
        # tree. Recovery validates its bytes against the reconstructed plan.
        with self.parent(path, create=True) as (fd, name), self.parent(STAGING, create=True) as (stage_fd, temp):
            handle = safe_open_below(stage_fd, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(blob)
                    os.fchmod(file.fileno(), mode)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, name, src_dir_fd=stage_fd, dst_dir_fd=fd)
                os.fsync(fd)
                os.fsync(stage_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=stage_fd)

    def unlink(self, path: str) -> None:
        with self.parent(path) as (fd, name):
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)

    def rmdir(self, path: str) -> None:
        try:
            with self.parent(path) as (fd, name):
                os.rmdir(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise


@contextlib.contextmanager
def selected_tree(selected: str | None, check: bool) -> Iterator[Tree]:
    if selected is None:
        raise Invalid("--project-root or LU_PRODUCT_ROOT is required")
    if selected != "/":
        relative(selected.removeprefix("/"))
    path = Path(selected)
    if not path.is_absolute():
        raise Invalid("project root must be absolute")
    relative(str(path)[1:])
    # Open each component: resolve() would silently accept symlink targets.
    base = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
            os.close(base)
            base = child
        tree = Tree(base)
        result = subprocess.run(
            ["git", "-C", f"/proc/self/fd/{base}", "rev-parse", "--show-toplevel"],
            pass_fds=(base,), capture_output=True, text=True, check=False,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
        if result.returncode or result.stdout.strip() != str(path):
            raise Invalid("project root must be the Git checkout root")
        # flock on the checkout inode needs no lockfile and writes nothing in check mode.
        fcntl.flock(base, (fcntl.LOCK_SH if check else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        yield tree
    except BlockingIOError as exc:
        raise Drift("another deployment holds this target's lock") from exc
    finally:
        os.close(base)


@dataclass(frozen=True)
class Output:
    blob: bytes
    mode: int
    owner: str
    source: str

    def record(self) -> dict[str, object]:
        return {"type": "file", "sha256": digest(self.blob), "mode": self.mode,
                "owner": self.owner, "source": self.source}


@dataclass
class Plan:
    files: dict[str, Output]
    preserve: list[str]
    roots: set[str]
    release: str
    configuration: str

    def manifest(self) -> bytes:
        directories = sorted({str(parent) for path in self.files for parent in PurePosixPath(path).parents if str(parent) != "."})
        return encoded({"version": 1, "release": self.release, "configuration": self.configuration,
                        "files": {p: out.record() for p, out in sorted(self.files.items())},
                        "directories": directories})


def agy_parts(core: bytes) -> dict[str, bytes]:
    text = core.decode().rstrip() + "\n"
    bodies: list[str] = []
    current = ""
    # Reserve the same two-digit header budget as the previous renderer.
    header = "---\ntrigger: always_on\ndescription: Binding rules core, part 99 of 99.\n---\n"
    limit = 24000 - len(header.encode())
    for line in text.splitlines(keepends=True):
        if len(line.encode()) > limit:
            raise Invalid("AGY core line exceeds rule limit")
        if len((current + line).encode()) > limit:
            bodies.append(current)
            current = ""
        current += line
    if current:
        bodies.append(current)
    if not bodies or len(bodies) > 99:
        raise Invalid("AGY core cannot be split")
    return {f"rules/core-part-{i:02d}.md":
            (f"---\ntrigger: always_on\ndescription: Binding rules core, part {i} of {len(bodies)}.\n---\n" + body).encode()
            for i, body in enumerate(bodies, 1)}


def release_inputs() -> tuple[dict[str, Output], dict[str, Any], str]:
    raw = resources.files("lu_machinery.agent_deploy").joinpath("release.v1.json").read_bytes()
    inventory = decode(raw)
    # Anchor the sibling data tree to this package, never to the mergeable
    # agents_extensions namespace. No cwd, PYTHONPATH or checkout lookup.
    root = resources.files("lu_machinery").joinpath("..", "agents_extensions", "shared")
    result = {}
    for path, record in inventory["files"].items():
        relative(path)
        blob = root.joinpath(path).read_bytes()
        if record["owner"] != "fleet" or record["mode"] not in {0o644, 0o755}:
            raise Invalid("invalid release ownership or mode")
        result[path] = Output(blob, record["mode"], "fleet", "release:" + path)
    if "rules/core.md" not in result:
        raise Invalid("release inventory has no core")
    # Bind release identity to both routing metadata and the packaged bytes.
    # Consumer ownership records retain hashes for drift detection.
    identity = digest(encoded({"inventory": digest(raw),
                               "files": {path: out.record() for path, out in result.items()}}))
    return result, inventory, identity


def build(tree: Tree) -> Plan:
    entry = tree.read(DESCRIPTOR)
    if entry is None:
        raise Invalid("missing " + DESCRIPTOR)
    descriptor = decode(entry[0])
    if not isinstance(descriptor, dict) or set(descriptor) != {"providers", "sources", "preserve"}:
        raise Invalid("descriptor requires only providers, sources and preserve")
    providers, sources, preserve = (descriptor[key] for key in ("providers", "sources", "preserve"))
    if not isinstance(providers, list) or not providers or any(p not in TARGETS for p in providers) or len(set(providers)) != len(providers):
        raise Invalid("invalid enabled providers")
    if not isinstance(sources, dict) or set(sources) - {"shared", *providers}:
        raise Invalid("invalid source groups")
    roots = {TARGETS[p] for p in providers}
    if "codex" in providers or "agy" in providers:
        roots.add(".agents")
    if not isinstance(preserve, list):
        raise Invalid("preserve must be a list")
    for path in preserve:
        relative(path)
        if path.split("/")[0] not in roots or "/" not in path:
            raise Invalid("preserve must name a subtree within an enabled target")
        if path == ".codex/skills" or path.startswith(".codex/skills/"):
            raise Invalid("legacy Codex discovery cannot be preserved")
    shared, inventory, release = release_inputs()
    def replaces(provider: str, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in inventory["provider_replacements"].get(provider, []))
    skill_names: set[str] = set()
    for path, out in shared.items():
        if path.startswith("skills/") and path.endswith("/SKILL.md"):
            data, error = _parse_frontmatter(out.blob.decode())
            declared_release_name = data.get("name") if data is not None else None
            if error or not isinstance(declared_release_name, str):
                raise Invalid("invalid release skill metadata: " + path)
            skill_names.add(declared_release_name)
    groups: dict[str, dict[str, Output]] = {"shared": shared}
    used_sources: list[str] = []
    for group, spec in sources.items():
        if not isinstance(spec, dict) or set(spec) != {"path", "required"} or type(spec["required"]) is not bool:
            raise Invalid("source requires path and boolean required")
        source = relative(spec["path"])
        if source.split("/")[0] in {*TARGETS.values(), ".agents", ".deploy-state", ".git"}:
            raise Invalid("product source overlaps deployment output or Git state")
        if any(source == p or source.startswith(p + "/") or p.startswith(source + "/") for p in used_sources):
            raise Invalid("overlapping product sources")
        used_sources.append(source)
        try:
            with tree.directory(source):
                pass
        except FileNotFoundError:
            if spec["required"]:
                raise Invalid("missing required source: " + source) from None
            continue
        items = tree.inventory(source)
        skills = {p.split("/")[1] for p in items if p.startswith("skills/") and len(p.split("/")) > 2}
        for name in skills:
            skill = items.get(f"skills/{name}/SKILL.md")
            if skill is None:
                raise Invalid("missing SKILL.md: " + name)
            metadata, _error = _parse_frontmatter(skill[0].decode())
            declared_name = metadata.get("name") if metadata is not None else None
            if isinstance(declared_name, str) and declared_name in skill_names:
                raise Invalid("duplicate skill name: " + declared_name)
            errors = metadata_errors(skill[0].decode(), name)
            if errors:
                raise Invalid("skill lint: " + "; ".join(errors))
            skill_names.add(name)
        # Product lint selection is code-side. Generic consumers never run curriculum checks.
        if tree.read("curriculum/l2-uk-en/curriculum.yaml") is not None:
            from lu_machinery.lint.lint_prompts import RULES
            for path, (blob, _mode) in items.items():
                if not path.endswith(".md") or not (path.startswith("skills/") or path.startswith("phases/")):
                    continue
                for rule_id, severity, _description, _glob, pattern, exception in RULES:
                    if severity == "error" and any(pattern.search(line) and not (exception and exception.search(line)) for line in blob.decode().splitlines()):
                        raise Invalid("prompt lint: " + rule_id)
        destination = groups.setdefault(group, {})
        for path, (blob, mode) in items.items():
            if path in destination or (path in shared and
                    (shared[path].owner == "fleet" or not replaces(group, path))):
                raise Invalid("duplicate fleet/product logical path: " + path)
            if path.startswith("hooks/") and not mode & 0o111:
                raise Invalid("product hook is not executable: " + path)
            destination[path] = Output(blob, 0o755 if mode & 0o111 else 0o644, "product", source + "/" + path)
    outputs: dict[str, Output] = {}
    def add(target: str, path: str, out: Output) -> None:
        full = target + "/" + relative(path)
        if full == ".codex/skills" or full.startswith(".codex/skills/"):
            raise Invalid("planned legacy Codex discovery path")
        if full in outputs:
            raise Invalid("duplicate output: " + full)
        if any(full == p or full.startswith(p + "/") or p.startswith(full + "/") for p in preserve):
            raise Invalid("preserved runtime path overlaps an output")
        outputs[full] = out
    for provider in providers:
        target = TARGETS[provider]
        for path, out in groups["shared"].items():
            if out.owner == "product" and replaces(provider, path):
                continue
            if out.owner == "fleet":
                destinations = inventory["files"][path]["targets"].get(provider, [])
            elif provider == "claude" and path in inventory["claude_autoload_excludes"]:
                destinations = []
            elif provider == "codex" and path.startswith("skills/"):
                destinations = [".agents/" + path]
            elif provider == "gemini" and not path.startswith(("skills/", "rules/")):
                destinations = []
            else:
                destinations = [target + "/" + path]
                if provider == "agy" and path.startswith("skills/"):
                    destinations.append(".agents/" + path)
            for full in destinations:
                root, relative_path = full.split("/", 1)
                # Codex and AGY intentionally share one discovery tree.
                if full in outputs and outputs[full] == out:
                    continue
                add(root, relative_path, out)
        for path, out in groups.get(provider, {}).items():
            output_root = ".agents" if provider == "agy" or (provider == "codex" and path.startswith("skills/")) else target
            add(output_root, path, out)
    if "agy" in providers:
        for path, blob in agy_parts(shared["rules/core.md"].blob).items():
            add(".agent", path, Output(blob, 0o644, "fleet", "generated:core"))
        add(".agent", "rules.json", Output(encoded({"exclude": ["core.md", "model-assignment.md", "workflow.md"]}), 0o644, "fleet", "generated:agy-excludes"))
    for provider in providers:
        for config, requirements in inventory["required_hooks"].get(provider, {}).items():
            output = outputs.get(config)
            if output is None:
                raise Invalid("missing provider settings: " + config)
            settings = decode(output.blob)
            for requirement in requirements:
                groups_for_event = settings
                for component in requirement["path"]:
                    if not isinstance(groups_for_event, dict):
                        raise Invalid("invalid provider hook settings")
                    groups_for_event = groups_for_event.get(component)
                if not isinstance(groups_for_event, list):
                    raise Invalid("missing mandatory hook event")
                for hook in requirement["hooks"]:
                    if not any(isinstance(group, dict)
                               and {k: v for k, v in group.items() if k != "hooks"} == requirement["group"]
                               and group.get("enabled", True) is True and group.get("disabled", False) is False
                               and hook in group.get("hooks", []) for group in groups_for_event):
                        raise Invalid("missing mandatory hook delegator: " + config)

    # Local project settings can override mandatory hooks even when the file
    # is preserved. Only recognized control files are read; runtime scratch
    # remains excluded from the target inventory below.
    if "claude" in providers:
        for config in (".claude/settings.json", ".claude/settings.local.json"):
            planned = outputs.get(config)
            existing = tree.read(config) if planned is None else None
            control_blob = planned.blob if planned is not None else existing[0] if existing is not None else None
            if control_blob is not None:
                settings = decode(control_blob)
                if not isinstance(settings, dict) or settings.get("disableAllHooks", False) is not False:
                    raise Invalid("mandatory Claude hooks disabled: " + config)
    if "codex" in providers:
        planned = outputs.get(".codex/config.toml")
        existing = tree.read(".codex/config.toml") if planned is None else None
        control_blob = planned.blob if planned is not None else existing[0] if existing is not None else None
        if control_blob is not None:
            features = tomllib.loads(control_blob.decode()).get("features", {})
            if not isinstance(features, dict) or any(key in features and features[key] is not True for key in ("hooks", "codex_hooks")):
                raise Invalid("mandatory Codex hooks disabled: .codex/config.toml")
    for path in outputs:
        if any(str(parent) in outputs for parent in PurePosixPath(path).parents):
            raise Invalid("file/directory collision in planned outputs")
    return Plan(outputs, preserve, roots, release, digest(encoded({"descriptor": descriptor, "sources": {g: {p: o.record() for p, o in items.items() if o.owner == "product"} for g, items in groups.items()}})))


def preserved(path: str, plan: Plan) -> bool:
    return any(path == p or path.startswith(p + "/") for p in plan.preserve)


def old_state(tree: Tree) -> dict[str, Any]:
    read = tree.read(STATE)
    if read is None:
        return {"files": {}, "directories": []}
    data = decode(read[0])
    if not isinstance(data, dict) or set(data) != {"version", "release", "configuration", "files", "directories"} or data["version"] != 1:
        raise Invalid("invalid ownership manifest")
    if not isinstance(data["files"], dict) or not isinstance(data["directories"], list):
        raise Invalid("invalid ownership records")
    for path, record in data["files"].items():
        relative(path)
        if path.split("/")[0] not in {*TARGETS.values(), ".agents"} or "/" not in path:
            raise Invalid("manifest output outside provider directories")
        if not isinstance(record, dict) or set(record) != {"type", "sha256", "mode", "owner", "source"}:
            raise Invalid("invalid file record")
        if record["type"] != "file" or record["owner"] not in {"fleet", "product"} or type(record["mode"]) is not int or record["mode"] not in {0o644, 0o755}:
            raise Invalid("invalid file ownership/mode")
        if not isinstance(record["sha256"], str) or len(record["sha256"]) != 64:
            raise Invalid("invalid file hash")
    ancestors = {str(p) for name in data["files"] for p in PurePosixPath(name).parents if str(p) != "."}
    if any(relative(p) not in ancestors for p in data["directories"]):
        raise Invalid("unowned directory in manifest")
    return data


def matches(actual: tuple[bytes, int] | None, record: dict[str, Any]) -> bool:
    return actual is not None and digest(actual[0]) == record["sha256"] and actual[1] == record["mode"]


def preflight(tree: Tree, plan: Plan, old: dict[str, Any], check: bool) -> list[str]:
    problems: list[str] = []
    actual: tuple[bytes, int] | None
    staged = tree.read(STAGING)
    if staged is not None:
        # A killed buffered write may leave an empty or partial prefix, with
        # either its initial private mode or its final mode. Unrelated bytes,
        # special files and symlinks are never adopted or removed.
        candidates = [(out.blob, out.mode) for out in plan.files.values()]
        candidates.append((plan.manifest(), 0o644))
        if not any(blob.startswith(staged[0]) and staged[1] in {0o600, mode} for blob, mode in candidates):
            raise Invalid("unrecognized deployment staging content")
        if check:
            problems.append("interrupted deployment staging remains")
    roots = plan.roots | {path.split("/")[0] for path in old["files"]}
    for root in sorted(roots):
        # Exclude runtime entries before stat/open/recursion. Non-preserved
        # links and special files still fail closed inside inventory().
        for path, actual in tree.inventory(root, exclude=lambda path: preserved(path, plan)).items():
            full = root + "/" + path
            expected = plan.files.get(full)
            if expected is not None:
                if full not in old["files"] and actual != (expected.blob, expected.mode):
                    raise Invalid("differing unowned output: " + full)
            elif full not in old["files"]:
                problems.append("unknown unowned output: " + full)
    for path, out in sorted(plan.files.items()):
        actual = tree.read(path)
        if check and actual != (out.blob, out.mode):
            problems.append("output drift: " + path)
    for path, record in old["files"].items():
        if path in plan.files:
            continue
        if preserved(path, plan):
            raise Invalid("preserved runtime path was previously owned: " + path)
        actual = tree.read(path)
        if actual is not None and not matches(actual, record):
            problems.append("modified retirement candidate: " + path)
        elif check and actual is not None:
            problems.append("retired output remains: " + path)
    # The discovery directory itself is forbidden, even if empty.
    if ".codex" in roots:
        try:
            with tree.directory(".codex/skills"):
                problems.append("legacy Codex discovery path remains")
        except FileNotFoundError:
            pass
    if check:
        state = tree.read(STATE)
        if state is None or state != (plan.manifest(), 0o644):
            problems.append("ownership manifest drift")
    return problems


def deploy(tree: Tree, *, check: bool) -> int:
    plan = build(tree)
    old = old_state(tree)
    problems = preflight(tree, plan, old, check)
    if problems:
        raise Drift("; ".join(problems))
    if check:
        print(f"verified {len(plan.files)} outputs")
        return 0
    try:
        return apply(tree, plan, old)
    except OSError as exc:
        raise Drift(f"application failed before success publication: {exc}") from exc


def apply(tree: Tree, plan: Plan, old: dict[str, Any]) -> int:
    """Apply a fully admitted plan, then publish its verified ownership record."""
    changed = 0
    if tree.read(STAGING) is not None:
        tree.unlink(STAGING)
        changed += 1
    for path, out in sorted(plan.files.items()):
        actual = tree.read(path)
        if actual != (out.blob, out.mode):
            # Recheck ownership immediately before replacement, using held descriptors.
            if actual is not None and path not in old["files"]:
                raise Invalid("output appeared after preflight: " + path)
            tree.replace(path, out.blob, out.mode)
            changed += 1
    for path, record in sorted(old["files"].items()):
        if path not in plan.files:
            actual = tree.read(path)
            if actual is not None:
                if not matches(actual, record):
                    raise Drift("retirement candidate changed: " + path)
                tree.unlink(path)
                changed += 1
    next_directories = decode(plan.manifest())["directories"]
    for path in sorted(set(old["directories"]) - set(next_directories), key=lambda p: (-p.count("/"), p)):
        tree.rmdir(path)
    # Verify actual bytes/modes before publishing success. No manifest is a source of expected bytes.
    for path, out in plan.files.items():
        if tree.read(path) != (out.blob, out.mode):
            raise Drift("post-write verification failed: " + path)
    remaining = preflight(tree, plan, old, False)
    if remaining:
        raise Drift("; ".join(remaining))
    if tree.read(STATE) != (plan.manifest(), 0o644):
        tree.replace(STATE, plan.manifest(), 0o644)
        changed += 1
    print(f"deployed {len(plan.files)} outputs; {changed} changes")
    return 0
