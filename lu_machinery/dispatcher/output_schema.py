"""Bind dispatcher schemas using the native adapters' checked parent reads.

The canonical packaged review schema is read through ``importlib.resources``.
Caller-supplied schema files stay on the attempt reader.
"""

import json
import os
import re
from json import JSONDecodeError
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from lu_machinery.agent_runtime.adapters._output_schema import load_output_schema
from lu_machinery.agent_runtime.attempt_safe_read import AttemptReadError, safe_read_attempt_file
from lu_machinery.dispatcher.store import digest
from lu_machinery.review.review_contract import is_packaged_review_schema, packaged_review_schema_bytes


def _uses_packaged_schema(options) -> bool:
    if not isinstance(options, dict):
        return False
    path = options.get("output_schema_path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        return False
    return is_packaged_review_schema(Path(path))


def _load_packaged_schema(options: dict) -> dict:
    """Validate the packaged findings schema without the attempt reader.

    The path is only the identity of that resource. The bytes, the digest and
    the schema check are the package resource. A refusal from any other file
    is not a reason to take this path.
    """
    path = options.get("output_schema_path")
    digest_hex = options.get("output_schema_sha256")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("output_schema_path must be a non-empty absolute path")
    if not isinstance(digest_hex, str) or re.fullmatch(r"[0-9a-f]{64}", digest_hex) is None:
        raise ValueError("output_schema_sha256 must be a lowercase SHA-256")
    if not is_packaged_review_schema(Path(path)):
        raise ValueError("output schema is not the packaged review schema")
    try:
        payload = packaged_review_schema_bytes()
    except OSError as exc:
        raise ValueError("output schema is not a readable file") from exc
    if digest(payload) != digest_hex:
        raise ValueError("output schema SHA-256 changed after dispatch validation")
    schema = json.loads(payload)
    if not isinstance(schema, dict):
        raise ValueError("output schema JSON must be an object")
    Draft202012Validator.check_schema(schema)
    return schema


def load(options):
    try:
        if _uses_packaged_schema(options):
            return _load_packaged_schema(options)
        return load_output_schema(options)
    except (AttemptReadError, SchemaError, UnicodeError, JSONDecodeError) as exc:
        raise ValueError("output schema is unsafe or invalid") from exc


def bind(raw_path: str, repo: Path) -> dict[str, str]:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
        if not Path(os.path.abspath(candidate)).is_relative_to(repo.resolve()):
            raise ValueError("relative output schema must be inside the repository")
    candidate = Path(os.path.abspath(candidate))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("output schema is not a readable file") from exc
    if resolved != candidate:
        raise ValueError("output schema path must not traverse symlinks")
    root = candidate.parent
    # Chosen before any read. A hard-linked caller file is not this resource,
    # and its attempt-reader refusal is final.
    if is_packaged_review_schema(candidate):
        try:
            payload = packaged_review_schema_bytes()
        except OSError as exc:
            raise ValueError("output schema is not a readable file") from exc
    else:
        try:
            payload = safe_read_attempt_file(candidate, trusted_root=root)
        except AttemptReadError as exc:
            raise ValueError("output schema is not a readable regular file") from exc
    options = dict(output_schema_path=str(candidate), output_schema_sha256=digest(payload), review_write_root=str(root))
    load(options)
    return options
