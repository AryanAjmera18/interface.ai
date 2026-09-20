"""Own the mandatory egress choke point; forbid surface and higher-layer imports."""

import base64
import hashlib
import json
import re
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from fnmatch import fnmatchcase
from typing import Literal, cast, overload

from pydantic import BaseModel, JsonValue, PrivateAttr, field_serializer

from cua.domain.capability import OutputSpec, ParamSpec
from cua.domain.common import Digest, DomainModel, Sensitivity, canonical_json

_TOKEN = object()


class RedactionRequiredError(ValueError):
    """An untagged or mutated payload reached an observability sink."""


class SealedValue(DomainModel):
    _issuer: object | None = PrivateAttr(default=None)
    _seal: str = PrivateAttr(default="")


class RedactedValue(SealedValue):
    """Hash the entire original value to join flows without storing that value.

    Equal hashes let a reviewer correlate step 3 with step 7 across runs. This is deterministic
    unsalted SHA-256, not encryption: low-entropy values remain vulnerable to guessing. The hint
    is the literal marker 'last4', not four characters of a secret. No secret fragment is retained.
    """

    redacted: Literal[True] = True
    sha256: Digest
    hint: Literal["last4"] = "last4"
    rule: str


class RedactedOriginal(SealedValue):
    """Unchanged JSON in an opaque envelope: Python bool/null cannot carry an origin tag.

    .value is the original safe representation. Sinks accept the envelope, never its unwrapped
    contents. A content seal detects mutation of nested dicts/lists after sanitization. This is
    an accidental-bypass control inside one process, not a sandbox against malicious Python.
    """

    value: JsonValue


class RedactedBytes(SealedValue):
    value: bytes
    media_type_hint: str | None = None

    @field_serializer("value")
    def encode(self, value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")


TaggedValue = RedactedValue | RedactedOriginal


class FieldRule(DomainModel):
    path: str
    sensitivity: Sensitivity


class RedactionPolicy(DomainModel):
    fields: tuple[FieldRule, ...] = ()
    strict: bool = True

    @classmethod
    def from_specs(
        cls, inputs: tuple[ParamSpec, ...], outputs: tuple[OutputSpec, ...]
    ) -> "RedactionPolicy":
        return cls(
            fields=tuple(
                FieldRule(path=f"/{section}/{pointer(spec.name)}", sensitivity=spec.sensitivity)
                for section, specs in (("inputs", inputs), ("outputs", outputs))
                for spec in specs
            )
        )


_DEFAULT_POLICY = RedactionPolicy()
_POLICY: ContextVar[RedactionPolicy] = ContextVar("cua_redaction_policy", default=_DEFAULT_POLICY)


@contextmanager
def redaction_policy(policy: RedactionPolicy) -> Iterator[None]:
    token = _POLICY.set(policy)
    try:
        yield
    finally:
        _POLICY.reset(token)


def pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _stamp[T: SealedValue](value: T) -> T:
    value._issuer = _TOKEN
    value._seal = hashlib.sha256(value.model_dump_json().encode()).hexdigest()
    return value


def require_tag(value: object) -> None:
    if (
        not isinstance(value, SealedValue)
        or value._issuer is not _TOKEN
        or value._seal != hashlib.sha256(value.model_dump_json().encode()).hexdigest()
    ):
        raise RedactionRequiredError("Sink rejected untagged or modified data; call redact() first")


def json_value(value: TaggedValue) -> JsonValue:
    require_tag(value)
    if isinstance(value, RedactedValue):
        return cast(JsonValue, value.model_dump(mode="json"))
    # Return a copy so callers cannot modify the validated envelope through this view.
    return cast(JsonValue, json.loads(canonical_json(value.value)))


def checked_json(value: object, *, strict: bool = True) -> JsonValue:
    if isinstance(value, (RedactedValue, RedactedOriginal)):
        return json_value(value)
    if strict:
        raise RedactionRequiredError("Sink requires a redact() envelope")
    return json_value(redact(cast(JsonValue, value)))


def luhn_valid(digits: str) -> bool:
    if not digits.isascii() or not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, char in enumerate(reversed(digits)):
        number = int(char) * (2 if i % 2 else 1)
        total += number - 9 if number > 9 else number
    return total % 10 == 0 and len(set(digits)) > 1


_PATTERNS = (
    ("bearer", re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")),
    ("ssn", re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")),
    ("email", re.compile(r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)"),
    ),
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")


def _metadata(value: str, path: str) -> bool:
    """Exempt only syntactically valid typed join identifiers, never arbitrary 'public' strings."""
    leaf = path.rsplit("/", 1)[-1]
    if leaf in {
        "run_id",
        "parent_run_id",
        "observation_id",
        "capability_id",
        "decision_id",
        "evidence_id",
    }:
        return re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", value) is not None
    if leaf in {
        "hash",
        "sha256",
        "content_hash",
        "prev_hash",
        "observation_hash",
        "prompt_hash",
        "journal_head_hash",
        "before_hash",
        "after_hash",
        "content_digest",
        "reviewed_digest",
    }:
        return re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if leaf in {"trace_id", "span_id", "traceId", "spanId", "parentSpanId"}:
        return re.fullmatch(r"(?:[0-9a-f]{16}|[0-9a-f]{32})", value) is not None
    return False


def _pattern(value: str, path: str) -> str | None:
    if _metadata(value, path):
        return None
    for name, regex in _PATTERNS:
        if regex.search(value):
            return name
    for match in _CARD.finditer(value):
        if luhn_valid(re.sub(r"\D", "", match.group())):
            return "luhn_card"
    for match in _DIGITS.finditer(value):
        # Excluding card-width failures is deliberate: otherwise long_digits would negate
        # the requested Luhn false-positive control. Explicit schema PII always wins.
        if not 13 <= len(match.group()) <= 19:
            return "long_digits"
    return None


def _redacted(value: JsonValue, rule: str) -> JsonValue:
    marker = RedactedValue(
        sha256=hashlib.sha256(canonical_json(value).encode()).hexdigest(), rule=rule
    )
    return cast(JsonValue, marker.model_dump(mode="json"))


def _walk(value: JsonValue, path: str, sensitivity: Sensitivity | None = None) -> JsonValue:
    classified = sensitivity
    for rule in _POLICY.get().fields:
        if (
            fnmatchcase(path, rule.path) or path.startswith(rule.path + "/")
        ) and rule.sensitivity in {"pii", "secret"}:
            classified = rule.sensitivity
    if classified in {"pii", "secret"}:
        return _redacted(value, f"schema:{classified}")
    if isinstance(value, str):
        matched = _pattern(value, path)
        return _redacted(value, matched) if matched else value
    if isinstance(value, list):
        return [_walk(child, f"{path}/{i}") for i, child in enumerate(value)]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            safe_key = (
                "redacted-key:" + hashlib.sha256(key.encode()).hexdigest()
                if _pattern(key, "")
                else key
            )
            result[safe_key] = _walk(child, f"{path}/{pointer(key)}")
        return result
    return value


def _opaque_png(value: bytes) -> bytes:
    width, height = struct.unpack(">II", value[16:24])
    if not 0 < width * height <= 40_000_000:
        raise ValueError("PNG dimensions exceed the redaction budget")

    def chunk(kind: bytes, content: bytes) -> bytes:
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content))
        )

    return (
        value[:8]
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\0\0\0" * width) * height))
        + chunk(b"IEND", b"")
    )


@overload
def redact(
    value: bytes, *, field_path: str | None = None, sensitivity: Sensitivity | None = None
) -> RedactedBytes: ...
@overload
def redact(
    value: BaseModel | JsonValue,
    *,
    field_path: str | None = None,
    sensitivity: Sensitivity | None = None,
) -> TaggedValue: ...
def redact(
    value: BaseModel | JsonValue | bytes,
    *,
    field_path: str | None = None,
    sensitivity: Sensitivity | None = None,
) -> TaggedValue | RedactedBytes:
    """Sanitize before any sink. Original safe content is sealed; raw scalar types cannot be tagged.

    Nested structures are inspected, including keys. PNGs are entirely masked (no OCR promise);
    unsupported opaque bytes become a hashed JSON marker rather than leaking arbitrary binary.
    """
    path = field_path or ""
    if isinstance(value, bytes):
        if value.startswith(b"\x89PNG\r\n\x1a\n"):
            return _stamp(RedactedBytes(value=_opaque_png(value), media_type_hint="image/png"))
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            marker = RedactedValue(sha256=hashlib.sha256(value).hexdigest(), rule="opaque_binary")
            return _stamp(
                RedactedBytes(
                    value=canonical_json(cast(JsonValue, marker.model_dump())).encode(),
                    media_type_hint="application/json",
                )
            )
        try:
            original = cast(JsonValue, json.loads(text))
        except json.JSONDecodeError:
            original = text
        clean = _walk(original, path, sensitivity)
        encoded = value if clean == original else canonical_json(clean).encode()
        return _stamp(
            RedactedBytes(
                value=encoded, media_type_hint=None if clean == original else "application/json"
            )
        )
    if isinstance(value, SealedValue):
        require_tag(value)
        return cast(TaggedValue, value)
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    # Round-trip validates JSON-serializability and detaches mutable caller-owned containers.
    original_json = cast(JsonValue, json.loads(json.dumps(raw, allow_nan=False)))
    clean_json = _walk(original_json, path, sensitivity)
    if isinstance(clean_json, dict) and set(clean_json) == {"redacted", "sha256", "hint", "rule"}:
        return _stamp(RedactedValue.model_validate(clean_json))
    return _stamp(RedactedOriginal(value=clean_json))
