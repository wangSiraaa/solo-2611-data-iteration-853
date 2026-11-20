#!/usr/bin/env python3
"""Ownership / lifetime-manifest / layout demo over real synthetic bytes.

Builds the source octets with the independent builder (no codec reuse), writes
them as ``.bin`` streams, imports each through the normal scanner + codec into a
reopenable lifetime manifest, and plans the manifest onto one or more targets.

Scenarios
---------

``coexist`` (the 8-byte sample)
    A (2 bytes) is pinned at address 3; A is handed to another channel and then
    a 4-byte B is allocated while A is still live.  In an 8-byte workspace A at
    3 leaves only 3 bytes on either side, so no plan fits.  Re-planning the
    *same* manifest with A pinned at 0 yields a peak of 6.

``release_first``
    The same handoff, but A's *new* handle is released before B is allocated, so
    A and B never coexist; the peak drops to 5 with A still pinned at 3.

``reuse_alignment``
    A handle is released and re-used for a new identity; two allocations that
    coexist are laid out under a 4-byte alignment, which leaves a hole.

``conflicts``
    A handoff onto an occupied receiver and a double release.  The manifest is
    marked incomplete and the planner refuses to emit a complete plan.

Run::

    python3 -m telber.ownership_demo --out out/ownership
    python3 tools/ownership_demo.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telber import build as B  # noqa: E402
from telber.errors import LayoutError, TelberError  # noqa: E402
from telber.layout import TargetSpec, plan_layout  # noqa: E402
from telber.ownership import (  # noqa: E402
    compile_ownership_product,
    import_bytes,
)

ROOT = Path(__file__).resolve().parent.parent
OWNERSHIP_SCHEMA_PATH = ROOT / "schemas" / "ownership_v1.json"


# ---------------------------------------------------------------------------
# Independent byte construction (delegates only to telber.build)


def frame_bytes(
    seq: int,
    moment: int,
    op: int,
    channel: int,
    handle: int,
    *,
    size: int | None = None,
    to_channel: int | None = None,
    to_handle: int | None = None,
) -> bytes:
    """One ownership frame as a real BER-subset record, fields in order."""
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


def _stream(*frames: dict) -> bytes:
    return b"".join(frame_bytes(**f) for f in frames)


def sample_streams() -> dict[str, bytes]:
    """The demo's real binary streams, keyed by scenario."""

    # A allocated on acquisition channel 0/handle 0; handed to processing
    # channel 1/handle 0; a 4-byte B allocated on channel 0/handle 1 while A is
    # live; both released through their then-current holders.
    coexist = _stream(
        dict(seq=1, moment=10, op=0, channel=0, handle=0, size=2),
        dict(seq=2, moment=10, op=1, channel=0, handle=0, to_channel=1, to_handle=0),
        dict(seq=3, moment=10, op=0, channel=0, handle=1, size=4),
        dict(seq=4, moment=20, op=2, channel=1, handle=0),
        dict(seq=5, moment=20, op=2, channel=0, handle=1),
    )

    # Release A through its *new* handle first, then allocate B.
    release_first = _stream(
        dict(seq=1, moment=10, op=0, channel=0, handle=0, size=2),
        dict(seq=2, moment=10, op=1, channel=0, handle=0, to_channel=1, to_handle=0),
        dict(seq=3, moment=10, op=2, channel=1, handle=0),
        dict(seq=4, moment=10, op=0, channel=0, handle=1, size=4),
        dict(seq=5, moment=20, op=2, channel=0, handle=1),
    )

    # P and R coexist; R is released, then P, then handle (0,0) is re-used for a
    # brand-new identity Q.  Under 4-byte alignment P and R leave a hole.
    reuse_alignment = _stream(
        dict(seq=1, moment=5, op=0, channel=0, handle=0, size=2),
        dict(seq=2, moment=5, op=0, channel=1, handle=0, size=2),
        dict(seq=3, moment=6, op=2, channel=1, handle=0),
        dict(seq=4, moment=7, op=2, channel=0, handle=0),
        dict(seq=5, moment=8, op=0, channel=0, handle=0, size=2),
        dict(seq=6, moment=9, op=2, channel=0, handle=0),
    )

    # A failed handoff (receiver already held) and a double release.
    conflicts = _stream(
        dict(seq=1, moment=30, op=0, channel=0, handle=0, size=2),
        dict(seq=2, moment=30, op=0, channel=1, handle=0, size=3),
        dict(seq=3, moment=31, op=1, channel=0, handle=0, to_channel=1, to_handle=0),
        dict(seq=4, moment=31, op=0, channel=2, handle=0, size=1),
        dict(seq=5, moment=32, op=2, channel=2, handle=0),
        dict(seq=6, moment=33, op=2, channel=2, handle=0),
        dict(seq=7, moment=34, op=2, channel=0, handle=0),
        dict(seq=8, moment=34, op=2, channel=1, handle=0),
    )
    return {
        "coexist": coexist,
        "release_first": release_first,
        "reuse_alignment": reuse_alignment,
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Demo run


def run(out: Path | None = None) -> dict:
    product = compile_ownership_product(OWNERSHIP_SCHEMA_PATH)
    streams = sample_streams()
    report: dict = {
        "product_id": product.product_id,
        "schema": product.schema_name,
        "streams": {},
        "plans": [],
    }

    manifests = {}
    for name, data in streams.items():
        manifest = import_bytes(data, product, source=f"ownership/{name}.bin")
        manifests[name] = manifest
        report["streams"][name] = {
            "bytes": len(data),
            "manifest": manifest.as_dict(),
        }
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        for name, data in streams.items():
            (out / f"{name}.bin").write_bytes(data)
            manifests[name].save(out / f"{name}.manifest.json")

    # The same coexist manifest, two independent targets.
    coexist_targets = [
        TargetSpec(name="pin-3", capacity=8, alignment=1, pinned={"occ-1": 3}),
        TargetSpec(name="pin-0", capacity=8, alignment=1, pinned={"occ-1": 0}),
    ]
    release_target = TargetSpec(name="pin-3", capacity=8, alignment=1, pinned={"occ-1": 3})
    align_target = TargetSpec(name="alignment-4", capacity=8, alignment=4)

    plan_specs = [
        ("coexist", coexist_targets[0]),
        ("coexist", coexist_targets[1]),
        ("release_first", release_target),
        ("reuse_alignment", align_target),
    ]
    for scenario, target in plan_specs:
        plan = plan_layout(manifests[scenario], target)
        entry = {"scenario": scenario, "plan": plan.as_dict()}
        report["plans"].append(entry)

    # The conflicted manifest must not yield a complete plan.
    try:
        plan_layout(manifests["conflicts"], TargetSpec(name="x", capacity=8, alignment=1))
        report["conflict_plan"] = {"refused": False}
    except LayoutError as exc:
        report["conflict_plan"] = {"refused": True, **exc.as_dict()}

    if out is not None:
        (out / "plans.json").write_text(
            json.dumps(report["plans"], indent=2, default=str) + "\n", encoding="utf-8"
        )
        (out / "report.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    return report


def _print(report: dict) -> None:
    for name, entry in report["streams"].items():
        manifest = entry["manifest"]
        print(f"== stream {name}: {entry['bytes']} bytes, "
              f"{manifest['frame_count']} frames, complete={manifest['complete']}")
        for occ in manifest["occupancies"]:
            rel = occ["released"]
            print(
                f"   {occ['id']} size={occ['size']} "
                f"born@step{occ['born']['step']} "
                f"released@{'live' if rel is None else 'step' + str(rel['step'])} "
                f"coexists_with={occ['coexists_with']}"
            )
        for c in manifest["conflicts"]:
            print(f"   conflict[{c['code']}] seq={c['seq']}: {c['message']}")
    print()
    for entry in report["plans"]:
        plan = entry["plan"]
        target = plan["target"]["name"]
        if plan["feasible"]:
            layout = ", ".join(
                f"{p['id']}@[{p['start']},{p['end']})" for p in plan["placements"]
            )
            print(f"== plan {entry['scenario']} / target {target}: peak={plan['peak']} "
                  f"holes={plan['holes']}  {layout}")
        else:
            print(f"== plan {entry['scenario']} / target {target}: FAILED "
                  f"({plan['reason']}): {plan['detail']}")
    print()
    cp = report["conflict_plan"]
    if cp.get("refused"):
        print(f"== layout of conflicted manifest refused: {cp['code']}: {cp['message']}")
    else:
        print("== ERROR: conflicted manifest produced a plan")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write streams/manifests/plans here")
    args = parser.parse_args(argv)
    try:
        report = run(args.out)
    except TelberError as exc:
        print(json.dumps(exc.as_dict(), indent=2), file=sys.stderr)
        return 2
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
