#!/usr/bin/env python3
"""Diff-aware Semgrep verdict for the machinery CI Gate sast job, and the writer of its baseline.

Usage:
  python -I -B semgrep_baseline.py write --results R.json --root DIR --rules-dir RULES --out BASELINE.json \
      --semgrep-version V --rules-commit SHA --configs "c1 c2 ..."
  python -I -B semgrep_baseline.py check --results R.json --root DIR --rules-dir RULES --baseline BASELINE.json \
      --changed CHANGED.z --base BASE_SHA --semgrep-version V --rules-commit SHA --configs "c1 c2 ..."

R.json is the `semgrep scan --json` output of a scan run from DIR with --config RULES/<config> for each
config. A finding's identity is (rule id relative to RULES, path, SHA-256 of the matched bytes). Line
numbers are not part of it, so edits elsewhere in a file do not churn the baseline; changing the matched
code makes it a new finding. The baseline is a multiset of finding identities plus the scan errors seen
when it was written, each with the SHA-256 of the source text of every span Semgrep reported for it.

A span is located by its 1-based line and column (end column exclusive) in the file under DIR; its byte
length must equal the difference of its reported offsets, or the span is unusable. A scan error
occurrence is one span of one error, identified by (path, error type, SHA-256 of the span text).

write refuses (exit 1) when the scan has a Timeout error, because a timed-out rule hides findings that a
faster run would report as new, or when a scan error has a span that cannot be read.

check fails (exit 1) on any of:
  - a finding whose identity occurs more often than in the baseline;
  - a scan error without a path;
  - a scan error in a path listed in CHANGED.z (NUL-separated paths the change touches,
    `git diff -z --no-renames --name-only BASE HEAD`) unless every one of its spans lies on lines that
    `git diff BASE HEAD` leaves unchanged (no added or modified line, no deleted line inside the span)
    and its occurrence identity is recorded in the baseline. Each recorded occurrence accepts at most
    one reported occurrence, so one occurrence more than the baseline records fails. A Timeout error, an
    error without spans and an error whose spans cannot be read are never accepted in a changed path;
  - a missing, unreadable or malformed results file, baseline or change list; a change list without a
    base commit; a git diff that fails or cannot be parsed; a baseline written for another schema,
    Semgrep version, rules commit or config list; a rule id outside RULES; a result without a usable
    path or byte offsets, or whose file cannot be read.
Scan errors in files the change does not touch are printed as warnings. Output names the rule or error
type, path and line only, never the matched code.

Exit codes: 0 pass (check) or baseline written (write); 1 anything else.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys

SCHEMA = "machinery-semgrep-baseline/v2"
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
DIGEST = re.compile(r"[0-9a-f]{64}")
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")

Span = tuple[int, int, int, int, int]
"""(start line, start column, end line, end column, byte length), lines and columns 1-based."""


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


class Sources:
    """Bytes of scanned files under the scan root, read once each."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.cache: dict[str, bytes | None] = {}

    def get(self, path: str) -> bytes | None:
        if path not in self.cache:
            try:
                with open(os.path.join(self.root, path), "rb") as handle:
                    self.cache[path] = handle.read()
            except OSError:
                self.cache[path] = None
        return self.cache[path]


def identities(results: dict, sources: Sources, rules_dir: str) -> list[tuple[str, str, str, int]]:
    prefix = os.path.abspath(rules_dir).lstrip("/").replace("/", ".") + "."
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
        data = sources.get(path)
        if data is None:
            fail(f"the file of a Semgrep result cannot be read: {path}; failing closed")
        if end > len(data):
            fail(f"a Semgrep result's offsets lie outside its file: {path}; failing closed")
        digest = hashlib.sha256(data[start:end]).hexdigest()
        found.append((check_id[len(prefix):], path, digest, line if isinstance(line, int) else 0))
    return found


def position(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, dict):
        return None
    line, col, offset = value.get("line"), value.get("col"), value.get("offset")
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in (line, col, offset)):
        return None
    if line < 1 or col < 1 or offset < 0:
        return None
    return line, col, offset


def parse_spans(raw: object, path: object) -> list[Span] | None:
    """The error's spans, [] when it reports none, or None when any span is malformed or names another file."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    spans = []
    for item in raw:
        if not isinstance(item, dict) or item.get("file") != path:
            return None
        start, end = position(item.get("start")), position(item.get("end"))
        if start is None or end is None or (end[0], end[1]) < (start[0], start[1]) or end[2] < start[2]:
            return None
        spans.append((start[0], start[1], end[0], end[1], end[2] - start[2]))
    return spans


def span_text(data: bytes, span: Span) -> bytes | None:
    """The span's bytes, or None when it lies outside the file or disagrees with its reported length."""
    start_line, start_col, end_line, end_col, length = span
    lines = data.split(b"\n")
    if end_line > len(lines):
        return None
    first, last = lines[start_line - 1], lines[end_line - 1]
    if start_col - 1 > len(first) or end_col - 1 > len(last):
        return None
    if start_line == end_line:
        text = first[start_col - 1 : end_col - 1]
    else:
        text = b"\n".join([first[start_col - 1 :], *lines[start_line : end_line - 1], last[: end_col - 1]])
    return text if len(text) == length else None


def error_rows(results: dict) -> list[tuple[str, str | None, list[Span] | None]]:
    rows: list[tuple[str, str | None, list[Span] | None]] = []
    for error in results["errors"]:
        if not isinstance(error, dict):
            rows.append(("malformed", None, None))
            continue
        kind = error.get("type")
        kind = kind[0] if isinstance(kind, list) and kind else kind
        raw_path = error.get("path")
        raw_spans = error.get("spans")
        if raw_path is None and isinstance(raw_spans, list) and raw_spans and isinstance(raw_spans[0], dict):
            raw_path = raw_spans[0].get("file")
        path = safe_relative(raw_path)
        spans = parse_spans(raw_spans, raw_path) if path is not None else None
        rows.append((kind if isinstance(kind, str) else "unknown", path, spans))
    return rows


def span_digests(sources: Sources, path: str, spans: list[Span]) -> list[str] | None:
    data = sources.get(path)
    if data is None:
        return None
    digests = []
    for span in spans:
        text = span_text(data, span)
        if text is None:
            return None
        digests.append(hashlib.sha256(text).hexdigest())
    return digests


def diff_hunks(root: str, base: str, path: str) -> list[tuple[int, int, int, int]]:
    """The zero-context hunks of `git diff BASE HEAD` for one path, as (old start, old count, new start, new count)."""
    command = [
        "git", "-C", root, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--text", "--no-color",
        "--unified=0", "--inter-hunk-context=0", base, "HEAD", "--", f":(literal){path}",
    ]  # fmt: skip
    # The command is a fixed git invocation; base is a validated commit id and the path a literal pathspec.
    proc = subprocess.run(command, capture_output=True, check=False)  # nosemgrep: dangerous-subprocess-use-audit
    if proc.returncode != 0 or not proc.stdout.startswith(b"diff --git "):
        fail(f"git diff of a changed file failed or printed no diff: {path}; failing closed")
    hunks = []
    lines = proc.stdout.split(b"\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith(b"Binary files "):
            fail(f"git diff of {path} is binary; failing closed")
        if not line.startswith(b"@@"):
            continue
        match = HUNK.fullmatch(line)
        if match is None:
            fail(f"git diff of {path} has an unparsable hunk header; failing closed")
        old_start, new_start = int(match[1]), int(match[3])
        old_count = 1 if match[2] is None else int(match[2])
        new_count = 1 if match[4] is None else int(match[4])
        # Zero context: the body is exactly old_count removed and new_count added lines.
        removed = added = 0
        while index < len(lines) and lines[index][:1] in (b"-", b"+", b"\\"):
            removed += lines[index][:1] == b"-"
            added += lines[index][:1] == b"+"
            index += 1
        if (removed, added) != (old_count, new_count):
            fail(f"git diff of {path} has a hunk whose body does not match its header; failing closed")
        hunks.append((old_start, old_count, new_start, new_count))
    return hunks


def base_line(hunks: list[tuple[int, int, int, int]], line: int) -> int | None:
    """The base line an unchanged head line came from, or None when the head line was added or modified."""
    delta = 0
    for _old_start, old_count, new_start, new_count in hunks:
        if new_count and new_start <= line < new_start + new_count:
            return None
        if (new_start + new_count - 1 if new_count else new_start) >= line:
            break
        delta += old_count - new_count
    return line + delta


def unchanged(hunks: list[tuple[int, int, int, int]], span: Span) -> bool:
    """Whether every line of the span is an unchanged line and no base line was deleted inside it."""
    mapped = [base_line(hunks, line) for line in range(span[0], span[2] + 1)]
    if any(item is None for item in mapped):
        return False
    return all(later == earlier + 1 for earlier, later in itertools.pairwise(mapped))


def meta(args: argparse.Namespace) -> dict:
    return {
        "schema": SCHEMA,
        "semgrep_version": args.semgrep_version,
        "rules_commit": args.rules_commit,
        "configs": args.configs.split(),
    }


def write(args: argparse.Namespace) -> int:
    results = load_results(args.results)
    sources = Sources(args.root)
    rows = error_rows(results)
    timeouts = [path for kind, path, _spans in rows if kind == "Timeout"]
    if timeouts:
        fail(f"{len(timeouts)} Timeout error(s): a baseline from a timed-out scan is incomplete; not written")
    errors = []
    for kind, path, spans in rows:
        digests: list[str] | None = []
        if path is not None:
            digests = None if spans is None else span_digests(sources, path, spans)
        if digests is None:
            fail(f"a scan error has a span that cannot be read: type={kind} path={path}; not written")
        errors.append({"type": kind, "path": path, "spans": sorted(digests)})
    found = identities(results, sources, args.rules_dir)
    document = meta(args)
    document["findings"] = [
        {"rule": rule, "path": path, "sha256": digest} for rule, path, digest, _line in sorted(found)
    ]
    document["errors_at_baseline"] = sorted(errors, key=lambda row: (row["path"] or "", row["type"], row["spans"]))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
        handle.write("\n")
    spans = sum(len(row["spans"]) for row in errors)
    print(f"baseline written: {len(found)} finding(s), {len(errors)} recorded scan error(s) with {spans} span(s)")
    return 0


def recorded_errors(baseline: dict) -> collections.Counter:
    rows = baseline.get("errors_at_baseline")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict)
        and isinstance(row.get("type"), str)
        and (row.get("path") is None or isinstance(row.get("path"), str))
        and isinstance(row.get("spans"), list)
        and all(isinstance(item, str) and DIGEST.fullmatch(item) for item in row["spans"])
        for row in rows
    ):
        fail("the Semgrep baseline's recorded scan errors are malformed; failing closed")
    return collections.Counter(
        (row["path"], row["type"], digest) for row in rows if row["path"] is not None for digest in row["spans"]
    )


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
    recorded = recorded_errors(baseline)
    try:
        with open(args.changed, "rb") as handle:
            changed = {item.decode("utf-8", "surrogateescape") for item in handle.read().split(b"\0") if item}
    except OSError:
        fail("the change list is missing or unreadable; failing closed")
    base = args.base or ""
    if changed and not COMMIT.fullmatch(base):
        fail("the change list is not empty but no full base commit id was given; failing closed")
    results = load_results(args.results)
    sources = Sources(args.root)
    found = identities(results, sources, args.rules_dir)
    new = []
    for rule, path, digest, line in sorted(found, key=lambda row: (row[1], row[3], row[0])):
        key = (rule, path, digest)
        if allowed[key] > 0:
            allowed[key] -= 1
        else:
            new.append((rule, path, line))
    blocking_errors, accepted_errors, other_errors = [], [], []
    hunks_by_path: dict[str, list[tuple[int, int, int, int]]] = {}
    for kind, path, spans in sorted(error_rows(results), key=lambda row: (row[1] or "", row[0], row[2] or [])):
        if path is None:
            blocking_errors.append((kind, None, 0, "it has no path"))
            continue
        if path not in changed:
            other_errors.append((kind, path))
            continue
        line = spans[0][0] if spans else 0
        if kind == "Timeout":
            blocking_errors.append((kind, path, line, "a timeout is never accepted"))
            continue
        if not spans:
            reason = "its spans are malformed" if spans is None else "it reports no span to map"
            blocking_errors.append((kind, path, line, reason))
            continue
        digests = span_digests(sources, path, spans)
        if digests is None:
            blocking_errors.append((kind, path, line, "a span cannot be read from the file"))
            continue
        if path not in hunks_by_path:
            hunks_by_path[path] = diff_hunks(args.root, base, path)
        hunks = hunks_by_path[path]
        reasons = []
        for span, digest in zip(spans, digests, strict=True):
            key = (path, kind, digest)
            if not unchanged(hunks, span):
                reasons.append((span[0], "a span touches an added or modified line"))
            elif recorded[key] > 0:
                recorded[key] -= 1
            else:
                reasons.append((span[0], "a span is not recorded in the baseline, or occurs more often than recorded"))
        if reasons:
            for span_line, reason in reasons:
                blocking_errors.append((kind, path, span_line, reason))
        else:
            accepted_errors.append((kind, path, len(spans)))
    for rule, path, line in new:
        print(f"::error file={path},line={line}::Semgrep finding not in the baseline: {rule} at {path}:{line}")
    for kind, path, line, reason in blocking_errors:
        where = f"{path}:{line}" if path is not None else "(no path)"
        print(f"::error::Semgrep scan error blocks: type={kind} at {where}: {reason}")
    for kind, path, count in accepted_errors:
        print(
            f"::notice::Semgrep scan error in a changed file accepted: type={kind} path={path}: "
            f"all {count} span(s) are unchanged and recorded in the baseline"
        )
    for kind, path in other_errors:
        print(f"::warning::Semgrep scan error in a file this change does not touch: type={kind} path={path}")
    print(
        f"Semgrep: {len(found)} finding(s), {len(new)} not in the baseline; "
        f"{len(blocking_errors)} blocking scan error span(s); {len(accepted_errors)} accepted and "
        f"{len(other_errors)} non-blocking scan error(s); {len(changed)} changed path(s)."
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
    parser.add_argument("--base", default="")
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
