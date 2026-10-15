from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

pq = pytest.importorskip("pyarrow.parquet")
pa = pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from crawler.catalog_export import combine_catalog
from crawler.exporter import _normalize
from crawler.presentation import present_row
from test_presentation import corner_row


def test_catalog_normalizes_legacy_copied_labels_without_rewriting_source(tmp_path):
    original = corner_row()
    before = deepcopy(original)
    # Explicitly construct an old export with the stale display label.
    legacy = _normalize(original)
    legacy["record_json"] = json.dumps(original)
    legacy["configuration_json"] = json.dumps(original["configuration"])
    legacy["attr:roundedCorners"] = json.dumps(
        original["configuration"]["roundedCorners"]
    )
    direct = _normalize(corner_row("direct_quote"))
    source = tmp_path / "product.parquet"
    pq.write_table(pa.Table.from_pylist([legacy, direct]), source)
    raw_bytes = source.read_bytes()
    manifest = combine_catalog(
        [
            {
                "row_count": 2,
                "items": [{"name": "combined.parquet", "path": str(source)}],
            }
        ],
        tmp_path / "catalog",
    )
    assert manifest["row_count"] == 2
    outputs = {Path(item["path"]).suffix: item["path"] for item in manifest["items"]}
    parquet_rows = pq.read_table(outputs[".parquet"]).to_pylist()
    with open(outputs[".csv"], newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert json.loads(parquet_rows[0]["record_json"]) == present_row(original)
    assert json.loads(parquet_rows[0]["configuration_json"]) == present_row(original)["configuration"]
    assert json.loads(parquet_rows[0]["attr:roundedCorners"])["label"] == "Round Corners"
    assert json.loads(parquet_rows[1]["record_json"]) == corner_row("direct_quote")
    assert parquet_rows[1]["subtotal"] == direct["subtotal"]
    assert parquet_rows[1]["attr:roundedCorners"] == direct["attr:roundedCorners"]
    assert csv_rows[0]["attr:roundedCorners"] == "Round Corners"
    assert csv_rows[1]["attr:roundedCorners"] == corner_row("direct_quote")["configuration"]["roundedCorners"]["label"]
    assert csv_rows[1]["subtotal"] == direct["subtotal"]
    assert "record_json" not in csv_rows[0]
    assert source.read_bytes() == raw_bytes
    assert original == before


def test_catalog_legacy_projection_streams_past_one_batch(tmp_path):
    rows = []
    for index in range(1025):
        original = {**corner_row(), "id": str(index)}
        legacy = _normalize(original)
        legacy["record_json"] = json.dumps(original)
        legacy["configuration_json"] = json.dumps(original["configuration"])
        legacy["attr:roundedCorners"] = json.dumps(
            original["configuration"]["roundedCorners"]
        )
        rows.append(legacy)
    source = tmp_path / "legacy.parquet"
    pq.write_table(pa.Table.from_pylist(rows), source)
    manifest = combine_catalog(
        [
            {
                "row_count": len(rows),
                "items": [{"name": "combined.parquet", "path": str(source)}],
            }
        ],
        tmp_path / "catalog",
    )
    output = next(
        item["path"] for item in manifest["items"] if item["name"] == "catalog.parquet"
    )
    projected = pq.read_table(output, columns=["record_json"]).column(0).to_pylist()
    assert len(projected) == 1025
    assert {json.loads(row)["id"] for row in projected} == {
        str(index) for index in range(1025)
    }
    assert all(
        json.loads(row)["configuration"]["roundedCorners"]["label"] == "Round Corners"
        for row in projected
    )


def test_catalog_corrected_rows_bypass_python_projection(tmp_path, monkeypatch):
    import crawler.catalog_export as catalog

    source = tmp_path / "corrected.parquet"
    normalized = _normalize(corner_row())
    assert "+$3.00" in normalized["attr:roundedCorners"]  # retained source_label
    pq.write_table(pa.Table.from_pylist([normalized]), source)

    def unexpected_projection(row):
        raise AssertionError(
            "Already-corrected labels must stay on the native copy path"
        )

    monkeypatch.setattr(catalog, "_normalize", unexpected_projection)
    manifest = catalog.combine_catalog(
        [
            {
                "row_count": 1,
                "items": [{"name": "combined.parquet", "path": str(source)}],
            }
        ],
        tmp_path / "catalog",
    )
    output = next(
        item["path"] for item in manifest["items"] if item["name"] == "catalog.parquet"
    )
    assert pq.read_table(output).to_pylist() == [normalized]
