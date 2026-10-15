import csv
import json
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from crawler.exporter import export_rows, query_price_analytics


def fixture_rows(count=200):
    for index in range(count):
        config = {"stock": "Matte, aqueous", "coating": "", "size": "3.5 x 2"}
        if index == count - 1:
            config["late attribute"] = "line one\nline two"
        yield {
            "product_url": f"https://fixture.test/product/{index % 40}",
            "source_product_id": str(index % 40),
            "title": f"Product {index % 40}",
            "quantity": index + 1,
            "currency": "USD",
            "subtotal": "19.99",
            "unit_price": "0.01999000",
            "configuration": config,
            "verification_status": "provisional",
            "provenance": {"method": "table", "row": index},
            "evidence_ids": [f"e{index}"],
        }


def test_streamed_export_preserves_dynamic_attributes_provenance_and_counts(tmp_path):
    manifest = export_rows(fixture_rows(201), tmp_path, batch_size=7)
    assert manifest["row_count"] == 201
    assert manifest["product_count"] == 40
    combined = next(
        item for item in manifest["items"] if item["name"] == "combined.parquet"
    )
    table = pq.read_table(combined["path"])
    assert table.num_rows == 201
    assert pa.types.is_string(table.schema.field("subtotal").type)
    assert table.column("subtotal")[0].as_py() == "19.99"
    assert table.column("attr:coating")[0].as_py() == ""
    assert table.column("attr:late attribute")[200].as_py() == "line one\nline two"
    assert json.loads(table.column("provenance_json")[200].as_py())["row"] == 200
    parquet_count = sum(
        pq.ParquetFile(item["path"]).metadata.num_rows
        for item in manifest["items"]
        if item["format"] == "parquet" and item["product_key"]
    )
    assert parquet_count == 201
    csv_path = next(
        item["path"] for item in manifest["items"] if item["name"] == "combined.csv"
    )
    with open(csv_path, newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert csv_rows[-1]["attr:late attribute"] == "line one\nline two"
    assert all(
        Path(item["path"]).stat().st_size == item["size"] for item in manifest["items"]
    )
    assert json.loads((tmp_path / "manifest.json").read_text()) == manifest
    assert not list(tmp_path.glob(".building-*"))


def test_failed_replacement_keeps_previous_published_manifest(tmp_path):
    previous = export_rows(fixture_rows(2), tmp_path)

    def failed_input():
        yield next(fixture_rows(1))
        raise RuntimeError("fixture source interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        export_rows(failed_input(), tmp_path)
    assert json.loads((tmp_path / "manifest.json").read_text()) == previous
    assert all(Path(item["path"]).exists() for item in previous["items"])
    assert not list(tmp_path.glob(".building-*"))
    replacement = export_rows(fixture_rows(3), tmp_path)
    assert replacement["generation"] != previous["generation"]
    assert replacement["row_count"] == 3
    assert all(Path(item["path"]).exists() for item in previous["items"])


def test_empty_export_has_readable_schema_and_no_completeness_claim(tmp_path):
    manifest = export_rows(iter(()), tmp_path)
    parquet = next(
        item["path"] for item in manifest["items"] if item["format"] == "parquet"
    )
    assert pq.read_table(parquet).num_rows == 0
    assert manifest["row_count"] == 0
    assert "complete" not in manifest


def test_url_identity_prevents_cross_site_id_collision_and_path_traversal(tmp_path):
    row = next(fixture_rows(1))
    rows = [
        row,
        {**row, "product_url": "https://other.test/a", "title": "../../escape"},
    ]
    manifest = export_rows(rows, tmp_path)
    assert manifest["product_count"] == 2
    assert all(
        Path(item["path"]).is_relative_to(tmp_path) for item in manifest["items"]
    )


def test_nonnumeric_price_prevents_publication_instead_of_looking_valid(tmp_path):
    row = next(fixture_rows(1))
    with pytest.raises(ValueError):
        export_rows([{**row, "subtotal": "unknown"}], tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_duckdb_analytics_keeps_currencies_separate(tmp_path):
    pytest.importorskip("duckdb")
    row = next(fixture_rows(1))
    manifest = export_rows(
        [row, {**row, "currency": "CAD", "subtotal": "29.99"}], tmp_path
    )
    parquet = next(
        item["path"] for item in manifest["items"] if item["name"] == "combined.parquet"
    )
    result = query_price_analytics(parquet)
    assert len(result) == 2
    assert {row["currency"] for row in result} == {"USD", "CAD"}
    assert all(row["row_count"] == 1 for row in result)


def test_single_product_reuses_combined_pair_without_duplicate_serialization(tmp_path):
    row = next(fixture_rows(1))
    manifest = export_rows([row], tmp_path)
    assert len(manifest["items"]) == 2
    assert all(item["product_key"] == row["product_url"] for item in manifest["items"])
    assert {item["name"] for item in manifest["items"]} == {
        "combined.csv",
        "combined.parquet",
    }


def test_exported_configuration_and_record_share_copy_only_label_projection(tmp_path):
    from copy import deepcopy
    from crawler.presentation import present_row
    from test_presentation import corner_row

    original, direct = corner_row(), corner_row("direct_quote")
    before = deepcopy(original)
    manifest = export_rows([original, direct], tmp_path)
    parquet = next(
        item["path"] for item in manifest["items"] if item["name"] == "combined.parquet"
    )
    csv_path = next(
        item["path"] for item in manifest["items"] if item["name"] == "combined.csv"
    )
    rows = pq.read_table(parquet).to_pylist()
    with open(csv_path, newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert json.loads(rows[0]["record_json"]) == present_row(original)
    assert json.loads(rows[0]["configuration_json"]) == present_row(original)["configuration"]
    assert json.loads(rows[0]["attr:roundedCorners"])["label"] == "Round Corners"
    assert json.loads(rows[1]["record_json"]) == direct
    assert csv_rows[0]["attr:roundedCorners"] == "Round Corners"
    assert csv_rows[1]["attr:roundedCorners"] == direct["configuration"]["roundedCorners"]["label"]
    assert not any(key.endswith("_json") for key in csv_rows[0])
    assert original == before
