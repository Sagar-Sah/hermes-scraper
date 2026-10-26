"""Pure declaration checks; fixtures intentionally omit a Cartesian combination."""

from copy import deepcopy
import json

import pytest

from crawler.fourover_options import inspect_option_contract


CHILDREN = [{"child_id": str(child), "sku": "OPAQUE-DO-NOT-DECODE"} for child in (101, 102, 103)]


def configuration():
    return {
        "attributes": {
            "197": {"id": "197", "code": "stock", "label": "Stock", "options": [
                {"id": "11", "label": "16PT C2S", "products": ["101", "102"]},
                {"id": "12", "label": "14PT C2S", "products": ["103"]}]},
            "204": {"id": "204", "code": "colorspec", "label": "Colorspec", "options": [
                {"id": "21", "label": "4/0", "products": ["101", "103"]},
                {"id": "22", "label": "4/4", "products": ["102"]}]},
        },
        "index": {"101": {"197": "11", "204": "21"},
                  "102": {"197": "11", "204": "22"},
                  "103": {"197": "12", "204": "21"}},
    }


def initializer(config=None):
    value = {"#product_addtocart_form": {"configurable": {"spConfig": config or configuration()}}}
    return '<script type="text/x-magento-init">' + json.dumps(value) + '</script>'


def test_exact_source_index_binds_only_declared_children_without_cartesian_expansion():
    children, config = deepcopy(CHILDREN), configuration()
    original = deepcopy(config)
    report = inspect_option_contract(initializer(config), children)
    assert report["status"] == "declared_binding_complete"
    assert report["binding_complete"] is True
    assert report["diagnostic"]["candidate_count"] == 1
    assert [row["child_id"] for row in report["rows"]] == ["101", "102", "103"]
    assert len(report["rows"]) == 3
    assert report["rows"][1]["fields"] == [
        {"attribute_id": "197", "code": "stock", "label": "Stock", "value": "11", "choice": "16PT C2S"},
        {"attribute_id": "204", "code": "colorspec", "label": "Colorspec", "value": "22", "choice": "4/4"},
    ]
    assert report["attributes"][0]["options"][0]["products"] == ["101", "102"]
    assert all(report[key] is False for key in ("source_data_complete", "catalog_complete", "final_eligible"))
    assert "OPAQUE" not in json.dumps(report)
    assert children == CHILDREN and config == original


def test_native_controls_are_separate_document_only_observations_with_numeric_values():
    html = '''<label for="attribute211">Size *</label>
      <select id="attribute211" name="super_attribute[211]">
        <option value="">Choose an option</option>
        <option value="11" selected>4 x 6</option>
        <option value="12" disabled>5 x 7</option>
        <option value="private-value">DO-NOT-RETURN</option>
      </select><select name="unrelated_account"><option value="1">SECRET</option></select>'''
    report = inspect_option_contract(html, CHILDREN)
    assert report["status"] == "unsupported" and not report["binding_complete"]
    assert report["native_select_scope"] == "supplied_document_dom_only_not_full_domain"
    native = report["native_selects"]
    assert len(native) == 1 and native[0]["attribute_id"] == "211"
    assert native[0]["label"] == "Size *" and native[0]["placeholder_count"] == 1
    assert native[0]["choices"] == [
        {"value": "11", "label": "4 x 6", "disabled": False, "selected": True},
        {"value": "12", "label": "5 x 7", "disabled": True, "selected": False},
    ]
    assert report["diagnostic"]["rejected_native_choice_count"] == 1
    assert not any(value in json.dumps(report) for value in ("DO-NOT-RETURN", "SECRET", "private-value"))


def test_json_whitelist_removes_unknown_prices_accounts_and_form_keys():
    config = configuration()
    config.update(account={"password": "SENSITIVE-A"}, optionPrices={"101": "PRICE-MATERIAL"},
                  form_key="FORM-KEY", unknown={"opaque": "UNKNOWN-SCALAR"})
    config["attributes"]["197"]["options"][0]["price"] = "OPTION-PRICE"
    private = {"*": {"Magento_Customer/js/customer-data": {
        "spConfig": configuration(), "access_token": "SECRET-TOKEN"}}}
    html = initializer(config) + '<script type="text/x-magento-init">' + json.dumps(private) + '</script>'
    report = inspect_option_contract(html, CHILDREN)
    assert report["binding_complete"] is True
    encoded = json.dumps(report)
    for value in ("SENSITIVE-A", "PRICE-MATERIAL", "FORM-KEY", "UNKNOWN-SCALAR", "OPTION-PRICE", "SECRET-TOKEN"):
        assert value not in encoded
    assert report["diagnostic"]["sensitive_branches_omitted"] >= 4
    assert report["diagnostic"]["module_summaries"]
    assert not any("Customer" in item["path"] for item in report["diagnostic"]["module_summaries"])


@pytest.mark.parametrize("alter, reason", [
    (lambda config: config["index"].pop("103"), "declared_child_set_mismatch"),
    (lambda config: config["index"].update({"104": {"197": "12", "204": "22"}}), "declared_child_set_mismatch"),
    (lambda config: config["index"]["101"].pop("204"), "incomplete_child_attributes"),
    (lambda config: config["index"]["101"].update({"999": "21"}), "incomplete_child_attributes"),
    (lambda config: config["index"]["101"].update({"197": "99"}), "undeclared_attribute_value"),
    (lambda config: config["attributes"]["197"].update(id="204"), "attribute_identifier_mismatch"),
    (lambda config: config["attributes"]["197"].update(code="colorspec"), "invalid_or_duplicate_attribute_code"),
    (lambda config: config["attributes"]["197"]["options"][0].pop("products"), "missing_option_product_membership"),
    (lambda config: config["attributes"]["197"]["options"][0]["products"].pop(), "option_product_membership_mismatch"),
    (lambda config: config["attributes"]["197"]["options"][0]["products"].append("104"), "duplicate_or_unknown_option_binding"),
    (lambda config: config["attributes"]["197"]["options"].append(deepcopy(config["attributes"]["197"]["options"][0])), "duplicate_or_unknown_option_binding"),
    (lambda config: config["index"]["102"].update({"204": "21"}), "duplicate_child_configuration"),
    (lambda config: config["attributes"]["197"].update(label=""), "invalid_source_label"),
])
def test_incomplete_conflicting_or_nonunique_bindings_produce_no_contract_rows(alter, reason):
    config = configuration()
    alter(config)
    report = inspect_option_contract(initializer(config), CHILDREN)
    assert report["status"] == "partial" and report["reason_code"] == reason
    assert report["binding_complete"] is False
    assert report["attributes"] == [] and report["rows"] == []


def test_duplicate_candidates_are_ambiguous_even_when_identical():
    report = inspect_option_contract(initializer() + initializer(), CHILDREN)
    assert report["status"] == "unsupported"
    assert report["reason_code"] == "ambiguous_option_contract"
    assert report["diagnostic"]["candidate_count"] == 2
    assert report["rows"] == []


def test_missing_index_does_not_decode_child_skus_or_static_spec_lists():
    html = initializer({"attributes": configuration()["attributes"]})
    html += '<script>window.spConfig = ' + json.dumps(configuration()) + ';</script>'
    report = inspect_option_contract(html, CHILDREN)
    assert report["status"] == "unsupported" and report["reason_code"] == "option_contract_missing"
    assert report["rows"] == []


def test_duplicate_json_index_keys_fail_instead_of_silently_replacing_source_values():
    html = initializer().replace('"101": {"197": "11", "204": "21"}',
                                 '"101": {"197": "11", "204": "21"}, "101": {}')
    report = inspect_option_contract(html, CHILDREN)
    assert report["status"] == "partial" and report["reason_code"] == "unreadable_option_contract"
    assert report["rows"] == []


def test_declarative_data_mage_init_is_supported_without_executable_script_parsing():
    from html import escape
    html = '<div data-mage-init="' + escape(json.dumps({"configurable": {"spConfig": configuration()}}), quote=True) + '"></div>'
    report = inspect_option_contract(html, CHILDREN)
    assert report["binding_complete"] is True and len(report["rows"]) == 3


@pytest.mark.parametrize("children", [[], [CHILDREN[0], CHILDREN[0]],
    [{"child_id": True}], [{"child_id": "../101"}], [{"child_id": "0"}], [{"child_id": "١٠١"}], ["101"]])
def test_bad_declared_children_never_create_a_binding(children):
    report = inspect_option_contract(initializer(), children)
    assert report["status"] == "unsupported" and not report["binding_complete"]
    assert report["rows"] == []


def test_native_observation_bounds_are_explicit_instead_of_looking_complete():
    controls = '<select name="super_attribute[211]"><option value="11">Size</option></select>' * 65
    report = inspect_option_contract(controls, CHILDREN)
    assert len(report["native_selects"]) == 64
    assert report["diagnostic"]["native_select_count"] == 65
    assert report["diagnostic"]["native_selects_truncated"] is True
    assert report["binding_complete"] is False


def test_excessive_initializer_or_nested_structure_is_explicitly_partial():
    report = inspect_option_contract('<script type="application/json">{}</script>' * 257, CHILDREN)
    assert report["status"] == "partial" and report["reason_code"] == "initializer_limit"
    nested = configuration()
    for _ in range(42):
        nested = {"wrapper": nested}
    report = inspect_option_contract(initializer(nested), CHILDREN)
    assert report["status"] == "partial" and report["reason_code"] == "declaration_structure_limit"
    assert report["rows"] == []
