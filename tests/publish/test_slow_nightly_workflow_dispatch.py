"""Keep slow-nightly dispatch exact and reject unregistered publication inputs."""

import json
import subprocess
from pathlib import Path
import pytest
from lu_machinery.common import github_client
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

    def put_outcome(outcome):
        calls = []

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            calls.append(method)
            if method == "GET":
                return subprocess.CompletedProcess(args, 0, json.dumps(document), "")
            return outcome(args)

        return runner, calls

    def timed_out(args):
        raise subprocess.TimeoutExpired(args, 30)

    def lost(args):
        return subprocess.CompletedProcess(args, 1, "", "connection reset by peer")

    def rejected(args):
        # The shape the client's run() returns for a GitHub 4xx answer (_result_process).
        done = subprocess.CompletedProcess(args, 1, json.dumps({"error": "github_http_error", "status": 422}), "")
        done.github_result = github_client.Result(status=422, error="github_http_error")
        return done

    # A PUT whose answer is lost may already have landed: unverified, never a refusal or a retry.
    for outcome, code, text in ((timed_out, 3, "Do not retry"), (lost, 3, "Do not retry"),
                                (rejected, 1, "refused by GitHub")):
        runner, calls = put_outcome(outcome)
        assert publisher.main(
            ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
             "--min-entries-to-merge-wait-minutes", "10"],
            runner=runner,
        ) == code, outcome.__name__
        assert calls == ["GET", "PUT"] and text in capsys.readouterr().err, outcome.__name__

    with pytest.raises(SystemExit) as exc:
        publisher.main(["ruleset-merge-queue", "--help"])
    assert exc.value.code == 0
    assert "A rate limit on that read-back is exit 3, not 75" in capsys.readouterr().out


@pytest.fixture
def dismissal_transport(monkeypatch, synthetic_opsec):
    """A merged triage plus independent current and baseline SARIF, without an alert mutation."""
    import copy

    for key in ("LEARN_UKRAINIAN_DISPATCH_TASK_ID", pub.BROKER_ENV, "LU_REVIEW_SHELL_TASK", "USER", "LOGNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    before, current = "a" * 40, "b" * 40
    comment = "Alert #1 stores public metadata. https://github.com/unit/public/pull/2"
    instance = {
        "state": "open", "ref": "refs/heads/main", "commit_sha": current,
        "analysis_key": "codeql", "environment": "{}", "category": "python",
        "location": {"path": "sample.py", "start_line": 7}, "message": {"text": "Public metadata"},
    }
    alert = {"number": 1, "state": "open", "rule": {"id": "py/fixture"}, "most_recent_instance": instance}
    result = {
        "ruleId": "py/fixture", "rule": {"id": "py/fixture", "index": 0, "toolComponent": {"index": 0}},
        "message": {"text": "Public metadata"}, "properties": {"github/alertNumber": 1},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "sample.py", "index": 0}, "region": {"startLine": 7},
        }}],
        "codeFlows": [{"threadFlows": [{"locations": [{"location": {"message": {"text": "public source"}}}]}]}],
    }
    sarif = {"runs": [{"results": [result], "tool": {
        "driver": {"name": "CodeQL", "semanticVersion": "1.0.0"},
        "extensions": [{"name": "queries", "rules": [{"id": "py/fixture"}]}],
    }}]}
    root = "repos/unit/public/"
    documents = {
        "pulls/2": {"merged": True, "merge_commit_sha": before, "html_url": "https://github.com/unit/public/pull/2", "base": {"ref": "main"}},
        "pulls/2/files?per_page=100": [[{"filename": pub._DISMISS_TRIAGE_PATH, "status": "modified"}]],
        f"contents/{pub._DISMISS_TRIAGE_PATH}?ref={before}": "| 1 | `py/fixture` | `sample.py:7` | False positive. Public metadata only. |\n",
        "code-scanning/alerts/1": alert,
        "code-scanning/alerts/1/instances?per_page=100": [[instance]],
        "code-scanning/analyses?ref=refs%2Fheads%2Fmain&per_page=100": [[
            {"id": aid, "commit_sha": sha, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": ""}
            for aid, sha in ((3, current), (2, before))
        ]],
        "code-scanning/analyses/2": copy.deepcopy(sarif),
        "code-scanning/analyses/3": copy.deepcopy(sarif),
    }
    writes, calls = [], []
    options = {"reason": "false positive", "readback": {}, "write_error": None, "read_error": None}

    def sender(argv, **kwargs):
        calls.append(list(argv))
        method = argv[argv.index("--method") + 1]
        endpoint = argv[argv.index("--method") + 2]
        assert endpoint.startswith(root)
        if method == "PATCH":
            assert endpoint == root + "code-scanning/alerts/1"
            writes.append(Path(argv[argv.index("--input") + 1]).read_bytes())
            if options["write_error"]:
                raise options["write_error"]
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        assert method == "GET"
        suffix = endpoint.removeprefix(root)
        if suffix == "code-scanning/alerts/1" and writes:
            if options["read_error"]:
                raise options["read_error"]
            document = {**alert, "state": "dismissed", "dismissed_reason": options["reason"], "dismissed_comment": comment, **options["readback"]}
        else:
            document = documents[suffix]
        return subprocess.CompletedProcess(argv, 0, document if isinstance(document, str) else json.dumps(document), "")

    return documents, writes, calls, options, sender, comment


@pytest.mark.parametrize("reason", ["false positive", "won't fix", "used in tests"])
def test_dismissal_fixes_endpoint_state_and_replays_scanned_comment(dismissal_transport, monkeypatch, reason):
    documents, writes, _, options, sender, comment = dismissal_transport
    options["reason"] = reason
    if reason == "won't fix":
        key = next(key for key in documents if key.startswith("contents/"))
        documents[key] = documents[key].replace("False positive.", "Won't fix.")
    elif reason == "used in tests":
        key = next(key for key in documents if key.startswith("contents/"))
        documents[key] = documents[key].replace("sample.py", "tests/sample.py")
        documents["code-scanning/alerts/1/instances?per_page=100"][0][0]["location"]["path"] = "tests/sample.py"
        for aid in (2, 3):
            documents[f"code-scanning/analyses/{aid}"]["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = "tests/sample.py"
    scanned = []
    original = gate.check_texts

    def scan(destination, texts, **kwargs):
        scanned.extend(zip(kwargs["field_names"], texts))
        return original(destination, texts, **kwargs)

    monkeypatch.setattr(gate, "check_texts", scan)
    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason=reason, comment=comment, runner=sender, capture_output=True, text=True)
    assert answer.returncode == 0
    assert len(writes) == 1
    assert json.loads(writes[0]) == {"state": "dismissed", "dismissed_reason": reason, "dismissed_comment": comment}
    assert ("comment", comment) in scanned
    assert json.loads(answer.stdout)["dismissed_comment"] == comment
    assert pub.ENUMS["reason"] == {"completed", "not planned"}


@pytest.mark.parametrize("change", [
    {"number": 0}, {"number": True}, {"number": "1"}, {"reason": "completed"}, {"reason": "mitigated"},
    {"endpoint": "user"}, {"method": "POST"}, {"state": "open"}, {"json": {}}, {"query": "mutation {}"},
    {"comment": ""}, {"comment": "   "}, {"comment": "Alert #1 public data."},
    {"comment": "Alert #1 public data. https://github.com/other/repo/pull/2"},
    {"comment": "Alert #10 public data. https://github.com/unit/public/pull/2"},
    {"comment": "Alert #1 https://github.com/unit/public/pull/2"},
    {"comment": "Alert #1 public data. https://github.com/unit/public/pull/2/other"},
])
def test_dismissal_rejects_open_payload_and_unbound_comments(dismissal_transport, change):
    _, writes, _, _, sender, comment = dismissal_transport
    fields = {"repo": "unit/public", "number": 1, "reason": "false positive", "comment": comment, **change}
    with pytest.raises(gate.PublishBlocked):
        pub.publish("code-scanning-dismiss", runner=sender, **fields)
    assert writes == []


@pytest.mark.parametrize("fault", ["unmerged", "unrelated", "missing-row", "rule", "location", "flow", "analysis", "tool", "branch", "unknown-flow"])
def test_dismissal_refuses_missing_or_changed_reviewed_evidence(dismissal_transport, fault):
    documents, writes, _, _, sender, comment = dismissal_transport
    if fault == "unmerged":
        documents["pulls/2"]["merged"] = False
    elif fault == "unrelated":
        documents["pulls/2/files?per_page=100"][0][0]["filename"] = "other.md"
    elif fault == "missing-row":
        key = next(key for key in documents if key.startswith("contents/"))
        documents[key] = documents[key].replace("| 1 |", "| 2 |")
    elif fault == "rule":
        documents["code-scanning/alerts/1"]["rule"]["id"] = "py/other"
    elif fault == "location":
        documents["code-scanning/alerts/1/instances?per_page=100"][0][0]["location"]["start_line"] = 8
    elif fault == "flow":
        documents["code-scanning/analyses/3"]["runs"][0]["results"][0]["codeFlows"] = []
    elif fault == "analysis":
        documents["code-scanning/analyses?ref=refs%2Fheads%2Fmain&per_page=100"][0] = []
    elif fault == "tool":
        documents["code-scanning/analyses/3"]["runs"][0]["tool"]["driver"]["semanticVersion"] = "2.0.0"
    elif fault == "branch":
        instance = dict(documents["code-scanning/alerts/1/instances?per_page=100"][0][0], ref="refs/heads/other")
        documents["code-scanning/alerts/1/instances?per_page=100"][0].append(instance)
    else:
        documents["code-scanning/analyses/3"]["runs"][0]["results"] = []
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []


@pytest.mark.parametrize("fault", ["state", "reason", "comment", "read", "rate-limit", "write"])
def test_dismissal_ambiguous_transport_or_pending_request_is_unknown_without_retry(dismissal_transport, fault):
    _, writes, _, options, sender, comment = dismissal_transport
    if fault in {"state", "reason", "comment"}:
        key, value = {"state": ("state", "open"), "reason": ("dismissed_reason", "won't fix"), "comment": ("dismissed_comment", "other")}[fault]
        options["readback"][key] = value
    elif fault == "rate-limit":
        options["read_error"] = github_client.GitHubRateLimited(None, "secondary_hold")
    elif fault == "write":
        options["write_error"] = TimeoutError("private transport detail")
    else:
        options["read_error"] = OSError("private transport detail")
    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender, capture_output=True, text=True)
    assert answer.returncode == 3
    assert "UNKNOWN" in answer.stderr and "Do not retry" in answer.stderr
    assert "private transport detail" not in answer.stderr
    assert len(writes) == 1


def test_dismissal_scanner_refusal_precedes_all_transport(dismissal_transport):
    from tests.opsec_fixtures import TOKEN

    _, writes, calls, _, sender, comment = dismissal_transport
    with pytest.raises(gate.PublishBlocked):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment + TOKEN, runner=sender)
    assert writes == [] and calls == []


@pytest.mark.parametrize("key,value", [
    ("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "worker-one"), ("LU_WRITE_BOUNDARY_BROKER", "/fixture/socket"),
    ("LU_REVIEW_SHELL_TASK", "review-one"), ("USER", "review"), ("LOGNAME", "review"),
])
@pytest.mark.parametrize("source", ["process", "api-env"])
def test_dismissal_worker_and_reviewer_cli_and_direct_api_refuse_before_transport(dismissal_transport, monkeypatch, key, value, source):
    _, writes, calls, _, sender, comment = dismissal_transport
    environment = {}
    if source == "process":
        monkeypatch.setenv(key, value)
    else:
        environment[key] = value
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_BOUNDARY_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, env=environment, runner=sender)
    if source == "process":
        assert pub.main(["code-scanning-dismiss", "--repo", "unit/public", "--number", "1", "--reason", "false positive", "--comment", comment], runner=sender) == 2
    assert calls == [] and writes == []
    assert "code-scanning-dismiss" not in pub.BROKER_FORWARDED


def test_dismissal_cli_verifies_the_final_state(dismissal_transport, capsys):
    _, writes, _, _, sender, comment = dismissal_transport
    assert pub.main(["code-scanning-dismiss", "--repo", "unit/public", "--number", "1", "--reason", "false positive", "--comment", comment], runner=sender) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"number": 1, "state": "dismissed", "dismissed_reason": "false positive", "dismissed_comment": comment}
    assert len(writes) == 1


def test_dismissal_refuses_evidence_that_changes_during_preflight(dismissal_transport):
    _, writes, _, _, sender, comment = dismissal_transport
    reads = 0

    def changing(argv, **kwargs):
        nonlocal reads
        result = sender(argv, **kwargs)
        if argv[:5] == ["gh", "api", "--method", "GET", "repos/unit/public/code-scanning/alerts/1"]:
            reads += 1
            if reads == 2:
                result.stdout = json.dumps({**json.loads(result.stdout), "state": "fixed"})
        return result

    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=changing)
    assert reads == 2 and writes == []


@pytest.mark.parametrize("reason,disposition", [("false positive", "Fixed."), ("won't fix", "False positive."), ("used in tests", "False positive.")])
def test_dismissal_reason_must_match_the_reviewed_disposition(dismissal_transport, reason, disposition):
    documents, writes, _, _, sender, comment = dismissal_transport
    key = next(key for key in documents if key.startswith("contents/"))
    documents[key] = documents[key].replace("False positive.", disposition)
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason=reason, comment=comment, runner=sender)
    assert writes == []
