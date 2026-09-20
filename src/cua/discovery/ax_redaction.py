"""Remove identity fields from AX egress while retaining actionable UI semantics."""

from pydantic import JsonValue

from cua.domain.observation import AxNode

MASK = "[REDACTED]"


def _sensitive_values(node: AxNode) -> set[str]:
    values: set[str] = set()
    children = node.children
    for index, child in enumerate(children[:-1]):
        if child.name.strip().casefold() in {"name", "address"}:
            value = children[index + 1].name.strip()
            if value:
                values.add(value)
    for child in children:
        if child.role == "table":
            rows = [row for row in child.children if row.role == "row"]
            if rows:
                sensitive_columns = {
                    index
                    for index, header in enumerate(rows[0].children)
                    if header.name.strip().casefold() in {"name", "address"}
                }
                for row in rows[1:]:
                    for index in sensitive_columns:
                        if index < len(row.children) and row.children[index].name:
                            values.add(row.children[index].name)
        values.update(_sensitive_values(child))
    return values


def sanitize_ax(root: AxNode) -> AxNode:
    """Mask values identified by semantic labels, including occurrences in aggregate row names."""
    sensitive = _sensitive_values(root)

    def mask(text: str) -> str:
        result = text
        for value in sorted(sensitive, key=len, reverse=True):
            result = result.replace(value, MASK)
            result = result.replace(" ".join(value.split()), MASK)
        return result

    def visit(node: AxNode) -> AxNode:
        return node.model_copy(
            update={
                "name": mask(node.name),
                "value": mask(node.value) if node.value is not None else None,
                "description": mask(node.description),
                "children": tuple(visit(child) for child in node.children),
            }
        )

    return visit(root)


def sanitized_ax_payload(root: AxNode, observation_hash: str) -> JsonValue:
    """Persist a redacted projection plus the raw in-memory observation's canonical identity."""
    return {
        "observation_hash": observation_hash,
        "ax_root": sanitize_ax(root).model_dump(mode="json"),
    }
