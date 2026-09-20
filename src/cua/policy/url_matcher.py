"""Match normalized URL components; forbid network access and whole-URL regexes."""

import posixpath
from urllib.parse import unquote, urlsplit

from cua.policy.models import UrlPattern


def _canonical_host(host: str) -> str:
    return host.rstrip(".").casefold().encode("idna").decode("ascii")


def _safe_path(raw: str) -> str | None:
    decoded = unquote(raw)
    if unquote(decoded) != decoded or "\\" in decoded:
        return None
    parts = decoded.split("/")
    if any(part in {".", ".."} for part in parts):
        return None
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _path_matches(pattern: str, path: str) -> bool:
    wanted = pattern.strip("/").split("/") if pattern != "/" else []
    actual = path.strip("/").split("/") if path != "/" else []

    def match(i: int, j: int) -> bool:
        if i == len(wanted):
            return j == len(actual)
        if wanted[i] == "**":
            return i + 1 == len(wanted) or any(match(i + 1, k) for k in range(j, len(actual) + 1))
        segment_matches = j < len(actual) and (wanted[i] == "*" or wanted[i] == actual[j])
        return segment_matches and match(i + 1, j + 1)

    return match(0, 0)


def url_matches(pattern: UrlPattern, value: str) -> bool:
    """Fail closed on credentials, invalid ports, traversal, or non-HTTP URL structure."""

    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.scheme.casefold() != pattern.scheme or parsed.hostname is None:
            return False
        host = _canonical_host(parsed.hostname)
        expected = _canonical_host(pattern.host.removeprefix("*."))
        if pattern.host.startswith("*."):
            labels = host.split(".")
            base = expected.split(".")
            if len(labels) != len(base) + 1 or labels[1:] != base:
                return False
        elif host != expected:
            return False
        default = 443 if pattern.scheme == "https" else 80
        if (parsed.port or default) != (pattern.port or default):
            return False
        path = _safe_path(parsed.path or "/")
        return path is not None and _path_matches(pattern.path, path)
    except (UnicodeError, ValueError):
        return False
