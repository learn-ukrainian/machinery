"""Tests for scripts/context_canary.py — the deterministic brain-rot monitor.

The load-bearing property: the SCRIPT decides pass/fail by diffing answers against
the frozen truth. A confident-but-wrong answer must score as DRIFT, and the CSV log
must accumulate the (context_tokens, score) data point for cross-session rot mapping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from lu_machinery import context_canary
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots")

FACTS = [
    {"id": "safe_env_sha", "q": "SHA of the safe_env commit", "a": "5cd67954ab"},
    {"id": "agy_pr", "q": "PR number for agy lane hardening", "a": "2839"},
    {"id": "wiki_reason", "q": "Why wiki lane not flipped", "a": "agy §7 fabrication unverified at pro tier"},
]


def _mint(tmp_path: Path) -> Path:
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(FACTS), encoding="utf-8")
    probe = tmp_path / "probe.json"
    rc = context_canary.main(["mint", "--facts", str(facts), "--out", str(probe)])
    assert rc == 0
    return probe


def test_mint_writes_anchors(tmp_path: Path):
    probe = _mint(tmp_path)
    data = json.loads(probe.read_text(encoding="utf-8"))
    assert [a["id"] for a in data["anchors"]] == ["safe_env_sha", "agy_pr", "wiki_reason"]


def test_perfect_recall_passes_and_logs(tmp_path: Path):
    probe = _mint(tmp_path)
    answers = tmp_path / "ans.json"
    answers.write_text(json.dumps({f["id"]: f["a"] for f in FACTS}), encoding="utf-8")
    log = tmp_path / "canary_log.csv"
    rc = context_canary.main(
        [
            "score",
            "--probe",
            str(probe),
            "--answers",
            str(answers),
            "--context-tokens",
            "500000",
            "--model",
            "claude-opus-4-8",
            "--log",
            str(log),
        ]
    )
    assert rc == 0
    body = log.read_text(encoding="utf-8").splitlines()
    assert body[0].startswith("context_tokens,model,k,correct,score,verdict")
    assert "500000,claude-opus-4-8,3,3,1.000,PASS" in body[1]


def test_confident_but_wrong_answer_fails_handoff(tmp_path: Path):
    probe = _mint(tmp_path)
    answers = tmp_path / "ans.json"
    # Wrong SHA + wrong PR + distorted reason = rot. Confident, but wrong.
    answers.write_text(
        json.dumps({"safe_env_sha": "deadbeef99", "agy_pr": "9999", "wiki_reason": "it was already fine"}),
        encoding="utf-8",
    )
    rc = context_canary.main(["score", "--probe", str(probe), "--answers", str(answers)])
    assert rc == 2  # FAIL-HANDOFF


def test_mint_accepts_inline_json(tmp_path: Path):
    # The arg help advertises "Inline JSON list of {id,q,a}" — passing the list
    # directly (not a file path) must work, not crash with 'File name too long'.
    probe = tmp_path / "probe.json"
    rc = context_canary.main(["mint", "--facts", json.dumps(FACTS), "--out", str(probe)])
    assert rc == 0
    data = json.loads(probe.read_text(encoding="utf-8"))
    assert [a["id"] for a in data["anchors"]] == ["safe_env_sha", "agy_pr", "wiki_reason"]


def test_mint_directory_as_facts_is_clean_usage_error(tmp_path: Path):
    # A path that is a directory raises IsADirectoryError inside read_text; the
    # widened (OSError, UnicodeDecodeError) catch must turn it into a clean rc 1,
    # not a traceback (every file-branch read failure is covered, not just missing).
    a_dir = tmp_path / "facts_dir"
    a_dir.mkdir()
    rc = context_canary.main(["mint", "--facts", str(a_dir), "--out", str(tmp_path / "p.json")])
    assert rc == 1


def test_mint_malformed_inline_json_is_clean_usage_error(tmp_path: Path):
    rc = context_canary.main(["mint", "--facts", "[{bad json", "--out", str(tmp_path / "p.json")])
    assert rc == 1  # inline branch JSONDecodeError caught, returns usage error



# The installed canary mint accepts only --facts and --out (lu_machinery/context_canary.py build_parser). The v2 --snapshot mint is not in this package, so those tests stay in the product repo.
