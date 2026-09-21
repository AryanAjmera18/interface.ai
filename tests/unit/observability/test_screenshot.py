"""Verify region masking, full-viewport fallback, and evidence media contracts."""

import io
from pathlib import Path

import pytest
from PIL import Image

from cua.domain.observation import AxNode, Bounds, ax_digest
from cua.domain.ports import EvidencePayload
from cua.observability.evidence import EvidenceStore, SurfaceEvidenceSink
from cua.observability.redaction import redact
from tests.unit.domain.samples import FixedClock, SequenceIds


def _png(color: tuple[int, int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(stream, format="PNG")
    return stream.getvalue()


def _tree(bounds: Bounds | None) -> AxNode:
    return AxNode(
        role="group",
        name="",
        children=(
            AxNode(role="text", name="Name"),
            AxNode(role="text", name="Synthetic Person", bounds=bounds),
        ),
    )


def test_region_mask_preserves_other_pixels_and_distinct_pages(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path, "0" * 26, clock=FixedClock(), ids=SequenceIds())
    sink = SurfaceEvidenceSink(store)
    tree = _tree(Bounds(x=2, y=3, w=5, h=4))

    async def capture(color: tuple[int, int, int], observation_id: str) -> bytes:
        ref = await sink.put(
            EvidencePayload(
                media_type="image/png",
                content=_png(color),
                observation_hash=ax_digest(tree),
                observation_id=observation_id,
                ax_root=tree,
            )
        )
        return store.get(ref)

    import asyncio

    first = asyncio.run(capture((255, 0, 0), "0" * 26))
    second = asyncio.run(capture((0, 0, 255), "0" * 25 + "1"))
    repeated = asyncio.run(capture((255, 0, 0), "0" * 25 + "2"))
    assert repeated == first
    assert len(store.index.entries) == 3
    assert len({entry.observation_id for entry in store.index.entries}) == 3
    assert first != second
    with Image.open(io.BytesIO(first)) as image:
        assert image.getpixel((3, 4)) == (0, 0, 0)
        assert image.getpixel((15, 15)) == (255, 0, 0)
    assert all(entry.observation_hash == ax_digest(tree) for entry in store.index.entries)
    assert all(entry.redaction == "region_masked" for entry in store.index.entries)
    assert all(entry.masked_regions for entry in store.index.entries)
    store.close()


def test_missing_bounds_blanks_only_that_screenshot(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path, "0" * 26, clock=FixedClock(), ids=SequenceIds())
    sink = SurfaceEvidenceSink(store)
    tree = _tree(None)

    import asyncio

    ref = asyncio.run(
        sink.put(
            EvidencePayload(
                media_type="image/png",
                content=_png((255, 0, 0)),
                observation_hash=ax_digest(tree),
                ax_root=tree,
            )
        )
    )
    with Image.open(io.BytesIO(store.get(ref))) as image:
        assert image.getpixel((15, 15)) == (0, 0, 0)
    assert store.index.entries[0].redaction == "full_viewport"
    assert store.index.entries[0].redaction_reason == "masked AX node lacked bounds"
    store.close()


@pytest.mark.parametrize(
    ("kind", "media_type"),
    [("screenshot", "application/json"), ("ax_snapshot", "image/png")],
)
def test_store_rejects_mismatched_kind_and_media(
    tmp_path: Path, kind: str, media_type: str
) -> None:
    store = EvidenceStore(tmp_path, "0" * 26, clock=FixedClock(), ids=SequenceIds())
    with pytest.raises(ValueError, match="invalid"):
        store.put(redact(b"safe"), media_type, kind=kind)  # type: ignore[arg-type]
    store.close()
