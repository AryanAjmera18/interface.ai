"""Prove identity fields do not leave the discovery process through AX evidence."""

import json

from cua.discovery.candidates import locator_ladder
from cua.domain.common import EvidenceRef
from cua.domain.names import NameMatcher
from cua.domain.observation import AxNode
from cua.domain.predicates import AxTarget
from cua.observability.redaction import redacted_ax_payload
from cua.target_app.state import seed_members
from tests.unit.domain.samples import observation


def test_identity_summary_and_name_column_are_masked() -> None:
    member = next(item for item in seed_members() if item.member_id == "10023")
    root = AxNode(
        role="document",
        name="",
        children=(
            AxNode(role="text", name="Name"),
            AxNode(role="text", name=member.name),
            AxNode(role="text", name="Address"),
            AxNode(role="text", name=member.address),
            AxNode(
                role="table",
                name="Members",
                children=(
                    AxNode(
                        role="row",
                        name="Member ID Name Action",
                        children=(
                            AxNode(role="columnheader", name="Member ID"),
                            AxNode(role="columnheader", name="Name"),
                        ),
                    ),
                    AxNode(
                        role="row",
                        name=f"{member.member_id} {member.name}",
                        children=(
                            AxNode(role="cell", name=member.member_id),
                            AxNode(role="cell", name=member.name),
                        ),
                    ),
                ),
            ),
        ),
    )
    encoded = json.dumps(redacted_ax_payload(root, "0" * 64))
    assert member.name not in encoded
    assert member.address not in encoded
    assert " ".join(member.address.split()) not in encoded
    assert member.member_id in encoded
    marker_text = json.dumps(redacted_ax_payload(root, "0" * 64))
    assert '"redacted": true' in marker_text
    assert '"sha256"' in marker_text
    assert '"rule": "ax:semantic_identity"' in marker_text


def test_scoped_target_records_unique_scoped_candidate_before_fallback() -> None:
    root = AxNode(
        role="document",
        name="",
        children=(
            AxNode(
                role="row",
                name="Checking",
                children=(AxNode(role="cell", name="$1.00"),),
            ),
            AxNode(
                role="row",
                name="Savings",
                children=(AxNode(role="cell", name="$2.00"),),
            ),
        ),
    )
    observed = observation().model_copy(update={"ax_root": root})
    target = AxTarget(
        role="cell",
        name_matcher=NameMatcher(mode="regex", value=r"^\$"),
        within=AxTarget(role="row", name_matcher=NameMatcher(value="Savings")),
    )
    ladder = locator_ladder(
        target,
        observed,
        EvidenceRef(evidence_id="0" * 26, content_hash="0" * 64, media_type="application/json"),
    )
    assert ladder is not None
    assert [candidate.strategy for candidate in ladder.candidates] == [
        "ax_role_name_scoped",
        "ax_role_name",
    ]
    assert [candidate.uniqueness_at_record for candidate in ladder.candidates] == [1, 2]
