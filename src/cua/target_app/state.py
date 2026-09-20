"""Manage in-memory synthetic sessions; forbid automation-package imports and disk persistence."""

import hashlib
import hmac
import secrets
from decimal import Decimal

from faker import Faker

from cua.target_app.models import Account, Member, Session


def seed_members() -> list[Member]:
    fake = Faker("en_US")
    fake.seed_instance(731)
    return [
        Member(
            member_id=str(number),
            name=fake.name(),
            address=fake.address(),
            restricted=number == 10050,
            accounts=[
                Account(
                    account_id=f"{number}-01",
                    account_type="Checking",
                    nickname="Everyday",
                    balance=Decimal(fake.random_int(100_000, 900_000)) / 100,
                ),
                Account(
                    account_id=f"{number}-02",
                    account_type="Savings",
                    nickname="Reserve",
                    balance=Decimal(fake.random_int(200_000, 2_000_000)) / 100,
                ),
            ],
        )
        for number in range(10001, 10051)
    ]


class SessionStore:
    """Sign only opaque IDs: no field values enter cookies, logs, exporters, or disk."""

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self._sessions: dict[str, Session] = {}

    def cookie(self, session: Session) -> str:
        signature = hmac.new(self._secret, session.session_id.encode(), hashlib.sha256).hexdigest()
        return f"{session.session_id}.{signature}"

    def resolve(self, cookie: str | None) -> Session:
        if cookie:
            session_id, separator, signature = cookie.partition(".")
            expected = hmac.new(self._secret, session_id.encode(), hashlib.sha256).hexdigest()
            if separator and hmac.compare_digest(signature, expected):
                existing = self._sessions.get(session_id)
                if existing is not None:
                    return existing
        session = Session(session_id=secrets.token_urlsafe(24))
        self._sessions[session.session_id] = session
        return session
