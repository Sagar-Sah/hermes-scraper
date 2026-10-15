"""Saved policies bind future browser filters and actual candidates, never prices."""

from copy import deepcopy

import pytest

from crawler.diagnosis_policy import normalize_selection
from crawler.diagnosis_run import compile_browser_policy, validate_policy_row


URL = "https://www.printograph.com/products/business-cards/order"


def field(key, *, kind="select", label=None, source_id=None, source_name=None, values=("1", "2"), **extra):
    return {"key": "internal-" + key, "label": label or key, "kind": kind, "configurable": True,
            "source_id": source_id if source_id is not None else key + "_abc123",
            "source_name": source_name or "", "choices": [{"value": value, "label": "Choice " + value} for value in values],
            "selected_values": [values[0]], **extra}


def diagnosis(fields=None, hidden=None):
    fields = fields if fields is not None else [field("paper"), field("color")]
    hidden = hidden if hidden is not None else {"internal-paper": "1"}
    result = {"id": "d" * 32, "url": URL, "status": "ready", "revision": 1, "fields": fields}
    result["selection"] = normalize_selection(result, 1, [
        {"key": item["key"], "include": item["key"] not in hidden,
         "fixed_values": hidden[item["key"]] if isinstance(hidden.get(item["key"]), list)
         else [hidden[item["key"]]] if item["key"] in hidden else []} for item in fields])
    return result


def compile(value=None, **changes):
    return compile_browser_policy(value or diagnosis(), changes.get("urls", [URL]),
                                  changes.get("adapter", "auto"), changes.get("filters", {}))


def test_saved_policy_compiles_hidden_choices_only_and_retains_original_fingerprint():
    source = diagnosis()
    before = deepcopy(source)
    filters = {"color": ["1", "2"], "quantity": "100"}
    result = compile(source, filters=filters)
    assert result["adapter"] == "browser"
    assert result["filters"] == {"color": ["1", "2"], "quantity": "100", "paper": "1"}
    assert filters == {"color": ["1", "2"], "quantity": "100"}
    assert source == before
    policy = result["field_policy"]
    assert policy["fingerprint"] == source["selection"]["fingerprint"]
    assert policy["fields"] == source["selection"]["fields"]
    assert set(policy["browser_bindings"]) == {"internal-paper"}
    assert policy["browser_bindings"]["internal-paper"]["source_raw_key"] == "paper_abc123"


@pytest.mark.parametrize("change", [
    {"urls": []}, {"urls": [URL, URL]}, {"urls": [URL + "?other=1"]},
    {"urls": ["https://www.printograph.com/products/club-flyers/order"]},
    {"adapter": "woocommerce"}, {"adapter": "printograph"}, {"adapter": "printitprints"},
])
def test_only_exact_single_product_browser_runs_are_supported(change):
    with pytest.raises(ValueError):
        compile(**change)


def test_existing_public_host_alias_and_trailing_slash_identity_is_preserved():
    assert compile(urls=[URL.replace("www.", "") + "/"])["adapter"] == "browser"


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(status="running"),
    lambda value: value.update(revision=2),
    lambda value: value.pop("selection"),
    lambda value: value["selection"].update(revision=True),
    lambda value: value["selection"].update(fingerprint="different"),
    lambda value: value["fields"][0].update(source_id="other_abc123"),
])
def test_missing_stale_and_changed_policies_cannot_start(mutation):
    value = diagnosis()
    mutation(value)
    with pytest.raises(ValueError):
        compile(value)


def test_select_and_number_use_id_then_observed_name_never_label():
    value = diagnosis([field("paper", source_id="", source_name="stock")])
    assert compile(value)["filters"] == {"paper": "1"}
    item = field("paper", source_id="", source_name="", source_key="Paper")
    with pytest.raises(ValueError, match="observed control name or ID"):
        compile(diagnosis([item]))
    # Labels do not rewrite a valid raw source identifier.
    value = diagnosis([field("paper", label="Quantity", source_id="rawStock_abc123")])
    assert compile(value)["filters"] == {"rawstock": "1"}


@pytest.mark.parametrize("kind,extra", [("checkbox", {}), ("select", {"multiple": True}), ("range", {})])
def test_unsupported_hidden_controls_reject_clearly(kind, extra):
    item = field("paper", kind=kind, **extra)
    if kind == "range":
        item.update(min="0", max="5", step="1")
    with pytest.raises(ValueError, match="Browser runs"):
        compile(diagnosis([item]))


@pytest.mark.parametrize("kind", ["custom_select", "static", "custom_button", "button_group", "checkbox"])
@pytest.mark.parametrize("include", [True, False])
def test_unsupported_custom_controls_block_included_as_well_as_hidden_dimensions(kind, include):
    item = field("paper", kind=kind)
    hidden = {} if include else {"internal-paper": "1"}
    with pytest.raises(ValueError, match="cannot traverse this diagnosed control"):
        compile(diagnosis([item], hidden))


@pytest.mark.parametrize("metadata", [
    {"binding_kind": "display_label_only"},
    {"value_origin": "display_label_surrogate"},
    {"binding_kind": "unknown_binding"},
])
@pytest.mark.parametrize("include", [True, False])
def test_display_surrogates_never_compile_as_native_supplier_values(metadata, include):
    item = field("paper", **metadata)
    with pytest.raises(ValueError, match="source option values, not display-label bindings"):
        compile(diagnosis([item], {} if include else {"internal-paper": "1"}))


def test_one_label_surrogate_choice_blocks_an_otherwise_native_included_select():
    item = field("paper", binding_kind="native_control", value_origin="native_value")
    item["choices"][1]["value_origin"] = "display_label_surrogate"
    with pytest.raises(ValueError, match="display-label bindings"):
        compile(diagnosis([item], {}))


def test_native_metadata_remains_usable_and_nonconfigurable_content_is_not_a_browser_control():
    fields = [field("paper", binding_kind="native_control", value_origin="native_value"),
              {"key": "description", "label": "Description", "kind": "static", "configurable": False,
               "binding_kind": "display_label_only", "value_origin": "display_label_surrogate", "choices": []}]
    result = compile(diagnosis(fields))
    assert result["filters"] == {"paper": "1"}


def test_radio_requires_observed_name_even_if_an_id_exists():
    item = field("paper", kind="radio", source_id="paper", source_name="")
    with pytest.raises(ValueError, match="observed control name or ID"):
        compile(diagnosis([item]))
    item["source_name"] = "stock_ab123"
    assert compile(diagnosis([item]))["filters"] == {"paper": "1"}


def test_canonical_service_and_production_keys_come_from_observed_names():
    fields = [field("proof", kind="radio", source_name="proofType", values=("31", "100")),
              field("production", kind="radio", source_name="productionTime")]
    result = compile(diagnosis(fields, {"internal-proof": "31", "internal-production": "1"}))
    assert result["filters"] == {"proofType": "31", "productionTime": "1"}


@pytest.mark.parametrize("hidden", [{"internal-paper": "1"}, {"internal-paper": "1", "internal-other": "1"}])
def test_normalized_control_collisions_cannot_fix_another_field(hidden):
    fields = [field("paper", source_id="stock_abc123"), field("other", source_id="paper_def456")]
    with pytest.raises(ValueError, match="share one browser key"):
        compile(diagnosis(fields, hidden))


@pytest.mark.parametrize("value", ["2", ["1", "2"], [], ["1", "1"], True, {"value": "1"}])
def test_conflicting_existing_filters_do_not_get_silently_overwritten(value):
    with pytest.raises(ValueError):
        compile(filters={"stock": value})


@pytest.mark.parametrize("key,value", [("paper", "1"), ("stock", ["1"]), ("paper_abc123", 1)])
def test_existing_exact_single_fixed_value_is_normalized(key, value):
    assert compile(filters={key: value, "color": ["1", "2"]})["filters"] == {"paper": "1", "color": ["1", "2"]}


def test_numeric_fixed_values_are_exact_decimal_canonical_not_float_rounded():
    item = field("width", kind="number", values=("0.30",), min="0.1", max="2.1", step="0.2")
    result = compile(diagnosis([item], {"internal-width": "0.30"}), filters={"width": ["0.300"]})
    assert result["filters"] == {"width": "0.3"}
    row = {"configuration": {"width": {"value": "0.3000"}}, "subtotal": "1.00"}
    assert validate_policy_row(result["field_policy"], row) is row
    with pytest.raises(ValueError):
        compile(diagnosis([item], {"internal-width": "0.30"}), filters={"width": "0.3000000000000000000000000001"})


def test_row_guard_preserves_every_internal_field_and_does_not_certify_price():
    policy = compile()["field_policy"]
    row = {"product_url": URL, "configuration": {"paper": {"value": "1", "label": "source paper"},
                                                "unknown_new_fee": {"value": "paid"}},
           "subtotal": None, "verification_status": "pending", "evidence_ids": ["evidence"]}
    before = deepcopy(row)
    assert validate_policy_row(policy, row) is row
    assert row == before
    assert "finalization_evidence" not in row


@pytest.mark.parametrize("row", [
    {"configuration": {}},
    {"configuration": {"paper": {"value": "2"}}},
    {"configuration": {"paper": {"value": "1"}}, "selection": {"paper": "2"}},
    {"configuration": {"paper": {"value": ["1", "2"]}}},
    {"product_url": "https://different.test/product", "configuration": {"paper": {"value": "1"}}},
])
def test_missing_changed_conflicting_or_wrong_product_rows_reject(row):
    with pytest.raises(ValueError):
        validate_policy_row(compile()["field_policy"], row)


def service_signature(proof="31"):
    return {"version": 2, "status": "observed", "groups": {
        "proofType": {"status": "selected", "values": [proof]},
        "fileDeliveryForIop": {"status": "selected", "values": ["5"]}}}


def test_actual_canonical_service_context_not_diagnosis_default_controls_validation():
    item = field("proof", kind="radio", source_name="proofType", values=("31", "100"))
    policy = compile(diagnosis([item], {"internal-proof": "31"}))["field_policy"]
    row = {"pricing_context": {"service_context": service_signature()}}
    assert validate_policy_row(policy, row) is row
    row["pricing_context"]["service_context"] = service_signature("100")
    with pytest.raises(ValueError, match="differs"):
        validate_policy_row(policy, row)
    with pytest.raises(ValueError, match="missing or conflicting"):
        validate_policy_row(policy, {})
    # Actual visible controls normalized by BrowserAdapter also bind exactly.
    row = {"configuration": {"prooftype": {"value": "31"}}}
    assert validate_policy_row(policy, row) is row


def test_conflicting_service_context_cannot_be_hidden_by_matching_configuration():
    item = field("proof", kind="radio", source_name="proofType", values=("31", "100"))
    policy = compile(diagnosis([item], {"internal-proof": "31"}))["field_policy"]
    row = {"configuration": {"prooftype": {"value": "31"}},
           "pricing_context": {"service_context": service_signature("100")}}
    with pytest.raises(ValueError, match="missing or conflicting"):
        validate_policy_row(policy, row)
    row["service_context_observed"] = True
    row["service_context"] = []  # Contradicts the retained selected signature.
    with pytest.raises(ValueError, match="context is conflicting"):
        validate_policy_row(policy, row)


@pytest.mark.parametrize("metadata", [
    {"service_context_observed": False},
    {"service_context_observed": True, "service_context": []},
    {"pricing_context": {"service_context": {"status": "unknown"}}},
])
def test_explicit_unknown_or_absent_service_evidence_does_not_accept_matching_selected_default(metadata):
    item = field("proof", kind="radio", source_name="proofType", values=("31", "100"))
    policy = compile(diagnosis([item], {"internal-proof": "31"}))["field_policy"]
    with pytest.raises(ValueError, match="service selection was not observed"):
        validate_policy_row(policy, {"configuration": {"prooftype": {"value": "31"}}, **metadata})


def test_quantity_and_production_bind_from_actual_top_level_row_values():
    fields = [field("quantity", source_id="runsize_abc123", values=("100", "250")),
              field("mode", kind="radio", source_name="productionTime")]
    policy = compile(diagnosis(fields, {"internal-quantity": "100", "internal-mode": "1"}))["field_policy"]
    row = {"quantity": 100, "production": {"value": "1", "label": "Regular"}}
    assert validate_policy_row(policy, row) is row
    row["production"]["value"] = "2"
    with pytest.raises(ValueError, match="differs"):
        validate_policy_row(policy, row)


def test_name_only_generic_browser_row_binding_uses_observed_key_not_printograph_aliases():
    value = diagnosis([field("paper", source_id="", source_name="custom_stock")])
    value["url"] = "https://generic.test/product"
    value["selection"] = normalize_selection(value, 1, [
        {"key": "internal-paper", "include": False, "fixed_values": ["1"]}])
    result = compile(value, urls=[value["url"]])
    assert result["filters"] == {"custom": "1"}  # Existing adapter strips the five-character suffix.
    row = {"configuration": {"custom": {"value": "1"}}}
    assert validate_policy_row(result["field_policy"], row) is row
