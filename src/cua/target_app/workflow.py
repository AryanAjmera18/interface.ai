"""Implement the synthetic banking flow; forbid imports from automation packages."""

import secrets
from decimal import Decimal, InvalidOperation

from cua.target_app.config import TenantConfig
from cua.target_app.models import Account, Draft, FormValues, Member, Receipt, Session, View


def find_member(session: Session, member_id: str) -> Member | None:
    return next((member for member in session.members if member.member_id == member_id), None)


def deposit_error(member: Member, form: FormValues) -> str:
    try:
        amount = Decimal(form.initial_deposit)
    except InvalidOperation:
        return "Enter a valid initial deposit."
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or amount <= 0 or not isinstance(exponent, int) or exponent < -2:
        return "Initial deposit must be positive with no more than two decimal places."
    source = next(
        (item for item in member.accounts if item.account_id == form.funding_source), None
    )
    if source is None:
        return "Choose a funding source belonging to this member."
    if amount > source.balance:
        return "Initial deposit exceeds the available funding balance."
    if form.account_type not in {"Savings", "Checking"}:
        return "Choose a supported account type."
    if not form.nickname.strip() or len(form.nickname) > 40:
        return "Enter a nickname between 1 and 40 characters."
    return ""


def workflow(
    session: Session,
    tenant: TenantConfig,
    action: str,
    method: str,
    form: FormValues,
    *,
    reject_deposit: bool = False,
) -> View:
    """Commit only server-held reviewed values; repeated confirmation tokens cannot debit twice."""
    if action == "search" and method == "GET":
        return View(screen="search")
    if action in {"confirm", "commit"}:
        previous = next((r for r in session.receipts if r.token == form.token), None)
        if previous is not None and method == "POST":
            return View(screen="confirmation", receipt=previous)
        draft = session.draft
        if (
            method != "POST"
            or draft is None
            or not secrets.compare_digest(draft.token.encode(), form.token.encode())
        ):
            return View(screen="invalid", error="No matching reviewed request. Start again.")
        form = draft.form
    member = find_member(session, form.member_id)
    if member is None:
        return View(screen="not_found", form=form)
    if member.restricted:
        return View(screen="denied")
    if action == "search":
        return View(screen="results", member=member, form=form)
    if action == "detail":
        return View(screen="detail", member=member)
    if action == "open":
        return View(screen="open", member=member, form=form)
    if action == "review" and method == "POST":
        session.draft = None
        error = (
            "Initial deposit rejected by the server."
            if reject_deposit
            else deposit_error(member, form)
        )
        if error:
            return View(screen="open", member=member, form=form, error=error)
        session.draft = Draft(form=form, token=secrets.token_urlsafe(24))
        return View(screen="review", member=member, form=form, token=session.draft.token)
    if action in {"confirm", "commit"}:
        draft = session.draft
        assert draft is not None
        if tenant.extra_confirmation:
            if action == "confirm":
                draft.extra_confirmed = True
                return View(screen="extra", member=member, form=form, token=draft.token)
            if not draft.extra_confirmed:
                return View(screen="invalid", error="Additional confirmation is required.")
        error = deposit_error(member, form)
        if error:
            session.draft = None
            return View(screen="open", member=member, form=form, error=error)
        amount = Decimal(form.initial_deposit)
        source = next(item for item in member.accounts if item.account_id == form.funding_source)
        account = Account(
            account_id=f"{member.member_id}-{len(member.accounts) + 1:02d}",
            account_type=form.account_type,
            nickname=form.nickname,
            balance=amount,
        )
        source.balance -= amount
        member.accounts.append(account)
        receipt = Receipt(
            reference=f"{tenant.tenant_id.upper()}-{len(session.receipts) + 1:06d}",
            token=draft.token,
            member_id=member.member_id,
            account=account.model_copy(deep=True),
        )
        session.receipts.append(receipt)
        session.draft = None
        return View(screen="confirmation", receipt=receipt)
    return View(screen="invalid", error="This operation is not available.")
