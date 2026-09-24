"""Enforce exclusive, expiring control leases; forbid UI and persistence dependencies."""

from datetime import timedelta

from cua.domain.ports import Clock, IdGenerator
from cua.escalation.models import IllegalLeaseTransitionError, SessionLease


class LeaseManager:
    """Apply only named state-machine edges and abort an expired paused session."""

    def __init__(self, clock: Clock, ids: IdGenerator, *, ttl_s: int = 900) -> None:
        self.clock, self.ids, self.ttl_s = clock, ids, ttl_s

    def create(self, reason: str) -> SessionLease:
        now = self.clock.now()
        return SessionLease(
            lease_id=self.ids.new(),
            acquired_at=now,
            expires_at=now + timedelta(seconds=self.ttl_s),
            reason=reason,
            resume_token=self.ids.new(),
        )

    def pause(self, lease: SessionLease) -> SessionLease:
        self._require(lease, "RUNNING")
        return lease.model_copy(update={"state": "PAUSED", "holder": "none"})

    def take_control(self, lease: SessionLease) -> SessionLease:
        lease = self.expire(lease)
        self._require(lease, "PAUSED")
        return lease.model_copy(update={"state": "OPERATOR_CONTROL", "holder": "operator"})

    def request_handback(self, lease: SessionLease) -> SessionLease:
        lease = self.expire(lease)
        self._require(lease, "OPERATOR_CONTROL")
        return lease.model_copy(update={"state": "HANDBACK_PENDING", "holder": "none"})

    def resume(self, lease: SessionLease, resume_token: str) -> SessionLease:
        lease = self.expire(lease)
        self._require(lease, "HANDBACK_PENDING")
        if resume_token != lease.resume_token:
            raise IllegalLeaseTransitionError("Resume token does not match this session lease")
        now = self.clock.now()
        return lease.model_copy(
            update={
                "state": "RUNNING",
                "holder": "automation",
                "acquired_at": now,
                "expires_at": now + timedelta(seconds=self.ttl_s),
            }
        )

    def abort(self, lease: SessionLease) -> SessionLease:
        if lease.state not in {"PAUSED", "OPERATOR_CONTROL", "HANDBACK_PENDING"}:
            raise IllegalLeaseTransitionError(f"Cannot transition {lease.state} -> ABORTED")
        return lease.model_copy(update={"state": "ABORTED", "holder": "none"})

    def expire(self, lease: SessionLease) -> SessionLease:
        if (
            lease.state in {"PAUSED", "OPERATOR_CONTROL", "HANDBACK_PENDING"}
            and self.clock.now() >= lease.expires_at
        ):
            return lease.model_copy(update={"state": "ABORTED", "holder": "none"})
        return lease

    @staticmethod
    def _require(lease: SessionLease, expected: str) -> None:
        if lease.state != expected:
            raise IllegalLeaseTransitionError(
                f"Cannot transition from {lease.state}; expected {expected}"
            )
