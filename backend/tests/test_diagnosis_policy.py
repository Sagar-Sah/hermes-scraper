"""Observed selection policies cannot hide invalid defaults or promote candidates."""

from copy import deepcopy
from decimal import Decimal

import pytest

from crawler.diagnosis_policy import (
    SelectionPolicyError, apply_selection, classify_finalization,
    normalize_selection, selection_fingerprint,
)
from crawler.variation_matrix import VariationMatrix
from test_variation_matrix import inventory


def diagnosis():
    return {"status": "ready", "revision": "source-revision-1", "url": "https://supplier.test/product",
            "fields": [
                {"key": "select:stock_product_7", "label": "Paper", "configurable": True, "kind": "select",
                 "source_name": "paper", "source_id": "paper_7", "source_key": "paper",
                 "selected_values": ["10"], "choices": [
                     {"value": "", "label": "Select paper", "placeholder": True},
                     {"value": "10", "label": "16 pt. Matte"},
                     {"value": "11", "label": "18 pt. Linen"},
                     {"value": "12", "label": "Unavailable stock", "disabled": True}]},
                {"key": "select:finish_product_9", "label": "Coating", "configurable": True, "kind": "select",
                 "selected_values": [""], "choices": [{"value": "", "label": "None"}, {"value": "5", "label": "UV"}]},
                {"key": "content:description", "label": "Description", "configurable": False, "kind": "content"}]}


def submission():
    return [{"key": "select:stock_product_7", "include": True, "fixed_values": []},
            {"key": "select:finish_product_9", "include": False, "fixed_values": [""]},
            {"key": "content:description", "include": False, "fixed_values": []}]


def policy(value=None, submitted=None):
    value = value or diagnosis()
    return normalize_selection(value, value["revision"], submitted or submission())


def test_exact_keys_and_source_labels_are_preserved_without_mutation():
    source, selected = diagnosis(), submission()
    before = deepcopy((source, selected))
    result = policy(source, selected)
    assert (source, selected) == before
    assert result["source_url"] == source["url"]
    assert result["coverage"] == "current_page_only"
    assert result["hidden_fixed"] == {"select:finish_product_9": [""]}
    assert result["omitted_content_keys"] == ["content:description"]
    assert result["included_field_keys"] == ["select:stock_product_7"]
    assert result["fields"][0]["source_id"] == "paper_7"
    assert result["fields"][0]["source_name"] == result["fields"][0]["source_key"] == "paper"
    assert result["fields"][0]["include"] is True
    assert result["fields"][1]["fixed_values"] == [""]
    assert len(result["fingerprint"]) == 64
    assert policy(source, list(reversed(selected)))["fingerprint"] == result["fingerprint"]
    selected[1]["fixed_values"] = ["5"]
    assert policy(source, selected)["fingerprint"] != result["fingerprint"]


def test_repeated_native_select_option_is_one_choice_but_conflicting_labels_fail():
    source = diagnosis()
    source["fields"][0]["choices"].append({"value": "10", "label": "16 pt. Matte"})
    selected = policy(source)
    assert [choice["value"] for choice in selected["fields"][0]["choices"]].count("10") == 1
    source["fields"][0]["choices"][-1]["label"] = "Different paper"
    with pytest.raises(SelectionPolicyError, match="Ambiguous duplicate choice value: Paper"):
        policy(source)


def test_custom_control_binding_metadata_survives_saved_policy_and_changes_fingerprint():
    source = diagnosis()
    source["fields"][0].update(kind="custom_select", binding_kind="display_label_only",
                               value_origin="display_label_surrogate")
    source["fields"][0]["choices"][1]["value_origin"] = "display_label_surrogate"
    before = deepcopy(source)
    result = policy(source)
    field = result["fields"][0]
    assert field["binding_kind"] == "display_label_only"
    assert field["value_origin"] == field["choices"][1]["value_origin"] == "display_label_surrogate"
    assert source == before
    source["fields"][0].update(binding_kind="native_control", value_origin="native_value")
    assert policy(source)["fingerprint"] != result["fingerprint"]


@pytest.mark.parametrize("key", ["binding_kind", "value_origin"])
def test_invalid_binding_metadata_is_not_silently_dropped(key):
    source = diagnosis()
    source["fields"][0][key] = {"unexpected": True}
    with pytest.raises(SelectionPolicyError, match="Invalid source binding: Paper"):
        policy(source)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop(),
    lambda value: value.append(deepcopy(value[0])),
    lambda value: value[0].update(key="unknown"),
    lambda value: value[0].update(include=1),
    lambda value: value[0].update(extra=True),
    lambda value: value[0].update(fixed_values=["10"]),
    lambda value: value[1].update(fixed_values=[]),
    lambda value: value[1].update(fixed_values=["", "5"]),
    lambda value: value[1].update(fixed_values=[5]),
    lambda value: value[1].update(fixed_values=["missing"]),
    lambda value: value[2].update(fixed_values=["description"]),
])
def test_rejects_incomplete_extra_ambiguous_and_implicit_defaults(mutation):
    selected = submission()
    mutation(selected)
    with pytest.raises(SelectionPolicyError):
        policy(submitted=selected)


@pytest.mark.parametrize("status,revision", [("running", "source-revision-1"), ("ready", "stale"), ("ready", 1)])
def test_requires_ready_current_revision(status, revision):
    source = diagnosis()
    source["status"] = status
    with pytest.raises(SelectionPolicyError):
        normalize_selection(source, revision, submission())


@pytest.mark.parametrize("revision", [0, 1, 25])
def test_scanner_integer_revision_requires_exact_type_and_value(revision):
    source = diagnosis()
    source["revision"] = revision
    assert normalize_selection(source, revision, submission())["revision"] == revision
    for wrong in (str(revision), revision + 1, bool(revision), float(revision)):
        with pytest.raises(SelectionPolicyError):
            normalize_selection(source, wrong, submission())


def test_validation_errors_use_source_labels_instead_of_internal_keys():
    selected = submission()
    selected[1]["fixed_values"] = ["unavailable"]
    with pytest.raises(SelectionPolicyError) as error:
        policy(submitted=selected)
    assert "Coating" in str(error.value)
    assert "select:finish_product_9" not in str(error.value)


@pytest.mark.parametrize("fixed", [[""], ["12"]])
def test_disabled_choice_and_placeholder_are_not_defaults(fixed):
    selected = submission()
    selected[0].update(include=False, fixed_values=fixed)
    with pytest.raises(SelectionPolicyError):
        policy(submitted=selected)


def test_disabled_field_can_only_fix_exact_valid_observed_selection():
    source, selected = diagnosis(), submission()
    source["fields"][0]["disabled"] = True
    with pytest.raises(SelectionPolicyError):
        policy(source, selected)
    selected[0].update(include=False, fixed_values=["10"])
    assert policy(source, selected)["hidden_fixed"]["select:stock_product_7"] == ["10"]
    selected[0]["fixed_values"] = ["11"]
    with pytest.raises(SelectionPolicyError):
        policy(source, selected)
    selected[0]["fixed_values"] = ["10"]
    source["fields"][0]["choices"][1]["disabled"] = True
    with pytest.raises(SelectionPolicyError):
        policy(source, selected)


def numeric_diagnosis():
    return {"status": "ready", "revision": "n1", "url": "https://supplier.test/product", "fields": [
        {"key": "number:width", "label": "Width (inches)", "configurable": True, "kind": "number",
         "selected_values": ["0.3"], "min": "0.1", "max": "2.1", "step": "0.2"}]}


@pytest.mark.parametrize("value,valid", [("0.3", True), ("2.1", True), ("0.3000000000000000000000000001", False),
                                        ("0.2", False), ("2.3", False), ("-0.1", False),
                                        ("NaN", False), ("Infinity", False), ("1_0", False)])
def test_numeric_bounds_and_step_use_decimal(value, valid):
    selected = [{"key": "number:width", "include": False, "fixed_values": [value]}]
    if valid:
        result = policy(numeric_diagnosis(), selected)
        assert result["hidden_fixed"]["number:width"] == [value]
    else:
        with pytest.raises(SelectionPolicyError):
            policy(numeric_diagnosis(), selected)


def test_numeric_equivalent_scalars_match_but_actual_custom_number_preserved():
    result = policy(numeric_diagnosis(), [{"key": "number:width", "include": False, "fixed_values": ["0.3"]}])
    assert apply_selection(result, {"number:width": "0.30"})["status"] == "included"
    assert apply_selection(result, {"number:width": "0.5"})["status"] == "excluded"
    result = policy(numeric_diagnosis(), [{"key": "number:width", "include": True, "fixed_values": []}])
    actual = {"number:width": Decimal("0.3")}
    applied = apply_selection(result, actual)
    assert applied["public_configuration"] == {"Width (inches)": "0.3"}
    assert applied["internal_configuration"] == actual


def test_checkbox_fixed_set_is_not_a_domain_of_hidden_price_variants():
    source = diagnosis()
    source["fields"][1].update(kind="checkbox", selected_values=["", "5"])
    selected = submission()
    selected[1]["fixed_values"] = ["5", ""]
    result = policy(source, selected)
    first = apply_selection(result, {"select:stock_product_7": "10", "select:finish_product_9": ["5", ""]})
    assert first["status"] == "included"
    assert apply_selection(result, {"select:stock_product_7": "10", "select:finish_product_9": ["5"]})["status"] == "excluded"
    assert first["internal_configuration"]["select:finish_product_9"] == ["5", ""]
    selected[1]["fixed_values"] = []
    with pytest.raises(SelectionPolicyError):
        policy(source, selected)
    source["fields"][1]["selected_values"] = []
    assert policy(source, selected)["hidden_fixed"]["select:finish_product_9"] == []


def test_public_projection_uses_labels_and_does_not_mutate_hidden_configuration():
    configuration = {"select:stock_product_7": "10", "select:finish_product_9": "", "content:description": "About this stock"}
    before = deepcopy(configuration)
    result = apply_selection(policy(), configuration)
    assert result == {"status": "included", "public_configuration": {"Paper": "16 pt. Matte"},
                      "internal_configuration": configuration, "reasons": []}
    result["internal_configuration"]["select:finish_product_9"] = "5"
    assert configuration == before
    result = apply_selection(policy(), {**configuration, "select:finish_product_9": "5"})
    assert result["status"] == "excluded"
    assert result["internal_configuration"]["select:finish_product_9"] == "5"


def test_missing_and_unobserved_fields_stay_unresolved():
    result = apply_selection(policy(), {"select:stock_product_7": "10"})
    assert result["status"] == "unresolved"
    assert "missing_required_field:select:finish_product_9" in result["reasons"]
    result = apply_selection(policy(), {"select:stock_product_7": "99", "select:finish_product_9": ""})
    assert result["status"] == "unresolved"
    result = apply_selection(policy(), {"select:stock_product_7": "10", "select:finish_product_9": "", "new_fee": "paid"})
    assert result["status"] == "unresolved" and result["internal_configuration"]["new_fee"] == "paid"


def test_duplicate_public_labels_cannot_overwrite_separate_configurations():
    source, selected = diagnosis(), submission()
    source["fields"][1]["label"] = "Paper"
    selected[1].update(include=True, fixed_values=[])
    with pytest.raises(SelectionPolicyError, match="duplicate public labels"):
        policy(source, selected)


def final_row():
    row = {"status": "supported", "configuration": {"paper": {"value": "10", "label": "16 pt. Matte"}},
           "quantity": 100, "production": {"value": "1", "label": "Regular 2–4 days"},
           "product_url": "https://supplier.test/product", "currency": "USD", "subtotal": "12.30",
           "pricing_context": {"proofType": "31", "coupon": ""}, "provenance": "direct_quote",
           "evidence_ids": ["availability-response", "production-response", "quote-response"]}
    fingerprint = selection_fingerprint(row)
    row["finalization_evidence"] = {
        "availability": {"status": "supported", "selection_fingerprint": fingerprint, "full_selection": True,
                         "evidence_ids": ["availability-response"]},
        "production": {"status": "supported", "selection_fingerprint": fingerprint, "full_selection": True,
                       "evidence_ids": ["production-response"], "range": {"min": 2, "max": 4}},
        "price": {"status": "direct_quote", "selection_fingerprint": fingerprint, "full_selection": True,
                  "evidence_ids": ["quote-response"], "subtotal": "12.3", "currency": "USD"}}
    return row


def test_current_matrix_rule_admissible_rows_are_held_even_with_component_prices():
    rows = list(VariationMatrix(inventory()).iter_rows())
    assert rows and all(row["status"] == "rule_admissible" for row in rows)
    assert all(classify_finalization(row)["status"] == "held" for row in rows)
    row = rows[0]
    row.update(status="supported", pricing_composition_verified=True, subtotal="12.30", currency="USD")
    assert classify_finalization(row)["status"] == "held"


def test_exact_full_selection_direct_quote_can_pass_without_guessing_composition():
    row = final_row()
    before = deepcopy(row)
    assert classify_finalization(row) == {"status": "eligible", "reasons": []}
    assert row == before
    row["provenance"] = "source_table"
    assert classify_finalization(row)["status"] == "held"


def test_calculated_source_components_require_actual_composition_reconciliation():
    row = final_row()
    row["provenance"] = "rule_derived"
    row["finalization_evidence"]["price"]["status"] = "reconciled"
    row["pricing_composition_verified"] = False
    assert classify_finalization(row)["status"] == "held"
    row["pricing_composition_verified"] = True
    assert classify_finalization(row)["status"] == "eligible"


@pytest.mark.parametrize("section", ["availability", "production", "price"])
@pytest.mark.parametrize("change", [{"full_selection": False}, {"evidence_ids": []},
                                    {"evidence_ids": ["not-attached"]}, {"selection_fingerprint": "old"}])
def test_every_gate_requires_full_selection_binding_and_attached_evidence(section, change):
    row = final_row()
    row["finalization_evidence"][section].update(change)
    assert classify_finalization(row)["status"] == "held"


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(quantity=250),
    lambda row: row["configuration"]["paper"].update(value="11"),
    lambda row: row["production"].update(value="3"),
    lambda row: row["pricing_context"].update(coupon="DISCOUNT"),
    lambda row: row["pricing_context"].update(proofType="100"),
])
def test_evidence_cannot_be_reused_for_changed_full_configuration(mutation):
    row = final_row()
    mutation(row)
    assert classify_finalization(row)["status"] == "held"


def test_configuration_cannot_hide_new_values_behind_an_unchanged_selection_object():
    row = final_row()
    row["selection"] = {"paper": "10"}
    fingerprint = selection_fingerprint(row)
    for section in row["finalization_evidence"].values():
        section["selection_fingerprint"] = fingerprint
    assert classify_finalization(row)["status"] == "eligible"
    row["configuration"]["unmodeled_finish"] = {"value": "paid", "label": "Paid finish"}
    assert classify_finalization(row)["status"] == "held"


@pytest.mark.parametrize("window", [{"min": True, "max": 4}, {"min": 4, "max": 2},
                                    {"min": -1, "max": 4}, {"min": 2}, {"min": 2, "max": None},
                                    {"min": "NaN", "max": 4}])
def test_production_range_requires_complete_valid_source_range(window):
    row = final_row()
    row["finalization_evidence"]["production"]["range"] = window
    assert classify_finalization(row)["status"] == "held"


def test_explicit_source_open_ended_production_is_distinguished_from_missing_max():
    row = final_row()
    row["finalization_evidence"]["production"].update(range={"min": 2, "max": None}, open_ended=True)
    assert classify_finalization(row)["status"] == "eligible"


@pytest.mark.parametrize("amount", [None, True, "NaN", "Infinity", "-1", "12.31"])
def test_missing_invalid_or_mismatched_source_cost_is_held(amount):
    row = final_row()
    row["subtotal"] = amount
    assert classify_finalization(row)["status"] == "held"


@pytest.mark.parametrize("section", ["availability", "production"])
def test_proven_invalid_is_excluded_but_unbound_invalid_is_held(section):
    row = final_row()
    row["finalization_evidence"][section]["status"] = "invalid"
    assert classify_finalization(row)["status"] == "invalid"
    row["finalization_evidence"][section]["selection_fingerprint"] = "other-selection"
    assert classify_finalization(row)["status"] == "held"
    row = {"status": "invalid", "invalid": ["UV plus Spot UV"]}
    assert classify_finalization(row)["status"] == "held"  # No universal label-based incompatibility.


def test_unknown_and_rule_admissible_not_promoted_by_other_successful_gates():
    row = final_row()
    row["status"] = "rule_admissible"
    assert classify_finalization(row)["status"] == "held"
    row["status"] = "supported"
    row["unresolved"] = ["unknown_option_compatibility"]
    assert classify_finalization(row)["status"] == "held"
