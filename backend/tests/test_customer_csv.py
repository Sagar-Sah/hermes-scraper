import csv
import json
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq

from crawler.customer_csv import LABEL_UNAVAILABLE, audit_record, customer_attribute_columns, customer_row
from crawler.catalog_export import combine_catalog
from crawler.exporter import export_rows


def record():
    return {
        "product_url": "https://fixture.test/cards", "title": "Business Cards",
        "source_product_id": "supplier-product-87654", "product_id": "internal-product-98765",
        "sku": "supplier-sku-56789", "id": "internal-record-76543",
        "quantity": 250, "currency": "USD", "subtotal": "15.00", "unit_price": "0.06000000",
        "verification_status": "pending", "evidence_ids": ["private-evidence-65432"],
        "configuration": {
            "paper": {"value": "87654321", "label": "16 pt. Cardstock"},
            "finish": {"value": "76543210"}, "empty": {"value": "", "label": "None"},
            "width": 2.5, "count": 4, "thickness": "0.02", "sku": "option-sku-45678",
            "dimensions": {"height": 3.5, "unit": "in", "sourceId": "dimension-id-54321"},
        },
        "production": {"value": "65432109", "label": "Regular 2–4 Business Days", "status": "source_displayed"},
        "pricing_context": {"source_base_product_id": "base-id-43210", "secret_context": "context-32109"},
        "service_context_observed": True,
        "service_context": [{"key": "proofType", "control_count": 2, "checked_count": 1,
            "selection_status": "selected", "selected": [
                {"value": "54321098", "checked": True, "label": "Manually Processed (+$3.00)"}]}],
    }


def test_customer_projection_uses_labels_and_preserves_numeric_user_attributes():
    original = record()
    before = deepcopy(original)
    row = customer_row(original)
    assert row["attr:paper"] == "16 pt. Cardstock"
    assert row["attr:finish"] == LABEL_UNAVAILABLE
    assert row["attr:empty"] == "None"
    assert row["attr:width"] == "2.5" and row["attr:count"] == "4" and row["attr:thickness"] == "0.02"
    assert row["attr:dimensions.height"] == "3.5" and row["attr:dimensions.unit"] == "in"
    assert row["production_label"] == "Regular 2–4 Business Days"
    assert row["service:proofType"] == "Manually Processed (+$3.00)"
    assert row["service_status:proofType"] == "selected"
    assert row["service_status:fileDeliveryForIop"] == "absent"
    assert "attr:sku" not in row and "attr:dimensions.sourceId" not in row
    assert original == before


def test_column_discovery_matches_nested_customer_attributes_without_pricing_projection():
    original = record()
    expected = {key for key in customer_row(original) if key.startswith("attr:")}
    # Discovery is structural. Invalid amounts remain an output validation
    # failure, but cannot require a complete service/price projection per pass.
    original["subtotal"] = "not a price"
    assert set(customer_attribute_columns(original)) == expected


def test_standard_csvs_omit_identifiers_while_parquet_preserves_audit(tmp_path):
    first = record()
    second = {**record(), "product_url": "https://fixture.test/flyers", "title": "Club Flyers"}
    manifest = export_rows([first, second], tmp_path)
    forbidden = ["supplier-product-87654", "internal-product-98765", "supplier-sku-56789", "internal-record-76543",
                 "private-evidence-65432", "87654321", "76543210", "65432109", "54321098", "base-id-43210",
                 "context-32109", "option-sku-45678", "dimension-id-54321"]
    for item in manifest["items"]:
        if item["format"] == "csv":
            text = open(item["path"], encoding="utf-8").read()
            assert not any(value in text for value in forbidden)
            with open(item["path"], newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            assert rows and all(row["attr:paper"] == "16 pt. Cardstock" for row in rows)
            assert set(rows[0]) == set(manifest["schemas"]["customer_csv"]["columns"])
            assert not any(key.endswith("_json") for key in rows[0])
            assert item["schema"] == "customer_csv"
        else:
            audit = pq.read_table(item["path"]).to_pylist()
            assert audit[0]["source_product_id"] == "supplier-product-87654"
            assert json.loads(audit[0]["record_json"])["evidence_ids"] == first["evidence_ids"]
            assert "record_json" in manifest["schemas"]["audit_parquet"]["columns"]
            assert item["schema"] == "audit_parquet"


def test_legacy_flat_fallback_recovers_labels_without_raw_json():
    legacy = {"title": "Legacy", "quantity": "50", "subtotal": "4.20",
              "configuration_json": json.dumps({"stock": {"value": "9", "label": "Matte"}}),
              "production_json": json.dumps({"value": "3", "status": "requires_configuration_quote"})}
    row = customer_row(audit_record(legacy))
    assert row["attr:stock"] == "Matte" and row["production_label"] == LABEL_UNAVAILABLE
    assert row["production_status"] == "requires_configuration_quote"
    del legacy["configuration_json"]
    legacy["attr:stock"] = '{"value":"9","label":"Gloss"}'
    legacy["attr:width"] = "2.5"
    row = customer_row(audit_record(legacy))
    assert row["attr:stock"] == "Gloss" and row["attr:width"] == "2.5"


def test_catalog_streams_record_only_legacy_and_all_rows_past_multiple_batches(tmp_path):
    count = 2057
    source = tmp_path / "legacy.parquet"
    rows = []
    for index in range(count):
        row = record()
        row["quantity"] = index + 1
        if index == count - 1:
            row["configuration"]["late label"] = {"value": "private-final-id", "label": "Last observed option"}
        rows.append({"record_json": json.dumps(row)})
    pq.write_table(pa.Table.from_pylist(rows), source)
    original_bytes = source.read_bytes()
    manifest = combine_catalog([{"row_count": count, "items": [{"name": "combined.parquet", "path": str(source)}]}], tmp_path / "catalog")
    output = next(item for item in manifest["items"] if item["name"] == "catalog.csv")
    with open(output["path"], newline="", encoding="utf-8") as handle:
        exported = list(csv.DictReader(handle))
    assert len(exported) == manifest["row_count"] == count
    assert {int(row["quantity"]) for row in exported} == set(range(1, count + 1))
    assert exported[-1]["attr:late label"] == "Last observed option"
    assert all(row["service:proofType"] == "Manually Processed (+$3.00)" for row in exported)
    assert "record_json" not in exported[0]
    assert source.read_bytes() == original_bytes
    assert output["schema"] == "customer_csv"
    assert manifest["schemas"]["audit_parquet"]["columns"] == ["record_json"]
