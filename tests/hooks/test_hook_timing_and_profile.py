"""Tests for the Grok hook profile applicator."""

from __future__ import annotations

from lu_machinery.hooks.apply_grok_hook_profile import ensure_compat_claude_hooks_false


def test_ensure_compat_appends() -> None:
    text, changed = ensure_compat_claude_hooks_false("")
    assert changed
    assert "[compat.claude]" in text
    assert "hooks = false" in text


def test_ensure_compat_idempotent() -> None:
    base = "[compat.claude]\nhooks = false\n"
    text, changed = ensure_compat_claude_hooks_false(base)
    assert changed is False
    assert text == base


def test_ensure_compat_idempotent_with_inline_comment() -> None:
    """Trailing inline comments must not trigger a second hooks key (CF F1)."""
    base = "[compat.claude]\nhooks = false  # operator note\n"
    text, changed = ensure_compat_claude_hooks_false(base)
    assert changed is False
    assert text == base
    assert text.count("hooks") == 1


def test_ensure_compat_flips_true() -> None:
    base = "[models]\ndefault = \"grok-4.5\"\n\n[compat.claude]\nhooks = true\n"
    text, changed = ensure_compat_claude_hooks_false(base)
    assert changed
    assert "hooks = false" in text
    assert "hooks = true" not in text


def test_ensure_compat_flips_true_keeps_inline_comment() -> None:
    base = "[compat.claude]\nhooks = true  # was on\n"
    text, changed = ensure_compat_claude_hooks_false(base)
    assert changed
    assert "hooks = false" in text
    assert "hooks = true" not in text
    assert text.count("hooks") == 1
    assert "# was on" in text
