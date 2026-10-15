"""Pure contract tests and an intercepted local Chromium controller fixture."""

import json
import re
from pathlib import Path

import pytest

from crawler.price_contract import (
    BASE_FIELDS,
    CONTROLLER_PRICE_STATE_JS,
    NORMAL_CONTEXT_FLAGS,
    OPTION_FIELDS,
    SOURCE_CONTRACT,
    calculate_source_total,
    normalize_price_context,
    round_source_component,
)


def captured(*, product_type="1", base="5.60"):
    selections = {key: "1" for key in BASE_FIELDS[product_type]}
    selections.update({key: "" for key in OPTION_FIELDS[product_type]})
    selections.update(
        quantity="50",
        productId="100000009",
        productionTime="1",
        proofType="31",
        manualProofType="1",
        designServices="42",
        fileDelivery="5",
    )
    if product_type == "36":
        selections["windPosition"] = "32"
    return {
        "capture_kind": "explicit_request",
        "observed": True,
        "settled": True,
        "source_contract": dict(SOURCE_CONTRACT),
        "product_type": product_type,
        "store_type": "broker",
        "currency": "USD",
        "userSelectedOptions": selections,
        "field_options": list(OPTION_FIELDS[product_type]),
        "appliedPrices": {"productPrice": base},
        "other_context": {key: False for key in NORMAL_CONTEXT_FLAGS},
        "coupon": {
            "observed": True,
            "mode": "parameters",
            "is_coupon_spec_changed": False,
            "valid_for_specs": False,
            "qualification_level": "1",
            "status_id": "1",
            "amount_percent": None,
            "max_amount": None,
        },
        "displayed_total": base,
    }


@pytest.mark.parametrize("product_type", ["1", "2", "27", "36"])
def test_known_explicit_baseline_has_no_inferred_delivery_fee(product_type):
    value = captured(product_type=product_type)
    result = calculate_source_total(value)
    assert result["status"] == "complete"
    assert result["comparison"] == "matched"
    assert result["subtotal"] == "5.60"
    assert result["cart_verified"] is False
    assert result["fresh_source_verified"] is False
    assert [item["field"] for item in result["components"]] == ["productPrice"]


def test_postcards_broker_fields_match_pinned_source_bundle():
    source = Path(__file__).resolve().parents[4] / 'work' / 'planner-contracts' / 'orderForm.js'
    if not source.exists():
        pytest.skip('Saved supplier bundle unavailable outside workspace')
    text = source.read_text()
    declarations = re.findall(r'(?<![\w])2:\{specs:(\[[^\]]*\]),options:(\[[^\]]*\])', text)
    assert len(declarations) == 2
    base, broker = declarations[1]
    assert json.loads(base) == list(BASE_FIELDS['2'])
    assert json.loads(broker) == list(OPTION_FIELDS['2'])


def test_manual_mode_requires_secondary_choice_not_a_residual_guess():
    value = captured()
    value["userSelectedOptions"].update(proofType="100", manualProofType="1")
    value["appliedPrices"]["proofType"] = {"1": "3.00", "36": "0.00"}
    value["displayed_total"] = "8.60"
    manual = calculate_source_total(value)
    assert manual["status"] == "complete" and manual["comparison"] == "matched"
    assert manual["subtotal"] == "8.60"
    value["userSelectedOptions"]["manualProofType"] = "36"
    no_proof = calculate_source_total(value)
    assert no_proof["subtotal"] == "5.60"
    assert no_proof["comparison"] == "mismatch"
    assert no_proof["context_hash"] != manual["context_hash"]
    del value["userSelectedOptions"]["manualProofType"]
    assert calculate_source_total(value)["status"] == "unresolved"


def test_hidden_finishing_components_and_design_package_all_count():
    value = captured(base="10.00")
    value["userSelectedOptions"].update(
        proofType="101",
        designServices="42",
        fileDelivery="9",
        roundedCorners="2",
        spotCoating="39",
        raisedPrint="135",
        raisedFoilColor="141",
    )
    value["appliedPrices"].update(
        roundedCorners={"2": "3.00"},
        spotCoating={"39": "4.00"},
        raisedPrint={"135": "5.00"},
        raisedFoilColor={"141": "6.00"},
        designServices={"42": "25.00"},
    )
    value["displayed_total"] = "53.00"
    result = calculate_source_total(value)
    assert result["comparison"] == "matched"
    assert result["subtotal"] == "53.00"
    assert len(result["components"]) == 6


def test_missing_active_price_is_unknown_but_explicit_zero_can_reconcile():
    value = captured()
    value["userSelectedOptions"]["roundedCorners"] = "2"
    assert calculate_source_total(value)["status"] == "unresolved"
    value["appliedPrices"]["roundedCorners"] = {"2": "0.00"}
    result = calculate_source_total(value)
    assert result["status"] == "complete"
    assert result["fresh_source_verified"] is False  # Initial zero is not fetch proof.


def test_missing_hidden_state_cannot_be_assumed_empty():
    value = captured()
    del value["userSelectedOptions"]["raisedFoilColor"]
    result = calculate_source_total(value)
    assert "missing_or_invalid_selection:raisedFoilColor" in result["reasons"]
    assert result["subtotal"] is None


@pytest.mark.parametrize("mode,delivery", [("31", "6"), ("100", "22"), ("101", "5")])
def test_delivery_group_is_explicitly_bound_to_proof_mode(mode, delivery):
    value = captured()
    value["userSelectedOptions"].update(proofType=mode, fileDelivery=delivery)
    assert (
        "unsupported_delivery_for_proof_mode"
        in normalize_price_context(value)["reasons"]
    )


@pytest.mark.parametrize(
    "amount,expected",
    [("1.005", "1.00"), ("2.675", "2.67"), ("1,234.50", "1234.50"), ("0.615", "0.61")],
)
def test_component_cache_matches_javascript_binary_rounding(amount, expected):
    assert round_source_component(amount) == expected


@pytest.mark.parametrize(
    "amount",
    ["-3.00", "(3.00)", "$-3.00", "1,00.00", "NaN", float("inf"), True, "3.00xyz"],
)
def test_invalid_or_negative_components_rejected(amount):
    with pytest.raises(ValueError):
        round_source_component(amount)
    value = captured()
    value["appliedPrices"]["productPrice"] = amount
    assert calculate_source_total(value)["status"] == "unresolved"


def test_context_hash_excludes_money_and_unrelated_sensitive_fields():
    value = captured()
    context = normalize_price_context(value)
    value["appliedPrices"]["productPrice"] = "15.60"
    value["userSelectedOptions"]["customerEmail"] = "PRIVATE_SENTINEL"
    value["unrelatedSecret"] = "PRIVATE_SENTINEL"
    changed = normalize_price_context(value)
    assert changed["context_hash"] == context["context_hash"]
    assert "PRIVATE_SENTINEL" not in json.dumps(calculate_source_total(value))
    value["userSelectedOptions"]["fileDelivery"] = "22"
    assert normalize_price_context(value)["context_hash"] != context["context_hash"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda c: c["source_contract"].update(common_sha256="changed"),
            "unreviewed_source_contract",
        ),
        (lambda c: c.update(settled=False), "state_not_settled"),
        (
            lambda c: c["field_options"].append("unknownPricedField"),
            "unreviewed_option_field_order",
        ),
        (
            lambda c: c["other_context"].update(is_admin=True),
            "unsupported_context:is_admin",
        ),
        (
            lambda c: c["other_context"].pop("has_price_group"),
            "missing_context_flag:has_price_group",
        ),
        (lambda c: c.pop("coupon"), "coupon_state_not_observed"),
    ],
)
def test_incomplete_or_unreviewed_context_never_computes_final_total(mutation, reason):
    value = captured()
    mutation(value)
    result = calculate_source_total(value)
    assert result["status"] == "unresolved" and result["subtotal"] is None
    assert reason in result["reasons"]


def test_unexpected_no_charge_bucket_is_not_silently_ignored():
    value = captured()
    value["appliedPrices"]["coating"] = {"": "5.00"}
    assert (
        "unexpected_applied_price_bucket:coating"
        in calculate_source_total(value)["reasons"]
    )


def test_coupon_percentage_and_cap_follow_reviewed_order():
    value = captured(base="13.60")
    value["coupon"].update(valid_for_specs=True, amount_percent="25", max_amount="2.00")
    value["displayed_total"] = "11.60"
    result = calculate_source_total(value)
    assert result["status"] == "complete" and result["comparison"] == "matched"
    assert result["coupon_reduction"] == "2.00"
    value["coupon"]["max_amount"] = None
    value["displayed_total"] = "10.20"
    assert calculate_source_total(value)["comparison"] == "matched"
    value["coupon"]["is_coupon_spec_changed"] = True
    assert calculate_source_total(value)["subtotal"] == "13.60"


def test_coupon_zero_cap_retains_javascript_string_vs_number_truthiness():
    value = captured(base="10.00")
    value["coupon"].update(valid_for_specs=True, amount_percent=10, max_amount=0)
    numeric = calculate_source_total(value)
    assert numeric["subtotal"] == "9.00"
    value["coupon"]["max_amount"] = "0"
    string = calculate_source_total(value)
    assert string["subtotal"] == "10.00"
    assert string["context_hash"] != numeric["context_hash"]


def test_noncanonical_coupon_status_is_unresolved_not_misclassified_inactive():
    value = captured()
    value["coupon"].update(
        valid_for_specs=True, qualification_level="01", amount_percent=10
    )
    assert calculate_source_total(value)["status"] == "unresolved"


def test_coupon_result_zero_is_current_quote_only_and_bound_to_subtotal():
    value = captured()
    value["coupon"] = {
        "observed": True,
        "mode": "result_only",
        "argument_subtotal": "5.60",
        "result": 0,
    }
    result = calculate_source_total(value)
    assert result["status"] == "complete"
    assert result["reusable_coupon_rule"] is False
    value["appliedPrices"]["productPrice"] = "6.60"
    result = calculate_source_total(value)
    assert "coupon_result_bound_to_different_subtotal" in result["reasons"]


def test_current_coupon_probe_money_does_not_change_context_identity():
    value = captured()
    value["coupon"] = {
        "observed": True,
        "mode": "result_only",
        "argument_subtotal": "5.60",
        "result": 0,
    }
    first = normalize_price_context(value)["context_hash"]
    value["appliedPrices"]["productPrice"] = "6.60"
    value["coupon"]["argument_subtotal"] = "6.60"
    assert normalize_price_context(value)["context_hash"] == first


def test_fractional_cent_display_is_not_silently_rounded_into_a_match():
    value = captured()
    value["displayed_total"] = "5.604"
    assert calculate_source_total(value)["status"] == "unresolved"


def test_active_coupon_result_without_parameters_is_not_derived_math():
    value = captured()
    value["coupon"] = {
        "observed": True,
        "mode": "result_only",
        "argument_subtotal": "5.60",
        "result": "$4.60",
    }
    result = calculate_source_total(value)
    assert result["status"] == "unresolved"
    assert result["observed_coupon_total"] == "4.60"
    assert result["subtotal"] is None


def test_observed_empty_source_coupon_is_reusable_without_inferred_parameters():
    value = captured()
    value["coupon"] = {
        "observed": True,
        "mode": "source_state",
        "coupon_object_observed": True,
        "coupon_object_key_count": 0,
        "parameters": {},
        "is_coupon_spec_changed": False,
        "source_quantity": "50",
    }
    result = calculate_source_total(value)
    assert result["status"] == "complete" and result["reusable_coupon_rule"] is True
    value["userSelectedOptions"]["quantity"] = "100"
    assert calculate_source_total(value)["status"] == "complete"
    value["coupon"]["coupon_object_key_count"] = 1
    assert calculate_source_total(value)["status"] == "unresolved"


@pytest.mark.parametrize(
    "mutation",
    [
        {"coupon_object_observed": False},
        {"coupon_object_key_count": -1},
        {"is_coupon_spec_changed": True},
        {"parameters": {"valid_for_specs": True}},
    ],
)
def test_missing_or_conflicting_empty_coupon_evidence_rejected(mutation):
    value = captured()
    value["coupon"] = {
        "observed": True,
        "mode": "source_state",
        "coupon_object_observed": True,
        "coupon_object_key_count": 0,
        "parameters": {},
        "is_coupon_spec_changed": False,
        **mutation,
    }
    assert calculate_source_total(value)["status"] == "unresolved"


def test_nonempty_source_coupon_eligibility_is_bound_to_current_quantity():
    value = captured(base="10.00")
    value["coupon"] = {
        "observed": True,
        "mode": "source_state",
        "coupon_object_observed": True,
        "coupon_object_key_count": 5,
        "is_coupon_spec_changed": False,
        "source_quantity": "50",
        "parameters": {
            "valid_for_specs": True,
            "qualification_level": 1,
            "status_id": 1,
            "amount_percent": 10,
            "max_amount": None,
        },
    }
    result = calculate_source_total(value)
    assert result["status"] == "complete" and result["subtotal"] == "9.00"
    assert result["reusable_coupon_rule"] is False
    value["userSelectedOptions"]["quantity"] = "100"
    assert (
        "coupon_eligibility_bound_to_different_quantity"
        in calculate_source_total(value)["reasons"]
    )


def test_controller_computed_total_disagreement_fails_closed():
    value = captured()
    value.update(capture_kind="controller", controller_total="8.60")
    assert "controller_formula_disagrees" in calculate_source_total(value)["reasons"]


async def test_controller_extractor_only_whitelisted_state_no_network_or_secrets():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium not installed")
        browser = await runtime.chromium.launch()
        try:
            page = await browser.new_page()
            requests = []

            async def route(request):
                requests.append(request.request.url)
                await request.fulfill(
                    body='<div id="order-form" data-printproducttype="1" data-currentstoretype="broker"></div>',
                    content_type="text/html",
                )

            await page.route("**/*", route)
            await page.goto("https://www.printograph.com/products/business-cards/order")
            state = captured()
            await page.evaluate(
                """state => {
                const element = document.querySelector('#order-form');
                const selected = {...state.userSelectedOptions,shoppingCartItemId:'',priceGroupId:'',email:'PRIVATE_SENTINEL'};
                const controller = {printProductType:1,fields:{1:{options:state.field_options}},
                  jsonObj:{userSelectedOptions:selected,appliedPrices:state.appliedPrices,
                    others:{isAdmin:false,waiveFees:false,itemId:'',editOrderItem:{},oauth:'PRIVATE_SENTINEL'},
                    images:{privateArtwork:'PRIVATE_SENTINEL'}},
                  totalPrice:'5.60',getCouponPrice(value){return 0}};
                const child = {_: {subTree: {component: {refs: {orderFormInstance: controller}}}}};
                element._vnode = {component: {proxy: {$refs: {childComponents: child}}}};
            }""",
                state,
            )
            result = await page.evaluate(
                CONTROLLER_PRICE_STATE_JS,
                {"source_contract": SOURCE_CONTRACT, "settled": True},
            )
            assert "PRIVATE_SENTINEL" not in json.dumps(result)
            assert result["userSelectedOptions"]["fileDelivery"] == "5"
            assert calculate_source_total(result)["subtotal"] == "5.60"
            assert len(requests) == 1  # Only the intercepted fixture document.
            await page.evaluate("""() => {
                const c = document.querySelector('#order-form')._vnode.component.proxy.$refs.childComponents._.subTree.component.refs.orderFormInstance;
                c.couponData = {coupon:{},isCouponSpecChanged:false};
            }""")
            empty_coupon = await page.evaluate(
                CONTROLLER_PRICE_STATE_JS,
                {"source_contract": SOURCE_CONTRACT, "settled": True},
            )
            assert empty_coupon["coupon"]["coupon_object_key_count"] == 0
            assert calculate_source_total(empty_coupon)["reusable_coupon_rule"] is True
            await page.evaluate("""() => {
                const c = document.querySelector('#order-form')._vnode.component.proxy.$refs.childComponents._.subTree.component.refs.orderFormInstance;
                c.couponData.coupon = {code:'PRIVATE_SENTINEL',isCouponValidForSpecs:true,qualificationLevel:1,status:{id:1},amountTiers:[{amount:10,maxAmount:null}],secret:'PRIVATE_SENTINEL'};
            }""")
            active_coupon = await page.evaluate(
                CONTROLLER_PRICE_STATE_JS,
                {"source_contract": SOURCE_CONTRACT, "settled": True},
            )
            assert "PRIVATE_SENTINEL" not in json.dumps(active_coupon)
            assert calculate_source_total(active_coupon)["subtotal"] == "5.04"
            assert (
                calculate_source_total(active_coupon)["reusable_coupon_rule"] is False
            )
            await page.evaluate("""() => {
                const c = document.querySelector('#order-form')._vnode.component.proxy.$refs.childComponents._.subTree.component.refs.orderFormInstance;
                c.jsonObj.others.itemId = 'null';
                c.jsonObj.userSelectedOptions.priceGroupId = 'null';
            }""")
            null_item = await page.evaluate(
                CONTROLLER_PRICE_STATE_JS,
                {"source_contract": SOURCE_CONTRACT, "settled": True},
            )
            assert null_item["other_context"]["has_item_id"] is False
            assert null_item["other_context"]["has_price_group"] is True
            unknown = await page.evaluate(
                CONTROLLER_PRICE_STATE_JS, {"source_contract": {}, "settled": True}
            )
            assert unknown["observed"] is False
        finally:
            await browser.close()
