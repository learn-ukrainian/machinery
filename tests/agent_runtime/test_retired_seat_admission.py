"""Retired Kimi spellings must be refused before target resolution or launch.

Extends the source repository's target-admission refusal coverage for R60.
"""

import pytest

from lu_machinery.agent_runtime.retired_seat_admission import (
    BRIDGE_MODE,
    MODEL_SELECTOR_KEYS,
    SEAT_SELECTOR_KEYS,
    KimiAdmissionRefused,
)
from lu_machinery.agent_runtime.target_admission import resolve_and_admit

# Literal inputs keep the expectation independent of the catalog reader.
RETIRED_SPELLINGS = (
    "k2.7", "k2.7-coding", "k2.7-coding-highspeed", "k2.7-highspeed",
    "k3", "k3-256k", "k3-max", "kimi-code/k3", "kimi-code/k3-256k",
    "kimi-code/kimi-for-coding", "kimi-code/kimi-for-coding-highspeed",
    "kimi-for-coding", "kimi-for-coding-highspeed", "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed", "kimi-k3", "kimi-k3-256k", "kimi-k3-max",
    "kimi-k3[1m]", "kimi", "kimicc", "kimi-infra",
    "acpx-kimi", "acpx-kimi-shadow", "acpx-kimicc", "acpx-kimicc-shadow",
)


@pytest.mark.parametrize("spelling", RETIRED_SPELLINGS)
@pytest.mark.parametrize("selector", ("seat", "explicit_model", *SEAT_SELECTOR_KEYS, *MODEL_SELECTOR_KEYS))
def test_retired_spellings_cannot_bypass_admission(spelling, selector):
    def resolution_must_not_run(_seat):
        pytest.fail("retired input reached target resolution")

    kwargs = {"mode": BRIDGE_MODE, "resolver": resolution_must_not_run}
    if selector == "seat":
        recipients = (spelling,)
    else:
        recipients = ("claude",)
        if selector == "explicit_model":
            kwargs["model"] = spelling
        else:
            kwargs["attachments"] = ({selector: spelling},)
    with pytest.raises(KimiAdmissionRefused, match="Kimi models and seats are retired"):
        resolve_and_admit(recipients, **kwargs)
