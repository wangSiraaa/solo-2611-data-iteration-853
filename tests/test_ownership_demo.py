"""End-to-end: real ownership bytes -> manifest -> layout, via CLI and demo."""

from __future__ import annotations

import json

import pytest

from telber.cli import main as cli_main
from telber.layout import TargetSpec
from telber.ownership import compile_ownership_product, import_bytes
from telber.ownership_demo import OWNERSHIP_SCHEMA_PATH, run, sample_streams


@pytest.fixture(scope="module")
def product():
    return compile_ownership_product(OWNERSHIP_SCHEMA_PATH)


def test_real_stream_to_manifest_to_layout(product):
    data = sample_streams()["coexist"]
    manifest = import_bytes(data, product)
    assert manifest.complete
    assert manifest.coexist_pairs == [("occ-1", "occ-2")]

    fail = __import__("telber.layout", fromlist=["plan_layout"]).plan_layout(
        manifest, TargetSpec(capacity=8, pinned={"occ-1": 3})
    )
    assert not fail.feasible

    ok = __import__("telber.layout", fromlist=["plan_layout"]).plan_layout(
        manifest, TargetSpec(capacity=8, pinned={"occ-1": 0})
    )
    assert ok.feasible and ok.peak == 6


def test_demo_run_reports_all_scenarios():
    report = run(None)
    assert set(report["streams"]) == {
        "coexist", "release_first", "reuse_alignment", "conflicts"
    }
    assert report["streams"]["coexist"]["manifest"]["complete"] is True
    assert report["streams"]["conflicts"]["manifest"]["complete"] is False
    assert report["conflict_plan"]["refused"] is True

    by_key = {(e["scenario"], e["plan"]["target"]["name"]): e["plan"] for e in report["plans"]}
    assert not by_key[("coexist", "pin-3")]["feasible"]
    assert by_key[("coexist", "pin-0")]["peak"] == 6
    assert by_key[("release_first", "pin-3")]["peak"] == 5
    reuse = by_key[("reuse_alignment", "alignment-4")]
    assert reuse["feasible"] and [2, 4] in reuse["holes"]


def test_cli_generate_import_layout(tmp_path, capsys):
    out = tmp_path / "streams"
    assert cli_main(["generate-ownership", str(out)]) == 0
    assert (out / "coexist.bin").exists()
    assert (out / "conflicts.bin").exists()

    manifest_path = tmp_path / "coexist.manifest.json"
    assert cli_main(["ownership-import", str(out / "coexist.bin"), "-o", str(manifest_path)]) == 0
    manifest = json.loads(manifest_path.read_text())
    assert manifest["complete"] is True
    assert manifest["coexistence_pairs"] == [["occ-1", "occ-2"]]

    target = tmp_path / "target0.json"
    target.write_text(json.dumps({"name": "pin-0", "capacity": 8, "pinned": {"occ-1": 0}}))
    plans_path = tmp_path / "plans.json"
    assert cli_main(["layout", str(manifest_path), str(target), "-o", str(plans_path)]) == 0
    (plan,) = json.loads(plans_path.read_text())
    assert plan["feasible"] and plan["peak"] == 6

    # A failing target makes the layout command exit non-zero but still prints.
    target3 = tmp_path / "target3.json"
    target3.write_text(json.dumps({"name": "pin-3", "capacity": 8, "pinned": {"occ-1": 3}}))
    assert cli_main(["layout", str(manifest_path), str(target3), "-o", str(plans_path)]) == 1
    (plan,) = json.loads(plans_path.read_text())
    assert plan["feasible"] is False


def test_cli_ownership_import_reports_conflicts(tmp_path):
    (tmp_path / "bin").mkdir()
    from telber.ownership_demo import sample_streams as streams

    bad = tmp_path / "conflicts.bin"
    bad.write_bytes(streams()["conflicts"])
    out = tmp_path / "m.json"
    rc = cli_main(["ownership-import", str(bad), "-o", str(out)])
    assert rc == 3
    manifest = json.loads(out.read_text())
    codes = {c["code"] for c in manifest["conflicts"]}
    assert {"receiver_in_use", "double_release"} <= codes
    # And the manifest cannot be planned.
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"capacity": 8}))
    assert cli_main(["layout", str(out), str(target)]) == 2


def test_legacy_telemetry_cli_still_works(tmp_path, capsys):
    # compile -> encode -> ingest -> report, the original pipeline, unchanged.
    from telber.demo import SCHEMA_PATH

    product_path = tmp_path / "product.json"
    assert cli_main(["compile", str(SCHEMA_PATH), "-o", str(product_path)]) == 0
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"station_id": 1, "observed_at": 2,
                                  "inner": {"a": 3}, "sensor": {"kind": 4}}))
    record_path = tmp_path / "record.bin"
    assert cli_main(["encode", str(product_path), str(values), "-o", str(record_path)]) == 0
    assert cli_main(["check", str(product_path), str(record_path)]) == 0
