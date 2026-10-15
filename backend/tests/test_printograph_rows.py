"""Saved public tables with explicitly synthetic request-start scope metadata."""

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from crawler.price_contract import SOURCE_CONTRACT
from crawler.printograph_rows import (
    RowGenerationError,
    bind_table_scope,
    compare_direct_quote,
    expected_comparison_key,
    generate_table_rows,
    source_service_signature,
)
from crawler.route_dependencies import compare_probe_observations
from test_price_contract import captured

FIXTURES = json.loads(
    (
        Path(__file__).parent / "fixtures" / "printograph_standard_tables.json"
    ).read_text()
)["branches"]


def observation(capture, scope):
    url = urlsplit(capture["url"])
    return {
        "request": {
            "method": "GET",
            "path": url.path,
            "params": dict(parse_qsl(url.query, keep_blank_values=True)),
            "context": deepcopy(scope["request_context"]),
        },
        "status": 200,
        "complete": True,
        "response_id": capture["evidence_id"],
        "data": deepcopy(capture["data"]),
    }


def inputs(branch="1_1", **selections):
    fixture = FIXTURES[branch]
    kind = fixture["base_selection"]["productType"]
    state = captured(product_type=kind)
    state["userSelectedOptions"].update(
        {k: v for k, v in fixture["base_selection"].items() if k != "productType"}
    )
    product_id = str(fixture["captures"]["product_lookup"]["data"]["items"][0]["id"])
    state["userSelectedOptions"].update(
        productId=product_id, coating="5" if kind != "36" else "", **selections
    )
    if kind == "36":
        state["userSelectedOptions"].pop("coating")
    state["coupon"] = {
        "observed": True,
        "mode": "source_state",
        "coupon_object_observed": True,
        "coupon_object_key_count": 0,
        "parameters": {},
        "is_coupon_spec_changed": False,
    }
    context = {
        "origin": "https://www.printograph.com",
        "session_scope": "fixture-session",
        "source_epoch": "fixture-epoch",
        "store_type": "broker",
        "currency": "USD",
        "service_signature": source_service_signature(state),
    }
    scope = bind_table_scope(
        state,
        request_context=context,
        selection_evidence_ids=["synthetic-complete-selection"],
    )
    captures = fixture["captures"]
    args = {
        "lookup": observation(captures["product_lookup"], scope),
        "catalog": observation(captures["option_catalog"], scope),
        "base_table": observation(captures["base_prices"], scope),
        "quantity_support": observation(captures["quantity_support"], scope),
        "option_quantity_support": observation(
            captures["option_quantity_support"], scope
        ),
        "option_tables": {},
        "scope": scope,
        "product_url": "https://www.printograph.com/products/"
        + {"1": "business-cards", "27": "club-flyers", "36": "roll-labels"}[kind]
        + "/order",
    }
    if "rounded_corner_prices" in captures:
        obs = observation(captures["rounded_corner_prices"], scope)
        args["option_tables"][obs["request"]["path"].split("/")[-2]] = obs
    return state, args


def synthetic_option(args, type_id, group_id, prices):
    items = [
        item
        for item in args["catalog"]["data"]["items"]
        if str(item["type"]["id"]) == str(type_id)
        and str(item["group"]["id"]) == str(group_id)
    ]
    assert len(items) == 1
    identifier = str(items[0]["id"])
    base = str(args["lookup"]["data"]["items"][0]["id"])
    args["option_tables"][identifier] = {
        "request": {
            "method": "GET",
            "path": f"/service/rest/v1/products/{base}/options/{identifier}/prices",
            "params": {},
            "context": deepcopy(args["scope"]["request_context"]),
        },
        "status": 200,
        "complete": True,
        "response_id": "synthetic-price-" + identifier,
        "data": {
            "items": [
                {"quantity": q, "markupPrice": amount} for q, amount in prices.items()
            ]
        },
    }
    return identifier


def test_known_baseline_exact_rows_pending_and_not_mutating_inputs():
    state, args = inputs()
    before = deepcopy((state, args))
    result = generate_table_rows(state, **args, quantities=[25, 50, 100, 250])
    assert [row["quantity"] for row in result["rows"]] == [50, 100, 250]
    assert result["rows"][0]["subtotal"] == "4.20"
    assert result["excluded_quantities"] == [
        {"quantity": 25, "reasons": ["quantity_or_turnaround_not_supported"]}
    ]
    assert result["whole_product_complete"] is False
    for row in result["rows"]:
        assert row["verification_status"] == "pending" and row["cart_verified"] is False
        assert row["production"] == {
            "value": "1",
            "status": "requires_configuration_quote",
        }
        assert row["components"][0]["evidence_id"] == args["base_table"]["response_id"]
    assert (state, args) == before


def test_corners_only_exact_selected_catalog_price_is_added():
    state, args = inputs(roundedCorners="2")
    result = generate_table_rows(state, **args, quantities=[50, 250])
    assert len(result["rows"]) == 2
    assert result["rows"][0]["subtotal"] == "7.20"
    assert result["rows"][0]["components"][1]["selected_value"] == "2"
    assert result["rows"][0]["components"][1]["option_id"] in args["option_tables"]


@pytest.mark.parametrize("manual,amount", [("1", "3.00"), ("36", "0.00")])
def test_manual_proof_subselection_has_exact_component_and_context(manual, amount):
    state, args = inputs("1_10", proofType="100", manualProofType=manual)
    synthetic_option(args, manual, 1, {50: amount, 100: amount})
    result = generate_table_rows(state, **args, quantities=[50, 100])
    assert len(result["rows"]) == 2
    row = result["rows"][0]
    assert row["subtotal"] == ("8.60" if manual == "1" else "5.60")
    component = next(c for c in row["components"] if c["field"] == "proofType")
    assert component["selected_value"] == manual and component["amount"] == amount


@pytest.mark.parametrize("branch,expected", [("1_10", [50, 100]), ("27_10", [100])])
def test_real_saved_raised_print_availability_50_vs_100(branch, expected):
    state, args = inputs(branch, raisedPrint="135")
    synthetic_option(args, 135, 50, {50: "10.00", 100: "12.00"})
    result = generate_table_rows(state, **args, quantities=[50, 100])
    assert [row["quantity"] for row in result["rows"]] == expected
    if branch == "27_10":
        assert (
            "selected_option_quantity_or_turnaround_not_supported:raisedPrint:135"
            in result["excluded_quantities"][0]["reasons"]
        )


def test_roll_wind_is_bound_but_has_no_invented_additive_fee():
    state, args = inputs("36_1")
    result = generate_table_rows(state, **args, quantities=[100, 250])
    assert len(result["rows"]) == 2
    assert result["rows"][0]["subtotal"] == "66.23"
    assert [c["field"] for c in result["rows"][0]["components"]] == ["productPrice"]
    assert {c["field"] for c in result["rows"][0]["non_additive_components"]} == {
        "windPosition",
        "proofType",
        "fileDelivery",
    }


@pytest.mark.parametrize("problem", ["missing", "null", "negative"])
def test_source_supported_quantity_with_bad_price_is_unresolved_not_excluded(problem):
    state, args = inputs()
    prices = args["base_table"]["data"]["items"]
    if problem == "missing":
        args["base_table"]["data"]["items"] = [
            row for row in prices if row["quantity"] != 100
        ]
    else:
        next(row for row in prices if row["quantity"] == 100)["markupPrice"] = (
            None if problem == "null" else "-1.00"
        )
    result = generate_table_rows(state, **args, quantities=[50, 100])
    assert len(result["rows"]) == 1
    assert result["unresolved_quantities"][0]["quantity"] == 100
    assert result["excluded_quantities"] == []


def test_exact_price_query_quantity_never_expands_singleton_to_other_tiers():
    state, args = inputs("1_10", proofType="100", manualProofType="1")
    identifier = synthetic_option(args, 1, 1, {50: "3.00"})
    args["option_tables"][identifier]["request"]["params"] = {"quantities": "50"}
    result = generate_table_rows(state, **args, quantities=[50, 100])
    assert [r["quantity"] for r in result["rows"]] == [50]
    assert result["unresolved_quantities"][0]["quantity"] == 100


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s, a: a["base_table"]["request"]["context"].update(source_epoch="stale"),
        lambda s, a: a["base_table"]["request"]["params"].update(cid="unknown-account"),
        lambda s, a: a["lookup"]["request"]["params"].update(turnaround="3"),
        lambda s, a: a["catalog"]["request"].update(
            path="/service/rest/v1/products/999/options"
        ),
        lambda s, a: a["base_table"].update(complete=False),
        lambda s, a: s["userSelectedOptions"].update(fileDelivery="22"),
        lambda s, a: s.pop("coupon"),
    ],
)
def test_cross_context_unknown_or_partial_sources_fail_closed(mutation):
    state, args = inputs()
    mutation(state, args)
    with pytest.raises(RowGenerationError):
        generate_table_rows(state, **args, quantities=[50])


def test_single_current_quote_coupon_probe_cannot_become_bulk_coupon_default():
    state, args = inputs()
    state["coupon"] = {
        "observed": True,
        "mode": "result_only",
        "argument_subtotal": "4.20",
        "result": 0,
    }
    with pytest.raises(RowGenerationError, match="coupon"):
        generate_table_rows(state, **args, quantities=[50, 100])


def test_coupon_requires_independent_quantity_bound_applicability_evidence():
    state, args = inputs()
    state["coupon"] = {
        "observed": True,
        "mode": "parameters",
        "is_coupon_spec_changed": False,
        "valid_for_specs": True,
        "qualification_level": 1,
        "status_id": 1,
        "amount_percent": 10,
        "max_amount": None,
    }
    scope = bind_table_scope(
        state,
        request_context=args["scope"]["request_context"],
        selection_evidence_ids=["synthetic-complete-coupon-selection"],
    )
    args["scope"] = scope
    for key in (
        "lookup",
        "catalog",
        "base_table",
        "quantity_support",
        "option_quantity_support",
    ):
        args[key]["request"]["context"] = deepcopy(scope["request_context"])
    scope["coupon_quantity_evidence"] = {
        "50": {
            "selection_scope": scope["pricing_scope"],
            "evidence_id": "synthetic-eligibility50",
            "coupon": deepcopy(state["coupon"]),
        }
    }
    result = generate_table_rows(state, **args, quantities=[50, 100])
    assert len(result["rows"]) == 1 and result["rows"][0]["subtotal"] == "3.78"
    assert result["rows"][0]["coupon"]["reduction"] == "0.42"
    assert result["unresolved_quantities"][0]["quantity"] == 100


def test_design_service_requires_mode_bound_base_and_option_price_routes():
    state, args = inputs("1_10", proofType="101", fileDelivery="9", designServices="42")
    identifier = synthetic_option(args, 42, 13, {50: "25.00"})
    with pytest.raises(RowGenerationError, match="Design-service"):
        generate_table_rows(state, **args, quantities=[50])
    args["base_table"]["request"]["params"]["designService"] = "true"
    args["option_tables"][identifier]["request"]["params"]["designService"] = "true"
    result = generate_table_rows(state, **args, quantities=[50])
    assert result["rows"][0]["subtotal"] == "30.60"


def test_source_binary_component_rounding_before_total():
    state, args = inputs(roundedCorners="2")
    next(row for row in args["base_table"]["data"]["items"] if row["quantity"] == 50)[
        "markupPrice"
    ] = "1.005"
    option = next(iter(args["option_tables"].values()))
    next(row for row in option["data"]["items"] if row["quantity"] == 50)[
        "markupPrice"
    ] = "2.675"
    result = generate_table_rows(state, **args, quantities=[50])
    assert result["rows"][0]["subtotal"] == "3.67"
    assert [c["amount"] for c in result["rows"][0]["components"]] == ["1.00", "2.67"]


def test_complete_direct_comparison_does_not_upgrade_pending_row_or_legacy():
    state, args = inputs()
    row = generate_table_rows(state, **args, quantities=[50])["rows"][0]
    direct = deepcopy(state)
    direct.update(
        capture_kind="controller", controller_total="4.20", displayed_total="$4.20"
    )
    direct["appliedPrices"] = {"productPrice": "4.20"}
    assert expected_comparison_key(direct) == row["expected_comparison_key"]
    assert compare_direct_quote(row, direct)["status"] == "matched"
    assert row["verification_status"] == "pending"
    direct["appliedPrices"]["productPrice"] = "5.20"
    direct.update(controller_total="5.20", displayed_total="5.20")
    assert compare_direct_quote(row, direct)["status"] == "price_mismatch"
    assert (
        expected_comparison_key({"record_schema_version": 2, "subtotal": "4.20"})
        is None
    )
    assert compare_direct_quote(row, {"subtotal": "4.20"})["status"] == "unresolved"


def test_finite_dependency_certificate_covers_only_exact_tested_request_members():
    state, args = inputs()
    original = deepcopy(args["base_table"])
    original["request"]["params"]["quantities"] = "50"
    second = deepcopy(original)
    second["request"]["params"]["quantities"] = "100"
    second["response_id"] = "synthetic-second-table"
    certificate = compare_probe_observations([original, second], "params.quantities")[
        "certificate"
    ]
    args["base_table"] = original
    args["scope"]["expected_requests"]["base"] = second["request"]
    args["scope"]["dependency_certificates"] = [certificate]
    assert len(generate_table_rows(state, **args, quantities=[100])["rows"]) == 1
    args["scope"]["expected_requests"]["base"]["params"]["quantities"] = "250"
    with pytest.raises(RowGenerationError, match="exact-member"):
        generate_table_rows(state, **args, quantities=[250])


def test_equivalence_certificate_cannot_hide_unsupported_account_price_context():
    state, args = inputs()
    original = deepcopy(args["base_table"])
    original["request"]["params"]["cid"] = "unsupported-account"
    expected = deepcopy(args["base_table"])
    certificate = compare_probe_observations([original, expected], "params.cid")[
        "certificate"
    ]
    args["base_table"] = original
    args["scope"]["expected_requests"]["base"] = expected["request"]
    args["scope"]["dependency_certificates"] = [certificate]
    with pytest.raises(RowGenerationError, match="Account/order"):
        generate_table_rows(state, **args, quantities=[50])
