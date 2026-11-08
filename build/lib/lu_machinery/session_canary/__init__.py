"""Lane session canaries for long interactive sessions (Grok/Codex/Claude).

Operational mid-session rot checks reuse ``context_canary`` facts-based scoring
(pass-ratio, default 0.8 = 8/10). Rollover confirmation uses the exact replacement
binding and lease, independently of these operational checks.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
