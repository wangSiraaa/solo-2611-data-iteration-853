"""Ownership-transfer import and lifetime manifest.

A single instrument hands full buffers from an acquisition channel to a
processing channel, and channel-local handles are recycled.  The wire records
(see ``schemas/ownership_v1.json``) are ordinary telemetry frames; this module
turns them into an occupancy lifetime manifest.

Occupancy model
---------------

* An **allocation** (``op=0``) creates a *new identity* with a byte ``size`` and
  an initial ``(channel, handle)`` holder.  A handle that was used before and
  released is simply a fresh identity again; identities are never reused.
* A **handoff** (``op=1``) atomically moves the *same* occupancy from the
  sender's ``(channel, handle)`` to a *free* ``(to_channel, to_handle)``.  The
  size and birth instant carry over unchanged.
* A **release** (``op=2``) frees whatever occupancy is the current holder of
  ``(channel, handle)`` at that moment.

Frames are applied in acquisition-sequence order (ties broken by moment and
stream ordinal).  Every frame after which state is observed is remembered in
the manifest timeline, so the manifest alone shows which occupancies coexisted.

Conflicts
---------

Conflicts are attributed to the frame that caused them: allocating onto a live
handle, handing off from an unknown holder, handing off onto a live receiver,
or releasing an unheld handle (a double release).  A conflict is *not* applied
to the state; the manifest is marked incomplete and no complete layout may be
produced from it.  At normal end every occupancy must have been released; a
still-live occupancy is recorded as a ``live_at_end`` conflict.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field as dc_field
from typing import Any, BinaryIO, Iterable, Mapping

from .codec import Record, decode_record
from .errors import OwnershipError, TelberError
from .schema import Product, compile_product
from .stream import DEFAULT_BLOCK_SIZE, DEFAULT_MAX_BODY, DEFAULT_MAX_RECORD, scan_records

OP_ALLOC = 0
OP_HANDOFF = 1
OP_RELEASE = 2

_OP_NAMES = {OP_ALLOC: "alloc", OP_HANDOFF: "handoff", OP_RELEASE: "release"}

MANIFEST_SCHEMA = "telber.lifetime-manifest"
MANIFEST_VERSION = 1


def op_name(op: int) -> str:
    return _OP_NAMES.get(op, f"op{op}")


# ---------------------------------------------------------------------------
# Frames


@dataclass(frozen=True)
class Frame:
    """One decoded ownership frame, with its byte provenance."""

    ordinal: int  # position in the physical stream
    seq: int
    moment: int
    op: int
    channel: int
    handle: int
    size: int | None
    to_channel: int | None
    to_handle: int | None
    start: int | None = None
    end: int | None = None

    def as_dict(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "seq": self.seq,
            "moment": self.moment,
            "op": self.op,
            "op_name": op_name(self.op),
            "channel": self.channel,
            "handle": self.handle,
            "size": self.size,
            "to_channel": self.to_channel,
            "to_handle": self.to_handle,
            "start": self.start,
            "end": self.end,
        }


def _int_field(record: Record, path: str) -> int | None:
    value = record.value_map().get(path)
    return None if value is None else value.value


def frame_from_record(record: Record, ordinal: int) -> Frame:
    """Extract an ownership frame from an already-decoded record."""
    values = record.value_map()
    missing = [p for p in ("seq", "moment", "op", "channel", "handle") if p not in values]
    if missing:
        raise OwnershipError(
            f"ownership frame {ordinal} is missing required field(s) {', '.join(missing)}",
            offset=record.start,
            code="missing_field",
        )
    return Frame(
        ordinal=ordinal,
        seq=_int_field(record, "seq"),
        moment=_int_field(record, "moment"),
        op=_int_field(record, "op"),
        channel=_int_field(record, "channel"),
        handle=_int_field(record, "handle"),
        size=_int_field(record, "size"),
        to_channel=_int_field(record, "to_channel"),
        to_handle=_int_field(record, "to_handle"),
        start=record.start,
        end=record.end,
    )


def frames_from_stream(
    fp: BinaryIO,
    product: Product,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    max_record: int = DEFAULT_MAX_RECORD,
    max_body: int = DEFAULT_MAX_BODY,
) -> list[Frame]:
    """Read a real ownership stream through the existing scanner + codec."""
    frames: list[Frame] = []
    for ordinal, stream_record in enumerate(
        scan_records(fp, product, block_size=block_size, max_record=max_record, max_body=max_body)
    ):
        record = decode_record(stream_record.data, product, base=stream_record.start)
        frames.append(frame_from_record(record, ordinal))
    return frames


# ---------------------------------------------------------------------------
# Manifest


@dataclass
class Occupancy:
    """One allocation's independent identity and lifetime."""

    id: str
    size: int
    born_step: int
    born_seq: int
    born_moment: int
    born_offset: int | None
    released_step: int | None = None
    released_seq: int | None = None
    released_moment: int | None = None
    released_offset: int | None = None
    holders: list[dict] = dc_field(default_factory=list)
    coexists_with: list[str] = dc_field(default_factory=list)

    @property
    def live_at_end(self) -> bool:
        return self.released_step is None

    def live_until(self, steps: int) -> int:
        """Exclusive end step; a leaked occupancy extends to the end of time."""
        return steps if self.released_step is None else self.released_step

    def coexists_with_other(self, other: "Occupancy", steps: int) -> bool:
        return (
            self.born_step < other.live_until(steps)
            and other.born_step < self.live_until(steps)
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "size": self.size,
            "born": {
                "step": self.born_step,
                "seq": self.born_seq,
                "moment": self.born_moment,
                "offset": self.born_offset,
            },
            "released": None
            if self.released_step is None
            else {
                "step": self.released_step,
                "seq": self.released_seq,
                "moment": self.released_moment,
                "offset": self.released_offset,
            },
            "live_at_end": self.live_at_end,
            "holders": self.holders,
            "coexists_with": list(self.coexists_with),
        }


@dataclass(frozen=True)
class Conflict:
    code: str
    message: str
    frame_index: int | None = None
    seq: int | None = None
    moment: int | None = None
    offset: int | None = None
    channel: int | None = None
    handle: int | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "frame_index": self.frame_index,
            "seq": self.seq,
            "moment": self.moment,
            "offset": self.offset,
            "channel": self.channel,
            "handle": self.handle,
        }

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> "Conflict":
        return cls(
            code=obj["code"],
            message=obj.get("message", ""),
            frame_index=obj.get("frame_index"),
            seq=obj.get("seq"),
            moment=obj.get("moment"),
            offset=obj.get("offset"),
            channel=obj.get("channel"),
            handle=obj.get("handle"),
        )


@dataclass
class LifetimeManifest:
    """An independent, reopenable lifetime manifest.

    It records every occupancy (with birth/release steps, moments and handle
    history), the explicit coexistence pairs, the per-frame timeline and any
    conflicts.  Reopening it needs neither the original bytes nor the schema.
    """

    occupancies: list[Occupancy]
    conflicts: list[Conflict]
    frame_count: int
    timeline: list[dict]
    source: str | None = None
    coexist_pairs: list[tuple[str, str]] = dc_field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.conflicts

    @property
    def steps(self) -> int:
        return self.frame_count

    def live_at_end(self) -> list[Occupancy]:
        return [o for o in self.occupancies if o.live_at_end]

    def occupancy(self, occupancy_id: str) -> Occupancy | None:
        for occ in self.occupancies:
            if occ.id == occupancy_id:
                return occ
        return None

    def by_id(self) -> dict[str, Occupancy]:
        return {o.id: o for o in self.occupancies}

    def coexisting(self) -> set[frozenset[str]]:
        return {frozenset(pair) for pair in self.coexist_pairs}

    def as_dict(self) -> dict:
        return {
            "schema": MANIFEST_SCHEMA,
            "version": MANIFEST_VERSION,
            "complete": self.complete,
            "source": self.source,
            "frame_count": self.frame_count,
            "occupancies": [o.as_dict() for o in self.occupancies],
            "coexistence_pairs": [list(p) for p in self.coexist_pairs],
            "conflicts": [c.as_dict() for c in self.conflicts],
            "timeline": self.timeline,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, sort_keys=True) + "\n"

    def digest(self) -> str:
        return hashlib.sha256(self.to_json(indent=0).encode("utf-8")).hexdigest()

    def save(self, path: str | os.PathLike[str]) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> "LifetimeManifest":
        if not isinstance(obj, Mapping) or obj.get("schema") != MANIFEST_SCHEMA:
            raise TelberError(
                "not a telber lifetime manifest", code="invalid_manifest"
            )
        occupancies = []
        for raw in obj.get("occupancies", []):
            born = raw["born"]
            released = raw.get("released")
            occupancies.append(
                Occupancy(
                    id=raw["id"],
                    size=raw["size"],
                    born_step=born["step"],
                    born_seq=born["seq"],
                    born_moment=born["moment"],
                    born_offset=born.get("offset"),
                    released_step=None if released is None else released["step"],
                    released_seq=None if released is None else released["seq"],
                    released_moment=None if released is None else released["moment"],
                    released_offset=None if released is None else released.get("offset"),
                    holders=list(raw.get("holders", [])),
                    coexists_with=list(raw.get("coexists_with", [])),
                )
            )
        return cls(
            occupancies=occupancies,
            conflicts=[Conflict.from_dict(c) for c in obj.get("conflicts", [])],
            frame_count=obj.get("frame_count", len(obj.get("timeline", []))),
            timeline=list(obj.get("timeline", [])),
            source=obj.get("source"),
            coexist_pairs=[(p[0], p[1]) for p in obj.get("coexistence_pairs", [])],
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "LifetimeManifest":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def _process_order(frames: Iterable[Frame]) -> list[Frame]:
    # Acquisition sequence number is the primary order; moment and physical
    # ordinal only break ties, so frames sharing an instant still apply one by
    # one deterministically.
    return sorted(frames, key=lambda f: (f.seq, f.moment, f.ordinal))


def build_manifest(frames: Iterable[Frame], *, source: str | None = None) -> LifetimeManifest:
    """Replay ownership frames into a lifetime manifest."""
    ordered = _process_order(frames)
    holders: dict[tuple[int, int], str] = {}
    occupancies: dict[str, Occupancy] = {}
    conflicts: list[Conflict] = []
    timeline: list[dict] = []
    next_id = 0

    def conflict(frame: Frame | None, index: int | None, code: str, message: str, **extra) -> None:
        conflicts.append(
            Conflict(
                code=code,
                message=message,
                frame_index=index,
                seq=None if frame is None else frame.seq,
                moment=None if frame is None else frame.moment,
                offset=None if frame is None else frame.start,
                channel=extra.get("channel"),
                handle=extra.get("handle"),
            )
        )

    for step, frame in enumerate(ordered):
        op = frame.op
        key = (frame.channel, frame.handle)
        if op == OP_ALLOC:
            if frame.size is None:
                conflict(frame, step, "missing_size", "allocation requires a size")
            elif frame.size < 0:
                conflict(frame, step, "bad_size", f"allocation size {frame.size} is negative")
            elif key in holders:
                conflict(
                    frame, step, "handle_in_use",
                    f"cannot allocate channel {frame.channel} handle {frame.handle}: "
                    f"already held by {holders[key]}",
                    channel=frame.channel, handle=frame.handle,
                )
            else:
                next_id += 1
                occ_id = f"occ-{next_id}"
                occ = Occupancy(
                    id=occ_id,
                    size=frame.size,
                    born_step=step,
                    born_seq=frame.seq,
                    born_moment=frame.moment,
                    born_offset=frame.start,
                    holders=[
                        {
                            "channel": frame.channel,
                            "handle": frame.handle,
                            "since_step": step,
                            "seq": frame.seq,
                        }
                    ],
                )
                occupancies[occ_id] = occ
                holders[key] = occ_id
        elif op == OP_HANDOFF:
            if frame.to_channel is None or frame.to_handle is None:
                conflict(frame, step, "missing_field", "handoff requires to_channel and to_handle")
            elif key not in holders:
                conflict(
                    frame, step, "unknown_source",
                    f"no occupancy is held by channel {frame.channel} handle {frame.handle}",
                    channel=frame.channel, handle=frame.handle,
                )
            else:
                dst = (frame.to_channel, frame.to_handle)
                if dst in holders:
                    conflict(
                        frame, step, "receiver_in_use",
                        f"cannot hand off to channel {dst[0]} handle {dst[1]}: "
                        f"receiver is held by {holders[dst]}",
                        channel=dst[0], handle=dst[1],
                    )
                else:
                    occ_id = holders.pop(key)
                    holders[dst] = occ_id
                    occupancies[occ_id].holders.append(
                        {
                            "channel": dst[0],
                            "handle": dst[1],
                            "since_step": step,
                            "seq": frame.seq,
                        }
                    )
        elif op == OP_RELEASE:
            if key not in holders:
                conflict(
                    frame, step, "double_release",
                    f"release of channel {frame.channel} handle {frame.handle}: "
                    "no occupancy is held there (already released or never held)",
                    channel=frame.channel, handle=frame.handle,
                )
            else:
                occ_id = holders.pop(key)
                occ = occupancies[occ_id]
                occ.released_step = step
                occ.released_seq = frame.seq
                occ.released_moment = frame.moment
                occ.released_offset = frame.start
        else:
            conflict(frame, step, "unknown_op", f"unknown ownership op {op}")

        frame_conflict = next(
            (c for c in reversed(conflicts) if c.frame_index == step), None
        )
        timeline.append(
            {
                "step": step,
                "seq": frame.seq,
                "moment": frame.moment,
                "op": frame.op,
                "op_name": op_name(frame.op),
                "live": sorted(holders.values()),
                "conflict": None if frame_conflict is None else frame_conflict.code,
            }
        )

    # Normal end: every occupancy must have been released.
    steps = len(ordered)
    for occ in occupancies.values():
        if occ.live_at_end:
            conflict(
                None, None, "live_at_end",
                f"occupancy {occ.id} (size {occ.size}) was never released",
            )

    # Explicit coexistence pairs, derived once from the recorded lifetimes.
    ordered_occs = list(occupancies.values())
    pairs: list[tuple[str, str]] = []
    for i in range(len(ordered_occs)):
        for j in range(i + 1, len(ordered_occs)):
            a, b = ordered_occs[i], ordered_occs[j]
            if a.coexists_with_other(b, steps):
                pairs.append((a.id, b.id))
                a.coexists_with.append(b.id)
                b.coexists_with.append(a.id)
    pairs.sort()

    return LifetimeManifest(
        occupancies=ordered_occs,
        conflicts=conflicts,
        frame_count=steps,
        timeline=timeline,
        source=source,
        coexist_pairs=pairs,
    )


def import_stream(
    fp: BinaryIO,
    product: Product,
    *,
    source: str | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    max_record: int = DEFAULT_MAX_RECORD,
    max_body: int = DEFAULT_MAX_BODY,
) -> LifetimeManifest:
    """Import a real ownership stream and build its lifetime manifest."""
    frames = frames_from_stream(
        fp,
        product,
        block_size=block_size,
        max_record=max_record,
        max_body=max_body,
    )
    return build_manifest(frames, source=source)


def import_bytes(data: bytes, product: Product, *, source: str | None = None, **kwargs) -> LifetimeManifest:
    import io

    return import_stream(io.BytesIO(data), product, source=source, **kwargs)


def import_file(path: str | os.PathLike[str], product: Product, **kwargs) -> LifetimeManifest:
    with open(path, "rb") as fh:
        return import_stream(fh, product, source=str(path), **kwargs)


def compile_ownership_product(schema_path: str | os.PathLike[str] | None = None) -> Product:
    if schema_path is None:
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "schemas",
            "ownership_v1.json",
        )
    with open(schema_path, "r", encoding="utf-8") as fh:
        return compile_product(fh.read())
