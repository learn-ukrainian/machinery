"""component_shadow scoring-instant handling (#100 R-9721)."""

import pytest

from lu_machinery.ci import component_shadow


@pytest.mark.parametrize("bad", ["", "   ", "not-a-time"])
def test_check_refuses_an_explicit_empty_or_malformed_now(bad):
    """An explicit empty --now must be refused, not silently replaced by the current time."""
    with pytest.raises(ValueError):
        component_shadow.check({}, None, now=bad)
