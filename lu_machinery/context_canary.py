#!/usr/bin/env python3
"""Freeze and score operational session-canary anchors.

The session_canary lanes use ``mint --facts`` to freeze early session facts and
``score`` to compare later answers with their configured similarity and pass
thresholds. Rollover confirmation uses the exact replacement binding and lease.

Exit codes: 0 PASS, 2 FAIL-HANDOFF or usage error, 1 input/IO/JSON error.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import sys
from pathlib import Path


def _norm(value: object) -> str:
    """Whitespace/case-insensitive normalization for fuzzy comparison."""
    return " ".join(str(value).strip().lower().split())


def _similarity(truth: str, got: str) -> float:
    return difflib.SequenceMatcher(None, _norm(truth), _norm(got)).ratio()


def _is_exact_legacy_anchors_only(probe: object) -> bool:
    """Legacy probes are ONLY the exact original shape produced by --facts: {"anchors": [{"id":, "q":, "a":}, ...]}.
    Extra keys are rejected, including the retired rollover probe format.
    """
    if not isinstance(probe, dict):
        return False
    if set(probe.keys()) != {"anchors"}:
        return False
    anchors = probe.get("anchors")
    if not isinstance(anchors, list) or len(anchors) == 0:
        return False
    for a in anchors:
        if not isinstance(a, dict):
            return False
        if set(a.keys()) != {"id", "q", "a"}:
            return False
        for k in ("id", "q", "a"):
            val = a.get(k)
            if not isinstance(val, str) or not val.strip():
                return False
    return True


def _load_facts_spec(spec: str) -> object:
    """Load facts from EITHER an inline JSON list OR a path to a JSON file.

    `--facts` accepts both forms: the documented `--facts facts.json` path AND an
    inline `[{...}]` payload (the arg help advertises "JSON list of {id,q,a}").
    A JSON list starts with '[' (an object with '{'); a filesystem path effectively
    never does — a bare name starting with a bracket would need quoting and is an
    edge case — so the leading bracket selects inline vs file for all practical
    inputs. This keeps the CLI help honest and avoids the opaque 'File name too
    long' OSError a caller hit when passing inline JSON to the file-only reader.
    """
    if spec.lstrip()[:1] in ("[", "{"):
        return json.loads(spec)
    return json.loads(Path(spec).read_text(encoding="utf-8"))


def cmd_mint(args: argparse.Namespace) -> int:
    facts_spec = args.facts
    try:
        facts = _load_facts_spec(facts_spec)
        if not isinstance(facts, list) or not facts:
            print("error: --facts must be a non-empty JSON list of {id,q,a}", file=sys.stderr)
            return 1
        ids = [f.get("id") for f in facts]
        if len(set(ids)) != len(ids) or not all(ids):
            print("error: every fact needs a unique non-empty 'id'", file=sys.stderr)
            return 1
        probe = {"anchors": [{"id": f["id"], "q": f["q"], "a": f["a"]} for f in facts]}
        Path(args.out).write_text(json.dumps(probe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"minted {len(facts)} anchors -> {args.out}")
        return 0
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: --facts cannot be read as a file: {facts_spec} ({type(exc).__name__})", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: --facts is not valid JSON: {exc}", file=sys.stderr)
        return 1


def cmd_score(args: argparse.Namespace) -> int:
    try:
        probe = json.loads(Path(args.probe).read_text(encoding="utf-8"))
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read probe or answers: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: probe or answers not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(probe, dict):
        print("error: probe must be a JSON object", file=sys.stderr)
        return 1
    anchors = probe.get("anchors", [])
    if not isinstance(anchors, list) or not anchors:
        print("error: probe has no anchors", file=sys.stderr)
        return 1

    if not _is_exact_legacy_anchors_only(probe):
        print("error: score requires an anchors-only probe minted with --facts", file=sys.stderr)
        return 2
    correct = 0
    rows = []
    for anchor in anchors:
        aid = anchor.get("id", "")
        truth = anchor.get("a", "")
        got = answers.get(aid, "") if isinstance(answers, dict) else ""
        sim = _similarity(truth, got)
        thresh = getattr(args, "threshold", 0.75)
        ok = sim >= thresh
        correct += 1 if ok else 0
        rows.append((aid, ok, sim, anchor.get("q", ""), truth, got))
    k = len(anchors)
    score_val = correct / k if k > 0 else 0.0
    for aid, ok, sim, q, truth, got in rows:
        tag = "PASS " if ok else "DRIFT"
        print(f"  [{tag}] {aid} (sim={sim:.2f}) {q}")
        if not ok:
            print(f"          truth: {truth!r}")
            print(f"          got  : {got!r}")
    pass_ratio = getattr(args, "pass_ratio", 0.85)
    verdict = "PASS" if score_val >= pass_ratio else "FAIL-HANDOFF"
    print(f"SCORE {correct}/{k} = {score_val:.2f}  ->  {verdict}")
    if args.log:
        log_path = Path(args.log)
        is_new = not log_path.exists()
        with log_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(["context_tokens", "model", "k", "correct", "score", "verdict"])
            writer.writerow(
                [
                    getattr(args, "context_tokens", 0),
                    getattr(args, "model", "unknown"),
                    k,
                    correct,
                    f"{score_val:.3f}",
                    verdict,
                ]
            )
    return 0 if verdict == "PASS" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    mint = sub.add_parser(
        "mint",
        help="Freeze operational session facts into a probe file.",
    )
    mint.add_argument("--facts", required=True, help="Inline JSON list of {id,q,a}, or a path to a JSON file")
    mint.add_argument("--out", required=True, help="Probe output path")
    mint.set_defaults(func=cmd_mint)

    score = sub.add_parser(
        "score",
        help="Compare from-memory answers against the frozen operational probe.",
    )
    score.add_argument("--probe", required=True)
    score.add_argument("--answers", required=True, help="JSON {id: answer}")
    score.add_argument(
        "--threshold",
        type=float,
        default=0.75,
        help="Per-anchor similarity to count as recalled (default 0.75)",
    )
    score.add_argument(
        "--pass-ratio", type=float, default=0.85, help="Fraction of anchors that must pass (default 0.85)"
    )
    score.add_argument("--context-tokens", type=int, default=0, help="Context size at probe time (for the log)")
    score.add_argument("--model", default="unknown")
    score.add_argument("--log", help="CSV to append the (context_tokens, score) data point")
    score.add_argument(
        "--text-match",
        choices=["normalized", "exact"],
        default="normalized",
        help="Answer comparison setting; IDs always match exactly",
    )
    score.set_defaults(func=cmd_score)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # Keep command-line usage errors observable to in-process callers.
        if isinstance(getattr(exc, "code", None), int):
            return exc.code
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
