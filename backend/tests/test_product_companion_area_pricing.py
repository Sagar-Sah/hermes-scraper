"""Offline evidence and arithmetic checks; no supplier/storefront requests."""
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from crawler.product_companion_area_pricing import (
    PRICING_CONFIG_SHA256, SCRIPT_SHA256, breakpoint_coverage,
    parse_public_rule, retail_prediction, source_model_cost, validate_selection,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pigf_area_6383"
SCRIPT = (FIXTURES / "configurator-loaded.js").read_bytes()
CONFIG = (FIXTURES / "config-inline.js").read_bytes()


def rule(config=CONFIG, script=SCRIPT, **overrides):
    return parse_public_rule(script, config,
        expected_script_sha256=overrides.get("script_sha256", SCRIPT_SHA256),
        expected_config_sha256=overrides.get("config_sha256", hashlib.sha256(config).hexdigest()))


def selection(**overrides):
    return {"product_id": "6383", "width": 24, "height": 18,
            "colorspec": "4/0", "quantity": 1, "flute_direction": "vertical",
            "stakes_per_sign": 0, **overrides}


def test_only_exact_script_and_reviewed_pricing_semantics_can_verify_public_rule():
    snapshot = rule()
    assert snapshot["public_rule_status"] == "verified"
    assert snapshot["pricing_config_sha256"] == PRICING_CONFIG_SHA256
    assert snapshot["status"] == snapshot["backend_status"] == "unknown"
    assert snapshot["rendered_price_status"] == snapshot["current_supplier_prices"] == "unknown"
    changed_script = SCRIPT + b"\n"
    assert rule(script=changed_script, script_sha256=hashlib.sha256(changed_script).hexdigest())["reason"] == "public_rule_script_changed"
    assert rule(config_sha256="0" * 64)["reason"] == "public_rule_config_capture_changed"


def test_nonce_rotation_changes_raw_capture_but_cannot_change_the_pricing_pin():
    changed = CONFIG.replace(b'"nonce":"e45a541514"', b'"nonce":"new-public-nonce"')
    assert changed != CONFIG
    assert rule(changed)["public_rule_status"] == "verified"
    assert rule(changed)["config_sha256"] != rule()["config_sha256"]
    assert rule(changed)["pricing_config_sha256"] == rule()["pricing_config_sha256"]


@pytest.mark.parametrize("old,new", [
    (b'"stakeCost":"1.23"', b'"stakeCost":"1.24"'),
    (b'"productId":"6383"', b'"productId":"6669"'),
    (b'"maximum_regular_quantity":499', b'"maximum_regular_quantity":500'),
    (b'"6":1.5', b'"6":1.6'),
    (b'"stakeCost":"1.23"', b'"stakeCost":true'),
    (b'"stakeCost":"1.23"', b'"stakeCost":1e999999999'),
    (b'"stakeCost":"1.23"', b'"stakeCost":NaN'),
    (b'"stakeCost":"1.23"', b'"stakeCost":"1.23","stakeCost":"1.23"'),
])
def test_changed_or_malformed_config_never_verifies_even_with_new_capture_sha(old, new):
    changed = CONFIG.replace(old, new)
    assert changed != CONFIG
    result = rule(changed)
    assert result["status"] == "unknown"
    assert result["reason"] == "public_rule_pricing_config_unverified"
    assert "public_rule_status" not in result


def test_javascript_after_config_declaration_is_not_executed_or_accepted():
    result = rule(CONFIG + b"\nfetch('https://example.invalid');")
    assert result["reason"] == "public_rule_pricing_config_unverified"


@pytest.mark.parametrize("change", [
    {"product_id": "6669"}, {"width": 5}, {"width": 37}, {"height": 47},
    {"height": "18.5"}, {"quantity": 0}, {"quantity": 500}, {"quantity": True},
    {"quantity": "1e2"}, {"stakes_per_sign": 3}, {"colorspec": "4/4 guessed"},
    {"flute_direction": "unverified"}, {"flute_direction": []},
    {"stakes_per_sign": 1, "flute_direction": "horizontal"},
    {"stakes_per_sign": 1, "width": 18, "height": 24},
    {"stakes_per_sign": 1, "width": 18, "height": 18}, {"price": "0.01"},
])
def test_invalid_or_conflicting_selection_cannot_generate_price(change):
    result = retail_prediction(rule(), selection(**change))
    assert result["public_selection_valid"] is False
    assert result["status"] == "unknown"
    assert "predicted_public_js_total" not in result


def test_height_conflict_is_visible_and_never_claims_backend_acceptance():
    result = validate_selection(rule(), selection(width=36, height=46))
    assert result["public_selection_valid"]
    assert result["historical_height_conflict"]
    assert result["backend_status"] == "unknown"
    assert not validate_selection(rule(), selection(height=36))["historical_height_conflict"]


@pytest.mark.parametrize("quantity,tier", [(1,1),(5,1),(6,6),(25,6),(26,26),(50,26),(51,51),(75,51),(76,76),(499,76)])
def test_typed_exact_quantity_earns_last_tier_instead_of_becoming_tier_quantity(quantity, tier):
    result = source_model_cost(rule(), selection(quantity=quantity))
    assert result["selection"]["quantity"] == quantity
    assert result["earned_quantity_tier"] == tier
    assert result["status"] == "unknown"


def test_exact_raw_decimal_and_fraction_are_preserved_without_cent_or_four_place_quantization():
    result = source_model_cost(rule(), selection(width=7, height=7, quantity=3))
    raw_rate = "1.649999999999999911182158029987476766109466552734375"
    assert result["raw_rate_decimal"] == raw_rate
    expected = Fraction(49, 144) * Fraction(Decimal(raw_rate)) * 3
    assert result["cost_rational"] == {"numerator": expected.numerator, "denominator": expected.denominator}
    assert len(result["source_model_cost_decimal"].split(".")[1]) > 50
    assert result["minimum_applied_by_public_formula"] is False
    assert result["supplier_minimum_status"] == "unknown"


@pytest.mark.parametrize("options,total", [
    ({"width":6,"height":6}, "1.24"),
    ({"width":6,"height":6,"colorspec":"4/4"}, "1.53"),
    ({}, "14.85"), ({"stakes_per_sign":1}, "18.54"),
    ({"quantity":6}, "67.50"), ({"quantity":499}, "2778.43"),
])
def test_reviewed_offline_public_display_probes(options, total):
    result = retail_prediction(rule(), selection(**options))
    assert result["predicted_public_js_total"] == total
    assert result["status"] == result["backend_status"] == result["rendered_price_status"] == "unknown"
    assert result["current_backend_rounding_verified"] is False
    assert result["historical_server_intermediate_rounding_decimal_places"] == 4


def test_tampering_projected_rule_cannot_change_the_cost_model():
    changed = rule()
    changed["pricing_config"]["stake_cost"] = "0"
    assert retail_prediction(changed, selection())["reason"] == "public_rule_not_verified"


def test_all_quantity_boundary_probes_still_do_not_prove_source_backend_or_rendered_completion():
    result = breakpoint_coverage(rule(), [selection(quantity=q) for q in [1,5,6,25,26,50,51,75,76,499]])
    assert result["missing_quantity_boundary_probes"] == []
    assert all(result["quantity_boundary_probes"].values())
    assert result["status"] == result["source_domain_coverage"] == result["backend_domain_coverage"] == "unknown"
    assert breakpoint_coverage(rule(), [selection()])["missing_quantity_boundary_probes"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required to independently execute reviewed JavaScript arithmetic")
def test_predictions_match_the_actual_loaded_javascript_number_operations_at_boundaries():
    cases = [selection(width=w, height=h, quantity=q, colorspec=color, stakes_per_sign=stakes)
             for w,h in [(6,6),(7,7),(24,18),(36,46)]
             for q in [1,5,6,25,26,50,51,75,76,499]
             for color in ["4/0", "4/4"] for stakes in [0,1,2]
             if not stakes or w > h]
    js = r"""
const fs=require('fs'), data=JSON.parse(fs.readFileSync(0,'utf8'));
const script=fs.readFileSync(process.argv[1],'utf8');
const inline=fs.readFileSync(process.argv[2],'utf8');
const config=JSON.parse(inline.match(/var PIGF_CONFIG = (\{.*\});/)[1]);
function between(a,b){const first=script.indexOf(a),last=script.indexOf(b,first+a.length);if(first<0||last<0)throw Error('reviewed function missing');return script.slice(first,last);}
const functions=[between('function e(e)','function o('),between('function d(t)','function n('),between('function a(t)','function f('),between('function f(n,s)','function v('),between('function v(n,s,o,i,a)','function b(')].join('\n');
const model=new Function('t',functions+';return {basis:v,retail:d};')(config);
console.log(JSON.stringify(data.map(c=>model.retail(model.basis(c.width,c.height,c.colorspec,c.quantity,c.stakes_per_sign)).toFixed(2))));
"""
    expected = json.loads(subprocess.check_output([shutil.which("node"), "-e", js,
        str(FIXTURES / "configurator-loaded.js"), str(FIXTURES / "config-inline.js")],
        input=json.dumps(cases).encode()))
    reviewed = rule()
    actual = [retail_prediction(reviewed, case)["predicted_public_js_total"] for case in cases]
    assert actual == expected
