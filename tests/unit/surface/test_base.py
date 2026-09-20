"""Test normalization, safety boundaries and desktop contracts; forbid external services."""

import ast
import struct
import zlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cua.domain.actions import Click
from cua.domain.names import normalize_name
from cua.domain.observation import ax_digest
from cua.domain.ports import ControlToken, Surface
from cua.domain.steps import StepTiming
from cua.surface.base import BackendNode, normalize_tree, tree_diff
from cua.surface.desktop import DesktopSurface
from cua.surface.web import LiteralValues, opaque_screenshot
from tests.unit.domain.samples import NOW, ladder, observation


@given(st.text(min_size=1, max_size=30))
def test_normalization_preserves_original_names_and_stable_paths(label: str) -> None:
    raw = BackendNode(
        role="DOCUMENT",
        children=(
            BackendNode(
                role="presentation",
                children=(BackendNode(role="BUTTON", name=label, backend_id=17),),
            ),
        ),
    )
    normalized = normalize_tree(raw, ("Content",))
    node = normalized.root.children[0]
    assert node.name == label and node.role == "button"
    assert node.frame_path == ("Content",) and node.node_path == (0,)
    assert normalized.bindings[-1].normalized_name == normalize_name(label)
    assert ax_digest(normalized.root) == ax_digest(
        normalize_tree(raw).root.model_copy(
            update={
                "frame_path": ("Content",),
                "children": normalized.root.children,
            }
        )
    )


def test_diff_contains_only_changed_field_names() -> None:
    before = observation()
    root = before.ax_root.model_copy(update={"name": "Sensitive changed label"})
    after = before.model_copy(update={"ax_root": root})
    diff = tree_diff(before, after)
    assert diff[0].fields == ("name",)
    assert "Sensitive" not in diff[0].model_dump_json()
    removed = before.model_copy(update={"ax_root": root.model_copy(update={"children": ()})})
    assert any(item.fields == ("removed",) for item in tree_diff(before, removed))
    focused = before.model_copy(
        update={"ax_root": before.ax_root.model_copy(update={"states": frozenset({"focused"})})}
    )
    assert tree_diff(before, focused) == ()


def test_opaque_screenshot_cannot_export_raw_pixels_or_metadata() -> None:
    header = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 3, 2)
    png = opaque_screenshot(header + b"sensitive metadata and pixels")
    assert b"sensitive" not in png
    offset = 8
    payload = b""
    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        if png[offset + 4 : offset + 8] == b"IDAT":
            payload += png[offset + 8 : offset + 8 + length]
        offset += length + 12
    assert zlib.decompress(payload) == b"\0" * 20
    with pytest.raises(ValueError, match="PNG"):
        opaque_screenshot(b"not a png")


async def test_desktop_stub_every_port_method_raises() -> None:
    surface: Surface = DesktopSurface()
    token = ControlToken(
        cdp_endpoint="native-not-implemented", context_id="x", page_guid="x", issued_at=NOW
    )
    calls = (
        surface.observe(),
        surface.resolve(ladder()),
        surface.act(Click()),
        surface.settle(StepTiming(settle_strategy="ax_stable", timeout_ms=1)),
        surface.cede_control(),
        surface.resume_control(token),
    )
    for call in calls:
        with pytest.raises(NotImplementedError):
            await call


async def test_unbound_values_fail_closed() -> None:
    from cua.domain.common import ParamRef, SecretRef

    for reference in (ParamRef(name="member"), SecretRef(name="password")):
        with pytest.raises(ValueError, match="injected"):
            await LiteralValues().resolve_value(reference)


def test_adapter_has_no_sleep_or_vendor_types_in_domain() -> None:
    root = Path(__file__).resolve().parents[3]
    for path in (root / "src/cua/surface").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"sleep", "wait_for_timeout"}
    assert "from playwright" not in (root / "src/cua/domain/ports.py").read_text()
