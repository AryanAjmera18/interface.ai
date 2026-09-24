"""Test lease transitions without browser or framework dependencies."""

from datetime import UTC, datetime, timedelta

import pytest

from cua.escalation.lease import LeaseManager
from cua.escalation.models import IllegalLeaseTransitionError


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class FakeIds:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"01ARZ3NDEKTSV4RRFFQ69G5F{self.value:02d}"


def test_complete_handoff_and_abort_paths() -> None:
    clock = FakeClock()
    manager = LeaseManager(clock, FakeIds())
    lease = manager.create("operator needed")
    assert (lease.state, lease.holder) == ("RUNNING", "automation")
    lease = manager.pause(lease)
    assert (lease.state, lease.holder) == ("PAUSED", "none")
    lease = manager.take_control(lease)
    assert (lease.state, lease.holder) == ("OPERATOR_CONTROL", "operator")
    lease = manager.request_handback(lease)
    assert (lease.state, lease.holder) == ("HANDBACK_PENDING", "none")
    lease = manager.resume(lease, lease.resume_token)
    assert (lease.state, lease.holder) == ("RUNNING", "automation")

    aborted = manager.abort(manager.pause(lease))
    assert (aborted.state, aborted.holder) == ("ABORTED", "none")


@pytest.mark.parametrize(
    ("method", "state"),
    [
        ("pause", "PAUSED"),
        ("take_control", "RUNNING"),
        ("request_handback", "PAUSED"),
        ("resume", "OPERATOR_CONTROL"),
        ("abort", "RUNNING"),
    ],
)
def test_illegal_transition_table(method: str, state: str) -> None:
    manager = LeaseManager(FakeClock(), FakeIds())
    lease = manager.create("test").model_copy(update={"state": state})
    with pytest.raises(IllegalLeaseTransitionError, match=r"transition|expected"):
        if method == "resume":
            manager.resume(lease, lease.resume_token)
        else:
            getattr(manager, method)(lease)


def test_wrong_token_and_expiry_abort_cleanly() -> None:
    clock = FakeClock()
    manager = LeaseManager(clock, FakeIds(), ttl_s=10)
    lease = manager.request_handback(manager.take_control(manager.pause(manager.create("test"))))
    with pytest.raises(IllegalLeaseTransitionError, match="token"):
        manager.resume(lease, "01ARZ3NDEKTSV4RRFFQ69G599")
    clock.value += timedelta(seconds=10)
    expired = manager.expire(lease)
    assert (expired.state, expired.holder) == ("ABORTED", "none")
