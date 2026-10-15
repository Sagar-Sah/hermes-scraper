"""Offline finite matrices: known exclusions, unknowns and price-key boundaries."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from crawler.printograph_constraints import configuration_fingerprint
from crawler.variation_matrix import MatrixError, VariationMatrix, component_subtotal, price_equivalence_key
from test_printograph_constraints import inputs


def inventory(product_type="1", **changes):
    _, base = inputs(product_type, **changes)
    context = base.pop("context")
    base.pop("turnaround_rule")
    base["lookup"]["response_id"] = "exact-lookup-evidence"
    for type_id in (1, 31, 36):
        base["catalog"]["items"].append({"id": 200000 + type_id, "type": {"id": type_id}, "group": {"id": 1}})
    return {"product_type": product_type, "context": context,
            "contract": json.loads((Path(__file__).parent / "fixtures/printograph-constraints.json").read_text()),
            "service_domains": [{"proofType": "31", "fileDelivery": "5", "designServices": ""}],
            "bases": [base]}


def test_static_matrix_counts_without_claiming_per_row_turnaround_or_price_validation():
    value = inventory()
    before = deepcopy(value)
    matrix = VariationMatrix(value)
    rows = list(matrix.iter_rows())
    report = matrix.report()
    assert len(rows) == report["static_count"] == report["candidate_rows"] == 8
    assert report["count_by_size"] == {"101": {"rule_admissible": 8}}
    assert report["valid_rows"] is report["priced_rows"] is None
    assert not report["catalog_complete"]
    assert all(row["selection"]["raisedPrint"] == "" for row in rows)  # stock1 rejects raised printing
    assert all(not (row["selection"]["coating"] and row["selection"]["spotCoating"]) for row in rows)
    assert report["work"]["finite_rule_evaluations"] < report["work"]["candidate_combinations_examined"]
    assert report["distinct_price_request_count"] is None
    assert value == before


def test_single_tier_visibility_ambiguity_is_not_reused_for_a_different_quantity():
    value = inventory()
    value["bases"][0]["option_prices"]["100002"] = {"items": [{"quantity": 100, "markupPrice": "3.00"}]}
    rows = [row for row in VariationMatrix(value).iter_rows() if row["selection"]["roundedCorners"] == "2"]
    low = [row for row in rows if row["quantity"] == 50]
    high = [row for row in rows if row["quantity"] == 100]
    assert low and high
    assert all("single_tier_option_visibility_requires_validation:2" in row["unresolved"] for row in low)
    assert all(row["status"] == "rule_admissible" for row in high)


def test_missing_support_uses_price_quantities_only_as_unresolved_candidates():
    value = inventory()
    base = value["bases"][0]
    base["base_support"] = None
    base["base_prices"] = {"items": [{"quantity": 25, "markupPrice": "1.00"}, {"quantity": 100, "markupPrice": "4.00"}]}
    base["option_support"] = {}
    value["context"].pop("domains_bound")
    base["lookup"]["request"]["context"] = deepcopy(value["context"])
    matrix = VariationMatrix(value)
    rows = list(matrix.iter_rows())
    assert rows and {row["quantity"] for row in rows} == {25, 100}
    assert all(row["status"] == "unresolved" for row in rows)
    assert all("base_quantity_mode_support_missing" in row["unresolved"] for row in rows)
    assert matrix.report()["valid_rows"] is None


def test_paid_finishing_and_foil_domains_intersect_instead_of_cross_multiplying():
    value = inventory(paper=10, coating="", raisedPrint=138, raisedFoilColor=143, proofType=100)
    value["service_domains"] = [{"proofType": "100", "manualProofType": "1", "fileDelivery": "5", "designServices": ""}]
    base = value["bases"][0]
    base["base_support"]["items"].append({"quantity": 2500, "turnaroundList": [1]})
    base["option_support"]["143"].append({"quantity": 2500, "turnaroundList": [1]})
    rows = list(VariationMatrix(value).iter_rows())
    assert any(row["selection"]["raisedPrint"] == "138" for row in rows)
    assert not any(row["quantity"] == 2500 and row["selection"]["raisedPrint"] == "138" for row in rows)
    assert all(not row["selection"]["raisedFoilColor"] or row["selection"]["raisedPrint"] in {"138", "139", "140"} for row in rows)


def test_price_keys_retain_production_services_coupon_and_unknown_fields():
    value = inventory()
    row = next(VariationMatrix(value).iter_rows())
    def key(selection=None, context=None, catalog=None):
        return price_equivalence_key(selection or row["selection"], base_product_id=row["base_product_id"],
            context=context or value["context"], catalog=catalog or value["bases"][0]["catalog"], contract=value["contract"])
    original = key()["key"]
    for field, changed in [("productionTime", "3"), ("proofType", "100"), ("unreviewedRadius", "1/8")]:
        assert key({**row["selection"], field: changed})["key"] != original
    assert key(context={**value["context"], "coupon": "PROMO"})["key"] != original
    assert key({**row["selection"], "paperFinish": "1"})["key"] == original
    assert key({**row["selection"], "paperFinish": "2"})["key"] != original
    manual = {**row["selection"], "proofType": "100", "manualProofType": "1"}
    bound = key(manual)
    assert any(item["option_record_id"] == "200001" for item in bound["dependencies"]["selected_option_records"])
    assert key({**manual, "manualProofType": "36"})["key"] != bound["key"]


def test_full_rule_proof_validates_only_its_exact_selection():
    value = inventory()
    first = next(VariationMatrix(value).iter_rows())
    base = value["bases"][0]
    fingerprint = configuration_fingerprint("1", first["selection"], value["context"], base["lookup"]["base_product_id"])
    base["turnaround_rules"] = {fingerprint: {"selection_fingerprint": fingerprint, "evidence_id": "combined-rule",
        "data": {"items": [{"id": 1, "range": {"min": 2, "max": 4}}]}}}
    report = VariationMatrix(value).report()
    assert report["fully_validated_configuration_count"] == 1
    assert report["valid_rows"] is None and report["priced_rows"] is None


def test_exact_duplicate_inventory_deduplicates_and_conflicts_fail_closed():
    value = inventory()
    value["bases"].append(deepcopy(value["bases"][0]))
    assert VariationMatrix(value).report()["source_lookup_tuples"] == 1
    value["bases"][1]["lookup"]["base_product_id"] = "different"
    with pytest.raises(MatrixError, match="Conflicting"):
        VariationMatrix(value)


def test_request_count_requires_full_context_requests_and_is_not_variation_count():
    value = inventory()
    request = {"method": "GET", "path": "/service/rest/v1/products/123/prices", "params": {}, "context": value["context"]}
    value["price_requests"] = [request, deepcopy(request)]
    report = VariationMatrix(value).report()
    assert report["distinct_price_request_count"] == 1
    assert report["conservative_unique_price_keys"] == 8
    assert report["component_table_target_count"] > 1


@pytest.mark.parametrize("failure", ["contract", "context", "lookup", "services"])
def test_unbound_matrix_inputs_fail_before_enumeration(failure):
    value = inventory()
    if failure == "contract":
        value["contract"]["bundle_sha256"]["orderForm.js"] = "new"
    elif failure == "context":
        value["context"]["session_scope"] = "new"
    elif failure == "lookup":
        value["bases"][0]["lookup"]["validated"] = False
    else:
        value["service_domains"] = []
    with pytest.raises(MatrixError):
        VariationMatrix(value)


def component_fixture():
    value = inventory()
    base = value["bases"][0]
    base["base_prices"] = {"items": [{"quantity": 100, "markupPrice": "1.005"}]}
    base["option_prices"]["100002"] = {"items": [{"quantity": 100, "markupPrice": "2.675"}]}
    base["option_prices"]["200001"] = {"items": [{"quantity": 100, "markupPrice": "3.00"}]}
    base["option_prices"]["200036"] = {"items": [{"quantity": 100, "markupPrice": "0.00"}]}
    base["table_evidence"] = {"base_prices": "base-response", "option_prices": {
        "100002": "corner-response", "200001": "pdf-response", "200036": "no-proof-response"}}
    row = next(row for row in VariationMatrix(value).iter_rows() if row["quantity"] == 100 and
               row["selection"]["roundedCorners"] == "2" and row["selection"]["coating"] == "5")
    return value, base, row


def test_component_subtotal_rounds_each_source_component_and_does_not_charge_coating():
    value, base, row = component_fixture()
    result = component_subtotal(row, base, value["contract"])
    assert result["table_subtotal"] == "3.67"  # JS:1.005->1.00 and2.675->2.67 before addition
    assert result["status"] == "calculated_pending_canary"
    assert {item["field"] for item in result["components"]} == {"base", "roundedCorners"}
    assert result["component_evidence_complete"]
    assert not result["source_context_verified_for_pricing"] and not result["cart_verified"]
    assert row["table_subtotal"] == "3.67"


def test_active_manual_proof_records_differ_and_latent_manual_selection_is_ignored():
    value, base, row = component_fixture()
    row["selection"]["manualProofType"] = "1"
    assert component_subtotal(row, base, value["contract"])["table_subtotal"] == "3.67"
    row["selection"]["proofType"] = "100"
    assert component_subtotal(row, base, value["contract"])["table_subtotal"] == "6.67"
    row["selection"]["manualProofType"] = "36"
    result = component_subtotal(row, base, value["contract"])
    assert result["table_subtotal"] == "3.67"
    manual = next(item for item in result["components"] if item["field"] == "manualProofType")
    assert manual["option_record_id"] == "200036" and manual["source_rounded_amount"] == "0.00"


@pytest.mark.parametrize("missing", ["null", "tier", "table", "unknown_option", "binding"])
def test_missing_components_are_never_zero_or_interpolated(missing):
    value, base, row = component_fixture()
    if missing == "null":
        base["option_prices"]["100002"]["items"][0]["markupPrice"] = None
    elif missing == "tier":
        base["option_prices"]["100002"]["items"][0]["quantity"] = 50
    elif missing == "table":
        del base["option_prices"]["100002"]
    elif missing == "unknown_option":
        row["selection"]["coating"] = "999"
    else:
        row["base_product_id"] = "different"
    result = component_subtotal(row, base, value["contract"])
    assert result["table_subtotal"] is None and result["status"] == "unresolved" and result["gaps"]


def test_club_flyer_candidates_use_exact_bound_geometry_and_grouped_price_amounts():
    value = inventory("27", size=292, orientation=2)
    base = value["bases"][0]
    base["base_prices"] = {"items": [{"quantity": 50, "markupPrice": "1,062.49"},
                                       {"quantity": 100, "markupPrice": "1,130.69"}]}
    rows = list(VariationMatrix(value).iter_rows())
    assert len(rows) == 8
    assert {(row["product_type"], row["selection"]["size"], row["selection"]["orientation"]) for row in rows} == {("27", "292", "2")}
    plain = next(row for row in rows if row["quantity"] == 50 and not row["selection"]["roundedCorners"])
    rounded = next(row for row in rows if row["quantity"] == 50 and row["selection"]["roundedCorners"] == "2")
    assert plain["table_subtotal"] == "1062.49"
    assert rounded["table_subtotal"] == "1065.49"
    assert not rounded["source_context_verified_for_pricing"]


def test_club_flyer_static_rules_reject_unavailable_rush_without_fabricating_regular():
    value = inventory("27", paper=4, color=1, productionTime=3)
    matrix = VariationMatrix(value)
    assert list(matrix.iter_rows()) == []
    assert matrix.report()["work"]["known_invalid_combinations"] > 0
    regular = inventory("27", paper=4, color=1, productionTime=1)
    rows = list(VariationMatrix(regular).iter_rows())
    assert rows and {row["selection"]["productionTime"] for row in rows} == {"1"}


def test_roll_has_only_observed_wind_domain_and_keeps_shape_finish_labels():
    value = inventory("36")
    base = value["bases"][0]
    base["source_labels"] = {"shape": "Rectangle", "size": "0.5 x 1", "finish": "Gloss"}
    for item in base["catalog"]["items"]:
        item["type"]["label"] = {32: "Off Bottom", 33: "Off Left", 34: "Off Right", 35: "Off Top", 54: "Not Important"}.get(item["type"]["id"], "Proof")
    rows = list(VariationMatrix(value).iter_rows())
    assert len(rows) == 10
    assert {row["selection"]["windPosition"] for row in rows} == {"32", "33", "34", "35", "54"}
    assert all("roundedCorners" not in row["selection"] and "paperFinish" not in row["selection"] for row in rows)
    assert all(row["configuration"]["shape"]["label"] == "Rectangle" and row["configuration"]["finish"]["label"] == "Gloss" for row in rows)
    assert {row["configuration"]["windPosition"]["label"] for row in rows} == {"Off Bottom", "Off Left", "Off Right", "Off Top", "Not Important"}
    assert VariationMatrix(value).report()["count_by_shape"] == {"5": {"rule_admissible": 10}}


def test_roll_intersects_wind_quantity_support_and_never_crosses_bound_shapes_sizes():
    value = inventory("36")
    value["bases"][0]["option_support"]["33"] = [{"quantity": 100, "turnaroundList": [1]}]
    square = deepcopy(value["bases"][0])
    square["lookup"]["base_product_id"] = "456"
    square["lookup"]["request"]["params"].update(shape="4", size="448")
    value["bases"].append(square)
    rows = list(VariationMatrix(value).iter_rows())
    assert len(rows) == 18
    assert {(row["selection"]["shape"], row["selection"]["size"]) for row in rows} == {("5", "742"), ("4", "448")}
    assert not any(row["quantity"] == 50 and row["selection"]["windPosition"] == "33" for row in rows)


def test_roll_finish_stock_correction_excludes_inconsistent_exact_lookup():
    value = inventory("36", paper=12, finish=3)
    matrix = VariationMatrix(value)
    assert list(matrix.iter_rows()) == []
    assert matrix.report()["work"]["known_invalid_combinations"] == 10
    assert len(list(VariationMatrix(inventory("36", paper=12, finish=1)).iter_rows())) == 10


def test_missing_required_wind_domain_is_a_gap_not_invented_none_or_default():
    value = inventory("36")
    value["bases"][0]["catalog"]["items"] = [item for item in value["bases"][0]["catalog"]["items"] if item["type"]["id"] not in {32, 33, 34, 35, 54}]
    matrix = VariationMatrix(value)
    assert list(matrix.iter_rows()) == []
    report = matrix.report()
    assert report["valid_rows"] is None
    assert report["inventory_gaps"][0]["reason"] == "required_option_has_no_observed_supported_choice"


def test_roll_components_cover_source_formula_but_wind_price_keys_remain_distinct():
    value = inventory("36")
    base = value["bases"][0]
    base["base_prices"] = {"items": [{"quantity": 50, "markupPrice": "1,062.49"}, {"quantity": 100, "markupPrice": "2.00"}]}
    base["option_prices"] = {"200001": {"items": [{"quantity": 50, "markupPrice": "3.00"}]}}
    base["table_evidence"] = {"base_prices": "base-response", "option_prices": {"200001": "manual-response"}}
    rows = [row for row in VariationMatrix(value).iter_rows() if row["quantity"] == 50]
    assert len(rows) == len({row["price_key"] for row in rows}) == 5
    assert {row["table_subtotal"] for row in rows} == {"1062.49"}
    assert all({part["field"] for part in row["table_subtotal_components"]} == {"base"} for row in rows)
    row = deepcopy(rows[0])
    row["selection"].update(proofType="100", manualProofType="1")
    assert component_subtotal(row, base, value["contract"])["table_subtotal"] == "1065.49"
    row["selection"]["windPosition"] = ""
    assert "missing_required_wind_position" in component_subtotal(row, base, value["contract"])["gaps"]
    row["selection"]["windPosition"] = "32"
    row["selection"]["shape"] = "4"
    assert component_subtotal(row, base, value["contract"])["table_subtotal"] is None


@pytest.mark.parametrize("product_type", ["27", "36"])
def test_other_product_types_require_exact_combined_rule_and_preserve_missing_support(product_type):
    value = inventory(product_type)
    base = value["bases"][0]
    row = next(VariationMatrix(value).iter_rows())
    fingerprint = configuration_fingerprint(product_type, row["selection"], value["context"], base["lookup"]["base_product_id"])
    base["turnaround_rules"] = {fingerprint: {"selection_fingerprint": fingerprint, "evidence_id": "combined-rule", "data": {"items": [{"id": 1, "range": {"min": 2, "max": 4}}]}}}
    assert VariationMatrix(value).report()["fully_validated_configuration_count"] == 1
    base["turnaround_rules"] = {}
    base["base_support"] = None
    base["base_prices"] = {"items": [{"quantity": 100, "markupPrice": "4.00"}]}
    rows = list(VariationMatrix(value).iter_rows())
    assert rows and all("base_quantity_mode_support_missing" in row["unresolved"] for row in rows)
    assert VariationMatrix(value).report()["valid_rows"] is None


def test_product_type_binding_and_unreviewed_types_fail_closed():
    value = inventory("36")
    row = next(VariationMatrix(value).iter_rows())
    row["product_type"] = "27"
    assert "unsupported_or_mismatched_product_type" in component_subtotal(row, value["bases"][0], value["contract"])["gaps"]
    value["bases"][0]["lookup"]["request"]["params"]["productType"] = "27"
    with pytest.raises(MatrixError, match="validated exact"):
        VariationMatrix(value)
    with pytest.raises(MatrixError, match="supports"):
        VariationMatrix({**value, "product_type": "999"})
