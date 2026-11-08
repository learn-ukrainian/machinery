"""The sast checker accepts a changed file's scan error only through the diff and the baseline.

A Semgrep scan error in a file the change touches passes only when each of its spans lies on
lines the PR diff leaves unchanged and the baseline records the same (path, error type, span
text), at most as many times as recorded. Everything else fails closed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_CHECKER = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "semgrep_baseline.py"
_META = ["--semgrep-version", "1.183.0", "--rules-commit", "0" * 40, "--configs", "bash/lang"]
_PATH = "lib/scope.sh"
_BASE_TEXT = "".join(
    [
        "#!/bin/bash\n",
        "set -euo pipefail\n",
        "check() {\n",
        "  (( 10#$A > 0 )) || return 1\n",
        "  echo ok\n",
        "  (( 10#$B > 0 )) || return 1\n",
        "}\n",
        "tail() {\n",
        "  echo done\n",
        "}\n",
    ]
)


def _load_checker():
    spec = importlib.util.spec_from_file_location("semgrep_baseline", _CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    return subprocess.run([*command, *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, text: str) -> str:
    target = repo / _PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


def _results(tmp: Path, name: str, spans: list[tuple[int, int, int]], kind: str = "PartialParsing") -> Path:
    """A Semgrep JSON result with one error in _PATH whose spans are (line, column, length) on one line each."""
    rows = [
        {
            "file": _PATH,
            "start": {"line": line, "col": col, "offset": 0},
            "end": {"line": line, "col": col + n, "offset": n},
        }
        for line, col, n in spans
    ]
    error = {"code": 3, "level": "warn", "type": [kind, rows], "message": "x", "path": _PATH, "spans": rows}
    path = tmp / name
    path.write_text(json.dumps({"results": [], "errors": [error], "paths": {"scanned": ["a.py"]}}), encoding="utf-8")
    return path


def _run(argv: list[str]) -> int:
    try:
        return _load_checker().main(argv)
    except SystemExit as exc:
        return int(exc.code)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


def _write_baseline(tmp: Path, repo: Path, spans: list[tuple[int, int, int]]) -> Path:
    rules = tmp / "rules"
    rules.mkdir(exist_ok=True)
    out = tmp / "baseline.json"
    argv = ["write", "--results", str(_results(tmp, "base.json", spans)), "--root", str(repo)]
    assert _run([*argv, "--rules-dir", str(rules), "--out", str(out), *_META]) == 0
    return out


def _check(tmp: Path, repo: Path, base: str, baseline: Path, results: Path) -> int:
    changed = tmp / "changed"
    changed.write_bytes(subprocess.run(
        ["git", "-C", str(repo), "diff", "-z", "--no-renames", "--name-only", base, "HEAD"],
        check=True, capture_output=True,
    ).stdout)  # fmt: skip
    argv = ["check", "--results", str(results), "--root", str(repo), "--rules-dir", str(tmp / "rules")]
    return _run([*argv, "--baseline", str(baseline), "--changed", str(changed), "--base", base, *_META])


def test_recorded_error_in_untouched_lines_of_a_changed_file_passes(tmp_path: Path, repo: Path) -> None:
    """Defect class: a recorded parse error in unchanged code blocks every edit to its file."""
    base = _commit(repo, _BASE_TEXT)
    baseline = _write_baseline(tmp_path, repo, [(4, 6, 3), (6, 6, 3)])
    head_text = "# header\n# more\n" + _BASE_TEXT.replace("echo done", "echo finished")
    _commit(repo, head_text)
    assert _check(tmp_path, repo, base, baseline, _results(tmp_path, "head.json", [(6, 6, 3), (8, 6, 3)])) == 0


def test_recorded_error_text_on_an_added_line_fails(tmp_path: Path, repo: Path, capsys) -> None:
    """Defect class: a content-hash match accepts a new error that reuses recorded span text.

    The change removes one recorded occurrence and adds the same text on a new line, so the
    counts still match the baseline; only the diff mapping catches it.
    """
    base = _commit(repo, _BASE_TEXT)
    baseline = _write_baseline(tmp_path, repo, [(4, 6, 3), (6, 6, 3)])
    head_text = _BASE_TEXT.replace("  (( 10#$B > 0 )) || return 1\n", "").replace(
        "  echo done\n", "  echo done\n  (( 10#$C > 0 )) || return 1\n"
    )
    _commit(repo, head_text)
    assert _check(tmp_path, repo, base, baseline, _results(tmp_path, "head.json", [(4, 6, 3), (9, 6, 3)])) == 1
    assert "lib/scope.sh:9: a span touches an added or modified line" in capsys.readouterr().out


def test_one_occurrence_more_than_recorded_fails(tmp_path: Path, repo: Path, capsys) -> None:
    """Defect class: multiplicity is ignored, so n recorded occurrences excuse n + 1 reported ones."""
    base = _commit(repo, _BASE_TEXT)
    baseline = _write_baseline(tmp_path, repo, [(4, 6, 3)])
    _commit(repo, _BASE_TEXT.replace("echo done", "echo finished"))
    assert _check(tmp_path, repo, base, baseline, _results(tmp_path, "head.json", [(4, 6, 3), (6, 6, 3)])) == 1
    assert "lib/scope.sh:6: a span is not recorded in the baseline, or occurs more often" in capsys.readouterr().out


@pytest.mark.parametrize("broken", ["missing-baseline", "crashed-scan"])
def test_missing_baseline_or_crashed_scan_output_fails(tmp_path: Path, repo: Path, broken: str) -> None:
    """Defect class: a missing baseline or truncated scanner output is read as a clean scan."""
    base = _commit(repo, _BASE_TEXT)
    baseline = _write_baseline(tmp_path, repo, [(4, 6, 3), (6, 6, 3)])
    _commit(repo, _BASE_TEXT.replace("echo done", "echo finished"))
    results = _results(tmp_path, "head.json", [(4, 6, 3), (6, 6, 3)])
    if broken == "missing-baseline":
        baseline.unlink()
    else:
        results.write_text(results.read_text(encoding="utf-8")[:40], encoding="utf-8")
    assert _check(tmp_path, repo, base, baseline, results) == 1
