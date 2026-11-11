"""Helpers copied from learn-ukrainian ``tests/agent_runtime/test_attempt_boundary.py`` at 5aba2adb.

The source file is product-kept or owned by another tranche; its tests do not move here.
Only the helpers that tests in this directory import are copied. Their bodies are verbatim; the import block keeps what they use, with machinery import paths.
"""

from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import string
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lu_machinery.agent_runtime import attempt_boundary, env_sanitize
from lu_machinery.agent_runtime.review_mcp import prepare_review_attempt

DEFAULT_ACCOUNT_UUID = "00000000-0000-4000-8000-00000000000a"


BOUND_ACCOUNT_UUID = "00000000-0000-4000-8000-00000000000b"


CLAUDE_AMBIENT_AUTH = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


ACCOUNT_ORG_UUIDS = {
    DEFAULT_ACCOUNT_UUID: "00000000-0000-4000-8000-0000000000a0",
    BOUND_ACCOUNT_UUID: "00000000-0000-4000-8000-0000000000b0",
}


PROFILE_ORGANIZATIONS = {
    account: {"uuid": organization, "organization_type": "claude_max"}
    for account, organization in ACCOUNT_ORG_UUIDS.items()
}


INERT_USER_SETTINGS = {
    "modelSettings": {"fixture-placeholder-model": {"effortLevel": "low", "autoCompactWindow": 200000}},
    "tui": "default",
    "env": {"DISABLE_AUTO_COMPACT": "1"},
}


INERT_USER_SETTINGS_CASES = {
    **{name: {name: value} for name, value in INERT_USER_SETTINGS.items()},
    "together": INERT_USER_SETTINGS,
}


def inert_user_settings_with(extra: dict) -> dict:
    """All the inert names plus ``extra``, whose ``env`` names join the inert one."""
    return {**INERT_USER_SETTINGS, **extra, "env": {**INERT_USER_SETTINGS["env"], **extra.get("env", {})}}


AUTO_MODE_LISTS = {key: [f"fixture-placeholder-{key}"] for key in ("allow", "soft_deny", "hard_deny", "environment")}


AUTO_MODE_SETTINGS = {
    "auto_mode": {"autoMode": AUTO_MODE_LISTS},
    "auto_mode_with_endpoint_keys": {
        "autoMode": {
            **AUTO_MODE_LISTS,
            "baseUrl": "https://fixture-placeholder.invalid",
            "apiKeyHelper": "fixture-placeholder-helper",
        }
    },
}


AUTO_MODE_REFUSED = {
    **AUTO_MODE_SETTINGS,
    **{f"{case}_beside_inert_names": inert_user_settings_with(value) for case, value in AUTO_MODE_SETTINGS.items()},
}


def _private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def _private_json(path: Path, data: object) -> None:
    _private_text(path, json.dumps(data))


_LOGIN_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_login"


def _credentials(path: Path, token: str, *, plan: str | None = "max") -> None:
    """A login's credential file, filled in from the committed template.

    ``token`` is the fixture access token and ``plan`` the subscription type
    Claude Code stored with it (``None`` takes the template without one). As
    in a real login, the record carries inference scope, which the installed
    CLI needs before it reports a subscription login.
    """
    name = "credentials-with-plan" if plan is not None else "credentials-without-plan"
    template = _LOGIN_FIXTURES / f"{name}.template.json"
    _private_text(path, string.Template(template.read_text()).substitute(access_token=token, subscription_type=plan))


def _login_identity(account_uuid: str) -> dict:
    """The account record Claude Code keeps in a login's global config, with its organisation."""
    return {"accountUuid": account_uuid, "organizationUuid": ACCOUNT_ORG_UUIDS[account_uuid]}


def _machine_policy_text(relative: str, text: str) -> None:
    path = env_sanitize._CLAUDE_MANAGED_SETTINGS_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _machine_policy_link() -> None:
    root = env_sanitize._CLAUDE_MANAGED_SETTINGS_DIR
    root.mkdir()
    (root / "managed-settings.json").symlink_to(root / "absent-target")


def _machine_policy_root_link() -> None:
    root = env_sanitize._CLAUDE_MANAGED_SETTINGS_DIR
    root.with_name("machine-policy-target").mkdir()
    root.symlink_to(root.with_name("machine-policy-target"), target_is_directory=True)


MACHINE_POLICY = {
    "managed_settings": lambda: _machine_policy_text("managed-settings.json", "{}"),
    "policy_helper": lambda: _machine_policy_text(
        "managed-settings.json", json.dumps({"policyHelper": {"path": "/fixture/policy-helper", "timeoutMs": 3000}})
    ),
    "drop_in": lambda: _machine_policy_text("managed-settings.d/50-fixture.json", "{}"),
    "drop_in_directory": lambda: (env_sanitize._CLAUDE_MANAGED_SETTINGS_DIR / "managed-settings.d").mkdir(parents=True),
    "managed_mcp": lambda: _machine_policy_text("managed-mcp.json", json.dumps({"mcpServers": {}})),
    "managed_memory": lambda: _machine_policy_text("CLAUDE.md", "fixture\n"),
    "managed_rules": lambda: _machine_policy_text(".claude/rules/fixture.md", "fixture\n"),
    "dangling_link": _machine_policy_link,
    "root_is_a_file": lambda: env_sanitize._CLAUDE_MANAGED_SETTINGS_DIR.write_text("{}", encoding="utf-8"),
    "root_is_a_link": _machine_policy_root_link,
}


def _mock_selected_account_runtime(tmp_path, monkeypatch):
    """Model safe system ancestry without changing shared scratch permissions."""
    ancestors = {(p.stat().st_dev, p.stat().st_ino) for p in tmp_path.parents}
    fstat = os.fstat

    def checked_stat(fd):
        info = fstat(fd)
        if (info.st_dev, info.st_ino) in ancestors:
            fields = list(info)
            fields[0] &= ~0o022
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", checked_stat)
    # A synthetic machine policy root, absent unless a test writes policy there,
    # on a host that is not WSL unless a test says so (#10619).
    monkeypatch.setattr(env_sanitize, "_CLAUDE_MANAGED_SETTINGS_DIR", tmp_path / "machine-policy")
    monkeypatch.setattr(attempt_boundary, "_CLAUDE_PROC_VERSION", tmp_path / "proc-version")
    for name in ("WSL_DISTRO_NAME", "WSL_INTEROP"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    identities = {}

    # A subclass, so urllib still finds the class attributes its default HTTPS
    # handler reads when another caller builds its first opener.
    class ProfileConnection(http.client.HTTPSConnection):
        def __init__(self, host, *, timeout):  # no socket is ever opened
            assert host == "api.anthropic.com" and timeout == 10

        def request(self, method, path, *, headers):
            assert method == "GET" and path == "/api/oauth/profile"
            self.selected = headers["Authorization"].removeprefix("Bearer ")
            calls.append(self.selected)

        def getresponse(self):
            account = identities[self.selected]
            profile = {"account": {"uuid": account}}
            if account in PROFILE_ORGANIZATIONS:
                profile["organization"] = PROFILE_ORGANIZATIONS[account]
            return SimpleNamespace(status=200, read=lambda _limit: json.dumps(profile).encode())

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", ProfileConnection)
    return identities, calls





def manifest_world(root: Path, kind: str) -> dict:
    state = "curriculum/l2-uk-en/evidence/a1/_state/sample"
    plans = "curriculum/l2-uk-en/lesson-plans/a1"
    paths = {
        "plan": f"{plans}/sample.yaml",
        "pack": "curriculum/l2-uk-en/evidence/a1/sample.yaml",
        "pack_lock": "curriculum/l2-uk-en/evidence/a1/sample.yaml.lock",
        "words": "curriculum/l2-uk-en/evidence/a1/_words.yaml",
        "words_lock": "curriculum/l2-uk-en/evidence/a1/_words.yaml.lock",
        "decisions": f"{plans}/_decisions.yaml",
    }
    lesson = kind != "plan"
    paths["learner_state"] = f"{state}/{'lesson-1' if lesson else 'plan-review'}.learner-state.yaml"
    if lesson:
        paths.update(
            {
                "lessons_lock": f"{state}/lessons.lock.yaml",
                "lesson": "site/src/content/docs/a1/sample/1.mdx",
                "provenance": f"{state}/lesson-1.provenance.yaml",
                "gate_report": f"{state}/lesson-1.gates.yaml",
                "style_card": "docs/style-cards/a1.md",
            }
        )
    else:
        paths.update(
            {
                "requirements": "docs/epics/fresh-build-requirements.md",
                "arc": f"{plans}/_arc.yaml",
                "arc_source": "docs/epics/fresh-build-a1-arc.md",
                "scope": f"{plans}/_scope/sample.yaml",
                "grammar": f"{plans}/_grammar.yaml",
                "validate_report": f"{state}/plan-validate.report.json",
                "pack_verify_report": f"{state}/pack-verify.report.json",
            }
        )

    def pin(name: str) -> dict:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Authorized current input.\n")
        return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    doc = {
        "manifest_schema": 1,
        "kind": "lesson" if lesson else "plan",
        "level": "a1",
        "slug": "sample",
        "inputs": {name: pin(path) for name, path in paths.items()},
        "learner_state": {"sha256": "a" * 64, "source": "planned_state"},
    }
    if lesson:
        doc.update(
            {
                "lesson": 1,
                "lesson_kind": "lesson",
                "recap": False,
                "review_eligible": True,
                "blocked_by": [],
                "lesson_lock_entry": {"path": paths["lesson"], "lesson": 1, "entry_sha256": "a" * 64},
                "module_digest": pin(f"{state}/digest-upto-1.yaml"),
                "digest_generator_version": "1",
                "upstream_lessons": [],
                "previous_attempt": None,
                "diff": None,
            }
        )
        doc["inputs"]["activity_data"] = []
        if kind == "rereview":
            doc["previous_attempt"] = {
                "attempt_id": "previous",
                "review": pin(f"{state}/lesson-1.review.previous.yaml"),
                "ledger": pin("batch_state/review-receipts/review/previous.jsonl"),
            }
            doc["diff"] = pin(f"{state}/lesson-1.diff.previous.{'a' * 16}.patch")
    else:
        doc["position"] = 1
    return doc


@pytest.fixture
def world(tmp_path, monkeypatch):
    # The curriculum pin policy lives in the product. Supply this test's
    # already-frozen pins while exercising the real machinery boundary.
    from lu_machinery.review.product_hooks import attempt_hooks

    hooks = attempt_hooks()
    monkeypatch.setattr(hooks, "pin_refusals", lambda manifest, root: [], raising=False)
    monkeypatch.setattr(hooks, "pinned_entries", lambda manifest: list(manifest["inputs"].items()), raising=False)
    root = tmp_path / "repository"
    root.mkdir()
    home = tmp_path / "old-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}")
    (home / ".codex" / "auth.json").chmod(0o600)
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("AGY_APP_DATA_DIR", str(token.parent))
    return root, home


def attempt_config(root: Path, tmp_path: Path, manifest: dict, agent: str, *, review_access: str = "isolated") -> dict:
    path = root / "attempt.yaml"
    path.write_text(yaml.safe_dump(manifest))
    plan = prepare_review_attempt(
        "review", "current", path, agent, receipts_root=tmp_path / "receipts", review_access=review_access
    )
    return {
        **plan.adapter_options,
        "review_id": "review",
        "attempt_id": "current",
        "review_manifest": str(path),
        "review_input_root": str(root),
        "review_access": review_access,
    }
