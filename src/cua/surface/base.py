"""Normalize backend trees and merge frames; forbid policy, replay, and discovery imports."""

from cua.domain.common import DomainModel
from cua.domain.names import normalize_name
from cua.domain.observation import AxNode, AxState, Bounds, FrameInfo, Observation, walk_ax
from cua.domain.ports import TreeChange


class BackendNode(DomainModel):
    """Portable input for UIA, AX or CDP; backend IDs stay outside the public observation."""

    role: str
    name: str = ""
    value: str | None = None
    description: str = ""
    states: frozenset[AxState] = frozenset()
    bounds: Bounds | None = None
    ignored: bool = False
    backend_id: int | None = None
    children: tuple["BackendNode", ...] = ()


class NodeBinding(DomainModel):
    frame_path: tuple[str, ...]
    node_path: tuple[int, ...]
    backend_id: int | None
    normalized_name: str


class NormalizedFrame(DomainModel):
    root: AxNode
    bindings: tuple[NodeBinding, ...]


class FrameSnapshot(DomainModel):
    info: FrameInfo
    root: AxNode
    # Parent AX node_path of the frame element. None is only valid for the main frame.
    owner_path: tuple[int, ...] | None = None


def normalize_tree(tree: BackendNode, frame_path: tuple[str, ...] = ()) -> NormalizedFrame:
    """Preserve original accessible names in AxNode.name; fold names only in a matching index.

    Replacing names by folded strings would hide label drift from observation hashes. Ignored
    and presentational wrappers are flattened before assigning structural paths within each frame.
    Backend identities and transient positions never enter the semantic hash.
    """
    bindings: list[NodeBinding] = []

    def exposed(node: BackendNode) -> tuple[BackendNode, ...]:
        if node.ignored or node.role.lower() in {"none", "presentation", "generic"}:
            return tuple(item for child in node.children for item in exposed(child))
        return (node,)

    def build(node: BackendNode, path: tuple[int, ...]) -> AxNode:
        bindings.append(
            NodeBinding(
                frame_path=frame_path,
                node_path=path,
                backend_id=node.backend_id,
                normalized_name=normalize_name(node.name),
            )
        )
        children = tuple(item for child in node.children for item in exposed(child))
        return AxNode(
            role=node.role.lower(),
            name=node.name,
            value=node.value,
            description=node.description,
            states=node.states,
            bounds=node.bounds,
            frame_path=frame_path,
            node_path=path,
            children=tuple(build(child, (*path, i)) for i, child in enumerate(children)),
        )

    return NormalizedFrame(root=build(tree, ()), bindings=tuple(bindings))


def merge_frames(frames: tuple[FrameSnapshot, ...]) -> AxNode:
    """Splice child document roots at their actual frame owners, preserving per-frame paths."""
    main = next(frame for frame in frames if not frame.info.frame_path)

    def merge(node: AxNode) -> AxNode:
        children = tuple(merge(child) for child in node.children)
        attached = tuple(
            merge(frame.root)
            for frame in frames
            if frame.info.frame_path[:-1] == node.frame_path
            and frame.info.frame_path
            and frame.owner_path == node.node_path
        )
        return node.model_copy(update={"children": (*children, *attached)})

    return merge(main.root)


def tree_diff(before: Observation, after: Observation) -> tuple[TreeChange, ...]:
    def indexed(obs: Observation) -> dict[tuple[tuple[str, ...], tuple[int, ...]], AxNode]:
        return {(n.frame_path, n.node_path): n for n in walk_ax(obs.ax_root)}

    left, right = indexed(before), indexed(after)
    changes = []
    for key in sorted(left.keys() | right.keys()):
        if key not in left or key not in right:
            fields: tuple[str, ...] = ("added" if key in right else "removed",)
        else:
            fields = tuple(
                field
                for field in ("role", "name", "value", "description", "states")
                if (
                    (left[key].states - {"focused"}) != (right[key].states - {"focused"})
                    if field == "states"
                    else getattr(left[key], field) != getattr(right[key], field)
                )
            )
        if fields:
            changes.append(TreeChange(frame_path=key[0], node_path=key[1], fields=fields))
    return tuple(changes)
