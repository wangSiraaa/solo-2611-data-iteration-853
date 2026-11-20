"""Layout planning: minimal peak, coexistence, alignment, pins, conflicts."""

from __future__ import annotations

import pytest

from telber.errors import LayoutError
from telber.layout import (
    SAMPLE_MAX_CAPACITY,
    SAMPLE_MAX_ITEMS,
    TargetSpec,
    plan_for_targets,
    plan_layout,
)
from telber.ownership import build_manifest


def _frame(ordinal, seq, moment, op, channel, handle, *, size=None, to_channel=None, to_handle=None):
    from telber.ownership import Frame

    return Frame(
        ordinal=ordinal,
        seq=seq,
        moment=moment,
        op=op,
        channel=channel,
        handle=handle,
        size=size,
        to_channel=to_channel,
        to_handle=to_handle,
        start=ordinal * 10,
        end=ordinal * 10 + 10,
    )


def manifest_of(*frames):
    return build_manifest(frames)


# The demo's coexist stream, built as frames directly (no bytes needed).
COEXIST = manifest_of(
    _frame(0, 1, 10, 0, 0, 0, size=2),
    _frame(1, 2, 10, 1, 0, 0, to_channel=1, to_handle=0),
    _frame(2, 3, 10, 0, 0, 1, size=4),
    _frame(3, 4, 20, 2, 1, 0),
    _frame(4, 5, 20, 2, 0, 1),
)

RELEASE_FIRST = manifest_of(
    _frame(0, 1, 10, 0, 0, 0, size=2),
    _frame(1, 2, 10, 1, 0, 0, to_channel=1, to_handle=0),
    _frame(2, 3, 10, 2, 1, 0),
    _frame(3, 4, 10, 0, 0, 1, size=4),
    _frame(4, 5, 20, 2, 0, 1),
)


def test_coexist_with_pin_3_fails_in_8_bytes():
    plan = plan_layout(COEXIST, TargetSpec(name="pin-3", capacity=8, pinned={"occ-1": 3}))
    assert not plan.feasible
    assert plan.reason == "insufficient_space"
    assert plan.placements == []


def test_coexist_with_pin_0_peak_is_6():
    plan = plan_layout(COEXIST, TargetSpec(name="pin-0", capacity=8, pinned={"occ-1": 0}))
    assert plan.feasible
    assert plan.peak == 6
    assert plan.placement("occ-1").start == 0
    assert plan.placement("occ-2").start == 2


def test_release_before_alloc_drops_peak_to_5_with_pin_3():
    plan = plan_layout(
        RELEASE_FIRST, TargetSpec(name="pin-3", capacity=8, pinned={"occ-1": 3})
    )
    assert plan.feasible
    assert plan.peak == 5
    assert plan.placement("occ-1").start == 3
    # they never coexisted, so B may sit at 0..4 overlapping nothing relevant
    assert plan.placement("occ-2").start == 0
    assert plan.coexisting_pairs == []


def test_same_manifest_two_targets_are_independent():
    t3 = TargetSpec(name="pin-3", capacity=8, pinned={"occ-1": 3})
    t0 = TargetSpec(name="pin-0", capacity=8, pinned={"occ-1": 0})
    fail_plan, ok_plan = plan_for_targets(COEXIST, [t3, t0])
    assert not fail_plan.feasible
    assert ok_plan.feasible and ok_plan.peak == 6
    # Re-planning for the failing target must not mutate the manifest or the
    # other plan.
    assert plan_layout(COEXIST, t0).peak == 6
    assert len(COEXIST.coexist_pairs) == 1


def test_capacity_too_small_reports_insufficient_space():
    plan = plan_layout(
        COEXIST, TargetSpec(name="small", capacity=4, pinned={"occ-1": 0})
    )
    assert not plan.feasible
    assert plan.reason == "insufficient_space"


def test_pinned_alignment_violation_is_a_conflict():
    plan = plan_layout(
        COEXIST, TargetSpec(name="mis", capacity=8, alignment=4, pinned={"occ-1": 3})
    )
    assert not plan.feasible
    assert plan.reason == "conflict"
    assert "alignment" in plan.detail


def test_pinned_overflow_is_a_conflict():
    plan = plan_layout(
        COEXIST, TargetSpec(name="over", capacity=8, pinned={"occ-1": 7})
    )
    assert not plan.feasible
    assert plan.reason == "conflict"
    assert "capacity" in plan.detail


def test_pinned_coexisting_overlap_is_a_conflict():
    # A pinned at 0 (size 2) and B pinned at 1 (size 4) overlap and coexisted.
    plan = plan_layout(
        COEXIST,
        TargetSpec(name="clash", capacity=8, pinned={"occ-1": 0, "occ-2": 1}),
    )
    assert not plan.feasible
    assert plan.reason == "conflict"
    assert "overlap" in plan.detail


def test_unknown_pin_is_rejected():
    with pytest.raises(LayoutError) as exc:
        plan_layout(COEXIST, TargetSpec(capacity=8, pinned={"occ-99": 0}))
    assert exc.value.code == "unknown_pin"


def test_alignment_leaves_a_hole():
    # P and R coexist (each size 2); Q is a later reused identity that never
    # coexists with them.  With 4-byte alignment P takes [0,2), R must take
    # [4,6), leaving [2,4) as a hole.
    manifest = manifest_of(
        _frame(0, 1, 5, 0, 0, 0, size=2),
        _frame(1, 2, 5, 0, 1, 0, size=2),
        _frame(2, 3, 6, 2, 1, 0),
        _frame(3, 4, 7, 2, 0, 0),
        _frame(4, 5, 8, 0, 0, 0, size=2),
        _frame(5, 6, 9, 2, 0, 0),
    )
    plan = plan_layout(manifest, TargetSpec(name="align4", capacity=8, alignment=4))
    assert plan.feasible
    assert plan.peak == 6
    assert (2, 4) in plan.holes
    for p in plan.placements:
        assert p.start % 4 == 0


def test_minimal_peak_is_actually_minimal():
    # Three pairwise-coexisting allocations of size 3 need two to share?  No:
    # all three coexist pairwise, so they are disjoint and need >= 9 bytes.
    manifest = manifest_of(
        _frame(0, 1, 0, 0, 0, 0, size=3),
        _frame(1, 2, 0, 0, 1, 0, size=3),
        _frame(2, 3, 0, 0, 2, 0, size=3),
        _frame(3, 4, 1, 2, 0, 0),
        _frame(4, 5, 1, 2, 1, 0),
        _frame(5, 6, 1, 2, 2, 0),
    )
    for cap in range(9, 13):
        plan = plan_layout(manifest, TargetSpec(capacity=cap))
        assert plan.feasible and plan.peak == 9
    too_small = plan_layout(manifest, TargetSpec(capacity=8))
    assert not too_small.feasible


def test_non_coexisting_allocations_may_share_addresses():
    manifest = manifest_of(
        _frame(0, 1, 0, 0, 0, 0, size=8),
        _frame(1, 2, 1, 2, 0, 0),
        _frame(2, 3, 2, 0, 0, 0, size=8),
        _frame(3, 4, 3, 2, 0, 0),
    )
    plan = plan_layout(manifest, TargetSpec(capacity=8))
    assert plan.feasible
    assert plan.peak == 8
    assert plan.placement("occ-1").start == 0
    assert plan.placement("occ-2").start == 0


def test_conflicted_manifest_yields_no_complete_plan():
    bad = manifest_of(
        _frame(0, 1, 0, 0, 0, 0, size=2),
        _frame(1, 2, 0, 0, 0, 0, size=2),  # allocate onto live handle
    )
    assert not bad.complete
    with pytest.raises(LayoutError) as exc:
        plan_layout(bad, TargetSpec(capacity=8))
    assert exc.value.code == "manifest_incomplete"


def test_sample_bounds_are_enforced():
    frames = []
    for i in range(SAMPLE_MAX_ITEMS + 1):
        frames.append(_frame(2 * i, i + 1, 0, 0, i, 0, size=1))
        frames.append(_frame(2 * i + 1, i + 1, 0, 2, i, 0))
    many = manifest_of(*frames)
    assert len(many.occupancies) == SAMPLE_MAX_ITEMS + 1
    with pytest.raises(LayoutError) as exc:
        plan_layout(many, TargetSpec(capacity=64))
    assert exc.value.code == "too_many_items"

    with pytest.raises(LayoutError) as exc:
        plan_layout(COEXIST, TargetSpec(capacity=SAMPLE_MAX_CAPACITY + 1))
    assert exc.value.code == "capacity_too_large"


def test_layout_never_overlaps_coexisting_occupancies():
    # Property: for any feasible plan, coexisting placements are disjoint.
    from telber.ownership import Frame

    def f(ordinal, seq, op, channel, handle, *, size=None, to_channel=None, to_handle=None):
        return Frame(ordinal=ordinal, seq=seq, moment=0, op=op, channel=channel,
                     handle=handle, size=size, to_channel=to_channel,
                     to_handle=to_handle)

    manifest = manifest_of(
        f(0, 1, 0, 0, 0, size=3),
        f(1, 2, 0, 0, 1, size=2),
        f(2, 3, 0, 0, 2, size=4),
        f(3, 4, 1, 0, 0, to_channel=3, to_handle=0),
        f(4, 5, 1, 0, 2, to_channel=3, to_handle=1),
        f(5, 6, 2, 3, 0),
        f(6, 7, 2, 0, 1),
        f(7, 8, 2, 3, 1),
    )
    assert manifest.complete
    assert len(manifest.coexist_pairs) == 3
    plan = plan_layout(manifest, TargetSpec(capacity=10, alignment=2))
    assert plan.feasible
    assert plan.peak == 9
    by_id = {p.id: p for p in plan.placements}
    for p in plan.placements:
        assert p.start % 2 == 0
    for a, b in plan.coexisting_pairs:
        pa, pb = by_id[a], by_id[b]
        assert pa.end <= pb.start or pb.end <= pa.start


def test_disjoint_components_pack_independently():
    # occ-1 (size 4) and occ-2 (size 3) coexist; occ-3 (size 5) is allocated
    # only after both are gone.  Two components: their peaks are independent, so
    # the overall peak is max(4+3, 5) = 7, and occ-3 may reuse address 0.
    manifest = manifest_of(
        _frame(0, 1, 0, 0, 0, 0, size=4),   # occ-1 on (0,0)
        _frame(1, 2, 0, 0, 1, 0, size=3),   # occ-2 on (1,0)
        _frame(2, 3, 2, 2, 0, 0),           # release occ-1
        _frame(3, 4, 2, 2, 1, 0),           # release occ-2
        _frame(4, 5, 5, 0, 0, 0, size=5),   # occ-3 on (0,0)
        _frame(5, 6, 6, 2, 0, 0),           # release occ-3
    )
    assert manifest.complete
    assert manifest.coexist_pairs == [("occ-1", "occ-2")]
    plan = plan_layout(manifest, TargetSpec(capacity=7))
    assert plan.feasible
    assert plan.peak == 7
    assert plan.placement("occ-3").start == 0


def test_pinned_clique_is_exact_around_the_pin():
    # Three coexisting 3-byte blocks under 4-byte alignment can only start at
    # distinct multiples of 4, so the minimal peak is 3 + 4 + 4 = 11 whether or
    # not one is pinned.  The pin must still be honoured exactly.
    manifest = manifest_of(
        _frame(0, 1, 0, 0, 0, 0, size=3),
        _frame(1, 2, 0, 0, 1, 0, size=3),
        _frame(2, 3, 0, 0, 2, 0, size=3),
        _frame(3, 4, 1, 2, 0, 0),
        _frame(4, 5, 1, 2, 1, 0),
        _frame(5, 6, 1, 2, 2, 0),
    )
    unpinned = plan_layout(manifest, TargetSpec(capacity=20, alignment=4))
    assert unpinned.feasible and unpinned.peak == 11
    pinned = plan_layout(
        manifest, TargetSpec(capacity=20, alignment=4, pinned={"occ-2": 4})
    )
    assert pinned.feasible
    assert pinned.placement("occ-2").start == 4
    assert pinned.peak == 11
    by_id = {p.id: p for p in pinned.placements}
    for p in pinned.placements:
        assert p.start % 4 == 0
    for a, b in pinned.coexisting_pairs:
        pa, pb = by_id[a], by_id[b]
        assert pa.end <= pb.start or pb.end <= pa.start


def test_pinned_clique_infeasible_when_pin_splits_too_tightly():
    # The 8-byte A@3 problem expressed as a clique against a partner.
    manifest = manifest_of(
        _frame(0, 1, 0, 0, 0, 0, size=2),
        _frame(1, 2, 0, 0, 1, 0, size=4),
        _frame(2, 3, 1, 2, 0, 0),
        _frame(3, 4, 1, 2, 1, 0),
    )
    fail = plan_layout(manifest, TargetSpec(capacity=8, pinned={"occ-1": 3}))
    assert not fail.feasible
    ok = plan_layout(manifest, TargetSpec(capacity=8, pinned={"occ-1": 0}))
    assert ok.feasible and ok.peak == 6
