"""An unclaimed launcher request must not schedule the next dead letter."""

from __future__ import annotations

import json

import pytest

from lu_machinery.fleet_comms.authority import DURABLE_CTO_RECIPIENT, AuthorityService

pytestmark = pytest.mark.usefixtures("placeholder_seats_registered")

_HANDOFF = "HANDOFF NOW (CTO automated, 2026-11-21T00:00:00Z): fixture"
_EXPIRED_AT = "2026-11-28T02:52:00Z"
_DRAINED_AT = "2026-11-28T03:00:00Z"
_SECOND_CYCLE = "2099-01-01T00:00:00Z"


@pytest.fixture
def plane(tmp_path, monkeypatch):
    root = tmp_path / "plane"
    monkeypatch.setenv("FLEET_COMMS_ROOT", str(root))
    monkeypatch.setenv("AB_DB_PATH", str(tmp_path / "unused-legacy.db"))
    return root


def _expire_unclaimed(service: AuthorityService, recipient: str, key: str):
    message = service.publish_message(
        sender=DURABLE_CTO_RECIPIENT,
        recipients=(recipient,),
        kind="request",
        body=_HANDOFF,
        created_at="2026-11-21T00:00:00Z",
        deadline_at="2026-11-28T00:00:00Z",
        idempotency_key=key,
    )
    assert service.reclaim_expired_deliveries(now=_EXPIRED_AT) == 1
    return message


def _notice(service: AuthorityService):
    rows = service.store.connection.execute(
        "SELECT message_id FROM comms_messages WHERE kind = 'delivery-error'",
    ).fetchall()
    assert len(rows) == 1
    notice = service.get_message(rows[0]["message_id"])
    body = json.loads(service.read_message_body(notice.message_id))
    return notice, body


@pytest.mark.parametrize("slot", ["claude-epics", "claude-infra", "codex-monitor"])
def test_unclaimed_launcher_request_notifies_drained_cto_once(plane, slot: str) -> None:
    """Defect class: an unclaimed launcher inbox's expiry notice is queued back to that inbox.

    The copy sits for seven days and dead-letters again. ``claude-epics`` is a
    registry slot, ``claude-infra`` is a slot that is also a static identity,
    and ``codex-monitor`` is a lane sender that is not a slot row.
    """
    with AuthorityService(root=plane) as service:
        message = _expire_unclaimed(service, slot, f"handoff-{slot}")
        notice, body = _notice(service)
        assert body == {
            "error": "coordinator_reply_delivery_failed",
            "reason_code": "deadline_expired",
            "delivery_id": message.delivery_ids[0],
            "message_id": message.message_id,
            "recipient": slot,
        }
        assert {service.get_delivery(delivery_id).recipient for delivery_id in notice.delivery_ids} == {
            DURABLE_CTO_RECIPIENT,
        }
        lease = service.claim_next_delivery(
            DURABLE_CTO_RECIPIENT,
            DURABLE_CTO_RECIPIENT,
            now=_DRAINED_AT,
        )
        assert lease is not None
        assert lease.delivery.message_id == notice.message_id
        service.acknowledge_delivery(
            lease.delivery.delivery_id,
            worker_id=DURABLE_CTO_RECIPIENT,
            fence_token=lease.fence_token,
            now=_DRAINED_AT,
        )
        assert service.store.connection.execute("SELECT COUNT(*) FROM authority_dead_letters").fetchone()[0] == 1
        assert service.reclaim_expired_deliveries(now=_SECOND_CYCLE) == 0
        assert service.store.connection.execute("SELECT COUNT(*) FROM authority_dead_letters").fetchone()[0] == 1
        assert (
            service.store.connection.execute(
                "SELECT COUNT(*) FROM comms_messages WHERE kind = 'delivery-error'",
            ).fetchone()[0]
            == 1
        )
        assert service.get_delivery(message.delivery_ids[0]).state == "expired"


def test_registered_non_launcher_recipient_still_receives_the_notice(plane) -> None:
    """Control: a registered recipient that is not a launcher inbox still gets the notice."""
    with AuthorityService(root=plane) as service:
        message = _expire_unclaimed(service, "worker-task", "handoff-worker")
        notice, body = _notice(service)
        assert body["recipient"] == "worker-task"
        assert body["delivery_id"] == message.delivery_ids[0]
        assert {service.get_delivery(delivery_id).recipient for delivery_id in notice.delivery_ids} == {
            "worker-task",
            DURABLE_CTO_RECIPIENT,
        }
