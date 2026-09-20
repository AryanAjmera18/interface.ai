"""Describe bounded surface actions as data; forbid I/O, frameworks, and other cua packages."""

from typing import Annotated, Literal

from pydantic import Field

from cua.domain.common import DomainModel, NonEmpty, ValueRef
from cua.domain.predicates import Predicate


class Navigate(DomainModel):
    kind: Literal["navigate"] = "navigate"
    url: NonEmpty


class Click(DomainModel):
    kind: Literal["click"] = "click"


class TypeText(DomainModel):
    kind: Literal["type_text"] = "type_text"
    value_ref: ValueRef


class SelectOption(DomainModel):
    kind: Literal["select_option"] = "select_option"
    value_ref: ValueRef


class ReadValue(DomainModel):
    kind: Literal["read_value"] = "read_value"
    output_name: NonEmpty
    attribute: Literal["value", "name", "description"] = "value"


class WaitFor(DomainModel):
    kind: Literal["wait_for"] = "wait_for"
    predicate: Predicate


class Assert(DomainModel):
    kind: Literal["assert"] = "assert"
    predicate: Predicate


class Dismiss(DomainModel):
    kind: Literal["dismiss"] = "dismiss"


class Scroll(DomainModel):
    kind: Literal["scroll"] = "scroll"
    direction: Literal["up", "down", "left", "right"]
    amount: int = Field(gt=0)


Action = Annotated[
    Navigate | Click | TypeText | SelectOption | ReadValue | WaitFor | Assert | Dismiss | Scroll,
    Field(discriminator="kind"),
]


class ActionInput(DomainModel):
    """Both providers require an object root; wrap the union instead of emitting a root anyOf."""

    action: Action
