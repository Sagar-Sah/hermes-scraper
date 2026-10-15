"""Offline source-derived catalog fixtures: no quote calls or product writes."""

import base64
import copy
import json
from urllib.parse import quote

import pytest

from crawler.b2sign_catalog import (
    CatalogError, PINNED_BUNDLE_SHA256, enumerate_catalog, evaluate_condition,
    parse_public_product, production_choices, size_bound_holds_without_shipping,
)
from crawler.b2sign_pilot import load_contract


URL = "https://www.b2sign.com/pole-banner-set"


def test_open_size_intersection_accepts_only_sizes_valid_for_both_shipping_modes():
    limits = [
        {'match': [{'key': '_shipping', 'logic': 'and', 'op': '=', 'value': 'storepickup'}], 'value': '96x48'},
        {'match': [{'key': '_shipping', 'logic': 'and', 'op': '=', 'value': 'dropship'}], 'value': '48x38'},
    ]
    assert size_bound_holds_without_shipping(limits, {'_qty': 1}, '12x18', 'max') is True
    assert size_bound_holds_without_shipping(limits, {'_qty': 1}, '60x40', 'max') is False
    assert size_bound_holds_without_shipping(limits[:1], {'_qty': 1}, '12x18', 'max') is None
    assert size_bound_holds_without_shipping([{'match': [{'key': '_shipping', 'op': '!=', 'value': 'dropship'}], 'value': '96x48'}], {'_qty': 1}, '12x18', 'max') is None


def test_compound_dropship_size_limits_prove_only_source_supported_sizes():
    shipping=lambda mode:{'key':'_shipping','logic':'and','op':'=','value':mode}
    size=lambda op,n:{'key':'_size','logic':'and','op':op,'value':n}
    limits=[{'match':[shipping('storepickup')],'value':'96x48'},
            {'match':[shipping('dropship'),size('ss>','30')],'value':'46x38'},
            {'match':[shipping('dropship'),size('ls>','46')],'value':'48x30'}]
    for value in ('12x18','40x35','47x28','46x38'):
        assert size_bound_holds_without_shipping(limits,{'_shipping':'dropship','_size':value},value,'max') is True
    for value in ('48x36','49x38'):
        assert size_bound_holds_without_shipping(limits,{'_shipping':'dropship','_size':value},value,'max') is False
    assert size_bound_holds_without_shipping(limits,{'_shipping':'storepickup','_size':'49x38'},'49x38','max') is True


def raw_pole():
    contract = load_contract()
    return {"props": {"item": {"id": 31, "title": "Pole Banner Set", "url": "/pole-banner-set", "attrs": contract["attrs"], "fields": []},
                      "customer": {"email": "PRIVATE_EMAIL", "address": "PRIVATE_ADDRESS"},
                      "quoteResult": {"quote": [{"subtotal": 56.26}]}}}


def catalog(raw=None, **kwargs):
    return parse_public_product(raw_pole() if raw is None else raw, source_url=URL,
                                source_bundle_sha256=kwargs.pop("source_bundle_sha256", PINNED_BUNDLE_SHA256), **kwargs)


def simple(attrs, production=None):
    return {"id": 999, "url": "/pole-banner-set", "title": "Fixture Product", "attrs": {
        "attrs": attrs, "turnaround": production or [{"match": [{"key": "_qty", "op": ">", "value": 0}], "options": [{"key": "regular", "label": "Regular"}]}]}}


def select(key, choices, **kwargs):
    return {"key": key, "label": key.title(), "component": "select", "options": [
        {"key": choice, "label": str(choice), "default": index == 0} for index, choice in enumerate(choices)], **kwargs}


def test_open_size_requires_explicit_bounded_scope():
    raw = simple([{"key": "_size", "label": "Size", "component": "size",
                   "options": [{"key": "width", "label": "Width"}, {"key": "height", "label": "Height"}],
                   "component_props": {"min": [{"match": [], "value": "6x6"}],
                                       "max": [{"match": [], "value": "48x36"}]}}])
    cat = catalog(raw)
    assert list(enumerate_catalog(cat, quantities=[1])) == []
    scope = enumerate_catalog(cat, quantities=[1], explicit_sizes={"_size": ["24x36", "60x40"]})
    rows = list(scope)
    assert [row["values"]["_size"] for row in rows] == ["24x36"]
    assert {"reason": "explicit_size_scope_only", "field": "_size"} in scope.summary()["gaps"]
    assert scope.summary()["catalog_complete"] is False
    with pytest.raises(CatalogError, match="invalid_explicit_size_scope"):
        enumerate_catalog(cat, quantities=[1], explicit_sizes={"_size": ["0x36"]})


@pytest.mark.parametrize("form", ["dict", "json", "plain_html", "encoded_html"])
def test_public_parsing_keeps_all_dimensions_and_excludes_account_and_prices(form):
    raw = raw_pole()
    data = raw
    if form == "json":
        data = json.dumps(raw)
    elif form in {"plain_html", "encoded_html"}:
        value = json.dumps(raw)
        if form == "encoded_html":
            value = base64.b64encode(quote(value).encode()).decode()
        else:
            value = value.replace('"', '&quot;')
        data = '<div data-page="' + value + '"></div>'
    result = catalog(data)
    assert result["source_product_id"] == "31"
    assert {field["key"] for field in result["dimensions"]} == {"hardware", "_size", "pole_width", "sides", "material", "polepocket", "hem", "grommet", "windslit"}
    size = next(field for field in result["dimensions"] if field["key"] == "_size")
    assert size["domain_kind"] == "finite_size_presets"
    assert len(size["axes"][0]["values"]) * len(size["axes"][1]["values"]) == 219
    assert result["quantity_domain"]["values"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert "56.26" not in json.dumps(result)
    assert result["catalog_complete"] is False and result["prices_verified"] is False


def test_pole_finite_quantity100_states_exclude_impossible_visible_combinations():
    iterator = enumerate_catalog(catalog(), quantities=[100])
    rows = list(iterator)
    assert len(rows) == 879  # 2 graphic packages ×219sizes×2wind choices +3hardware widths
    assert len({row["candidate_key"] for row in rows}) == 879
    hardware = [row for row in rows if row["values"]["hardware"] == "h"]
    assert len(hardware) == 3
    assert {row["values"]["pole_width"] for row in hardware} == {"18", "24", "30"}
    assert all(row["visible"]["_size"] is False and row["values"]["_size"] == "18x24" for row in hardware)
    graphic = [row for row in rows if row["values"]["hardware"] != "h"]
    assert all(row["values"]["pole_width"] == row["values"]["_size"].split("x")[0] for row in graphic)
    assert all(row["values"]["hem"] == "0" and row["visible"]["hem"] is False for row in rows)
    assert all(row["production"]["value"] == "v1" and row["status"] == "source_rule_candidate_requires_quote" for row in rows)
    assert iterator.summary()["finite_scope_exhausted"] is True
    assert iterator.summary()["catalog_complete"] is False
    assert iterator.summary()["hidden_state_history_exhausted"] is False


def test_missing_cutoff_context_preserves_regular_but_marks_unknown_rush():
    result = production_choices(catalog(), {"_size": "18x24"}, 1)
    assert result["status"] == "production_availability_unresolved"
    assert result["choices"][0]["available"] is True
    assert result["choices"][1]["available"] is None
    context = {"_time": "10:00:00", "_business_day": True}
    resolved = production_choices(catalog(), {"_size": "18x24"}, 1, context)
    assert resolved["status"] == "resolved"
    assert [choice["available"] for choice in resolved["choices"]] == [True, True]
    assert production_choices(catalog(), {}, 101)["choices"][0]["value"] == "v3"
    assert production_choices(catalog(), {}, 201)["choices"][0]["value"] == "v5"


@pytest.mark.parametrize("values,rule,expected", [
    ({"a": "2"}, {"key": "a", "value": "10", "op": ">"}, True),  # JS string comparison
    ({"a": "2"}, {"key": "a", "value": 10, "op": ">"}, False),
    ({"a": "24x18"}, {"key": "a", "value": "20x30", "op": "inside"}, True),
    ({"a": "40x10"}, {"key": "a", "value": "30x20", "op": "outside"}, False),  # both axes, source rule
    ({"a": "18x24"}, {"key": "a", "value": "400", "op": "area>"}, True),
    ({}, {"key": "missing", "value": None}, None),
    ({"a": "x"}, {"key": "a", "value": "x", "op": "regex"}, None),
    ({"a": 1}, {"key": "a", "value": ["1", 2], "op": "in"}, False),
    ({"a": "x"}, {"key": "a", "value": {"x": 0}, "op": "exists"}, True),
    ({"a": "x"}, [{"key": "a", "value": "x"}, {"key": "missing", "value": 1, "logic": "or"}], True),
    ({"a": "x"}, [{"key": "a", "value": "x"}, {"key": "a", "value": "x", "logic": "and_not"}], True),
    ({"a": "x"}, [{"key": "a", "value": "y"}, {"key": "a", "value": "x", "logic": "and_not"}], False),
    ({"a": "x"}, [{"key": "a", "value": "x", "logic": "or_not"}], True),
    ({}, [{"key": "missing", "value": "x", "logic": "or_not"}], False),
])
def test_source_condition_supported_semantics_and_unknowns(values, rule, expected):
    assert evaluate_condition(values, rule) is expected


def test_generic_dependencies_hidden_defaults_and_duplicates_are_not_flat_cartesian():
    mode = select("mode", ["printed", "hardware"])
    color = select("color", ["black", "red"], visible={"key": "mode", "value": "printed"})
    trim = select("trim", ["plain", "round"])
    trim["options"][1]["visible"] = {"key": "color", "value": "red"}
    enumeration = enumerate_catalog(catalog(simple([mode, color, trim])), quantities=[5])
    rows = list(enumeration)
    assert len(rows) == 4  # printed black/plain, red/plain+round, hardware hidden black/plain
    assert len([row for row in rows if row["values"]["mode"] == "hardware"]) == 1
    assert next(row for row in rows if row["values"]["mode"] == "hardware")["values"]["color"] == "black"
    assert enumeration.summary()["finite_scope_exhausted"] is True


def test_unknown_rule_open_dimension_unreviewed_bundle_and_bounds_remain_explicit():
    field = {"key": "length", "label": "Custom length", "component": "input", "options": [], "component_props": {"format": "number", "min": 1, "max": 100, "step": .5}}
    cat = catalog(simple([field]))
    assert cat["dimensions"][0]["domain_kind"] == "open_numeric"
    enum = enumerate_catalog(cat, quantities=[10])
    assert list(enum) == []
    assert {gap["reason"] for gap in enum.summary()["gaps"]} >= {"open_numeric", "unsupported_or_open_dimension"}
    unknown = catalog(simple([select("stock", ["a"], visible={"key": "stock", "op": "regex", "value": "a"})]))
    enum = enumerate_catalog(unknown, quantities=[10])
    assert list(enum) == []
    assert {gap["reason"] for gap in enum.summary()["gaps"]} == {"attribute_availability_unresolved"}
    enum = enumerate_catalog(catalog(source_bundle_sha256="0" * 64), quantities=[100])
    assert list(enum) == []
    assert enum.summary()["gaps"] == [{"reason": "unreviewed_source_bundle"}]


def test_explicit_scope_limit_and_lazy_consumer_do_not_claim_completion():
    limited = enumerate_catalog(catalog(), quantities=[100], max_candidates=3)
    assert len(list(limited)) == 3
    assert limited.summary()["enumeration_stopped"] is True
    assert limited.summary()["finite_scope_exhausted"] is False
    lazy = enumerate_catalog(catalog(), quantities=[100])
    next(lazy)
    assert lazy.summary()["finite_scope_exhausted"] is False
    for invalid in [None, [], [True], [0], [1, 1]]:
        with pytest.raises(CatalogError, match="explicit_positive_quantities_required"):
            enumerate_catalog(catalog(), quantities=invalid)


def test_unknown_earlier_production_rule_blocks_later_fallback_and_special_modes_disable_regular():
    raw = simple([select("size", ["small"])], [
        {"match": {"key": "unknown_external", "value": True}, "options": [{"key": "rush", "label": "Rush"}]},
        {"match": True, "options": [{"key": "regular", "label": "Regular"}]}])
    assert production_choices(catalog(raw), {}, 1)["status"] == "production_match_unresolved"
    raw["attrs"]["turnaround"] = [{"match": True, "options": [{"key": "v-1", "label": "Stock"}, {"key": "regular", "label": "Regular"}]}]
    result = production_choices(catalog(raw), {}, 1)
    assert [choice["available"] for choice in result["choices"]] == [True, False]


def test_duplicate_active_source_values_and_changed_earlier_dependencies_fail_closed():
    duplicated = select("color", ["red", "red"])
    enum = enumerate_catalog(catalog(simple([duplicated])), quantities=[5])
    assert list(enum) == []
    assert {gap["reason"] for gap in enum.summary()["gaps"]} == {"ambiguous_active_option_values"}
    first = select("first", ["a"])
    second = select("second", ["b"], action=[{"type": "change", "change": [{"key": "first", "value": "invalid"}]}])
    enum = enumerate_catalog(catalog(simple([first, second])), quantities=[5])
    assert list(enum) == []
    assert enum.summary()["gaps"] == [{"reason": "changed_value_requires_source_reset", "field": "first"}]


def test_disabled_controls_keep_explicit_source_empty_value_and_quantity_never_implies_price_scope():
    mode = select("mode", ["on", "off"])
    finish = select("finish", ["gloss", "matte"], disabled={"key": "mode", "value": "off"})
    enum = enumerate_catalog(catalog(simple([mode, finish])), quantities=[7, 99])
    rows = list(enum)
    assert len(rows) == 6
    off = [row for row in rows if row["values"]["mode"] == "off"]
    assert len(off) == 2 and all(row["values"]["finish"] == "" for row in off)
    assert len({row["candidate_key"] for row in rows}) == 6
    assert all(row["pricing_independence"] == "not_established" for row in rows)
    assert enum.summary()["finite_scope_exhausted"] is True


def test_unnamed_synthetic_select_entry_is_not_a_product_choice():
    holes = select("holespunch", ["0", "_v4", "custom"])
    holes["options"][1]["label"] = ""
    enum = enumerate_catalog(catalog(simple([holes])), quantities=[1])
    assert {row["values"]["holespunch"] for row in enum} == {"0", "custom"}
    assert enum.summary()["finite_scope_exhausted"] is True


def test_custom_select_entry_is_held_for_calculator_instead_of_quoted_as_plain_choice():
    holes = select("holespunch", ["0", "_v4", "custom"])
    holes["options"][1].update(component="divider", label="")
    holes["options"][2].update(component="custom", component_props={"type": "hole"})
    enum = enumerate_catalog(catalog(simple([holes])), quantities=[1])
    assert [row["values"]["holespunch"] for row in enum] == ["0"]
    assert enum.summary()["gaps"] == [{"reason": "custom_option_requires_calculator", "field": "holespunch"}]


def test_public_context_is_strict_and_numeric_membership_matches_js_number_type():
    assert evaluate_condition({"value": 1.0}, {"key": "value", "op": "in", "value": [1]}) is True
    assert evaluate_condition({"value": True}, {"key": "value", "op": "in", "value": [1]}) is False
    for context in [{"_shipping": {}}, {"_time": "SECRET"}, {"_business_day": 1}, {"cookie": "SECRET"}]:
        with pytest.raises(CatalogError):
            enumerate_catalog(catalog(), quantities=[1], context=context)


@pytest.mark.parametrize("change", [
    lambda raw: raw["props"]["item"].update(url="https://private.invalid/pole-banner-set"),
    lambda raw: raw["props"]["item"].update(id=True),
    lambda raw: raw["props"]["item"]["attrs"]["attrs"].append(copy.deepcopy(raw["props"]["item"]["attrs"]["attrs"][0])),
])
def test_product_identity_and_duplicate_attribute_keys_rejected(change):
    raw = raw_pole()
    change(raw)
    with pytest.raises(CatalogError):
        catalog(raw)
