#!/usr/bin/env python3
"""Diff-aware Semgrep verdict for the machinery CI Gate sast job, and the writer of its baseline.

Usage:
  python -I -B semgrep_baseline.py write --results R.json --root DIR --rules-dir RULES --out BASELINE.json \
      --semgrep-version V --rules-commit SHA --configs "c1 c2 ..."
  python -I -B semgrep_baseline.py check --results R.json --root DIR --rules-dir RULES --baseline BASELINE.json \
      --changed CHANGED.z --semgrep-version V --rules-commit SHA --configs "c1 c2 ..."

R.json is the `semgrep scan --json` output of a scan run from DIR with --config RULES/<config> for each
config. A finding's identity is (rule id relative to RULES, path, SHA-256 of the matched bytes). Line
numbers are not part of it, so edits elsewhere in a file do not churn the baseline; changing the matched
code makes it a new finding. The baseline is a multiset of identities plus the scan errors seen when it was
written (recorded for reference, never used to excuse an error).

write refuses (exit 1) when the scan has a Timeout error, because a timed-out rule hides findings that a
faster run would report as new.

check fails (exit 1) on any of:
  - a finding whose identity occurs more often than in the baseline;
  - a scan error without a path, or with a path listed in CHANGED.z (NUL-separated paths the change
    touches, `git diff -z --no-renames --name-only BASE HEAD`);
  - a missing, unreadable or malformed results file, baseline or change list; a baseline written for
    another schema, Semgrep version, rules commit or config list; a rule id outside RULES; a result
    without a usable path or byte offsets, or whose file cannot be read.
Scan errors in files the change does not touch are printed as warnings. Output names the rule, path and
line only, never the matched code.

Exit codes: 0 pass (check) or baseline written (write); 1 anything else.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys

SCHEMA = "machinery-semgrep-baseline/v1"


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def load_json(path: str, what: str) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        fail(f"{what} is missing, unreadable or not JSON ({type(exc).__name__}); failing closed")
    raise AssertionError("unreachable")


def safe_relative(path: object) -> str | None:
    if not isinstance(path, str) or not path or path.startswith("/") or "\0" in path:
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return path


def load_results(path: str) -> dict:
    data = load_json(path, "the Semgrep results file")
    if not (
        isinstance(data, dict)
        and isinstance(data.get("results"), list)
        and isinstance(data.get("errors"), list)
        and isinstance(data.get("paths"), dict)
        and isinstance(data["paths"].get("scanned"), list)
    ):
        fail("the Semgrep results file is malformed; failing closed")
    return data


def identities(results: dict, root: str, rules_dir: str) -> list[tuple[str, str, str, int]]:
    prefix = os.path.abspath(rules_dir).lstrip("/").replace("/", ".") + "."
    cache: dict[str, bytes] = {}
    found = []
    for result in results["results"]:
        if not isinstance(result, dict):
            fail("a Semgrep result is not an object; failing closed")
        check_id = result.get("check_id")
        path = safe_relative(result.get("path"))
        start = (result.get("start") or {}).get("offset")
        end = (result.get("end") or {}).get("offset")
        line = (result.get("start") or {}).get("line")
        if not (isinstance(check_id, str) and check_id.startswith(prefix) and len(check_id) > len(prefix)):
            fail("a Semgrep result has a rule id outside the pinned rules checkout; failing closed")
        if path is None or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end:
            fail("a Semgrep result has no usable path or byte offsets; failing closed")
        if path not in cache:
            try:
                with open(os.path.join(root, path), "rb") as handle:
                    cache[path] = handle.read()
            except OSError:
                fail(f"the file of a Semgrep result cannot be read: {path}; failing closed")
        data = cache[path]
        if end > len(data):
            fail(f"a Semgrep result's offsets lie outside its file: {path}; failing closed")
        digest = hashlib.sha256(data[start:end]).hexdigest()
        found.append((check_id[len(prefix):], path, digest, line if isinstance(line, int) else 0))
    return found


def error_rows(results: dict) -> list[tuple[str, str | None]]:
    rows = []
    for error in results["errors"]:
        if not isinstance(error, dict):
            rows.append(("malformed", None))
            continue
        kind = error.get("type")
        kind = kind[0] if isinstance(kind, list) and kind else kind
        path = error.get("path")
        if path is None:
            spans = error.get("spans")
            if isinstance(spans, list) and spans and isinstance(spans[0], dict):
                path = spans[0].get("file")
        rows.append((kind if isinstance(kind, str) else "unknown", safe_relative(path)))
    return rows


def meta(args: argparse.Namespace) -> dict:
    return {
        "schema": SCHEMA,
        "semgrep_version": args.semgrep_version,
        "rules_commit": args.rules_commit,
        "configs": args.configs.split(),
    }


def write(args: argparse.Namespace) -> int:
    results = load_results(args.results)
    errors = error_rows(results)
    timeouts = [path for kind, path in errors if kind == "Timeout"]
    if timeouts:
        fail(f"{len(timeouts)} Timeout error(s): a baseline from a timed-out scan is incomplete; not written")
    found = identities(results, args.root, args.rules_dir)
    document = meta(args)
    document["findings"] = [
        {"rule": rule, "path": path, "sha256": digest} for rule, path, digest, _line in sorted(found)
    ]
    document["errors_at_baseline"] = [
        {"type": kind, "path": path} for kind, path in sorted(errors, key=lambda row: (row[1] or "", row[0]))
    ]
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print(f"baseline written: {len(found)} finding(s), {len(errors)} recorded scan error(s)")
    return 0


def check(args: argparse.Namespace) -> int:
    baseline = load_json(args.baseline, "the Semgrep baseline")
    expected = meta(args)
    if not isinstance(baseline, dict) or any(baseline.get(key) != value for key, value in expected.items()):
        fail("the Semgrep baseline is malformed or was written for another schema, version, rules commit or config list")
    rows = baseline.get("findings")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ("rule", "path", "sha256"))
        for row in rows
    ):
        fail("the Semgrep baseline's findings are malformed; failing closed")
    allowed = collections.Counter((row["rule"], row["path"], row["sha256"]) for row in rows)
    try:
        with open(args.changed, "rb") as handle:
            changed = {item.decode("utf-8", "surrogateescape") for item in handle.read().split(b"\0") if item}
    except OSError:
        fail("the change list is missing or unreadable; failing closed")
    results = load_results(args.results)
    found = identities(results, args.root, args.rules_dir)
    new = []
    for rule, path, digest, line in sorted(found, key=lambda row: (row[1], row[3], row[0])):
        key = (rule, path, digest)
        if allowed[key] > 0:
            allowed[key] -= 1
        else:
            new.append((rule, path, line))
    blocking_errors, other_errors = [], []
    for kind, path in error_rows(results):
        (blocking_errors if path is None or path in changed else other_errors).append((kind, path))
    for rule, path, line in new:
        print(f"::error file={path},line={line}::Semgrep finding not in the baseline: {rule} at {path}:{line}")
    for kind, path in blocking_errors:
        where = path if path is not None else "(no path)"
        print(f"::error::Semgrep scan error in a changed file or without a path: type={kind} path={where}")
    for kind, path in other_errors:
        print(f"::warning::Semgrep scan error in a file this change does not touch: type={kind} path={path}")
    print(
        f"Semgrep: {len(found)} finding(s), {len(new)} not in the baseline; "
        f"{len(blocking_errors)} blocking and {len(other_errors)} non-blocking scan error(s); "
        f"{len(changed)} changed path(s)."
    )
    return 1 if new or blocking_errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("write", "check"))
    parser.add_argument("--results", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--rules-dir", required=True)
    parser.add_argument("--semgrep-version", required=True)
    parser.add_argument("--rules-commit", required=True)
    parser.add_argument("--configs", required=True)
    parser.add_argument("--out")
    parser.add_argument("--baseline")
    parser.add_argument("--changed")
    args = parser.parse_args(argv)
    if args.mode == "write":
        if not args.out:
            parser.error("write needs --out")
        return write(args)
    if not args.baseline or not args.changed:
        parser.error("check needs --baseline and --changed")
    return check(args)


if __name__ == "__main__":
    sys.exit(main())
