# telber — BER-subset telemetry directory, codec and fact repository

A Python 3.11 implementation of a *finite* BER-style type directory and
telemetry fact repository. This is a deliberately bounded subset, not a
general ASN.1 toolkit.

The motivating failures this fixes:

* **`00 00` inside a primitive body is data, not end-of-message.** The earlier
  ingestion treated every `00 00` as a record terminator and truncated legal
  binary fields. Here `00 00` terminates only an indefinite-length constructed
  layer, and only when it sits at that layer's child boundary.
* **Long tags and implicit tags were mapped to the wrong field.** Tags are
  decoded with class + constructed bit + base-128 number, and the schema
  compiler pre-computes each field's exact wire tag (including the constructed
  bit that explicit wrapping adds), so a long tag or an implicit tag can never
  be silently matched to the wrong field.

## Supported wire subset

* **Identifier octets**: class (`universal`/`application`/`context`/`private`),
  constructed bit, tag number in short form (`0..30`) and base-128 long form
  (`31..2^63-1`). Long-form tags must be minimal: no leading `0x80` padding and
  no long form for a number that fits the short form.
* **Length octets**: short form (`0..127`), long form with 1..4 octets, and
  indefinite form `0x80` for constructed values only. Long-form lengths must be
  minimal (no leading zero, short form used whenever it fits).
* **Base types**: `INTEGER` (shortest two's-complement big-endian body),
  `OCTET STRING`, `NULL` (length must be 0), and `SEQUENCE`.
* **End-of-contents**: `00 00` closes an indefinite constructed layer only. A
  definite-length layer is bounded by its declared length and is never ended
  early by `00 00`; bytes inside a primitive body are never inspected for EOC.
* Records are a stream of consecutive top-level TLVs; each record is decoded
  whole, and trailing bytes after a record are an error rather than silently
  folded into the next record.

Anything outside this subset (other universal types, indefinite primitive
strings, non-minimal encodings) is rejected with an absolute byte offset.

## Schema directory

The directory is JSON with a small, strict shape (`schemas/telemetry_v1.json`):

```json
{
  "schema": "telemetry.record",
  "version": 1,
  "root": {
    "name": "TelemetryRecord",
    "tag": {"class": "universal", "number": 16},
    "type": "SEQUENCE",
    "fields": [
      {"name": "station_id", "tag": {"class": "context", "number": 0},
       "mode": "implicit", "type": "INTEGER"},
      {"name": "note", "tag": {"class": "context", "number": 2},
       "mode": "explicit", "type": "NULL", "optional": true},
      {"name": "sensor", "tag": {"class": "application", "number": 1000},
       "mode": "implicit", "type": "SEQUENCE", "fields": [ ... ]}
    ]
  }
}
```

Each field declares an ordered name, a tag class/number, `explicit` or
`implicit`, a base type and `optional`. `constructed` is **derived**, never
declared: an explicit field is always a constructed wrapper; an implicit
`SEQUENCE` keeps the constructed bit; all other implicit fields are primitive.
This removes the class of bug where the same tag is read both ways.

Compilation is strict and deterministic:

* unknown keys, unsupported types, duplicate field names, duplicate wire tags
  and over-deep nesting are rejected;
* **ambiguous optionals are rejected at compile time**: if any two fields in
  one SEQUENCE share a wire tag (in particular a run of adjacent optionals
  followed by a field repeating an earlier optional's tag), presence/identity
  would be undecidable at decode time, so the schema does not compile. The
  decoder never "picks the first plausible" match;
* the result is an immutable, content-addressed `Product` (SHA-256 of the
  canonical schema). Publishing a revision is a *new* product id; existing
  imports keep the product they were bound to.

Matching semantics: fields are matched in declaration order against the next
header. An optional field is present iff the next header's wire tag equals its
pre-computed wire tag; otherwise it is skipped. A required field whose tag does
not match is an error at that header's offset — it is not skipped in search of a
later match. An explicit field must contain exactly one complete value with the
declared universal inner tag. An implicit field's body is interpreted as the
declared base type.

## Modules

| module | responsibility |
| --- | --- |
| `telber/ber.py` | tag/length parsing with minimality and EOC rules |
| `telber/schema.py` | strict schema compiler, ambiguity checks, match table |
| `telber/codec.py` | record decoder/encoder with absolute byte provenance |
| `telber/stream.py` | bounded streaming delimitation over the original octets |
| `telber/repository.py` | SQLite product registry + per-record fact relation store |
| `telber/service.py` | independent compile service: artifact, check, encode |
| `telber/ownership.py` | ownership-transfer import → reopenable lifetime manifest |
| `telber/layout.py` | minimal-peak contiguous workspace planner over a manifest |
| `telber/build.py` | independent byte builder used only by tests/demo |
| `telber/demo.py`, `telber/ownership_demo.py`, `telber/cli.py` | demos and CLI |

Two source modules carry the core: `schema.py` + `codec.py` compile and
interpret the directory, and `stream.py` + `repository.py` produce the streaming
relation table. The compiled prediction table (`Product.match_table()`) drives
optional presence decisions, nested boundaries and error offsets.

## Ownership and lifetime manifests

A separate directory (`schemas/ownership_v1.json`) describes buffer
ownership-transfer frames. It reuses the *same* schema compiler, codec and
scanner as ordinary telemetry; only the field layout is new. Each frame has an
operation:

* **allocate** (`op=0`, `size` present) creates a *new identity* with a byte
  size and an initial channel-local handle `(channel, handle)`;
* **handoff** (`op=1`, `to_channel`/`to_handle`) atomically moves the same
  occupancy from the sender's handle to a *free* receiver handle — size and
  birth instant carry over;
* **release** (`op=2`) frees whatever occupancy is the *current* holder of
  `(channel, handle)`.

Frames are applied in acquisition order (`seq`, tie-broken by `moment` then
stream ordinal), one at a time. `telber.ownership.import_stream/import_bytes/
import_file` read real bytes through `scan_records` + `decode_record` and build
a `LifetimeManifest`:

* every occupancy with birth/release steps, moments, absolute byte offsets and
  its handle history;
* **coexistence pairs**, derived once from the recorded lifetimes (the manifest
  alone shows which occupancies ever coexisted — no source bytes needed);
* a per-frame `timeline` with the live set after each frame;
* conflicts, each attributed to the offending frame: `handle_in_use` (allocate
  onto a live handle), `unknown_source`, `receiver_in_use` (handoff onto an
  occupied receiver), `double_release`, and `live_at_end` (a normal end that
  leaves an occupancy unreleased).

A conflict is recorded but not applied. A manifest with any conflict is
`complete == False`; the planner then refuses to emit a complete plan. A
manifest round-trips through JSON independently of the original stream.

## Workspace layout

`telber.layout.plan_layout(manifest, TargetSpec(capacity, alignment, pinned))`
places every occupancy so that:

* each allocation keeps one contiguous run of integer addresses for its whole
  lifetime;
* two allocations that *ever coexisted* never overlap (non-coexisting ones may
  reuse addresses);
* every start honours the target's `alignment`; `pinned` starts are honoured
  exactly (a pinned start still has to be aligned and in range);
* the highest occupied endpoint (`peak`) is **minimal**.

The same manifest can be planned independently for several targets
(`plan_for_targets`); plans do not affect one another. A layout is exact: it
decomposes the coexistence graph into connected components (independent, each
packed from 0), solves dense cliques with a subset DP, and otherwise runs a
canonical branch-and-bound with a greedy upper bound and an admissible
concurrency lower bound. On failure it reports `conflict` (a pin that cannot be
honoured / overlaps a coexisting pin) or `insufficient_space`, with a detail
string. The offline sample is bounded to **≤ 12 allocations** and **≤ 64 bytes**
of capacity; larger inputs are rejected rather than silently planned.

## Bounds

* nesting depth 32+ supported (compile and stream both bounded; default cap 64)
* 100 fields per SEQUENCE (cap 4096)
* 100 MiB streams processed with bounded memory (one record + read buffer)
* records up to 1 MiB by default (`max_record`, raise for larger)
* primitive bodies buffered up to 2 MiB (`max_body`); larger bodies are rejected
  before copy

The block size is I/O granularity only and never changes offsets or results.

## Fact repository

## Fact repository

`Repository` binds each import to one product id at creation. Ingest walks the
original stream and writes, per record:

* `records` — ordinal, absolute start/end, length;
* `fields` — one row per declared field path (absent optionals have
  `present=0`), type, mode, presence, null flag, decoded INTEGER/OCTET STRING,
  and absolute offsets of the complete encoding and value body;
* `field_source_ranges` — role-tagged byte ranges (`encoding` for an implicit
  field's full TLV, `wrapper`/`inner` for an explicit field, and the primitive
  body range).

On the first top-level error the import stops. Completed records stay
committed as a **partial** import with the error code, absolute offset and the
failing record's start offset. A partial import cannot be published as
whole-stream complete. There is no resynchronisation: the scanner never scans
forward for a plausible tag.

## Running

```bash
python3 -m telber.demo                 # end-to-end demo, prints relation table
python3 -m telber.demo --with-truncation
python3 tools/demo.py --out out/       # writes product.json, telemetry.bin, DB, relation.json
python3 -m telber.cli compile schemas/telemetry_v1.json -o product.json
python3 -m telber.cli check product.json record.bin
python3 -m telber.cli encode product.json values.json -o record.bin   # {"$hex": "00ff"} for bytes
python3 -m telber.cli ingest product.json telemetry.bin --db facts.sqlite
python3 -m telber.cli report facts.sqlite --import-id 1 --relation
```

Ownership / lifetime / layout (the `data` is a real synthetic byte stream):

```bash
python3 -m telber.ownership_demo                 # all sample scenarios + plans
python3 -m telber.ownership_demo --out out/       # writes *.bin, *.manifest.json, plans.json
python3 tools/ownership_demo.py                   # same, discoverable entry point

python3 -m telber.cli generate-ownership out/own  # write the demo .bin streams
python3 -m telber.cli ownership-import out/own/coexist.bin -o manifest.json
# target.json: {"name": "pin-3", "capacity": 8, "pinned": {"occ-1": 3}}
python3 -m telber.cli layout manifest.json target.json
```

`ownership-import` writes a manifest and exits `3` when it recorded conflicts
(a valid artifact, not a clean run). `layout` exits `1` when a target cannot
hold the manifest and `2` when the manifest itself has a conflict.

## Tests

```bash
python3 -m pytest tests -q
TELBER_FULL_SCALE=1 python3 -m pytest tests/test_scale.py -q   # 100 MiB stream
```

Coverage highlights (`tests/`):

* `test_ber_wire.py` — base-128 long tags, minimal tag/length, indefinite
  primitive rejection;
* `test_schema.py` — ambiguity rejection, unknown keys, depth/field limits,
  product revision identity;
* `test_codec.py` — independently built records: negative integer boundaries,
  `00 00` inside primitive and definite constructed bodies, optional-vs-null,
  explicit multi-value rejection, implicit interpretation, source ranges;
* `test_stream.py` — block-size invariance (1..4096), truncation prefix
  stability, bounded memory, EOC at the correct level, no resync;
* `test_repository.py` — relation rows, byte provenance slices, partial imports,
  product-revision binding, persistence;
* `test_service.py` — artifact recompilation, presence/type preservation for
  definite and indefinite encodings;
* `test_demo_truth.py` — checks the demo's relation table against an
  independently derived presence map and byte ranges;
* `test_ownership.py` — real-byte ownership import: handoff moves an identity,
  release targets the current holder, handle reuse creates a new identity, and
  failed handoff / double release / allocate-on-live / live-at-end are flagged
  on the offending frame; manifest round-trip;
* `test_layout.py` — minimal peak, coexistence disjointness, non-coexisting
  reuse, alignment holes, pins, disjoint components, conflicted-manifest refusal,
  and the ≤12/≤64 sample bounds;
* `test_ownership_demo.py` — real stream → manifest → layout end-to-end, the
  eight-byte sample scenarios, CLI, and the unchanged legacy telemetry CLI;
* `test_scale.py` — depth 32, 100 MiB stream, 2 MiB body cap (opt-in).
