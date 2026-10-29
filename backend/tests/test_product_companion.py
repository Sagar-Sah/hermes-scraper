import csv
import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.product_companion import (compare_snapshots, live_fourover_structure,
    storefront_html_snapshot, supplier_csv_snapshot, woocommerce_public_snapshot,
    current_child_table_snapshot, selling_scope_for_product)
from crawler.product_companion_service import ProductCompanionService


SOURCE = "https://4over.com/brown-kraft-business-cards"
SITE = "https://printitprints.com/shop/example/"


def source_table(quantities=range(1, 23)):
    output = io.StringIO()
    writer = csv.DictWriter(output, ["Product", "Size", "Shape", "Stock", "Colorspec", "Coating", "Quantity",
                                     "Supplier Table Price", "Currency", "Source URL"])
    writer.writeheader()
    for quantity in quantities:
        writer.writerow({"Product": "Cards", "Size": '2" x 3.5"', "Shape": "Rectangle", "Stock": "18PT",
                         "Colorspec": "4/0", "Coating": "None", "Quantity": quantity,
                         "Supplier Table Price": str(quantity + 10), "Currency": "USD", "Source URL": SOURCE})
    raw = output.getvalue().encode()
    return supplier_csv_snapshot(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), source_url=SOURCE)


def site_html(quantities=range(1, 11), *, price_delta=0):
    rows = [{"SIZE ": '2" x 3.5"', "SHAPE ": "Rectangle", "STOCK ": "18PT", "COLORSPEC ": "4/0",
             "COATING ": "None", "RunSize": str(quantity), "UnitPrice": "1",
             "TotalPrice": str(quantity + 10 + price_delta)} for quantity in quantities]
    return ("<html><body class='postid-9532'><h1 class='product_title'>Cards</h1>"
            "<div id='product_calculator_display'></div>"
            "<script>const current_product_id_is_this = 9532;</script>"
            f"<script>const csvdata = {json.dumps(rows)};</script></body></html>")


def test_hidden_22_quantities_cannot_pass_with_10_storefront_quantities():
    source = source_table()
    site = storefront_html_snapshot(site_html(), url=SITE, product_id="9532")
    result = compare_snapshots(source, site, source_freshness="historical_saved_table", structure={"status": "match"})
    assert result["status"] == "mismatch"
    assert result["counts"]["missing_site_rows"] == 12
    assert result["checks"]["options"] == "mismatch"
    assert result["checks"]["current_supplier_prices"] == "unknown"
    assert all(item["requires_review"] for item in result["proposed_changes"])


def test_matching_saved_costs_are_not_current_price_or_green_evidence():
    source = source_table(range(1, 3))
    site = storefront_html_snapshot(site_html(range(1, 3)), url=SITE, product_id="9532")
    result = compare_snapshots(source, site, source_freshness="historical_saved_table", structure={"status": "match"})
    assert result["checks"]["stored_costs_vs_saved_source"] == "match"
    assert result["checks"]["current_supplier_prices"] == "unknown"
    assert result["checks"]["retail_rule"] == "unknown"
    assert result["status"] == "unknown"


def test_standard_merchant_25000_limit_does_not_truncate_the_supplier_domain():
    source = source_table([100, 25000, 30000, 100000])
    site = storefront_html_snapshot(site_html([100, 25000]), url=SITE, product_id="9532")
    scope = selling_scope_for_product("6862")
    result = compare_snapshots(source, site, source_freshness="current_exact_quotes",
                              structure={"status": "match"}, selling_scope=scope)
    assert result["checks"]["options"] == "match"
    assert result["counts"]["missing_site_rows"] == 0
    assert result["counts"]["source_rows_outside_selling_scope"] == 2
    assert result["saved_supplier_quantities"] == [100, 25000, 30000, 100000]
    assert result["applicable_supplier_quantities"] == [100, 25000]
    assert result["checks"]["current_supplier_quantity_domain"] == "unknown"
    assert result["status"] != "green"
    assert selling_scope_for_product("9532") is None
    unscoped = compare_snapshots(source, site, source_freshness="current_exact_quotes", structure={"status": "match"})
    assert unscoped["counts"]["missing_site_rows"] == 2


def test_storefront_quantity_above_authorized_cap_still_requires_review():
    source = source_table([100, 25000, 100000])
    site = storefront_html_snapshot(site_html([100, 25000, 100000]), url=SITE, product_id="9532")
    result = compare_snapshots(source, site, source_freshness="current_exact_quotes", structure={"status": "match"},
                              selling_scope=selling_scope_for_product("6862"))
    assert result["checks"]["options"] == "mismatch"
    assert result["extra_site_rows"][0]["quantity"] == "100000"
    assert all(change["requires_review"] for change in result["proposed_changes"])


def test_cost_change_is_field_level_mismatch_and_evidence_hash_is_guarded():
    source = source_table(range(1, 3))
    site = storefront_html_snapshot(site_html(range(1, 3), price_delta=1), url=SITE, product_id="9532")
    result = compare_snapshots(source, site, source_freshness="historical_saved_table", structure={"status": "match"})
    assert result["counts"]["cost_mismatches"] == 2
    assert result["checks"]["stored_costs_vs_saved_source"] == "mismatch"
    with pytest.raises(ValueError, match="source_evidence_hash_changed"):
        supplier_csv_snapshot(b"tampered", expected_sha256=source["sha256"], source_url=SOURCE)


def test_conditional_finish_missing_from_site_is_reported_without_inventing_price():
    source = source_table(range(1, 2))
    site = storefront_html_snapshot(site_html(range(1, 2)), url=SITE, product_id="9532")
    saved = next(iter(source["rows"].values()))
    selection = dict(saved["selection"], coating="Raised Foil")
    source["rows"][tuple(selection[field] for field in source["fields"])] = {
        "selection": selection, "cost": "25.00", "production": None}
    result = compare_snapshots(source, site, source_freshness="historical_saved_table", structure={"status": "unknown"})
    assert result["counts"]["missing_site_rows"] == 1
    assert result["missing_site_rows"][0]["coating"] == "Raised Foil"
    assert result["checks"]["current_supplier_prices"] == "unknown"
    assert result["status"] == "mismatch"


def test_unverified_source_structure_and_extra_store_field_never_go_green():
    source = source_table(range(1, 2))
    site = storefront_html_snapshot(site_html(range(1, 2)), url=SITE, product_id="9532")
    site["fields"] = tuple(sorted((*site["fields"], "unmatchedfield")))
    result = compare_snapshots(source, site, source_freshness="historical_saved_table", structure={"status": "unknown"})
    assert result["status"] == "mismatch"
    assert result["extra_site_fields"] == ["unmatchedfield"]
    assert result["checks"]["stored_costs_vs_saved_source"] == "unknown"


def test_live_4over_child_domain_detects_new_child():
    html = """<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":{"components":{"child-sku":{"childSkuConfig":{"1":{"sku":"A"},"2":{"sku":"B"}}}}}}}</script>"""
    result = live_fourover_structure(html, url=SOURCE, expected_child_ids={"1"})
    assert result["status"] == "mismatch"
    assert result["new_child_ids"] == ["2"]


def test_schedule_is_opt_in_and_autocorrect_requires_connector(tmp_path):
    class Campaigns:
        def get(self, _):
            return {"products": [{"id": "9532", "title": "Brown Kraft Business Cards", "supplier": "fourover",
                                  "supplier_url": SOURCE, "storefront_url": SITE, "stage": "finalized"}]}
    service = ProductCompanionService(SimpleNamespace(root=Path(tmp_path)), Campaigns())
    service.import_confirmed_business_cards()
    pair = service.get_pair("woo-9532")
    assert pair["next_run_at"] is None and pair["auto_correct"] is False
    with pytest.raises(ValueError, match="automatic_correction_not_enabled"):
        service.set_schedule("woo-9532", hours=24, auto_correct=True)
    assert service.get_pair("woo-9532")["next_run_at"] is None


def test_woocommerce_public_snapshot_guards_product_identity_and_price_context():
    payload = {"id": 9532, "permalink": SITE, "name": "Cards", "type": "simple",
               "prices": {"price": "0", "currency_code": "USD", "currency_minor_unit": 2},
               "images": [{"src": "https://printitprints.com/card.webp"}], "has_options": False}
    raw = json.dumps(payload).encode()
    result = woocommerce_public_snapshot(raw, product_id="9532", storefront_url=SITE)
    assert result["native_price_minor_units"] == "0"
    assert result["images"] == ["https://printitprints.com/card.webp"]
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="woocommerce_product_identity_mismatch"):
        woocommerce_public_snapshot(raw, product_id="9533", storefront_url=SITE)


def test_supplier_signout_cannot_be_read_as_success(tmp_path):
    async def signed_out(_url):
        return 302, "https://4over.com/customer/account/login", b""
    service = ProductCompanionService(SimpleNamespace(root=Path(tmp_path)), None, fetch=signed_out)
    with pytest.raises(ValueError, match="page_unavailable_or_login_required"):
        asyncio.run(service._document(SOURCE, {"4over.com"}))
    assert not list(service.audits_dir.glob("*.json"))


@pytest.mark.parametrize("coverage", [None, {}, {"expanded_rendered_domain": "unknown", "source_domain_complete": True},
    {"expanded_rendered_domain": "verified", "source_domain_complete": False}])
def test_legacy_quantity_match_is_reassessed_on_read_without_changing_saved_evidence(tmp_path, coverage):
    service = ProductCompanionService(SimpleNamespace(root=tmp_path), None)
    audit_id = "a" * 32
    record = {"id": audit_id, "pair_id": "woo-9532", "product_id": "9532", "observed_at": 123,
              "status": "green", "evidence": {"supplier_rows_sha256": "b" * 64},
              "report": {"status": "green", "checks": {"current_supplier_quantity_domain": "match", "options": "match"},
                         "counts": {"source_rows": 2}, "proposed_changes": []},
              "review": {"status": "pending", "approved_proposal_indexes": []}}
    if coverage is not None:
        record["report"]["source_quantity_coverage"] = coverage
    path = service.audits_dir / f"{audit_id}.json"
    original = json.dumps(record).encode()
    path.write_bytes(original)
    service.pairs["woo-9532"] = {"id": "woo-9532", "audit_ids": [audit_id]}
    view = service.get_audit(audit_id)
    assert view["status"] == view["report"]["status"] == "unknown"
    assert view["report"]["checks"]["current_supplier_quantity_domain"] == "unknown"
    assert view["automatic_correction_eligible"] is False
    assert view["read_assessment"]["reason"] == "expanded_quantity_domain_evidence_missing"
    assert service.history("woo-9532")[0]["status"] == "unknown"
    assert path.read_bytes() == original
    # A review may persist review metadata, but never the reassessed evidence.
    reviewed = service.review(audit_id, [])
    assert reviewed["report"]["checks"]["current_supplier_quantity_domain"] == "unknown"
    raw = json.loads(path.read_text())
    assert raw["report"] == record["report"] and raw["evidence"] == record["evidence"]
    assert raw["status"] == "green" and "read_assessment" not in raw


def test_reassessing_legacy_quantity_certainty_does_not_hide_real_mismatch():
    record = {"status": "mismatch", "report": {"status": "mismatch", "checks": {
        "current_supplier_quantity_domain": "match", "options": "mismatch"}}}
    view = ProductCompanionService._audit_read_view(record)
    assert view["status"] == "mismatch" and view["report"]["checks"]["options"] == "mismatch"
    assert record["report"]["checks"]["current_supplier_quantity_domain"] == "match"


def test_current_child_table_requires_complete_binding_and_quantity_domain():
    source = source_table(range(1, 3))
    labels = {"size": "Size *", "shape": "Shape *", "stock": "Stock *",
              "colorspec": "Colorspec *", "coating": "Coating *"}
    fields = [{"label": labels[key], "value": str(i + 1), "choice": source["rows"][next(iter(source["rows"]))]["selection"][key]}
              for i, key in enumerate(labels)]
    mapping = {"rows": [{"child_id": "1", "fields": fields}]}
    collection = {"source_url": SOURCE, "status": "completed_requested_subset",
                  "scope": {"sets": 1, "job_sample": False, "digital_proof": False},
                  "declared_children": [{"child_id": "1"}],
                  "option_contract": {"binding_complete": True, "rows": mapping["rows"]},
                  "observations": [{"child_id": "1", "status": "collected", "source_url": SOURCE,
                    "scope": {"sets": 1, "job_sample": False, "digital_proof": False},
                    "tier_prices": [], "package_prices": [
                        {"quantity": 1, "price": "11"}, {"quantity": 2, "price": "12"}]}]}
    current = current_child_table_snapshot(mapping, collection, fields=source["fields"], source_url=SOURCE)
    assert current["row_count"] == 2
    assert {row["selection"]["quantity"] for row in current["rows"].values()} == {"1", "2"}
    assert current["quantity_domains_by_child"] == {"1": [1, 2]}
    assert current["quantity_coverage"]["expanded_rendered_domain"] == "unknown"
    assert current["quantity_coverage"]["source_domain_complete"] is False
    # Adding read-only coverage diagnostics must not invalidate immutable
    # supplier objects referenced by audits saved before that diagnostic existed.
    collection["quantity_coverage"] = {"expanded_rendered_domain": "unknown"}
    annotated = current_child_table_snapshot(mapping, collection, fields=source["fields"], source_url=SOURCE)
    assert annotated["sha256"] == current["sha256"]
    collection["option_contract"]["binding_complete"] = False
    with pytest.raises(ValueError, match="current_option_binding_unverified"):
        current_child_table_snapshot(mapping, collection, fields=source["fields"], source_url=SOURCE)
