"""Match normalized accessible names as data; forbid I/O, frameworks, and other cua packages."""

import re
import unicodedata
from typing import Literal, Self

from pydantic import model_validator

from cua.domain.common import DomainModel


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = "".join(" " if unicodedata.category(char).startswith("P") else char for char in text)
    return " ".join(text.split())


class NameMatcher(DomainModel):
    """Default to normalization: exact case/punctuation is brittle across tenant themes.

    Normalization does NOT invent semantic synonyms. one_of lists explicit, reviewable
    normalized aliases such as Member ID and Account Holder Number; a model must not guess them.
    """

    mode: Literal["exact", "normalized", "regex", "one_of"] = "normalized"
    value: str | None = None
    alternatives: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_shape(self) -> Self:
        if self.mode == "one_of":
            if self.value is not None or not self.alternatives:
                raise ValueError("one_of needs alternatives and no value")
        elif self.value is None or self.alternatives:
            raise ValueError("exact, normalized, and regex need value and no alternatives")
        if self.mode == "regex":
            try:
                re.compile(self.value or "")
            except re.error as exc:
                raise ValueError("Invalid name regex") from exc
        return self

    def matches(self, name: str) -> bool:
        match self.mode:
            case "exact":
                return name == self.value
            case "normalized":
                return normalize_name(name) == normalize_name(self.value or "")
            case "regex":
                return re.search(self.value or "", name) is not None
            case "one_of":
                return any(
                    normalize_name(name) == normalize_name(item) for item in self.alternatives
                )

    def describe(self) -> str:
        return f"name {self.mode} {self.alternatives if self.mode == 'one_of' else self.value!r}"
