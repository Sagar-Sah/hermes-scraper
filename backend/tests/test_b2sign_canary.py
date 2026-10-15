"""Canary plans are offline coverage proposals, never successful tests."""

import copy

import pytest

from crawler.b2sign_canary import prepare_canaries
from crawler.b2sign_catalog import CatalogError, enumerate_catalog, parse_public_product, PINNED_BUNDLE_SHA256
from crawler.b2sign_harvest import make_payload, prepare_candidate, validate_observed_request, quote_only_candidates
from test_b2sign_catalog import catalog, select, simple


def result(cat, candidate, subtotal="56.26", context="saved-context"):
    safe = prepare_candidate(cat, candidate)
    return {"status": "quoted", "candidate": safe, "candidate_id": safe["candidate_id"], "context_id": context,
            "request": validate_observed_request(make_payload(cat, safe), cat, safe),
            "quote": {"subtotal": subtotal, "unit_price": subtotal, "quantity": safe["quantity"]},
            "captured_at": "2026-09-27T00:00:00Z"}


@pytest.fixture(scope="module")
def pole_plan():
    cat = catalog()
    rows = list(enumerate_catalog(cat, quantities=[1]))
    saved = [result(cat, row) for row in rows]
    return prepare_canaries(cat, saved, quantities=[1, 10, 100, 101])


def test_pole_requires_all219_sizes_and_never_counts_saved_quotes_as_canaries(pole_plan):
    plan = pole_plan
    assert plan["minimum_value_coverage_cases"] == 219
    assert plan["dimension_value_counts"]["_size"] == 219
    assert plan["selected_count"] >= 219
    assert plan["option_values_covered_by_plan"] is True
    sizes = {case["candidate"]["values"]["_size"] for case in plan["cases"] if case["candidate"]["visible"]["_size"]}
    assert len(sizes) == 219
    assert plan["canaries_passed"] == 0 and plan["prices_verified"] is False
    assert plan["catalog_complete"] is False and plan["final_eligible"] is False
    assert all(case["verification_status"] == "not_run" and not case["price_passed"] for case in plan["cases"])


def test_quantity_package_wind_boundaries_and_missing_production_scopes(pole_plan):
    plan = pole_plan
    assert {case["candidate"]["quantity"] for case in plan["cases"]} == {1, 10, 100, 101}
    assert {case["candidate"]["values"]["hardware"] for case in plan["cases"]} == {"p", "0", "h"}
    assert {case["candidate"]["values"]["windslit"] for case in plan["cases"] if case["candidate"]["visible"]["windslit"]} == {"0", "1"}
    assert all(item["kind"] == "production" for item in plan["uncovered_requirements"])
    assert {item["value"] for item in plan["uncovered_requirements"]} == {"v0", "v5"}
    assert plan["plan_scope_complete"] is False
    assert all(case["expected_subtotal"] is None for case in plan["cases"] if case["candidate"]["quantity"] != 1)
    assert any(case["reference_status"] == "matching_saved_quote" for case in plan["cases"])
    assert all(case["candidate"]["values"]["hem"] == "0" for case in plan["cases"])
    assert any(req["kind"] == "size_boundary_in_option_branch" for req in plan["requirements"])


def test_budget_remains_incomplete_instead_of_downgrading_to_boundary_sampling():
    plan = prepare_canaries(catalog(), [], quantities=[1, 10, 100, 101], max_canaries=8)
    assert plan["selected_count"] == 8
    assert plan["option_values_covered_by_plan"] is False
    assert sum(req["kind"] == "option_value" and req["field"] == "_size" for req in plan["uncovered_requirements"]) >= 211
    assert any(req["reason"] == "canary_budget_exhausted" for req in plan["uncovered_requirements"])


def test_open_size_canary_uses_harvested_explicit_scope():
    cat = parse_public_product({'id': 901, 'title': 'Open Size', 'url': '/open-size',
        'attrs': {'attrs': [{'key': '_size', 'label': 'Size', 'component': 'size',
            'options': [{'key': 'width', 'label': 'Width'}, {'key': 'height', 'label': 'Height'}],
            'component_props': {'max': [{'match': [], 'value': '48x36'}]}}],
            'turnaround': [{'match': [{'key': '_qty', 'op': '>', 'value': 0}],
                            'options': [{'key': 'v1', 'label': 'Next Day'}]}]}},
        source_url='https://www.b2sign.com/open-size', source_bundle_sha256=PINNED_BUNDLE_SHA256)
    scope = {'_size': ['12x12', '24x36']}
    rows = list(enumerate_catalog(cat, quantities=[101], explicit_sizes=scope))
    saved = [result(cat, row) for row in rows]
    plan = prepare_canaries(cat, saved, quantities=[101], explicit_sizes=scope,
                            scope_candidates=[item['candidate'] for item in saved])
    assert plan['selected_count'] == 2
    assert {case['candidate']['values']['_size'] for case in plan['cases']} == set(scope['_size'])
    assert plan['catalog_complete'] is False


def test_scoped_canaries_do_not_select_unharvested_supplier_options():
    cat = catalog(simple([select('stock', ['one', 'two', 'three'])]))
    rows = list(enumerate_catalog(cat, quantities=[101]))
    saved = [result(cat, rows[1])]
    plan = prepare_canaries(cat, saved, quantities=[101],
                            scope_candidates=[saved[0]['candidate']])
    assert plan['selected_count'] == 1
    assert plan['cases'][0]['candidate']['values']['stock'] == saved[0]['candidate']['values']['stock']
    assert plan['cases'][0]['reference_status'] == 'matching_saved_quote'
    assert plan['catalog_complete'] is False


def test_exact_references_do_not_cross_configuration_quantity_or_contract():
    cat = catalog(simple([select("stock", ["one", "two"])]))
    rows = list(enumerate_catalog(cat, quantities=[1, 10]))
    good = result(cat, rows[0], "10.00")
    wrong_request = copy.deepcopy(good)
    wrong_request["candidate"]["values"]["stock"] = "two"
    wrong_contract = copy.deepcopy(good)
    wrong_contract["candidate"]["contract_sha256"] = "wrong"
    plan = prepare_canaries(cat, [good, wrong_request, wrong_contract], quantities=[1, 10])
    assert plan["rejected_reference_count"] == 2
    referenced = [case for case in plan["cases"] if case["expected_subtotal"] is not None]
    assert len(referenced) == 1
    assert referenced[0]["candidate"]["values"]["stock"] == "one" and referenced[0]["candidate"]["quantity"] == 1
    assert plan["plan_scope_complete"] is True
    assert plan["canaries_passed"] == 0


def test_conflicting_same_configuration_prices_remain_conflicted():
    cat = catalog(simple([select("stock", ["one"])]))
    row = next(enumerate_catalog(cat, quantities=[1]))
    plan = prepare_canaries(cat, [result(cat, row, "10", "old"), result(cat, row, "11", "new")], quantities=[1])
    case = plan["cases"][0]
    assert case["reference_status"] == "conflicting_saved_quotes"
    assert case["expected_subtotal"] is None
    assert {ref["context_id"] for ref in case["reference_quotes"]} == {"old", "new"}


def test_hardware_without_production_control_can_plan_exact_quote_checks():
    cat = parse_public_product({'id': 202, 'title': 'Hard Case Only', 'url': '/hard-case-only',
        'attrs': {'attrs': [], 'designproof': False}},
        source_url='https://www.b2sign.com/hard-case-only', source_bundle_sha256=PINNED_BUNDLE_SHA256)
    rows = quote_only_candidates(cat, [1, 10])
    saved = [result(cat, row, str(214.5 * row['quantity'])) for row in rows]
    plan = prepare_canaries(cat, saved, quantities=[1, 10], scope_candidates=rows)
    assert plan['selected_count'] == 2
    assert {case['candidate']['turnaround'] for case in plan['cases']} == {None}
    assert all(case['reference_status'] == 'matching_saved_quote' for case in plan['cases'])
    assert plan['catalog_complete'] is False and plan['final_eligible'] is False


def test_open_fields_unsupported_rules_and_truncated_candidate_pool_remain_gaps():
    cat = catalog(simple([select("stock", ["one", "two"]),
        {"key": "width", "component": "input", "options": [], "component_props": {"format": "number"}}]))
    plan = prepare_canaries(cat, [], quantities=[1])
    assert plan["selected_count"] == 0
    assert plan["plan_scope_complete"] is False
    assert plan["enumeration"]["gaps"]
    plan = prepare_canaries(catalog(), [], quantities=[1], max_candidate_pool=3)
    assert plan["candidate_pool_count"] == 3
    assert plan["enumeration"]["enumeration_stopped"] is True
    assert plan["option_values_covered_by_plan"] is False


def test_planner_is_deterministic_and_requires_explicit_scope():
    cat = catalog(simple([select("stock", ["one", "two"])]))
    first = prepare_canaries(cat, [], quantities=[1, 10])
    second = prepare_canaries(cat, [], quantities=[1, 10])
    assert first == second
    for quantities in [None, [], [True], [0]]:
        with pytest.raises(CatalogError):
            prepare_canaries(cat, [], quantities=quantities)
