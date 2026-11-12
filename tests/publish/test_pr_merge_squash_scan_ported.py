"""pr-merge scans the squash subject and body GitHub will publish (#9339); synthetic data, no network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest
from lu_machinery.opsec import prepublish as gate
from lu_machinery.publish import github as pub
from tests.opsec_fixtures import CATALOG, TOKEN, recorded_approval
from tests.helpers.product_tree import configured_roots  # noqa: F401





HEAD = "a" * 40
_ABSENT = object()
_UNVERIFIABLE_RECORDER = (
    "OPSEC: merge refused: the repository's verdict_recorder setting is unverifiable."
)


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)


def transport(calls, *, subject="clean (#1)", body="* clean", queue=True, head=HEAD, reads=None, queued=None):
    """Answer readiness and squash-text reads; record the queue mutation, and every call in ``reads``.

    ``queued`` is non-empty once an enqueue succeeded; later reads report queue membership.
    """
    queued = [] if queued is None else queued

    def send(args, **kwargs):
        if reads is not None:
            reads.append(args)
        pull = {
            "id": "PR_node", "number": 1, "isDraft": False, "headRefOid": HEAD, "state": "OPEN",
            "baseRefName": "main", "isCrossRepository": False, "headRefName": "codex/fixture",
            "statusCheckRollup": [], "mergeStateStatus": "CLEAN", "labels": [],
            "isInMergeQueue": bool(queued), "isMergeQueueEnabled": queue,
            "viewerMergeHeadlineText": subject, "viewerMergeBodyText": body,
        }
        if args[:3] == ["gh", "pr", "view"]:
            value = pull
        elif args[:3] == ["gh", "pr", "checks"]:
            value = []
        elif args[:4] == ["gh", "api", "--method", "GET"]:
            value = ([[{"filename": "scripts/example.py", "status": "modified"}]] if "/pulls/1/files" in args[4]
                     else [[recorded_approval(HEAD)]] if "/issues/1/comments" in args[4]
                     else {"login": "unit"})
        elif args[:5] == ["gh", "api", "--method", "POST", "graphql"]:
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            if payload["query"].startswith("mutation"):
                calls.append({"argv": args, "payload": payload})
                queued.append(True)
                value = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}
            else:
                value = {"data": {"repository": {"pullRequest": {
                    **pull, "headRefOid": head if "viewerMergeHeadlineText" in payload["query"] else HEAD
                }}}}
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
    return send


@pytest.mark.parametrize(
    "field,defaults", [("subject", {"subject": TOKEN + " (#1)"}), ("body", {"body": "* " + TOKEN})]
)
def test_default_squash_text_hit_is_refused_before_merge(synthetic_opsec, field, defaults):
    calls = []
    with pytest.raises(gate.PublishBlocked) as error:
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls, **defaults))
    assert not calls and TOKEN not in str(error.value)
    assert f"rule=synthetic-rule class=1 field={field} line=1" in str(error.value)


@pytest.mark.parametrize("field", ["subject", "body"])
def test_explicit_squash_text_hit_is_refused_before_merge(synthetic_opsec, field):
    """A blocked supplied field is refused before any GitHub read, not only before the write."""
    calls, reads = [], []
    with pytest.raises(gate.PublishBlocked, match=f"field={field} "):
        pub.publish(
            "pr-merge", repo="unit/public", number=1, runner=transport(calls, reads=reads), **{field: "x " + TOKEN}
        )
    assert not calls
    assert reads == []


def test_clean_defaults_are_sent_explicitly_as_scanned(synthetic_opsec):
    calls = []
    pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls, body="* clean\n* also clean"))
    assert len(calls) == 1
    assert "enqueuePullRequest" in calls[0]["payload"]["query"]
    assert calls[0]["payload"]["variables"] == {"id": "PR_node", "head": HEAD}


def test_merge_queue_defaults_are_scanned_even_with_explicit_text(synthetic_opsec):
    calls = []
    with pytest.raises(gate.PublishBlocked, match="field=default_body "):
        pub.publish(
            "pr-merge",
            repo="unit/public",
            number=1,
            subject="clean",
            body="clean",
            runner=transport(calls, body="* " + TOKEN, queue=True),
        )
    assert not calls






@pytest.mark.parametrize("recorder", [True, False])
def test_head_without_a_recorded_verdict_is_refused_only_where_the_recorder_is_used(
    synthetic_opsec, monkeypatch, recorder
):
    # #12031: zero verdict comments; the registry's per-repository setting decides.
    row = {**CATALOG["public"], "verdict_recorder": recorder}
    monkeypatch.setattr(gate, "catalog", lambda: {**CATALOG, "public": row})
    calls = []
    base = transport(calls)

    def send(args, **kwargs):
        if args[:4] == ["gh", "api", "--method", "GET"] and "/issues/1/comments" in args[4]:
            return subprocess.CompletedProcess(args, 0, "[[]]", "")
        return base(args, **kwargs)

    if recorder:
        with pytest.raises(gate.PublishBlocked, match=f"no cross-family APPROVE from .* is recorded at {HEAD}"):
            pub.publish("pr-merge", repo="unit/public", number=1, runner=send)
        assert not calls
    else:
        pub.publish("pr-merge", repo="unit/public", number=1, runner=send)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param(_ABSENT, id="absent"),
        pytest.param("true", id="string-true"),
        pytest.param("false", id="string-false"),
        pytest.param(None, id="null"),
    ],
)
def test_absent_string_or_null_verdict_recorder_is_refused_before_publication(
    synthetic_opsec, monkeypatch, setting,
):
    """An absent, string (including \"true\") or null recorder is unverifiable, never coerced."""
    row = {key: value for key, value in CATALOG["public"].items() if key != "verdict_recorder"}
    if setting is not _ABSENT:
        row["verdict_recorder"] = setting
    monkeypatch.setattr(gate, "catalog", lambda: {**CATALOG, "public": row})
    calls = []
    with pytest.raises(gate.PublishBlocked) as caught:
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls))
    assert str(caught.value) == _UNVERIFIABLE_RECORDER
    assert not calls


@pytest.mark.parametrize(
    "overrides",
    [{"head": "b" * 40}, {"subject": None}, {"body": 7}, {"queue": "yes"}],
)
def test_unverifiable_squash_text_is_refused(synthetic_opsec, overrides):
    calls = []
    with pytest.raises(gate.PublishBlocked, match="squash text unverifiable"):
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls, **overrides))
    assert not calls


def test_missing_matcher_fails_closed_for_default_text(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "private_tooling", lambda: tmp_path / "absent")
    calls = []
    with pytest.raises(gate.PublishBlocked, match="unavailable"):
        pub.publish("pr-merge", repo="unit/public", number=1, runner=transport(calls))
    assert not calls


def test_override_is_logged_and_single_use(synthetic_opsec, tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "primary_root", lambda cwd=None: tmp_path)
    calls = []
    env = {"LU_OPSEC_OVERRIDE": "synthetic false positive"}
    pub.publish(
        "pr-merge", repo="unit/public", number=1, env=dict(env), runner=transport(calls, subject=TOKEN + " (#1)")
    )
    assert len(calls) == 1
    log = tmp_path / "batch_state/opsec/overrides.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["rule_ids"] == ["synthetic-rule"] and TOKEN not in log.read_text()
    with pytest.raises(gate.PublishBlocked, match="override already consumed"):
        pub.publish(
            "pr-merge", repo="unit/public", number=1, env=dict(env), runner=transport(calls, subject=TOKEN + " (#1)")
        )
    assert len(calls) == 1


def _transfer_into(directory, env=None):
    return pub.publish(
        "issue-transfer", repo="public", number=7, destination="infra-private", receipt_dir=str(directory),
        env=env, runner=lambda *a, **k: pytest.fail("read or write"),
    )


@pytest.mark.parametrize("where", ["product", "product-primary", "symlinked-product-primary", "call-env"])
def test_transfer_receipt_never_lands_in_the_product_checkout_of_an_installed_release(
    synthetic_opsec, tmp_path, monkeypatch, where,
):
    """An installed release's own tree is site-packages; the product checkout still bounds the receipt.

    Defect class: the receipt boundary taken from the package location, from the process
    environment instead of the publication's, or lost for a symlinked product root, so an
    installed release writes a receipt into the product checkout before refusing the transfer.
    """
    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.setattr(gate, "ROOT", site)
    product, linked = tmp_path / "product", tmp_path / "linked"
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    for args in (["init", "-q", "-b", "main", str(product)],
                 ["-C", str(product), "commit", "-q", "--allow-empty", "-m", "init"],
                 ["-C", str(product), "worktree", "add", "-q", "--detach", str(linked)]):
        subprocess.run([*git, *args], check=True, capture_output=True, timeout=30)
    (tmp_path / "link").symlink_to(linked)
    (tmp_path / "elsewhere").mkdir()
    # The product root is the primary, a linked worktree of it, or a symlink to that worktree.
    # In the call-env case the process names another tree and only the publication's env names this one.
    root = {"product": product, "product-primary": linked, "symlinked-product-primary": tmp_path / "link",
            "call-env": tmp_path / "elsewhere"}[where]
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
    env = {"LU_PRODUCT_ROOT": str(product)} if where == "call-env" else None
    target = product / "receipts"
    with pytest.raises(gate.PublishBlocked, match="outside the repository"):
        _transfer_into(target, env)
    assert not target.exists()


def test_transfer_without_a_product_root_is_refused_before_any_receipt(synthetic_opsec, tmp_path, monkeypatch):
    monkeypatch.delenv("LU_PRODUCT_ROOT", raising=False)
    target = tmp_path / "receipts"
    with pytest.raises(gate.PublishBlocked, match="product checkout is unresolved"):
        _transfer_into(target)
    assert not target.exists()


@pytest.mark.parametrize("field", ["subject", "body"])
def test_override_for_a_supplied_field_is_offered_once(synthetic_opsec, tmp_path, monkeypatch, field):
    """Defect class: the early supplied-field scan consuming or auditing the override a second time."""
    monkeypatch.setattr(gate, "primary_root", lambda cwd=None: tmp_path)
    calls = []
    pub.publish(
        "pr-merge", repo="unit/public", number=1, env={"LU_OPSEC_OVERRIDE": "synthetic false positive"},
        runner=transport(calls), **{field: "x " + TOKEN},
    )
    assert len(calls) == 1
    rows = (tmp_path / "batch_state/opsec/overrides.jsonl").read_text().splitlines()
    assert len(rows) == 1 and TOKEN not in rows[0]


def priority_transport(calls, *, labels, timeline):
    """A queue repository with one recorded approval of HEAD; records either enqueue write."""
    queued = []
    base = transport(calls, queue=True, queued=queued)
    approval = recorded_approval(HEAD, "fleet")
    pages = {"/comments": [[approval]], "/files": [[{"filename": "scripts/example.py", "status": "modified"}]], "/timeline": [timeline]}

    def send(args, **kwargs):
        if args[:3] == ["gh", "pr", "view"] and "labels" in args[args.index("--json") + 1]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "id": "PR_node", "headRefOid": HEAD, "isCrossRepository": False, "baseRefName": "main",
                "number": 1, "isDraft": False, "mergeStateStatus": "CLEAN",
                "labels": [{"name": name} for name in labels],
            }), "")
        if args[:4] == ["gh", "api", "--method", "GET"]:
            if args[4].startswith("user"):
                return subprocess.CompletedProcess(args, 0, json.dumps({"login": "fleet"}), "")
            page = next(value for key, value in pages.items() if key + "?" in args[4])
            return subprocess.CompletedProcess(args, 0, json.dumps(page), "")
        if args[:5] == ["gh", "api", "--method", "POST", "graphql"]:
            document = json.loads(Path(args[6]).read_text())
            if "enqueuePullRequest" in document["query"]:
                calls.append({"argv": args, "jump": "jump:true" in document["query"], "variables": document["variables"], "payload": document})
                queued.append(True)
                return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}), "")
        return base(args, **kwargs)

    return send


def labeled(at):
    return {"event": "labeled", "label": {"name": "merge-priority"}, "actor": {"login": "fleet"}, "created_at": at}


def test_labelled_approved_pr_is_enqueued_with_jump(synthetic_opsec):
    calls = []
    result = pub.publish("pr-merge", repo="unit/public", number=1, match_head=HEAD,
                         runner=priority_transport(calls, labels=["merge-priority"],
                                                   timeline=[labeled("2026-10-29T10:10:00Z")]))
    assert len(calls) == 1 and calls[0]["jump"] is True
    assert calls[0]["variables"] == {"id": "PR_node", "head": HEAD}
    assert result.queue_jump is True


def test_unlabelled_pr_is_enqueued_without_jump(synthetic_opsec):
    calls = []
    result = pub.publish("pr-merge", repo="unit/public", number=1, match_head=HEAD,
                         runner=priority_transport(calls, labels=[], timeline=[]))
    assert len(calls) == 1 and "jump:false" in calls[0]["payload"]["query"]
    assert not getattr(result, "queue_jump", False)


def test_label_applied_after_an_earlier_heads_approval_does_not_jump(synthetic_opsec):
    """Labelled after approving an old head; this head's approval came later, so the label predates it."""
    calls = []
    old_head_label = labeled("2026-10-29T10:03:00Z")
    result = pub.publish("pr-merge", repo="unit/public", number=1, match_head=HEAD,
                         runner=priority_transport(calls, labels=["merge-priority"], timeline=[old_head_label]))
    assert len(calls) == 1 and "jump:false" in calls[0]["payload"]["query"]
    assert not getattr(result, "queue_jump", False)


pytestmark = pytest.mark.usefixtures("configured_roots")


def test_non_queue_merge_sends_scanned_squash_text_at_approved_head(synthetic_opsec):
    """Non-queue landing must avoid enqueue-only regression and pin scanned squash text and head."""
    calls, reads = [], []
    base = transport(calls, queue=False, reads=reads)

    def send(args, **kwargs):
        if args[:3] == ["gh", "pr", "merge"]:
            calls.append({"argv": args, "body": Path(args[args.index("--body-file") + 1]).read_text()})
            return subprocess.CompletedProcess(args, 0, "", "")
        assert not calls, "non-queue merge must not probe enqueue confirmation"
        return base(args, **kwargs)

    result = pub.publish("pr-merge", repo="unit/public", number=1, runner=send)
    assert result.returncode == 0
    assert len(calls) == 1
    assert calls[0]["argv"][:6] == ["gh", "pr", "merge", "1", "--repo", "unit/public"]
    assert "--squash" in calls[0]["argv"]
    assert "--match-head-commit=" + HEAD in calls[0]["argv"]
    assert "--subject=clean (#1)" in calls[0]["argv"]
    assert calls[0]["body"] == "* clean"
    assert not {"--auto", "--admin", "--delete-branch"}.intersection(calls[0]["argv"])


@pytest.mark.parametrize("subject", [
    "", " ", "\t\n", "\u200b", "\ufeff", "\u2800", "\nclean",
    # Every line break str.splitlines() honours ends the first line, not only LF.
    "\rclean", "\r\nclean", "\u2028clean", "\u2029clean", "\x85clean", "\x0bclean", "\x0cclean",
])
@pytest.mark.parametrize("source", ["explicit", "default", "queue_default"])
def test_blank_subject_cannot_bypass_default_headline_scan(synthetic_opsec, subject, source):
    """Invisible or blank first lines must not publish an unscanned default headline.

    A merge queue publishes its own default headline, so a visible explicit subject cannot vouch for it.
    """
    calls, reads = [], []
    with pytest.raises(gate.PublishBlocked, match="merge subject must not be blank"):
        pub.publish(
            "pr-merge", repo="unit/public", number=1,
            **({"subject": subject} if source == "explicit" else {"subject": "clean"} if source == "queue_default" else {}),
            runner=transport(calls, subject=TOKEN + " (#1)" if source == "explicit" else subject,
                             queue=source == "queue_default", reads=reads),
        )
    assert calls == []
    if source == "explicit":
        assert reads == []


@pytest.mark.parametrize("queue", [False, True])
@pytest.mark.parametrize(
    "scenario", ["publisher", "empty_stdin", "relative_cwd", "repo_from_cwd", "conflict", "unreadable", "invalid_utf8"]
)
def test_squash_body_file_translation_cannot_fall_through_or_enqueue(
    synthetic_opsec, tmp_path, monkeypatch, queue, scenario,
):
    """Publisher argv must translate to exact REST text, or refuse a queue flip without a write.

    A per-call cwd reaches every request of an explicit client (repository and body alike) and never
    retargets that client; an unreadable body source is a typed, status-400 refusal before any send.
    """
    from lu_machinery.common import github_client as gh

    calls, http_calls = [], []
    base = transport(calls, queue=False)

    def http(method, endpoint, headers, body, timeout):
        payload = json.loads(body) if body else None
        http_calls.append((method, endpoint, payload))
        if endpoint == "graphql":
            assert {payload["variables"][key] for key in ("owner", "name")} == {"unit", "public"}
            value = {"data": {"repository": {"pullRequest": {
                "id": "PR_node", "isMergeQueueEnabled": queue,
            }}}}
        else:
            assert method == "PUT" and endpoint == "repos/unit/public/pulls/1/merge"
            value = {"merged": True}
        return gh.Response(200, {}, json.dumps(value).encode())

    other = tmp_path / "client-cwd"
    if scenario == "repo_from_cwd":
        # The client's own directory names another repository and holds another body.
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        subprocess.run(["git", "-C", str(other), "remote", "add", "origin", "https://github.com/unit/wrong.git"], check=True)
        (other / "body.txt").write_text("body from client cwd")
    client = gh.GitHubClient(
        cache_dir=tmp_path / "github", transport=http, env={}, cwd=other if scenario == "repo_from_cwd" else None
    )

    def send(args, **kwargs):
        if args[:3] == ["gh", "pr", "merge"]:
            return gh.run(args, client=client, **kwargs)
        return base(args, **kwargs)

    # Read fixtures stay at the publisher boundary; final argv traverses the
    # real parser and HTTP translator with no injected command runner.
    monkeypatch.setattr(pub, "_run_transport", send)
    expected_body = "* clean"
    if scenario == "publisher":
        result = pub.publish("pr-merge", repo="unit/public", number=1)
    else:
        directory = tmp_path / "command-cwd"
        directory.mkdir()
        if scenario == "repo_from_cwd":
            subprocess.run(["git", "init", "-q", str(directory)], check=True)
            subprocess.run(
                ["git", "-C", str(directory), "remote", "add", "origin", "https://github.com/unit/public.git"], check=True
            )
        (directory / "body.txt").write_bytes(b"\xff\xfe" if scenario == "invalid_utf8" else b"body from command cwd")
        source = {"empty_stdin": "-", "unreadable": "missing.txt"}.get(scenario, "body.txt")
        expected_body = "" if scenario == "empty_stdin" else "body from command cwd"
        argv = ["gh", "pr", "merge", "1", "--repo", "unit/public", "--squash",
                "--match-head-commit=" + HEAD, "--subject=clean (#1)", "--body-file", source]
        if scenario == "repo_from_cwd":
            argv[4:6] = []
        if scenario == "conflict":
            argv += ["--body=inline"]
        result = gh.run(argv, client=client, cwd=directory, capture_output=True, text=True)
        assert client.cwd == (other if scenario == "repo_from_cwd" else None)
    if scenario in {"conflict", "unreadable", "invalid_utf8"}:
        refused = result.github_result
        assert result.returncode == 1
        if scenario == "conflict":
            assert refused.error == "github_invalid_command"
        else:
            assert (refused.error, refused.reason, refused.status) == ("github_invalid_request", "body_unreadable", 400)
        assert http_calls == [] and calls == []
        return
    assert result.returncode == (1 if queue else 0)
    assert result.github_result.error == ("github_queue_changed" if queue else None)
    writes = [call for call in http_calls if call[1] != "graphql"]
    assert writes == ([] if queue else [(
        "PUT", "repos/unit/public/pulls/1/merge",
        {"merge_method": "squash", "sha": HEAD, "commit_title": "clean (#1)", "commit_message": expected_body},
    )])
    assert len(http_calls) == (1 if queue else 2)
    assert not calls


@pytest.mark.parametrize("phase", ["plan", "run"])
def test_installed_shard_helper_is_not_relative_to_product_cwd(tmp_path, phase):
    """Defect class: installed controller paths override locked runner dependencies in plan/run.

    Direct execution must put neither the controller's site root nor its own directory on the
    runner's path, and must load its own siblings whatever ``lu_machinery`` the runner has locked.
    """
    import os
    import shutil
    import sys

    from lu_machinery.common.repo_root import machinery_path

    installed = tmp_path / "installed" / "lu_machinery" / "ci"
    installed.mkdir(parents=True)
    for name in ("pytest_shards.py", "_location.py", "stall_watch.py"):
        shutil.copyfile(machinery_path("ci/" + name), installed / name)
    (installed.parent / "__init__.py").write_text("")
    (installed / "__init__.py").write_text("")
    (installed.parent.parent / "probe_dependency.py").write_text('ORIGIN = "outer-controller"\n')
    (installed.parent.parent / "controller_only.py").write_text("")
    (installed / "probe_shadow.py").write_text('ORIGIN = "controller-directory"\n')
    runner_site = tmp_path / "runner-site"
    (runner_site / "lu_machinery" / "ci").mkdir(parents=True)
    (runner_site / "probe_dependency.py").write_text('ORIGIN = "locked-runner"\n')
    (runner_site / "probe_shadow.py").write_text('ORIGIN = "locked-runner"\n')
    # An older locked release: its package has neither of the helper's siblings.
    (runner_site / "lu_machinery" / "__init__.py").write_text("")
    (runner_site / "lu_machinery" / "ci" / "__init__.py").write_text("")
    product = tmp_path / "product"
    (product / "tests").mkdir(parents=True)
    expected = {f"tests/test_probe_{index}.py::test_runner_dependency" for index in range(4)}
    for index in range(4):  # The runner's four shards each require a nonempty file group.
        (product / "tests" / f"test_probe_{index}.py").write_text(
            'import importlib.util, lu_machinery, probe_dependency, probe_shadow\n'
            'assert probe_dependency.ORIGIN == probe_shadow.ORIGIN == "locked-runner"\n'
            'assert importlib.util.find_spec("controller_only") is None\n'
            f'assert lu_machinery.__file__.startswith({str(runner_site)!r})\n'
            'def test_runner_dependency():\n    assert probe_dependency.ORIGIN == "locked-runner"\n'
        )
    nodeids = tmp_path / "nodeids.txt"
    nodeids.write_text("\n".join(sorted(expected)) + "\n")
    plan_dir = tmp_path / "plan"
    lines = machinery_path("ci/cursor_cloud_full_pytest.sh").read_text().splitlines()
    directory = next((line for line in lines if line.startswith("SCRIPT_DIR=")), "")
    if phase == "plan":
        command = next(line for line in lines if line.startswith("PLAN_ARGS="))
        invoke = command + '\n"${PLAN_ARGS[@]}"\n'
    else:
        command = next(line.strip() for line in lines if "pytest_shards.py" in line and " run --nodeids" in line)
        invoke = command.split(" --nodeids", 1)[0] + ' --nodeids "${NODEIDS}" -- -q\n'
    probe = installed / "probe.sh"
    probe.write_text("set -eu\n" + directory + "\n" + invoke)
    result = subprocess.run(
        ["bash", str(probe)], cwd=product, capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHON": sys.executable, "PLAN_DIR": str(plan_dir),
             "NODEIDS": str(nodeids), "PYTHONPATH": str(runner_site)}, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if phase == "plan":
        planned = {line for path in plan_dir.rglob("test-nodeids.txt") for line in path.read_text().splitlines()}
        assert planned == expected
    else:
        assert "4 passed" in result.stdout


@pytest.mark.parametrize("directory", ["ci", "-ci"])
def test_environment_sibling_resolution_does_not_parse_path_as_options(tmp_path, directory):
    """The actual postgres task must resolve its sibling even under a leading-hyphen path."""
    import os
    import shutil

    from lu_machinery.common.repo_root import machinery_path

    sibling = tmp_path / directory
    sibling.mkdir()
    shutil.copyfile(machinery_path("ci/test_env.sh"), sibling / "test_env.sh")
    shutil.copyfile(machinery_path("ci/start_postgres.sh"), sibling / "start_postgres.sh")
    (sibling / "setup_bubblewrap.sh").write_text("printf 'sibling reached\\n'\nexit 73\n")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path / "state"), "TEST_ENV_WAIT_TIMEOUT_S": "5",
           "LEARN_UKRAINIAN_CP_PG_DSN": "fixture"}
    argv = ["bash", "--", directory + "/test_env.sh"]
    started = subprocess.run(argv + ["start", "postgres"], cwd=tmp_path, env=env,
                             capture_output=True, text=True, check=False, timeout=30)
    assert started.returncode == 0, started.stderr
    result = subprocess.run(argv + ["wait"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=False, timeout=30)
    # The sentinel stops before any real package installation or PostgreSQL operation.
    assert result.returncode == 1, result.stdout + result.stderr
    assert "exit 73" in result.stdout + result.stderr
    assert "sibling reached" in (tmp_path / "state/test-env/postgres.log").read_text()


@pytest.mark.parametrize("repo", ["learn-ukrainian/machinery", "learn-ukrainian/learn-ukrainian.github.io"])
@pytest.mark.parametrize("carbon_verdict", [None, "APPROVED", "CHANGES_REQUESTED", "other-model", "stale", "label-create-fails", "label-edit-fails"])
def test_carbon_debt_labels_only_release_gated_missing_reviews(synthetic_opsec, monkeypatch, repo, carbon_verdict):
    """Defect class: absent Carbon stalls machinery or admits product; rejection/substitution bypasses the gate."""
    from lu_machinery.review.record_cf_verdict import build_comment

    monkeypatch.setattr(gate, "catalog", lambda: {
        "public": {**CATALOG["public"], "github": repo},
    })
    comments = []
    for second in (False, True):
        if second and carbon_verdict in (None, "label-create-fails", "label-edit-fails"):
            continue
        carbon = second and carbon_verdict != "other-model"
        body = build_comment(
            sha="b" * 40 if second and carbon_verdict == "stale" else HEAD,
            task_id="review-carbon" if carbon else "review-one",
            started="2026-10-29T10:00:00.000001+00:00",
            verdict="CHANGES_REQUESTED" if second and carbon_verdict == "CHANGES_REQUESTED" else "APPROVED",
            model="gemini-4-carbon-medium" if carbon else "gpt-6.1-sol",
            family="google" if carbon else "openai", second_review=carbon,
            author_models=("claude-opus-5-5",), reply="VERDICT: APPROVE",
        )
        comments.append({**recorded_approval(HEAD), "body": body})
    calls, labels = [], []
    base = transport(calls)

    def send(args, **kwargs):
        if args[:3] == ["gh", "label", "create"] or args[:3] == ["gh", "pr", "edit"]:
            labels.append(args)
            failed_label = carbon_verdict == "label-create-fails" or (
                carbon_verdict == "label-edit-fails" and args[:3] == ["gh", "pr", "edit"]
            )
            return subprocess.CompletedProcess(args, 1 if failed_label else 0, "", "")
        if args[:4] == ["gh", "api", "--method", "GET"]:
            if "/pulls/1/files" in args[4]:
                return subprocess.CompletedProcess(args, 0, json.dumps([[{
                    "filename": "lu_machinery/publish/github.py", "status": "modified",
                }]]), "")
            if "/issues/1/comments" in args[4]:
                return subprocess.CompletedProcess(args, 0, json.dumps([comments]), "")
        if args[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"name": "CI Gate", "bucket": "pass"}]), "")
        return base(args, **kwargs)

    debt = repo == "learn-ukrainian/machinery" and carbon_verdict in (None, "other-model", "stale")
    if repo == "learn-ukrainian/machinery" and carbon_verdict in ("label-create-fails", "label-edit-fails"):
        with pytest.raises(gate.PublishBlocked, match="Carbon debt label could not be recorded"):
            pub.publish("pr-merge", repo=repo, number=1, match_head=HEAD, runner=send)
        assert not calls
        assert len(labels) == (1 if carbon_verdict == "label-create-fails" else 2)
    elif carbon_verdict == "APPROVED" or debt:
        pub.publish("pr-merge", repo=repo, number=1, match_head=HEAD, runner=send)
        assert len(calls) == 1  # the enqueue mutation succeeded
        assert len(labels) == (2 if debt else 0)
        if debt:
            assert labels[0][-1] == "carbon-debt" and "--force" in labels[0]
            assert "--add-label=carbon-debt" in labels[1]
    else:
        reason = "CHANGES_REQUESTED" if carbon_verdict == "CHANGES_REQUESTED" else "Carbon second review has not approved"
        with pytest.raises(gate.PublishBlocked, match=reason):
            pub.publish("pr-merge", repo=repo, number=1, match_head=HEAD, runner=send)
        assert not calls and not labels
