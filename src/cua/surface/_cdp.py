"""Translate CDP into portable trees; forbid higher automation-layer imports."""

from typing import Any, cast

from playwright.async_api import BrowserContext, CDPSession, Error, Frame, Page
from pydantic import BaseModel, ConfigDict, Field

from cua.domain.observation import AxNode, AxState, Bounds, FrameInfo
from cua.surface.base import BackendNode, FrameSnapshot, NodeBinding, normalize_tree


class WireModel(BaseModel):
    """CDP may add transport metadata; consume only known fields inside this adapter."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AXValue(WireModel):
    value: str | float | bool | None = None


class AXProperty(WireModel):
    name: str
    value: AXValue


class AXWireNode(WireModel):
    node_id: str = Field(alias="nodeId")
    ignored: bool = False
    role: AXValue = Field(default_factory=AXValue)
    name: AXValue = Field(default_factory=AXValue)
    value: AXValue = Field(default_factory=AXValue)
    description: AXValue = Field(default_factory=AXValue)
    backend_id: int | None = Field(default=None, alias="backendDOMNodeId")
    child_ids: tuple[str, ...] = Field(default=(), alias="childIds")
    properties: tuple[AXProperty, ...] = ()


class AXTree(WireModel):
    nodes: tuple[AXWireNode, ...]


def portable_tree(tree: AXTree) -> BackendNode:
    nodes = {node.node_id: node for node in tree.nodes}

    def build(node: AXWireNode) -> BackendNode:
        children = tuple(build(nodes[key]) for key in node.child_ids if key in nodes)
        role = str(node.role.value or "none").lower()
        role = {"rootwebarea": "document", "statictext": "text"}.get(role, role)
        name = str(node.name.value or "")
        if role == "row" and not name:
            # Native table rows often have no computed name. Aggregate their cell names, as
            # ARIA snapshots/UIA grid adapters do, so semantic row scoping is portable.
            name = " ".join(child.name for child in children if child.name)
        states = frozenset(
            cast(AxState, prop.name)
            for prop in node.properties
            if prop.name in {"disabled", "focused", "checked", "expanded", "required", "invalid"}
            and prop.value.value not in {False, None, "false", "none"}
        )
        return BackendNode(
            role=role,
            name=name,
            value=str(node.value.value) if node.value.value is not None else None,
            description=str(node.description.value or ""),
            states=states,
            ignored=node.ignored or role == "inlinetextbox",
            backend_id=node.backend_id,
            children=children,
        )

    return build(tree.nodes[0])


class FrameCapture:
    """Private transport ownership; none of these Playwright handles cross the Surface port."""

    def __init__(self) -> None:
        self.frames: dict[tuple[str, ...], Frame] = {}
        self.bindings: dict[tuple[tuple[str, ...], tuple[int, ...]], NodeBinding] = {}
        self.sessions: dict[tuple[str, ...], CDPSession] = {}
        self.owned_sessions: list[CDPSession] = []
        self.snapshots: list[FrameSnapshot] = []

    async def close(self) -> None:
        for session in self.owned_sessions:
            await session.detach()


async def capture_frames(context: BrowserContext, page: Page, cdp: CDPSession) -> FrameCapture:
    capture = FrameCapture()
    wire = await cdp.send("Page.getFrameTree")

    async def visit(
        info: dict[str, Any],
        frame: Frame,
        path: tuple[str, ...],
        owner_path: tuple[int, ...] | None,
    ) -> None:
        try:
            session = await context.new_cdp_session(frame)
            capture.owned_sessions.append(session)
        except Error as exc:
            if not path or "does not have a separate CDP session" not in str(exc):
                raise
            # Same-process frames share their parent's target, but still need their own frameId.
            session = capture.sessions[path[:-1]]
        capture.sessions[path] = session
        raw = await session.send("Accessibility.getFullAXTree", {"frameId": info["frame"]["id"]})
        normalized = normalize_tree(portable_tree(AXTree.model_validate(raw)), path)
        capture.frames[path] = frame
        capture.bindings.update({(path, b.node_path): b for b in normalized.bindings})
        capture.snapshots.append(
            FrameSnapshot(
                info=FrameInfo(frame_path=path, title=await frame.title()),
                root=normalized.root,
                owner_path=owner_path,
            )
        )
        used: set[str] = set()
        for child_info in info.get("childFrames", []):
            child_id = child_info["frame"]["id"]
            owner = await session.send("DOM.getFrameOwner", {"frameId": child_id})
            backend_id = owner["backendNodeId"]
            binding = next((b for b in normalized.bindings if b.backend_id == backend_id), None)
            if binding is None:
                raise RuntimeError("Frame owner is absent from the accessibility tree")
            css = await backend_selector(session, backend_id)
            element = await frame.locator(css).element_handle()
            if element is None:
                raise RuntimeError("Frame owner detached during observation")
            child_frame = await element.content_frame()
            if child_frame is None:
                raise RuntimeError("Frame content detached during observation")
            label = await element.get_attribute("title") or await element.get_attribute("name")
            if not label:
                label = f"frame[{binding.node_path}]"
            if label in used:
                raise RuntimeError("Duplicate sibling frame names require explicit fixture scoping")
            used.add(label)
            await visit(child_info, child_frame, (*path, label), binding.node_path)

    try:
        await visit(wire["frameTree"], page.main_frame, (), None)
    except BaseException:
        await capture.close()
        raise
    return capture


async def attach_mask_bounds(
    capture: FrameCapture,
    targets: tuple[tuple[tuple[str, ...], tuple[int, ...]], ...],
) -> None:
    """Query only redactor-selected nodes; Playwright maps nested frames to viewport boxes.

    Querying every AX backend node caused hundreds of missing-element timeouts. A missing
    target box remains None so the evidence sink blacks out that entire screenshot.
    """
    positions: dict[tuple[tuple[str, ...], tuple[int, ...]], Bounds] = {}
    for frame_path, node_path in targets:
        binding = capture.bindings.get((frame_path, node_path))
        if binding is None or binding.backend_id is None:
            continue
        try:
            selector = await backend_selector(capture.sessions[frame_path], binding.backend_id)
            box = await capture.frames[frame_path].locator(selector).bounding_box(timeout=200)
        except (Error, RuntimeError):
            continue
        if box is not None:
            positions[(frame_path, node_path)] = Bounds(
                x=box["x"], y=box["y"], w=box["width"], h=box["height"]
            )

    def attach(node: AxNode) -> AxNode:
        return node.model_copy(
            update={
                "bounds": positions.get((node.frame_path, node.node_path)),
                "children": tuple(attach(child) for child in node.children),
            }
        )

    capture.snapshots = [
        snapshot.model_copy(update={"root": attach(snapshot.root)})
        for snapshot in capture.snapshots
    ]


SELECTOR_FUNCTION = """function() {
    let e = this.nodeType === 1 ? this : this.parentElement, parts = [];
    while (e && e.nodeType === 1) {
        let i = 1, sibling = e.previousElementSibling;
        while (sibling) { i++; sibling = sibling.previousElementSibling; }
        parts.unshift(e.localName + ':nth-child(' + i + ')'); e = e.parentElement;
    }
    return parts.join(' > ');
}"""


async def backend_selector(session: CDPSession, backend_id: int) -> str:
    """Resolve this observed node to a short-lived DOM selector, never a recorded fallback.

    CDP backend IDs are authoritative within the snapshot. The selector is regenerated each
    resolve and rejected after AX state changes; it is not exported as semantic provenance.
    """
    remote = await session.send("DOM.resolveNode", {"backendNodeId": backend_id})
    object_id = remote["object"]["objectId"]
    try:
        result = await session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": SELECTOR_FUNCTION,
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, str) or not value:
            raise RuntimeError("Observed node has no actionable DOM element")
        return value
    finally:
        await session.send("Runtime.releaseObject", {"objectId": object_id})


async def accessible_element(
    session: CDPSession, document_backend: int, selector: str
) -> BackendNode | None:
    """Read browser-computed AX attributes for a fallback element, never guess from innerText."""
    document = await session.send("DOM.resolveNode", {"backendNodeId": document_backend})
    document_id = document["object"]["objectId"]
    element_id: str | None = None
    try:
        element = await session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": document_id,
                "functionDeclaration": "function(s) { return this.querySelector(s); }",
                "arguments": [{"value": selector}],
            },
        )
        element_id = element["result"].get("objectId")
        if element_id is None:
            return None
        raw = await session.send(
            "Accessibility.getPartialAXTree",
            {
                "objectId": element_id,
                "fetchRelatives": False,
            },
        )
        return portable_tree(AXTree.model_validate(raw))
    finally:
        if element_id is not None:
            await session.send("Runtime.releaseObject", {"objectId": element_id})
        await session.send("Runtime.releaseObject", {"objectId": document_id})
