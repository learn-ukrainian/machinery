"""Closed-registry integration coverage for the native Kimi lane."""
from __future__ import annotations


import pytest




def test_kimi_background_worker_refuses_before_any_processor(monkeypatch):
    from lu_machinery.ai_agent_bridge import _ask_lifecycle, _messaging

    monkeypatch.setattr(_messaging, "read_message", lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="Kimi is retired"):
        _ask_lifecycle._process_target(12, "kimi", {"new_session": True, "no_timeout": True, "review": True})
