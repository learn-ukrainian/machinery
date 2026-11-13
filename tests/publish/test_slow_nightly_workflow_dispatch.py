"""Keep slow-nightly dispatch exact and reject unregistered publication inputs."""

import json
import subprocess
from pathlib import Path
import pytest
from lu_machinery.opsec import prepublish as gate
from lu_machinery.publish import github as pub
from tests.common.github_cache_isolation import isolate_github_client_cache
from tests.opsec_fixtures import CATALOG
from tests.helpers.product_tree import configured_roots  # noqa: F401






@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    yield from isolate_github_client_cache(tmp_path, monkeypatch)


@pytest.fixture
def dispatch_calls(tmp_path, synthetic_opsec, monkeypatch):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    calls = []

    def sender(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return calls, sender


def test_missing_registration_or_wrong_dispatch_request(dispatch_calls):
    """An omitted workflow or a rewritten destination/ref breaks canonical dispatch."""
    calls, sender = dispatch_calls
    assert pub.main([
        "workflow-run", "--repo", "unit/public",
        "--workflow", "pytest-slow-nightly.yml", "--ref", "reviewed/slow-nightly",
    ], runner=sender) == 0
    assert calls == [
        [
            "gh", "workflow", "run", "pytest-slow-nightly.yml",
            "--repo", "unit/public", "--ref=reviewed/slow-nightly",
        ],
    ]


def test_unknown_workflow_cannot_reach_dispatch(dispatch_calls, capsys):
    """A near-match filename must not bypass the exact workflow allowlist."""
    calls, sender = dispatch_calls
    with pytest.raises(SystemExit) as error:
        pub.main([
            "workflow-run", "--repo", "unit/public",
            "--workflow", "pytest-slow-nightly.yaml", "--ref", "main",
        ], runner=sender)
    assert error.value.code == 2
    assert capsys.readouterr().err == "OPSEC: invalid publisher arguments; use --help.\n"
    assert calls == []


def test_unexpected_inputs_and_fields_cannot_reach_dispatch(dispatch_calls, capsys):
    """No-input workflow registration must not admit extra inputs or raw fields."""
    calls, sender = dispatch_calls
    assert pub.main([
        "workflow-run", "--repo", "unit/public",
        "--workflow", "pytest-slow-nightly.yml", "--ref", "main",
        "--inputs", '{"force": true}',
    ], runner=sender) == 2
    assert capsys.readouterr().err == "OPSEC: invalid publisher field inputs.\n"
    with pytest.raises(gate.PublishBlocked, match=r"^OPSEC: unknown publisher field\.$"):
        pub.publish(
            "workflow-run", repo="unit/public", workflow="pytest-slow-nightly.yml",
            ref="main", raw_field="force=true", runner=sender,
        )
    assert calls == []


def test_invalid_ref_cannot_reach_dispatch(dispatch_calls, capsys):
    """A ref containing a newline must fail existing ref validation before transport."""
    calls, sender = dispatch_calls
    assert pub.main([
        "workflow-run", "--repo", "unit/public",
        "--workflow", "pytest-slow-nightly.yml", "--ref", "main\nother",
    ], runner=sender) == 2
    assert capsys.readouterr().err == "OPSEC: invalid publisher field ref.\n"
    assert calls == []


pytestmark = pytest.mark.usefixtures("configured_roots")


def test_ruleset_merge_queue_changes_only_the_named_parameter(dispatch_calls, capsys):
    """A ruleset write that edits another field, or reports success when the read-back differs, would ship.

    The defect is a merge-queue edit that also changes enforcement, another rule, an unnamed
    parameter, or a ruleset field the verb does not know how to preserve. Ported from product #12993.
    """
    publisher = pub
    assert [name for name in publisher.SCHEMAS if "ruleset" in name] == ["ruleset-merge-queue"]
    parameters = {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 2,
        "max_entries_to_merge": 4,
        "merge_method": "SQUASH",
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0,
    }
    document = {
        "id": 20977023,
        "name": "main-merge-queue",
        "target": "branch",
        "source_type": "Repository",
        "source": "unit/public",
        "enforcement": "active",
        "node_id": "NODE",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "_links": {"self": {"href": "https://api.github.com/repos/unit/public/rulesets/20977023"}},
        "current_user_can_bypass": "never",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
        "rules": [{"type": "deletion"}, {"type": "merge_queue", "parameters": parameters}],
    }
    endpoint = "repos/unit/public/rulesets/20977023"

    def serve(document, *, apply):
        calls, sent = [], {}
        state = json.loads(json.dumps(document))

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            assert args[args.index("--method") + 2] == endpoint
            calls.append(method)
            if method == "GET":
                return subprocess.CompletedProcess(args, 0, json.dumps(state), "")
            if method == "PUT":
                sent["body"] = json.loads(Path(args[args.index("--input") + 1]).read_text())
                if apply:
                    state.update(sent["body"])
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(method)

        return runner, calls, sent

    runner, calls, sent = serve(document, apply=True)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 0
    report = json.loads(capsys.readouterr().out)
    expected = {**parameters, "min_entries_to_merge_wait_minutes": 10}
    body = sent["body"]
    assert calls == ["GET", "PUT", "GET"]
    assert set(body) == {"name", "target", "enforcement", "bypass_actors", "conditions", "rules"}
    assert body["enforcement"] == "active" and body["conditions"] == document["conditions"]
    assert body["rules"][0] == {"type": "deletion"}
    assert body["rules"][1]["parameters"] == expected
    assert report == {"merge_queue": expected, "repo": "unit/public", "ruleset": 20977023}

    def refused(args, **kwargs):
        raise AssertionError("transport")

    with pytest.raises(publisher.gate.PublishBlocked, match="unknown publisher field"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=10, enforcement="disabled", runner=refused,
        )
    with pytest.raises(publisher.gate.PublishBlocked, match="invalid publisher field min_entries_to_merge_wait_minutes"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=361, runner=refused,
        )
    with pytest.raises(publisher.gate.PublishBlocked, match="explicit merge queue fields"):
        publisher.publish("ruleset-merge-queue", repo="unit/public", ruleset=20977023, runner=refused)
    with pytest.raises(SystemExit) as exc:
        publisher.main(
            ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
             "--min-entries-to-merge-wait-minutes", "10", "--enforcement", "disabled"],
            runner=refused,
        )
    assert exc.value.code == 2

    def before_put(document):
        calls = []

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            calls.append(method)
            if method == "PUT":
                raise AssertionError("put")
            return subprocess.CompletedProcess(args, 0, json.dumps(document), "")

        return runner, calls

    preserved = json.loads(json.dumps(document))
    preserved["future_field"] = True
    runner, calls = before_put(preserved)
    with pytest.raises(publisher.gate.PublishBlocked, match="does not preserve"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=10, runner=runner,
        )
    assert calls == ["GET"]

    missing = json.loads(json.dumps(document))
    del missing["rules"][1]["parameters"]["max_entries_to_build"]
    runner, calls = before_put(missing)
    with pytest.raises(publisher.gate.PublishBlocked, match="not on this ruleset"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            max_entries_to_build=2, runner=runner,
        )
    assert calls == ["GET"]

    duplicate = json.loads(json.dumps(document))
    duplicate["rules"].append({"type": "merge_queue", "parameters": dict(parameters)})
    runner, calls = before_put(duplicate)
    with pytest.raises(publisher.gate.PublishBlocked, match="no single merge_queue rule"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=10, runner=runner,
        )
    assert calls == ["GET"]

    runner, calls, sent = serve(document, apply=False)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 3
    assert calls == ["GET", "PUT", "GET"] and "Do not retry" in capsys.readouterr().err
    assert sent["body"]["rules"][1]["parameters"]["min_entries_to_merge_wait_minutes"] == 10

    def readback(second_get):
        calls = []

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            calls.append(method)
            if method == "GET" and calls.count("GET") == 1:
                return subprocess.CompletedProcess(args, 0, json.dumps(document), "")
            if method == "PUT":
                return subprocess.CompletedProcess(args, 0, "", "")
            if method == "GET":
                return second_get(args)
            raise AssertionError(method)

        return runner, calls

    def rate_limited(args):
        return subprocess.CompletedProcess(
            args, 75, json.dumps({"error": "github_rate_limited", "reset_at": 1}), "",
        )

    runner, calls = readback(rate_limited)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 3
    assert calls == ["GET", "PUT", "GET"] and "Do not retry" in capsys.readouterr().err

    def broken(args):
        raise RuntimeError("read-back transport")

    runner, calls = readback(broken)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 3
    assert calls == ["GET", "PUT", "GET"] and "Do not retry" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        publisher.main(["ruleset-merge-queue", "--help"])
    assert exc.value.code == 0
    assert "A rate limit on that read-back is exit 3, not 75" in capsys.readouterr().out
