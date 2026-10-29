"""Strict US money syntax and evidence-preserving Companion projections."""
import csv
from decimal import Decimal
import hashlib
import io
import json

import pytest

from crawler.product_companion import (_money, compare_snapshots,
    current_child_table_snapshot, storefront_html_snapshot, supplier_csv_snapshot)

SOURCE = "https://4over.com/4coro-coroplast-signs"
SITE = "https://printitprints.com/shop/example/"


@pytest.mark.parametrize("raw,expected", [
    ("1,003.20", "1003.20"), ("12,345", "12345"), ("123,456.789012345678901234567890", "123456.789012345678901234567890"),
    ("1,234,567.00", "1234567.00"), (" 1,003.2000 ", "1003.2000"),
    ("1003.20", "1003.20"), ("0001003.20", "1003.20"), ("0", "0"), ("00.000", "0.000"),
    ("0.00000001", "0.00000001"), (1003, "1003"), (1003.2, "1003.2"),
    (Decimal("1003.200000000000000000001"), "1003.200000000000000000001"),
])
def test_valid_us_prices_preserve_exact_decimal_value_and_scale(raw, expected):
    parsed = _money(raw)
    assert isinstance(parsed, Decimal)
    assert parsed == Decimal(expected)
    assert parsed.as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.parametrize("raw", [
    "1,00.00", "10,03.20", "1,003,20", "1,2345.67", "1234,567.89", "1,,003.20", ",1003.20", "1003.20,",
    "01,003.20", "0,003.20", "1.003,20", "1,003.2.0", "1 003.20", "1\u00a0003.20", "1,\n003.20",
    "NaN", "sNaN", "Infinity", "-Infinity", "inf", "1e3", "1E+3", "1e-8", "1_003.20",
    "+1003.20", "-0", "-1003.20", "(1,003.20)", "$1,003.20", "USD 1,003.20", "1,003.20 USD",
    "€1,003.20", "1,003.20 / 2,000.00", "1003.20%", "", " ", None, True, False, [], {},
    float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"),
])
def test_malformed_nonfinite_or_ambiguous_prices_are_rejected(raw):
    with pytest.raises(ValueError, match="^invalid_price$"):
        _money(raw)


def supplier(costs):
    output = io.StringIO()
    writer = csv.DictWriter(output, ["Size", "Quantity", "Supplier Table Price", "Source URL"])
    writer.writeheader()
    for quantity, price in enumerate(costs, 1):
        writer.writerow({"Size": '18" x 24"', "Quantity": quantity, "Supplier Table Price": price, "Source URL": SOURCE})
    raw = output.getvalue().encode()
    digest = hashlib.sha256(raw).hexdigest()
    return supplier_csv_snapshot(raw, expected_sha256=digest, source_url=SOURCE), raw


def storefront(costs):
    rows = [{"SIZE ": '18" x 24"', "RunSize": str(quantity), "TotalPrice": price}
            for quantity, price in enumerate(costs, 1)]
    html = ('<html><script>const current_product_id_is_this = 6383;</script>'
            f'<script>const csvdata = {json.dumps(rows)};</script></html>')
    return storefront_html_snapshot(html, url=SITE, product_id="6383")


def comparison(source, site):
    return compare_snapshots(source, site, source_freshness="historical_saved_table", structure={"status": "unknown"})


@pytest.mark.parametrize("source_prices,site_prices", [
    (["1003.20", "12345.678901234567890123456789"], ["1,003.20", "12,345.678901234567890123456789"]),
    (["1,003.20", "12,345.00"], ["1003.20", "12345.00"]),
    (["0.00000001", "0.00000002"], ["0.00000001", "0.00000002"]),
])
def test_grouped_and_ungrouped_csv_and_storefront_match_without_rewriting_evidence(source_prices, site_prices):
    source, raw = supplier(source_prices)
    original = bytes(raw)
    site = storefront(site_prices)
    result = comparison(source, site)
    assert result["checks"]["stored_costs_vs_saved_source"] == "match"
    assert result["counts"]["matching_rows"] == 2
    assert result["counts"]["cost_mismatches"] == 0
    assert result["checks"]["current_supplier_prices"] == "unknown"
    assert result["status"] != "green"
    assert source["sha256"] == hashlib.sha256(raw).hexdigest()
    assert raw == original
    assert all("," not in row["cost"] and "e" not in row["cost"].lower()
               for snapshot in (source, site) for row in snapshot["rows"].values())
    with pytest.raises(ValueError, match="source_evidence_hash_changed"):
        supplier_csv_snapshot(raw + b"\n", expected_sha256=source["sha256"], source_url=SOURCE)


def test_tiny_mismatch_reports_fixed_point_values_that_parse_again():
    source, _ = supplier(["0.00000001"])
    result = comparison(source, storefront(["0.00000002"]))
    item = result["cost_mismatches"][0]
    assert item["supplier_cost"] == "0.00000001"
    assert item["site_cost"] == "0.00000002"
    assert _money(item["supplier_cost"]) == Decimal("0.00000001")
    assert _money(item["site_cost"]) == Decimal("0.00000002")


def test_current_child_table_projection_uses_same_exact_parser_and_fixed_point_output():
    source, _ = supplier(["1003.20", "0.00000001"])
    fields = [{"label": "Size *", "value": "1", "choice": '18" x 24"'}]
    mapping = {"rows": [{"child_id": "1", "fields": fields}]}
    scope = {"sets": 1, "job_sample": False, "digital_proof": False}
    collection = {"source_url": SOURCE, "status": "completed_requested_subset", "scope": scope,
        "declared_children": [{"child_id": "1"}], "option_contract": {"binding_complete": True, "rows": mapping["rows"]},
        "observations": [{"child_id": "1", "status": "collected", "source_url": SOURCE, "scope": scope,
            "tier_prices": [], "package_prices": [{"quantity": 1, "price": "1,003.20"}, {"quantity": 2, "price": "0.00000001"}]}]}
    before = json.dumps(collection, sort_keys=True)
    current = current_child_table_snapshot(mapping, collection, fields=source["fields"], source_url=SOURCE)
    result = comparison(current, storefront(["1003.20", "0.00000001"]))
    assert result["checks"]["stored_costs_vs_saved_source"] == "match"
    assert current["rows"][('2', '18" x 24"')]["cost"] == "0.00000001"
    assert json.dumps(collection, sort_keys=True) == before
