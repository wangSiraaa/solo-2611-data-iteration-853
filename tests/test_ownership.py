"""Ownership import and lifetime-manifest reconstruction.

The streams are built with the independent byte builder, then read back through
the ordinary scanner + codec, so this validates the real binary path.
"""

from __future__ import annotations

import pytest

from telber import build as B
from telber.ownership import (
    OP_ALLOC,
    OP_HANDOFF,
    OP_RELEASE,
    LifetimeManifest,
    build_manifest,
    compile_ownership_product,
    frame_from_record,
    import_bytes,
    import_stream,
)
from telber.codec import decode_record

SCHEMA_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent / "schemas" / "ownership_v1.json"


@pytest.fixture(scope="module")
def product():
    return compile_ownership_product(SCHEMA_PATH)


def frame(
    seq,
    moment,
    op,
    channel,
    handle,
    *,
    size=None,
    to_channel=None,
    to_handle=None,
):
    body = B.integer(seq, cls="context", number=0)
    body += B.integer(moment, cls="context", number=1)
    body += B.integer(op, cls="context", number=2)
    body += B.integer(channel, cls="context", number=3)
    body += B.integer(handle, cls="context", number=4)
    if size is not None:
        body += B.integer(size, cls="context", number=5)
    if to_channel is not None:
        body += B.integer(to_channel, cls="context", number=6)
    if to_handle is not None:
        body += B.integer(to_handle, cls="context", number=7)
    return B.seq(body)


def stream(*frames):
    return b"".join(frames)


COEXIST = stream(
    frame(1, 10, OP_ALLOC, 0, 0, size=2),
    frame(2, 10, OP_HANDOFF, 0, 0, to_channel=1, to_handle=0),
    frame(3, 10, OP_ALLOC, 0, 1, size=4),
    frame(4, 20, OP_RELEASE, 1, 0),
    frame(5, 20, OP_RELEASE, 0, 1),
)


def test_schema_compiles_and_has_expected_fields(product):
    assert product.schema_name == "telber.ownership"
    assert product.field_paths() == [
        "seq", "moment", "op", "channel", "handle", "size", "to_channel", "to_handle",
    ]


def test_import_reconstructs_two_lifetimes(product):
    manifest = import_bytes(COEXIST, product)
    assert manifest.complete
    assert manifest.frame_count == 5
    assert [o.id for o in manifest.occupancies] == ["occ-1", "occ-2"]
    a, b = manifest.occupancies
    assert (a.size, b.size) == (2, 4)
    # A was born first and released after B appeared: they coexisted.
    assert a.born_step == 0 and b.born_step == 2
    assert a.released_step == 3 and b.released_step == 4
    assert manifest.coexist_pairs == [("occ-1", "occ-2")]
    assert a.coexists_with == ["occ-2"] and b.coexists_with == ["occ-1"]
    assert manifest.live_at_end() == []


def test_handoff_moves_identity_and_preserves_birth(product):
    # Release the *new* handle; the identity/size/birth are unchanged by the move.
    data = stream(
        frame(1, 10, OP_ALLOC, 0, 0, size=2),
        frame(2, 10, OP_HANDOFF, 0, 0, to_channel=1, to_handle=0),
        frame(3, 10, OP_RELEASE, 1, 0),
    )
    manifest = import_bytes(data, product)
    assert manifest.complete
    (occ,) = manifest.occupancies
    assert occ.size == 2
    assert occ.born_step == 0
    assert [h["handle"] for h in occ.holders] == [0, 0]
    assert [h["channel"] for h in occ.holders] == [0, 1]
    assert occ.released_seq == 3


def test_release_through_old_handle_does_not_release_moved_identity(product):
    # After moving A from (0,0) to (1,0), releasing (0,0) is a double release;
    # A stays live and becomes a live_at_end conflict (not silently freed).
    data = stream(
        frame(1, 10, OP_ALLOC, 0, 0, size=2),
        frame(2, 10, OP_HANDOFF, 0, 0, to_channel=1, to_handle=0),
        frame(3, 10, OP_RELEASE, 0, 0),
    )
    manifest = import_bytes(data, product)
    assert not manifest.complete
    assert {c.code for c in manifest.conflicts} == {"double_release", "live_at_end"}
    assert manifest.occupancies[0].released_step is None


def test_handle_reuse_creates_a_new_identity(product):
    data = stream(
        frame(1, 10, OP_ALLOC, 0, 0, size=2),
        frame(2, 10, OP_RELEASE, 0, 0),
        frame(3, 10, OP_ALLOC, 0, 0, size=3),
        frame(4, 10, OP_RELEASE, 0, 0),
    )
    manifest = import_bytes(data, product)
    assert manifest.complete
    assert [o.id for o in manifest.occupancies] == ["occ-1", "occ-2"]
    assert [o.size for o in manifest.occupancies] == [2, 3]
    # Sequential lifetimes: they never coexisted.
    assert manifest.coexist_pairs == []


def test_failed_handoff_onto_occupied_receiver(product):
    data = stream(
        frame(1, 30, OP_ALLOC, 0, 0, size=2),
        frame(2, 30, OP_ALLOC, 1, 0, size=3),
        frame(3, 31, OP_HANDOFF, 0, 0, to_channel=1, to_handle=0),
        frame(4, 32, OP_RELEASE, 0, 0),
        frame(5, 32, OP_RELEASE, 1, 0),
    )
    manifest = import_bytes(data, product)
    assert not manifest.complete
    (conflict,) = manifest.conflicts
    assert conflict.code == "receiver_in_use"
    assert conflict.seq == 3
    # The failed move is not applied: A is still held by (0,0) and releases cleanly.
    assert manifest.occupancies[0].holders == [
        {"channel": 0, "handle": 0, "since_step": 0, "seq": 1}
    ]


def test_failed_handoff_from_unknown_holder(product):
    data = stream(
        frame(1, 10, OP_HANDOFF, 5, 5, to_channel=6, to_handle=0),
    )
    manifest = import_bytes(data, product)
    assert [c.code for c in manifest.conflicts] == ["unknown_source"]


def test_double_release_is_attributed_to_the_frame(product):
    data = stream(
        frame(1, 10, OP_ALLOC, 0, 0, size=2),
        frame(2, 10, OP_RELEASE, 0, 0),
        frame(3, 10, OP_RELEASE, 0, 0),
    )
    manifest = import_bytes(data, product)
    (conflict,) = manifest.conflicts
    assert conflict.code == "double_release"
    assert conflict.seq == 3
    assert conflict.moment == 10
    assert conflict.frame_index == 2


def test_allocate_onto_live_handle_conflicts(product):
    data = stream(
        frame(1, 10, OP_ALLOC, 0, 0, size=2),
        frame(2, 10, OP_ALLOC, 0, 0, size=5),
        frame(3, 10, OP_RELEASE, 0, 0),
    )
    manifest = import_bytes(data, product)
    assert [c.code for c in manifest.conflicts] == ["handle_in_use"]
    assert len(manifest.occupancies) == 1  # the second allocation was rejected


def test_frames_applied_in_seq_order_and_ties_by_ordinal(product):
    # Physically B's allocation comes before an earlier-sequence A release, and
    # two frames share an instant.  Sequence then ordinal decides.
    data = stream(
        frame(2, 10, OP_ALLOC, 0, 0, size=4),  # seq 2
        frame(1, 10, OP_ALLOC, 1, 0, size=2),  # seq 1 applies first
        frame(3, 10, OP_RELEASE, 0, 0),
        frame(4, 10, OP_RELEASE, 1, 0),
    )
    manifest = import_bytes(data, product)
    assert manifest.complete
    # A (seq 1) born step 0, B (seq 2) born step 1 -> coexistence respected.
    assert manifest.occupancies[0].born_seq == 1
    assert manifest.occupancies[1].born_seq == 2
    assert [e["seq"] for e in manifest.timeline] == [1, 2, 3, 4]


def test_timeline_records_live_set_after_each_frame(product):
    manifest = import_bytes(COEXIST, product)
    live_sets = [e["live"] for e in manifest.timeline]
    assert live_sets == [
        ["occ-1"],
        ["occ-1"],  # handoff keeps the same identity live
        ["occ-1", "occ-2"],
        ["occ-2"],
        [],
    ]


def test_missing_required_field_is_rejected_at_decode(product):
    # A record without `op` is invalid at the codec layer, before ownership.
    from telber.errors import DecodeError

    body = B.integer(1, cls="context", number=0) + B.integer(1, cls="context", number=1)
    bad = B.seq(body)
    with pytest.raises(DecodeError) as exc:
        import_bytes(bad, product)
    assert exc.value.code == "missing_field"


def test_manifest_roundtrips_independently_without_stream(product, tmp_path):
    manifest = import_bytes(COEXIST, product)
    path = tmp_path / "m.json"
    manifest.save(path)
    reopened = LifetimeManifest.load(path)
    assert reopened.as_dict() == manifest.as_dict()
    assert reopened.coexist_pairs == [("occ-1", "occ-2")]
    assert reopened.complete
    assert reopened.digest() == manifest.digest()


def test_import_stream_honours_block_sizes(product):
    results = []
    for block in (1, 2, 3, 5, 64):
        manifest = import_stream(__import__("io").BytesIO(COEXIST), product, block_size=block)
        results.append(manifest.as_dict())
    assert all(r == results[0] for r in results)


def test_frames_carry_byte_provenance(product):
    import io

    from telber.stream import scan_records

    frames = []
    for ordinal, sr in enumerate(scan_records(io.BytesIO(COEXIST), product)):
        record = decode_record(sr.data, product, base=sr.start)
        frames.append(frame_from_record(record, ordinal))
    assert frames[0].start == 0
    assert [f.start for f in frames] == sorted(f.start for f in frames)
    assert all(f.start < f.end for f in frames)
    manifest = build_manifest(frames)
    assert manifest.occupancies[0].born_offset == frames[0].start
