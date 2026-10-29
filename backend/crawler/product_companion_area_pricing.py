"""Read-only predictions from an evidence-pinned PIGF area-price rule.

This first reviewed profile is Coroplast 6383. Public JavaScript arithmetic is
evidence of the displayed-price model, never proof of current supplier costs,
backend acceptance, or the amount payable in a cart. No writes or HTTP calls.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import math
import re


SCRIPT_SHA256 = "9ba21629eedd94e94794a64adf2cf7cee66d020a0011dcbf88765e959cefc4c7"
PRICING_CONFIG_SHA256 = "caf9d73f5b24c652c098fbe10966fb211752e62ade64771c2fc789b5ade905a7"
PRODUCT_ID = "6383"
TIERS = (1, 6, 26, 51, 76)
RETAIL_BANDS = ((25, "3"), (50, "2.5"), (100, "2"), (250, "1.85"),
                (600, "1.7"), (1000, "1.65"), (1500, "1.5"),
                (2000, "1.45"), (3000, "1.4"))
SELECTION_FIELDS = {"product_id", "width", "height", "colorspec", "quantity",
                    "flute_direction", "stakes_per_sign"}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: dict) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _unknown(reason: str, **data) -> dict:
    return {"status": "unknown", "reason": reason, "backend_status": "unknown",
            "rendered_price_status": "unknown", "current_supplier_prices": "unknown", **data}


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate_config_key")
        result[key] = value
    return result


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("invalid_decimal")
    if isinstance(value, Decimal) and (not value.is_finite()
            or abs(value.as_tuple().exponent) > 256 or len(value.as_tuple().digits) > 256):
        raise ValueError("invalid_decimal")
    text = format(value, "f") if isinstance(value, Decimal) else str(value)
    if len(text) > 256 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        raise ValueError("invalid_decimal")
    result = Decimal(text)
    if not result.is_finite():
        raise ValueError("invalid_decimal")
    return result


def _pricing_config(config: dict) -> dict:
    coroplast = config["coroplast"]
    maximum = coroplast["maximum_regular_quantity"]
    if type(maximum) is not int:
        raise ValueError("invalid_maximum")
    rates = coroplast["rates"]
    if not isinstance(rates, dict) or set(rates) != {"4/0", "4/4"}:
        raise ValueError("invalid_colorspec_rates")
    output = {}
    for colorspec, values in rates.items():
        if not isinstance(values, dict) or set(values) != {str(tier) for tier in TIERS}:
            raise ValueError("invalid_quantity_tiers")
        output[colorspec] = {key: format(_decimal(value), "f") for key, value in values.items()}
    return {"product_id": str(config["productId"]), "coroplast_id": str(config["coroplastId"]),
            "maximum_regular_quantity": maximum,
            "stake_cost": format(_decimal(config["stakeCost"]), "f"), "rates": output}


def parse_public_rule(script_bytes: bytes, config_bytes: bytes, *,
                      expected_script_sha256: str, expected_config_sha256: str) -> dict:
    """Require reviewed script and captured config bytes; never trust a version label.

    The raw config SHA binds the capture, including its changing public nonce.
    A second reviewed digest binds only pricing inputs, permitting a fresh nonce
    without silently accepting changed prices, product scope or quantity limits.
    """
    if (not isinstance(script_bytes, bytes) or not isinstance(config_bytes, bytes)
            or not 1 <= len(script_bytes) <= 2_097_152 or not 1 <= len(config_bytes) <= 65_536):
        return _unknown("public_rule_evidence_unavailable")
    if expected_script_sha256 != SCRIPT_SHA256 or _sha(script_bytes) != SCRIPT_SHA256:
        return _unknown("public_rule_script_changed")
    if (not isinstance(expected_config_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_config_sha256)
            or _sha(config_bytes) != expected_config_sha256):
        return _unknown("public_rule_config_capture_changed")
    try:
        text = config_bytes.decode("utf-8")
        match = re.fullmatch(r"\s*var PIGF_CONFIG\s*=\s*(\{.*\});\s*(?://# sourceURL=[^\r\n]*\s*)?", text, re.S)
        if not match:
            raise ValueError("config_declaration")
        config = json.loads(match[1], parse_float=Decimal, object_pairs_hook=_pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite_config")))
        pricing = _pricing_config(config)
        if _digest(pricing) != PRICING_CONFIG_SHA256:
            raise ValueError("unreviewed_pricing_config")
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, OverflowError):
        return _unknown("public_rule_pricing_config_unverified")
    return _unknown("backend_rule_unverified", schema="pigf-area-public-rule/v1",
                    public_rule_status="verified", kind="pigf_coroplast_area",
                    product_id=PRODUCT_ID, script_sha256=SCRIPT_SHA256,
                    config_sha256=expected_config_sha256,
                    pricing_config_sha256=PRICING_CONFIG_SHA256, pricing_config=pricing,
                    dimension_unit="inches", billing_area_unit="square_feet",
                    recorded_public_bounds={"width": [6, 36], "height": [6, 46]},
                    historical_backend_height_maximum=36,
                    backend_rounding="unknown_current_backend; historical PHP rounds basis4dp then retail2dp",
                    supplier_minimum_status="unknown", public_coroplast_minimum=None)


def _rule_valid(rule: dict) -> bool:
    try:
        return (rule.get("schema") == "pigf-area-public-rule/v1"
                and rule.get("public_rule_status") == "verified"
                and rule.get("product_id") == PRODUCT_ID
                and rule.get("script_sha256") == SCRIPT_SHA256
                and rule.get("pricing_config_sha256") == PRICING_CONFIG_SHA256
                and _digest(rule["pricing_config"]) == PRICING_CONFIG_SHA256)
    except (TypeError, KeyError, AttributeError):
        return False


def _integer(value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("integer_required")
    if len(str(value)) > 12:
        raise ValueError("integer_out_of_bounds")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError("integer_out_of_bounds")
    return result


def validate_selection(rule: dict, selection: dict) -> dict:
    """Validate the recorded public domain; backend/source coverage remains unknown."""
    if not _rule_valid(rule):
        return _unknown("public_rule_not_verified", public_selection_valid=False)
    try:
        if not isinstance(selection, dict) or set(selection) - SELECTION_FIELDS:
            raise ValueError("unexpected_selection_fields")
        if str(selection.get("product_id", PRODUCT_ID)) != PRODUCT_ID:
            raise ValueError("product_scope")
        normalized = {"product_id": PRODUCT_ID,
                      "width": _integer(selection.get("width"), 6, 36),
                      "height": _integer(selection.get("height"), 6, 46),
                      "quantity": _integer(selection.get("quantity"), 1, 499),
                      "stakes_per_sign": _integer(selection.get("stakes_per_sign"), 0, 2)}
        colorspec = selection.get("colorspec")
        aliases = {"4/0": "4/0", "4/0 (4 color front)": "4/0", "4/4": "4/4", "4/4 (4 color both sides)": "4/4"}
        if not isinstance(colorspec, str) or colorspec not in aliases:
            raise ValueError("colorspec")
        normalized["colorspec"] = aliases[colorspec]
        flute = selection.get("flute_direction")
        if flute not in {"vertical", "horizontal"}:
            raise ValueError("flute_direction")
        normalized["flute_direction"] = flute
        if normalized["stakes_per_sign"] and (flute != "vertical" or normalized["width"] <= normalized["height"]):
            raise ValueError("stake_orientation")
    except (ValueError, TypeError) as exc:
        return _unknown("invalid_public_selection", public_selection_valid=False,
                        selection_error=str(exc))
    tier = max(tier for tier in TIERS if normalized["quantity"] >= tier)
    return _unknown("backend_selection_unverified", public_selection_valid=True,
                    selection=normalized, earned_quantity_tier=tier,
                    historical_height_conflict=normalized["height"] > 36)


def source_model_cost(rule: dict, selection: dict) -> dict:
    """Keep exact recorded decimals/rational basis separate from JS Number math.

    The returned basis is a SITE supplier-cost model, not a supplier quote.
    Fractions preserve exact input precision even when area/144 is repeating.
    """
    validation = validate_selection(rule, selection)
    if not validation.get("public_selection_valid"):
        return validation
    selected = validation["selection"]
    pricing = rule["pricing_config"]
    rate = Decimal(pricing["rates"][selected["colorspec"]][str(validation["earned_quantity_tier"])])
    stake = Decimal(pricing["stake_cost"])
    quantity, width, height = (selected[key] for key in ("quantity", "width", "height"))
    area = Fraction(width * height, 144)
    cost = area * Fraction(rate) * quantity + quantity * selected["stakes_per_sign"] * Fraction(stake)
    with localcontext() as context:
        context.prec = max(100, len(str(cost.numerator)) + len(str(cost.denominator)) + 32)
        decimal_cost = format(Decimal(cost.numerator) / Decimal(cost.denominator), "f")
    # Same left-to-right IEEE754 operations as the reviewed function v().
    js_basis = (width * height / 144) * float(rate) * quantity + quantity * selected["stakes_per_sign"] * float(stake)
    return {**validation, "reason": "site_source_model_not_current_supplier_quote",
            "source_model_status": "verified_public_formula", "raw_rate_decimal": format(rate, "f"),
            "raw_stake_cost_decimal": format(stake, "f"),
            "area_rational": {"numerator": area.numerator, "denominator": area.denominator},
            "cost_rational": {"numerator": cost.numerator, "denominator": cost.denominator},
            "source_model_cost_decimal": decimal_cost,
            "public_js_number_basis": format(Decimal.from_float(js_basis), "f"),
            "minimum_applied_by_public_formula": False,
            "supplier_minimum_status": "unknown"}


def retail_prediction(rule: dict, selection: dict) -> dict:
    """Predict the pinned public JS display; never certify backend/cart pricing."""
    cost = source_model_cost(rule, selection)
    if not cost.get("public_selection_valid"):
        return cost
    basis = float(Decimal(cost["public_js_number_basis"]))
    multiplier = next((value for maximum, value in RETAIL_BANDS if basis <= maximum), "1.35")
    scaled = (basis * float(multiplier) + 2.0 ** -52) * 100
    integer = math.floor(scaled)
    # JavaScript Math.round compares the fractional part. floor(x+.5) can
    # itself round up on a value immediately below a half-cent boundary.
    cents = integer + (1 if scaled - integer >= 0.5 else 0)
    return {**cost, "reason": "backend_and_rendered_retail_unverified",
            "retail_operation": "whole_run_cost_band_markup", "retail_multiplier": multiplier,
            "predicted_public_js_total_cents": cents,
            "predicted_public_js_total": f"{cents // 100}.{cents % 100:02d}",
            "rounding": "reviewed JavaScriptNumber/Math.round with Number.EPSILON",
            "historical_server_intermediate_rounding_decimal_places": 4,
            "current_backend_rounding_verified": False}


def breakpoint_coverage(rule: dict, selections: list[dict]) -> dict:
    """Report reviewed probe coverage, never infer supplier-domain completeness."""
    if not _rule_valid(rule):
        return _unknown("public_rule_not_verified")
    valid = [result for selection in selections
             if (result := validate_selection(rule, selection)).get("public_selection_valid")]
    quantities = {result["selection"]["quantity"] for result in valid}
    required = [1, 5, 6, 25, 26, 50, 51, 75, 76, 499]
    return _unknown("probe_coverage_is_not_source_or_backend_completion",
                    valid_public_probe_count=len(valid), rejected_probe_count=len(selections) - len(valid),
                    quantity_boundary_probes={str(value): value in quantities for value in required},
                    missing_quantity_boundary_probes=[value for value in required if value not in quantities],
                    colorspecs_observed=sorted({result["selection"]["colorspec"] for result in valid}),
                    stakes_per_sign_observed=sorted({result["selection"]["stakes_per_sign"] for result in valid}),
                    retail_cost_band_boundaries=[maximum for maximum, _ in RETAIL_BANDS],
                    source_domain_coverage="unknown", backend_domain_coverage="unknown",
                    rendered_price_coverage="unknown")
