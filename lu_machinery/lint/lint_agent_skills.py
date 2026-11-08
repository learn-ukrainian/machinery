#!/usr/bin/env python3
"""Validate local agent skill metadata.

The skill loader reads YAML frontmatter before it can show a useful error.
This lint catches malformed metadata at deploy time so broken skills do not
get copied into `.claude/`, `.agent/`, or `.agents/`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from lu_machinery.common.repo_root import product_root

REQUIRED_KEYS = {"name", "description"}


def _parse_frontmatter(text: str) -> tuple[dict[str, object] | None, str | None]:
    if not text.startswith("---\n"):
        return None, "missing opening frontmatter delimiter"
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, "missing closing frontmatter delimiter"
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter: {exc}"
    if not isinstance(data, dict):
        return None, "frontmatter must be a mapping"
    return data, None


def _frontmatter(path: Path) -> tuple[dict[str, object] | None, str | None]:
    """Parse a skill file for the dispatcher as well as the standalone linter."""
    return _parse_frontmatter(path.read_text(encoding="utf-8"))


def metadata_errors(text: str, expected_name: str) -> list[str]:
    """Lint one already-read skill, allowing deployment to hold its source snapshot."""
    data, error = _parse_frontmatter(text)
    if error is not None:
        return [error]
    assert data is not None
    errors = []
    missing = sorted(REQUIRED_KEYS - set(data))
    if missing:
        errors.append(f"missing required key(s): {', '.join(missing)}")
    for key in REQUIRED_KEYS | {"argument-hint", "compatibility", "effort"}:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key} must be a string")
    if data.get("name") != expected_name:
        errors.append(f"name {data.get('name')!r} does not match directory {expected_name!r}")
    return errors


def lint_skills(project_root: Path, skills_dir: Path | None = None) -> list[str]:
    """Lint the selected consumer's skills, independently of curriculum markers."""
    skills_dir = skills_dir or project_root / "agents_extensions/shared/skills"
    if not skills_dir.is_dir():
        return [f"skills directory not found: {skills_dir}"]
    errors = []
    for skill_dir in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"{skill_file}: missing SKILL.md")
        else:
            errors.extend(f"{skill_file}: {error}" for error in metadata_errors(skill_file.read_text(encoding="utf-8"), skill_dir.name))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the selected consumer's skill metadata.\nUse before deployment; does not scan curriculum content.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  .venv/bin/python -m lu_machinery.lint.lint_agent_skills --project-root /work/project
Outputs: diagnostics only; writes nothing.
Exit codes: 0 clean, 1 lint violations, 2 invalid arguments.
Related: lu_machinery.agent_deploy; agents_extensions/shared/skills.""",
    )
    parser.add_argument("--project-root", type=Path, default=None,
                        help="Absolute consumer directory, e.g. /work/project; default: configured product root")
    args = parser.parse_args()
    if args.project_root is not None and not (args.project_root.is_absolute() and args.project_root.is_dir()):
        parser.error("--project-root must be an existing absolute directory")
    project_root = args.project_root if args.project_root is not None else product_root()
    errors = lint_skills(project_root)

    if errors:
        print("Skill metadata lint failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Skill metadata OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
