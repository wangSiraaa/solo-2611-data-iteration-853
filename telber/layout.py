"""Contiguous-workspace layout planning over a lifetime manifest.

Given a lifetime manifest and a *target* (total capacity, per-item first-address
alignment granularity, and a few hardware-pinned start addresses), place every
occupancy so that:

* each allocation gets one contiguous run of integer addresses for its whole
  lifetime;
* two allocations that ever coexisted never overlap (allocations that never
  coexisted may reuse addresses);
* every start address honours the target's alignment;
* the highest occupied endpoint is as small as the capacity allows.

The planner is independent of the manifest's provenance: the same manifest can
be planned separately for two different targets, and the results do not affect
one another.  An incomplete manifest (one that recorded a conflict) yields no
complete plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Mapping

from .errors import LayoutError
from .ownership import LifetimeManifest, Occupancy

# The offline sample is deliberately bounded: at most 12 allocations and a
# workspace of at most 64 bytes.  These are enforced so an over-large input is
# reported rather than silently planned.
SAMPLE_MAX_ITEMS = 12
SAMPLE_MAX_CAPACITY = 64


@dataclass(frozen=True)
class TargetSpec:
    """One target machine's placement constraints.

    ``alignment`` is the granularity every allocation's *first* address must be
    a multiple of.  ``pinned`` maps an occupancy id to a hardware-fixed start
    address; a pinned start still has to be aligned and in range.
    """

    capacity: int
    alignment: int = 1
    name: str | None = None
    pinned: Mapping[str, int] = dc_field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "alignment": self.alignment,
            "pinned": dict(sorted(self.pinned.items())),
        }

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> "TargetSpec":
        return cls(
            capacity=int(obj["capacity"]),
            alignment=int(obj.get("alignment", 1)),
            name=obj.get("name"),
            pinned={str(k): int(v) for k, v in obj.get("pinned", {}).items()},
        )


@dataclass
class Placement:
    id: str
    size: int
    start: int
    end: int
    pinned: bool

    @property
    def addresses(self) -> range:
        return range(self.start, self.end)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "size": self.size,
            "start": self.start,
            "end": self.end,
            "pinned": self.pinned,
        }


@dataclass
class LayoutPlan:
    target: TargetSpec
    feasible: bool
    peak: int | None
    placements: list[Placement]
    holes: list[tuple[int, int]]
    coexisting_pairs: list[tuple[str, str]]
    reason: str | None = None
    detail: str | None = None

    def placement(self, occupancy_id: str) -> Placement | None:
        for p in self.placements:
            if p.id == occupancy_id:
                return p
        return None

    def as_dict(self) -> dict:
        return {
            "target": self.target.as_dict(),
            "feasible": self.feasible,
            "peak": self.peak,
            "placements": [p.as_dict() for p in self.placements],
            "holes": [list(h) for h in self.holes],
            "coexisting_pairs": [list(p) for p in self.coexisting_pairs],
            "reason": self.reason,
            "detail": self.detail,
        }


def _require_complete(manifest: LifetimeManifest) -> None:
    if not manifest.complete:
        codes = sorted({c.code for c in manifest.conflicts})
        raise LayoutError(
            "manifest recorded conflict(s) ("
            + ", ".join(codes)
            + "); a complete plan cannot be produced after a known conflict",
            code="manifest_incomplete",
        )


def _holes(placements: list[Placement], peak: int) -> list[tuple[int, int]]:
    """Gaps between occupied addresses inside ``[0, peak)``."""
    covered = sorted((p.start, p.end) for p in placements if p.size > 0)
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in covered:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < peak:
        gaps.append((cursor, peak))
    return gaps


def plan_layout(manifest: LifetimeManifest, target: TargetSpec) -> LayoutPlan:
    """Place all occupancies under ``target`` at the minimum peak endpoint."""
    _require_complete(manifest)
    if len(manifest.occupancies) > SAMPLE_MAX_ITEMS:
        raise LayoutError(
            f"manifest has {len(manifest.occupancies)} allocations; the offline "
            f"sample supports at most {SAMPLE_MAX_ITEMS}",
            code="too_many_items",
        )
    if target.capacity < 0:
        raise LayoutError("capacity must be non-negative", code="bad_target")
    if target.capacity > SAMPLE_MAX_CAPACITY:
        raise LayoutError(
            f"capacity {target.capacity} exceeds the offline sample maximum of "
            f"{SAMPLE_MAX_CAPACITY} bytes",
            code="capacity_too_large",
        )
    if target.alignment < 1:
        raise LayoutError("alignment must be at least 1", code="bad_target")

    occs = list(manifest.occupancies)
    by_id = {o.id: o for o in occs}
    coexist = manifest.coexisting()
    pairs = [(a, b) for a, b in manifest.coexist_pairs]
    unknown_pins = sorted(set(target.pinned) - set(by_id))
    if unknown_pins:
        raise LayoutError(
            "target pins unknown occupancy id(s): " + ", ".join(unknown_pins),
            code="unknown_pin",
        )

    # Static target conflicts: a pinned start that cannot be honoured at all.
    for occ_id, start in sorted(target.pinned.items()):
        occ = by_id[occ_id]
        if start < 0:
            return _infeasible(target, pairs, "conflict",
                               f"pinned start {start} for {occ_id} is negative")
        if start % target.alignment != 0:
            return _infeasible(
                target, pairs, "conflict",
                f"pinned start {start} for {occ_id} violates {target.alignment}-byte alignment",
            )
        if start + occ.size > target.capacity:
            return _infeasible(
                target, pairs, "conflict",
                f"pinned {occ_id} at {start} (size {occ.size}) exceeds capacity {target.capacity}",
            )
    for i in range(len(occs)):
        for j in range(i + 1, len(occs)):
            a, b = occs[i], occs[j]
            if frozenset((a.id, b.id)) not in coexist:
                continue
            if a.id in target.pinned and b.id in target.pinned:
                sa = target.pinned[a.id]
                sb = target.pinned[b.id]
                if sa < sb + b.size and sb < sa + a.size:
                    return _infeasible(
                        target, pairs, "conflict",
                        f"pinned occupancies {a.id} at {sa} and {b.id} at {sb} "
                        "overlap but coexisted",
                    )

    if any(o.size < 0 for o in occs):
        return _infeasible(target, pairs, "conflict", "negative occupancy size")
    if any(o.size > target.capacity for o in occs):
        offender = next(o for o in occs if o.size > target.capacity)
        return _infeasible(
            target, pairs, "insufficient_space",
            f"occupancy {offender.id} needs {offender.size} bytes but capacity is {target.capacity}",
        )

    # Allocations in different connected components of the coexistence graph
    # never coexist with each other, so each component can be packed from
    # address 0 independently; the overall peak is the largest component peak.
    assignment: dict[str, int] = {}
    for component in _components(occs, coexist):
        comp_ids = {o.id for o in component}
        comp_coexist = {
            frozenset((a, b)) for a, b in pairs if a in comp_ids and b in comp_ids
        }
        solved, starts = _solve_component(component, comp_coexist, target, manifest.steps)
        if solved is None:
            return _infeasible(
                target, pairs, "insufficient_space",
                f"no placement for the allocations in "
                f"{', '.join(sorted(comp_ids))} fits within capacity "
                f"{target.capacity} without overlapping coexisting occupancies",
            )
        assignment.update(starts)

    placements = [
        Placement(id=o.id, size=o.size, start=assignment[o.id],
                  end=assignment[o.id] + o.size,
                  pinned=o.id in target.pinned)
        for o in occs
    ]
    peak = max((p.end for p in placements), default=0)
    if peak > target.capacity:
        return _infeasible(
            target, pairs, "insufficient_space",
            f"minimum peak {peak} exceeds capacity {target.capacity}",
        )
    return LayoutPlan(
        target=target,
        feasible=True,
        peak=peak,
        placements=placements,
        holes=_holes(placements, peak),
        coexisting_pairs=list(pairs),
    )


def _components(
    occs: list[Occupancy], coexist: set[frozenset[str]]
) -> list[list[Occupancy]]:
    """Connected components of the coexistence graph."""
    by_id = {o.id: o for o in occs}
    neighbours: dict[str, set[str]] = {o.id: set() for o in occs}
    for pair in coexist:
        a, b = tuple(pair)
        neighbours[a].add(b)
        neighbours[b].add(a)
    seen: set[str] = set()
    out: list[list[Occupancy]] = []
    for occ in occs:
        if occ.id in seen:
            continue
        stack = [occ.id]
        seen.add(occ.id)
        component: list[Occupancy] = []
        while stack:
            current = stack.pop()
            component.append(by_id[current])
            for other in neighbours[current]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        out.append(component)
    return out


def _solve_component(
    component: list[Occupancy],
    coexist: set[frozenset[str]],
    target: TargetSpec,
    steps: int,
) -> tuple[int | None, dict[str, int]]:
    """Smallest peak for one connected component, plus a placement at that peak."""
    ids = [o.id for o in component]
    index = {occ_id: i for i, occ_id in enumerate(ids)}
    size = [o.size for o in component]
    n = len(component)
    conflict = [set() for _ in range(n)]
    for pair in coexist:
        a, b = tuple(pair)
        i, j = index[a], index[b]
        conflict[i].add(j)
        conflict[j].add(i)
    align = target.alignment
    pinned = target.pinned

    # Admissible lower bound: max single size, max pinned end, and the greatest
    # total size live at one instant.
    lower = max(size, default=0)
    for occ in component:
        if occ.id in pinned:
            lower = max(lower, pinned[occ.id] + occ.size)
    delta: dict[int, int] = {}
    for occ in component:
        delta[occ.born_step] = delta.get(occ.born_step, 0) + occ.size
        delta[occ.live_until(steps)] = delta.get(occ.live_until(steps), 0) - occ.size
    running = 0
    for step in sorted(delta):
        running += delta[step]
        lower = max(lower, running)

    # A clique admits no address reuse: every layout is a left-to-right order
    # of blocks.  Subset DP over that order is exact and fast.  A pin only
    # forces a block's start, which in an ordering means "the cursor must
    # already be at that pin"; pinned blocks are placed when the cursor reaches
    # them, so the DP state (set + cursor) stays small.
    if all(len(conflict[i]) == n - 1 for i in range(n)):
        return _solve_clique(component, size, align, target, lower)

    # Hardware pins are fixed, so place them first.  Every free block can then
    # slide left until it starts at 0 or flush (aligned) against the end of an
    # already placed conflicting block (pinned or free), which makes a monotone
    # start-order search over the free blocks complete.
    placed: dict[str, int] = {}
    current_peak = 0
    for occ in component:
        if occ.id not in pinned:
            continue
        start = pinned[occ.id]
        end = start + occ.size
        for other_id, other_start in placed.items():
            if frozenset((occ.id, other_id)) in coexist and (
                start < other_start + _size_of(component, other_id) and other_start < end
            ):
                return None, {}
        placed[occ.id] = start
        current_peak = max(current_peak, end)

    # Greedy upper bound (decreasing size, first aligned fit) very often equals
    # the lower bound, in which case it is already optimal.
    greedy = _greedy_place(component, conflict, target, steps)
    if greedy is not None:
        gpeak = max(greedy[o.id] + o.size for o in component)
        if gpeak <= lower and all(
            occ.id not in pinned or greedy.get(occ.id) == pinned[occ.id]
            for occ in component
        ):
            return gpeak, greedy
        best: list[int] = [gpeak, dict(greedy)]
    else:
        best = [target.capacity + 1, {}]
    if best[1]:
        # The greedy solution must also honour the fixed pins.
        for occ in component:
            if occ.id in pinned and best[1].get(occ.id) != pinned[occ.id]:
                best = [target.capacity + 1, {}]
                break

    failed: set = set()
    occ_by_id = {o.id: o for o in component}

    def candidates(occ: Occupancy, floor: int) -> list[int]:
        options = {_ceil_align(floor, align), 0}
        for other_id, other_start in placed.items():
            if frozenset((occ.id, other_id)) in coexist:
                options.add(_ceil_align(other_start + _size_of(component, other_id), align))
        out = []
        for start in sorted(options):
            if start < floor:
                continue
            end = start + occ.size
            if end >= best[0]:
                continue
            if all(
                not (start < placed[other] + _size_of(component, other)
                     and placed[other] < end)
                for other in placed
                if frozenset((occ.id, other)) in coexist
            ):
                out.append(start)
        return out

    free_ids = frozenset(o.id for o in component if o.id not in pinned)

    def recurse(remaining: frozenset[str], floor: int, peak: int) -> None:
        if peak >= best[0]:
            return
        if not remaining:
            best[0] = peak
            best[1] = dict(placed)
            return
        key = (
            frozenset(remaining),
            frozenset((k, placed[k]) for k in placed),
            floor,
        )
        if key in failed:
            return
        seen_sig = set()
        ordered = sorted(
            remaining,
            key=lambda oid: (
                -sum(1 for o in conflict[index[oid]] if ids[o] in remaining),
                -occ_by_id[oid].size,
                oid,
            ),
        )
        for occ_id in ordered:
            occ = occ_by_id[occ_id]
            signature = (
                occ.size,
                tuple(sorted((o, placed[o]) for o in placed
                             if frozenset((occ_id, o)) in coexist)),
            )
            if signature in seen_sig:
                continue
            seen_sig.add(signature)
            for start in candidates(occ, floor):
                placed[occ_id] = start
                recurse(remaining - {occ_id}, start, max(peak, start + occ.size))
                del placed[occ_id]
                if best[0] <= lower:
                    return
        failed.add(key)

    recurse(free_ids, 0, current_peak)
    if not best[1]:
        return None, {}
    return best[0], best[1]


def _size_of(component: list[Occupancy], occ_id: str) -> int:
    for occ in component:
        if occ.id == occ_id:
            return occ.size
    raise KeyError(occ_id)


def _ceil_align(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _greedy_place(
    component: list[Occupancy],
    conflict: list[set[int]],
    target: TargetSpec,
    steps: int,
) -> dict[str, int] | None:
    index = {o.id: i for i, o in enumerate(component)}
    placed: dict[str, int] = {}
    for occ in sorted(component, key=lambda o: (-o.size, o.id)):
        i = index[occ.id]
        if occ.id in target.pinned:
            start = target.pinned[occ.id]
            if start + occ.size > target.capacity or start < 0:
                return None
            if any(
                start < placed[o] + _size_of(component, o)
                and placed[o] < start + occ.size
                for o in placed
                if index[o] in conflict[i]
            ):
                return None
            placed[occ.id] = start
            continue
        start = 0
        while True:
            end = start + occ.size
            if end > target.capacity:
                return None
            if all(
                not (start < placed[o] + _size_of(component, o) and placed[o] < end)
                for o in placed
                if index[o] in conflict[i]
            ):
                placed[occ.id] = start
                break
            start += target.alignment
    return placed


def _solve_clique(
    component: list[Occupancy],
    size: list[int],
    align: int,
    target: TargetSpec,
    lower: int,
) -> tuple[int | None, dict[str, int]]:
    """Exact placement for a fully-connected (all-pairwise-coexisting) set.

    A clique admits no address reuse, so any layout is a left-to-right order of
    blocks.  Subset DP finds the order minimising the final cursor; the DP
    state is (placed set, minimal cursor), which is sufficient because a
    smaller cursor is never worse.  Pinned blocks may only be placed when the
    cursor has not passed their fixed start.
    """
    n = len(component)
    pinned = target.pinned
    INF = 10 ** 9
    full = (1 << n) - 1
    dp = [INF] * (1 << n)
    dp[0] = 0
    need = [size[i] for i in range(n)]
    pin_at = [pinned.get(component[i].id) for i in range(n)]
    for mask in range(1, 1 << n):
        best = INF
        bits = mask
        while bits:
            bit = bits & -bits
            i = bit.bit_length() - 1
            bits ^= bit
            prev = dp[mask ^ bit]
            if prev >= INF:
                continue
            pin = pin_at[i]
            if pin is not None:
                if pin < prev:
                    continue
                cursor = pin + need[i]
            else:
                cursor = _ceil_align(prev, align) + need[i]
            if cursor < best:
                best = cursor
        dp[mask] = best
    if dp[full] > target.capacity:
        return None, {}

    def cursor_after(prev: int, i: int) -> int | None:
        pin = pin_at[i]
        if pin is not None:
            return pin + need[i] if pin >= prev else None
        return _ceil_align(prev, align) + need[i]

    # Reconstruct one optimal ordering and its starts by walking the DP back.
    # ``mask`` is the set of placed blocks; the block removed here was the last
    # one placed, so the cursor before it is ``dp[prev_mask]``.
    starts: dict[str, int] = {}
    mask = full
    while mask:
        chosen = None
        chosen_prev = None
        for i in range(n):
            if not (mask >> i) & 1:
                continue
            prev = dp[mask ^ (1 << i)]
            if prev >= INF:
                continue
            if cursor_after(prev, i) == dp[mask]:
                chosen = i
                chosen_prev = prev
                break
        assert chosen is not None
        i = chosen
        pin = pin_at[i]
        start = pin if pin is not None else _ceil_align(chosen_prev, align)
        starts[component[i].id] = start
        mask ^= 1 << i
    peak = max(starts[o.id] + o.size for o in component)
    return peak, starts


def _infeasible(
    target: TargetSpec,
    pairs: list[tuple[str, str]],
    reason: str,
    detail: str,
) -> LayoutPlan:
    return LayoutPlan(
        target=target,
        feasible=False,
        peak=None,
        placements=[],
        holes=[],
        coexisting_pairs=list(pairs),
        reason=reason,
        detail=detail,
    )


def plan_for_targets(manifest: LifetimeManifest, targets: list[TargetSpec]) -> list[LayoutPlan]:
    """Plan the same manifest independently for several targets."""
    return [plan_layout(manifest, t) for t in targets]
