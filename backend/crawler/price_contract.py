"""Pure, source-pinned reconciliation for new, explicitly captured pricing state.

This does not fetch prices, infer legacy state, change row identity, or establish
that a controller's applied prices are fresh. A complete result reconciles the
reviewed calculator formula; source freshness and cart verification are separate.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import math
import re
from typing import Any

SOURCE_CONTRACT = {
    "order_form_sha256": "fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b",
    "common_sha256": "577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d",
}
# Keep historical identities unchanged. The new exact pair is reviewed only
# for this mapped broker family; hashes from different releases never combine.
SPECIAL_SHAPES_SOURCE_CONTRACT = {
    "order_form_sha256": "0aef40e4995a0ecfbcae99f8ad4dd900fdc2201147a37e0b5bbbf0a7fa5525f6",
    "common_sha256": "22fd26adacdf0e55142139c0eff52ffc227b8028cb3121662d8678b1d9e337a7",
}
SPECIAL_SHAPES_SOURCE_URL = "https://www.printograph.com/products/special-shapes/order"
# Restricted arithmetic support only: source-observed manual proof + upload now.
# Other source services and every freshness/catalog/commerce gate stay separate.
STAGGERED_CUT_SOURCE_URL = "https://www.printograph.com/products/staggered-cut-flyers/order"
STAGGERED_CUT_SOURCE_CONTRACT = dict(SPECIAL_SHAPES_SOURCE_CONTRACT)
# A distinct source-native request variant, never asserted equivalent to no query.
# Only the exact literal strings are safe to project; actual item/customer IDs stay excluded.
STAGGERED_LITERAL_BASE_QUERY = {"mode": "source_literal_pair_v1", "itemId": "null", "cid": ""}


def staggered_literal_base_query(value):
    return (type(value) is dict and value == STAGGERED_LITERAL_BASE_QUERY
            and all(type(value.get(key)) is str for key in STAGGERED_LITERAL_BASE_QUERY))

REVIEWED_SOURCE_SCOPES = (
    {"source_contract": SOURCE_CONTRACT, "product_types": ("1", "2", "27", "36"), "source_url": None},
    {"source_contract": SPECIAL_SHAPES_SOURCE_CONTRACT, "product_types": ("39",), "source_url": SPECIAL_SHAPES_SOURCE_URL},
    {"source_contract": STAGGERED_CUT_SOURCE_CONTRACT, "product_types": ("38",), "source_url": STAGGERED_CUT_SOURCE_URL},
)


def reviewed_source_scope(contract, product_type=None):
    # Hash-only callers report bundle identity, not supported product coverage.
    # Type-aware callers must distinguish families sharing the same exact pair.
    matches = [scope for scope in REVIEWED_SOURCE_SCOPES
               if isinstance(contract, dict) and contract == scope["source_contract"]]
    if product_type is not None:
        matching = next((scope for scope in matches
                         if str(product_type) in scope["product_types"]), None)
        if matching is not None:
            return matching
    return matches[0] if matches else None


OPTION_FIELDS = {
    "1": (
        "roundedCorners",
        "coating",
        "spotCoating",
        "raisedPrint",
        "raisedFoilColor",
        "paperFinish",
    ),
    # The pinned orderForm bundle declares Postcards (type 2) with these six
    # broker option fields; its alternate store omits spotCoating.
    "2": (
        "roundedCorners",
        "coating",
        "spotCoating",
        "raisedPrint",
        "raisedFoilColor",
        "paperFinish",
    ),
    "27": (
        "roundedCorners",
        "coating",
        "spotCoating",
        "raisedPrint",
        "raisedFoilColor",
        "paperFinish",
    ),
    "36": ("windPosition",),
    "39": ("coating", "spotCoating", "paperFinish"),
    "38": ("coating",),
}
BASE_FIELDS = {
    "1": ("size", "orientation", "paper", "color"),
    "2": ("size", "orientation", "paper", "color"),
    "27": ("size", "orientation", "paper", "color"),
    "36": ("shape", "size", "orientation", "paper", "finish", "color"),
    "39": ("shape", "size", "orientation", "paper", "color"),
    "38": ("size", "orientation", "paper", "color"),
}
SPECIAL_SHAPES_VALUE_DOMAINS = {
    "orientation": ("1", "2"), "color": ("1", "2", "3"),
    "coating": ("", "5", "6", "40"), "spotCoating": ("", "3", "4", "39"),
    "paperFinish": ("", "1"), "productionTime": ("1", "3"),
}
PRICED_OPTIONS = {
    "roundedCorners": {"2"},
    "spotCoating": {"3", "4", "39"},
    "raisedPrint": {str(value) for value in range(135, 141)},
    "raisedFoilColor": {"141", "142", "143"},
}
MANUAL_PROOFS = {"1", "36", "37", "38"}
DESIGN_PACKAGES = {"41", "42", "43", "44", "45", "53"}
DELIVERY_VALUES = {
    "31": {"5", "22", "33", "34"},
    "100": {"5", "6"},
    "101": {"9", "14", "15", "29"},
}
SERVICE_FIELDS = ("proofType", "manualProofType", "designServices", "fileDelivery")
NORMAL_CONTEXT_FLAGS = (
    "is_admin",
    "waive_fees",
    "has_item_id",
    "has_shopping_cart_item",
    "has_price_group",
    "has_edit_order_item",
)
_MONEY = re.compile(r"(?:\d+)(?:\.\d+)?\Z")
_DISPLAY_MONEY = re.compile(r"\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\Z")


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _raw(value):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
        raise ValueError("Expected an explicit scalar source value")
    text = str(value)
    if len(text) > 64 or not re.fullmatch(r"\d*", text):
        raise ValueError("Unsupported source value")
    return text


def _number(value, *, display=False):
    if (
        isinstance(value, bool)
        or value is None
        or not isinstance(value, (str, int, float, Decimal))
    ):
        raise ValueError("Expected a finite nonnegative amount")
    text = str(value)
    if not (_DISPLAY_MONEY if display else _MONEY).fullmatch(text):
        raise ValueError("Expected an unambiguous nonnegative amount")
    value = float(text.removeprefix("$").replace(",", ""))
    if not math.isfinite(value) or value >= 1e21:
        raise ValueError("Amount is outside the reviewed fixed-point range")
    return value


def _fixed(value):
    """ECMAScript positive Number.toFixed(2), including binary rounding."""
    if not math.isfinite(value) or value < 0 or value >= 1e21:
        raise ValueError("Amount is outside the reviewed fixed-point range")
    with localcontext() as context:
        context.prec = 80
        return format(
            Decimal.from_float(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            ".2f",
        )


def round_source_component(markup_price):
    """The reviewed markupPrice -> parseFloat -> toFixed cache assignment."""
    if isinstance(markup_price, str) and "$" in markup_price:
        raise ValueError("The source markup price has no currency symbol")
    return _fixed(_number(markup_price, display=True))


def _display_amount(value):
    _number(value, display=True)  # Validate sign, grouping and reviewed range.
    amount = Decimal(str(value).removeprefix("$").replace(",", ""))
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("A displayed total must already be rounded to cents")
    return format(amount, ".2f")


def normalize_price_context(capture: dict[str, Any]) -> dict[str, Any]:
    """Return explicit source context; missing/unsupported inputs stay unresolved.

    Only whitelisted fields are returned. The context hash excludes prices and
    observation IDs, so a changed price cannot disguise a same-context conflict.
    Coupon result-only evidence is tied to one captured subtotal and cannot be
    reused as a discount rule for another quantity.
    """
    reasons = []
    if not isinstance(capture, dict):
        capture = {}
    product_type = str(capture.get("product_type", ""))
    scope = reviewed_source_scope(capture.get("source_contract"), product_type)
    if scope is None:
        reasons.append("unreviewed_source_contract")
    if capture.get("observed") is not True:
        reasons.append("state_not_explicitly_observed")
    if capture.get("settled") is not True:
        reasons.append("state_not_settled")
    if capture.get("capture_kind") not in {"controller", "explicit_request"}:
        reasons.append("unknown_capture_kind")
    if product_type not in OPTION_FIELDS or capture.get("store_type") != "broker":
        reasons.append("unsupported_product_or_store")
    if scope is not None and product_type not in scope["product_types"]:
        reasons.append("unreviewed_product_for_source_contract")
    if scope is not None and scope["source_url"] is not None and capture.get("source_url") != scope["source_url"]:
        reasons.append("unreviewed_source_url")
    if capture.get("currency") != "USD":
        reasons.append("unsupported_or_missing_currency")
    expected = OPTION_FIELDS.get(product_type, ())
    if capture.get("field_options") != list(expected):
        reasons.append("unreviewed_option_field_order")
    if product_type in {"39", "38"} and capture.get("field_specs") != list(BASE_FIELDS[product_type]):
        reasons.append("unreviewed_base_field_order")
    selected = capture.get("userSelectedOptions")
    selected = selected if isinstance(selected, dict) else {}
    normalized = {}
    required = (
        *BASE_FIELDS.get(product_type, ()),
        *expected,
        *SERVICE_FIELDS,
        "quantity",
        "productId",
        "productionTime",
    )
    for key in required:
        try:
            normalized[key] = _raw(selected[key])
        except (KeyError, ValueError):
            reasons.append(f"missing_or_invalid_selection:{key}")
    positive = (
        *BASE_FIELDS.get(product_type, ()),
        "quantity",
        "productId",
        "productionTime",
    )
    for key in positive:
        if key in normalized and (not normalized[key] or int(normalized[key]) <= 0):
            reasons.append(f"unselected_required_value:{key}")
    for key in expected:
        if key in PRICED_OPTIONS and normalized.get(key, "") not in {
            "",
            *PRICED_OPTIONS[key],
        }:
            reasons.append(f"unsupported_selected_option:{key}")
    if product_type == "39":
        for key, values in SPECIAL_SHAPES_VALUE_DOMAINS.items():
            if key in normalized and normalized[key] not in values:
                reasons.append(f"unsupported_selected_option:{key}")
    proof = normalized.get("proofType")
    if proof not in DELIVERY_VALUES:
        reasons.append("unsupported_proof_mode")
    elif normalized.get("fileDelivery") not in DELIVERY_VALUES[proof]:
        reasons.append("unsupported_delivery_for_proof_mode")
    if proof == "100" and normalized.get("manualProofType") not in MANUAL_PROOFS:
        reasons.append("unsupported_manual_proof")
    if proof == "101" and normalized.get("designServices") not in DESIGN_PACKAGES:
        reasons.append("unsupported_design_package")
    retained_manual_controls = None
    if product_type == "38":
        # No service defaults: source-rendered controls must positively retain
        # this explicit restricted mode, independent of residual cache values.
        if proof != "100" or normalized.get("fileDelivery") != "5":
            reasons.append("unsupported_staggered_service_scope")
        if normalized.get("productionTime") not in {"1", "3"}:
            reasons.append("unsupported_staggered_production")
        guard = capture.get("manual_controls")
        expected_guard = {
            "proof_type": {"value": "100", "checked": True, "enabled": True, "visible": True},
            "manual_proof": {"value": normalized.get("manualProofType"), "checked": True, "enabled": True, "visible": True},
            "file_delivery": {"value": "5", "checked": True, "enabled": True, "visible": True},
        }
        guard_valid = (isinstance(guard, dict) and guard == expected_guard
                       and all(isinstance(guard.get(name), dict)
                               and type(guard[name].get("value")) is str
                               and all(type(guard[name].get(flag)) is bool
                                       for flag in ("checked", "enabled", "visible"))
                               for name in expected_guard))
        if not guard_valid:
            reasons.append("staggered_manual_controls_not_retained")
        else:
            retained_manual_controls = expected_guard
    base_query = None
    if product_type == "38" and "base_price_query" in capture:
        if "productSpecificationId" in selected:
            reasons.append("unsupported_staggered_specification_context")
        if staggered_literal_base_query(capture["base_price_query"]):
            base_query = dict(STAGGERED_LITERAL_BASE_QUERY)
        else:
            reasons.append("unsupported_base_price_query_profile")
    context_flags = capture.get("other_context")
    context_flags = context_flags if isinstance(context_flags, dict) else {}
    flags = {}
    for key in NORMAL_CONTEXT_FLAGS:
        if key not in context_flags or not isinstance(context_flags[key], bool):
            reasons.append(f"missing_context_flag:{key}")
        else:
            flags[key] = context_flags[key]
            if flags[key]:
                reasons.append(f"unsupported_context:{key}")
    coupon, coupon_reasons = _normalize_coupon(capture.get("coupon"))
    reasons.extend(coupon_reasons)
    context = {
        "schema_version": 1,
        "source_contract": dict(scope["source_contract"]) if scope is not None else {},
        "product_type": product_type,
        "store_type": capture.get("store_type"),
        "currency": capture.get("currency"),
        "selections": normalized,
        "field_options": list(expected),
        "other_context": flags,
        "coupon": coupon,
    }
    if scope is not None and scope["source_url"] is not None:
        context["source_url"] = capture.get("source_url") if capture.get("source_url") == scope["source_url"] else None
    if product_type in {"39", "38"}:
        context["field_specs"] = list(BASE_FIELDS[product_type])
    if retained_manual_controls is not None:
        context["manual_controls"] = retained_manual_controls
    if base_query is not None:
        # Hash this exact variant instead of collapsing it into ordinary no-query prices.
        context["base_price_query"] = base_query
    hash_context = {
        **context,
        "coupon": {
            key: value
            for key, value in coupon.items()
            if key not in {"argument_subtotal", "observed_adjusted_total"}
        },
    }
    return {
        "status": "unresolved" if reasons else "complete",
        "context": context,
        "context_hash": _hash(hash_context) if not reasons else None,
        "reasons": sorted(set(reasons)),
        "reusable_coupon_rule": coupon.get("mode")
        in {"parameters", "empty_source_coupon"}
        and not coupon_reasons,
    }


def _normalize_coupon(value):
    reasons = []
    if not isinstance(value, dict) or value.get("observed") is not True:
        return {}, ["coupon_state_not_observed"]
    if value.get("mode") == "source_state":
        if (
            value.get("coupon_object_observed") is not True
            or type(value.get("coupon_object_key_count")) is not int
        ):
            return {}, ["coupon_object_not_observed"]
        if value["coupon_object_key_count"] == 0:
            if (
                value.get("parameters") != {}
                or value.get("is_coupon_spec_changed") is not False
            ):
                return {}, ["inconsistent_empty_coupon_state"]
            # H starts empty, is synchronously populated at mount, and requests
            # eligibility only when qualificationLevel==1. Updates merge rather
            # than clear H. A settled empty object establishes no queued coupon.
            return {"mode": "empty_source_coupon", "is_coupon_spec_changed": False}, []
        if value["coupon_object_key_count"] < 0 or not isinstance(
            value.get("parameters"), dict
        ):
            return {}, ["invalid_coupon_object_state"]
        normalized, reasons = _normalize_coupon(
            {
                **value["parameters"],
                "observed": True,
                "mode": "parameters",
                "is_coupon_spec_changed": value.get("is_coupon_spec_changed"),
            }
        )
        normalized["mode"] = "source_parameters"
        try:
            normalized["source_quantity"] = _raw(value["source_quantity"])
            if (
                not normalized["source_quantity"]
                or int(normalized["source_quantity"]) <= 0
            ):
                raise ValueError("Missing coupon eligibility quantity")
        except (KeyError, ValueError):
            reasons.append("coupon_eligibility_quantity_not_recorded")
        return normalized, reasons
    if value.get("mode") == "result_only":
        normalized = {"mode": "result_only"}
        try:
            normalized["argument_subtotal"] = _display_amount(
                value["argument_subtotal"]
            )
            result = value["result"]
            # Numeric zero is the actual inactive sentinel returned by the method.
            if type(result) in (int, float) and result == 0:
                normalized["inactive_for_this_quote"] = True
            else:
                normalized["observed_adjusted_total"] = _display_amount(result)
                reasons.append("active_coupon_parameters_not_recorded")
        except (KeyError, ValueError):
            reasons.append("invalid_coupon_result_evidence")
        return normalized, reasons
    if value.get("mode") != "parameters":
        return {}, ["unsupported_coupon_evidence_mode"]
    normalized = {"mode": "parameters"}
    for key in ("is_coupon_spec_changed", "valid_for_specs"):
        if not isinstance(value.get(key), bool):
            reasons.append(f"missing_coupon_parameter:{key}")
        else:
            normalized[key] = value[key]
    for key in ("qualification_level", "status_id"):
        try:
            normalized[key] = _raw(value[key])
            if normalized[key] and str(int(normalized[key])) != normalized[key]:
                raise ValueError("Noncanonical coupon status")
        except (KeyError, ValueError):
            reasons.append(f"missing_coupon_parameter:{key}")
    for key in ("amount_percent", "max_amount"):
        if key not in value:
            reasons.append(f"missing_coupon_parameter:{key}")
        elif value[key] is None:
            normalized[key] = None
            normalized[f"{key}_truthy"] = False
        else:
            try:
                number = _number(value[key])
                if key == "amount_percent" and number > 100:
                    raise ValueError("Unreviewed discount percentage")
                normalized[key] = str(value[key])
                # JS string "0" is truthy; numeric 0 is not. The maximum cap
                # condition depends on that difference, so retain it explicitly.
                normalized[f"{key}_truthy"] = isinstance(value[key], str) or number != 0
            except ValueError:
                reasons.append(f"invalid_coupon_parameter:{key}")
    return normalized, reasons


def calculate_source_total(capture: dict[str, Any]) -> dict[str, Any]:
    """Reconcile the pinned formula; never upgrade table/cart verification.

    Applied prices are consumed as captured, in source floating-addition order.
    A caller starting with table markupPrice should first use
    round_source_component, just as the reviewed source cache does.
    """
    normalized = normalize_price_context(capture)
    reasons = list(normalized["reasons"])
    context = normalized["context"]
    selected = context["selections"]
    applied = capture.get("appliedPrices") if isinstance(capture, dict) else None
    applied = applied if isinstance(applied, dict) else {}
    components = []

    def component(field, selection=None):
        try:
            raw = applied[field] if selection is None else applied[field][selection]
            number = _number(raw)
            components.append(
                {
                    "field": field,
                    "selected_value": selection,
                    "captured_amount": str(raw),
                    "amount": _fixed(number),
                }
            )
            return number
        except (KeyError, TypeError, ValueError):
            reasons.append(f"missing_or_invalid_applied_price:{field}")
            return None

    base = component("productPrice")
    if base is not None and base <= 0:
        reasons.append("base_price_not_positive")
    proof = selected.get("proofType")
    manual = (
        component("proofType", selected.get("manualProofType"))
        if proof == "100"
        else 0.0
    )
    design = (
        component("designServices", selected.get("designServices"))
        if proof == "101"
        else 0.0
    )
    option_sum = 0.0
    for field in context["field_options"]:
        value = selected.get(field)
        if field not in PRICED_OPTIONS:
            if field in applied:
                reasons.append(f"unexpected_applied_price_bucket:{field}")
        elif value:
            number = component(field, value)
            if number is None:
                option_sum = None
            elif option_sum is not None:
                option_sum += number
        elif isinstance(applied.get(field), dict) and "" in applied[field]:
            # The reviewed source would add a truthy empty-key price too.
            reasons.append(f"unexpected_empty_selection_price:{field}")
    result = {
        **normalized,
        "components": components,
        "subtotal_before_coupon": None,
        "subtotal": None,
        "comparison": "unresolved",
        "cart_verified": False,
        "fresh_source_verified": False,
        "reusable_coupon_rule": normalized["reusable_coupon_rule"],
    }
    numeric_complete = all(
        value is not None for value in (base, manual, design, option_sum)
    )
    if numeric_complete:
        try:
            # Match t+i+o+r exactly; o is separately accumulated in field order.
            subtotal = _fixed(base + manual + option_sum + design)
            result["subtotal_before_coupon"] = subtotal
            controller_total = capture.get("controller_total")
            if capture.get("capture_kind") == "controller" and controller_total is None:
                reasons.append("controller_total_not_recorded")
            elif (
                controller_total is not None
                and _display_amount(controller_total) != subtotal
            ):
                reasons.append("controller_formula_disagrees")
            coupon = context["coupon"]
            adjusted = subtotal
            if coupon.get("mode") == "result_only":
                if coupon.get("argument_subtotal") != subtotal:
                    reasons.append("coupon_result_bound_to_different_subtotal")
                if "observed_adjusted_total" in coupon:
                    result["observed_coupon_total"] = coupon["observed_adjusted_total"]
            elif coupon.get("mode") in {"parameters", "source_parameters"} and not any(
                "coupon_parameter" in reason for reason in reasons
            ):
                if coupon.get("mode") == "source_parameters" and coupon.get(
                    "source_quantity"
                ) != selected.get("quantity"):
                    reasons.append("coupon_eligibility_bound_to_different_quantity")
                active = (
                    not coupon["is_coupon_spec_changed"]
                    and coupon["valid_for_specs"]
                    and coupon["qualification_level"] == "1"
                    and coupon["status_id"] == "1"
                    and coupon["amount_percent_truthy"]
                )
                if active:
                    total_number = _number(subtotal)
                    reduction = total_number * _number(coupon["amount_percent"]) / 100
                    cap = (
                        _number(coupon["max_amount"])
                        if coupon["max_amount"] is not None
                        else 0.0
                    )
                    reduction = (
                        cap
                        if coupon["max_amount_truthy"] and reduction > cap
                        else reduction
                    )
                    adjusted = _fixed(total_number - reduction)
                    result["coupon_reduction"] = _fixed(
                        total_number - _number(adjusted)
                    )
            if not reasons:
                result["subtotal"] = adjusted
                result["comparison"] = "not_observed"
                if capture.get("displayed_total") is not None:
                    observed = _display_amount(capture["displayed_total"])
                    result["observed_subtotal"] = observed
                    result["comparison"] = (
                        "matched" if observed == adjusted else "mismatch"
                    )
        except (ValueError, InvalidOperation, KeyError):
            reasons.append("invalid_total_or_coupon_arithmetic")
    result["reasons"] = sorted(set(reasons))
    result["status"] = "unresolved" if reasons else "complete"
    if reasons:
        result["subtotal"] = None
        result["comparison"] = "unresolved"
    return result


# This follows only the mounted source controller path observed in updatePrice.
# The caller must supply the actual loaded exact reviewed bundle pair first
# and supply settled=True only after its ordinary page/request stability checks.
# It never reads cookies, storage, account tokens, artwork, addresses or notes.
CONTROLLER_PRICE_STATE_JS = (
    r"""({source_contract, settled = false}) => {
  const scopes = __REVIEWED_SOURCE_SCOPES__;
  const matches = source_contract && scopes.filter(rule =>
    Object.keys(source_contract).length === Object.keys(rule.source_contract).length &&
    Object.keys(rule.source_contract).every(k => source_contract[k] === rule.source_contract[k]));
  if (!matches || !matches.length)
    return {observed:false, reason:'unreviewed_source_contract'};
  if (!['www.printograph.com','printograph.com'].includes(location.hostname) || location.protocol !== 'https:')
    return {observed:false, reason:'unsupported_source_origin'};
  const element = document.querySelector('#order-form');
  if (!element) return {observed:false, reason:'controller_root_missing'};
  const roots = [element._vnode?.component?.proxy, element.__vue_app__?._instance?.proxy].filter(Boolean);
  const controllers = [...new Set(roots.map(root => root.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];
  if (controllers.length !== 1) return {observed:false, reason:'controller_missing_or_ambiguous'};
  const c = controllers[0], data = c.jsonObj;
  if (!data || !data.userSelectedOptions || !data.appliedPrices || !data.others)
    return {observed:false, reason:'controller_state_missing'};
  const type = String(c.printProductType), attrType = element.getAttribute('data-printproducttype');
  const scope = matches.find(rule => rule.product_types.includes(type)) || matches[0];
  const expected = scope.source_contract;
  if (type !== attrType || !scope.product_types.includes(type)) {
    // Schema names identify the unsupported contract without reading selections,
    // prices, account state or arbitrary properties from the controller.
    const typeName = value => typeof value === 'string' && /^[0-9]{1,16}$/.test(value) ? value : null;
    const controllerType = typeName(type);
    const store = element.getAttribute('data-currentstoretype');
    const fields = controllerType !== null && Array.isArray(c.fields?.[controllerType]?.options) ? c.fields[controllerType].options : [];
    const optionNames = [...new Set(fields.slice(0,256).filter(value =>
      typeof value === 'string' && /^[A-Za-z][A-Za-z0-9]{0,63}$/.test(value)))].slice(0,64);
    return {observed:false, reason:'controller_product_mismatch', diagnostic_metadata:{
      declared_type:typeName(attrType), controller_type:controllerType,
      store_type:typeof store === 'string' && /^[A-Za-z0-9]{1,32}$/.test(store) ? store : null,
      option_names:optionNames}};
  }
  if (element.getAttribute('data-currentstoretype') !== 'broker')
    return {observed:false, reason:'unsupported_source_store'};
  if (scope.source_url !== null && location.href !== scope.source_url)
    return {observed:false, reason:'unreviewed_source_url'};
  const options = __OPTION_FIELDS__[type], bases = __BASE_FIELDS__[type];
  if (!Array.isArray(c.fields?.[type]?.options) || JSON.stringify(c.fields[type].options) !== JSON.stringify(options))
    return {observed:false, reason:'controller_schema_mismatch'};
  if (['39','38'].includes(type) && (!Array.isArray(c.fields?.[type]?.specs) || JSON.stringify(c.fields[type].specs) !== JSON.stringify(bases)))
    return {observed:false, reason:'controller_base_schema_mismatch'};
  const scalar = value => typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null;
  const selected = {};
  for (const key of [...bases,...options,'proofType','manualProofType','designServices','fileDelivery','quantity','productId','productionTime'])
    if (Object.hasOwn(data.userSelectedOptions,key) && scalar(data.userSelectedOptions[key])) selected[key] = data.userSelectedOptions[key];
  if (type === '39') {
    const domains = __SPECIAL_SHAPES_VALUE_DOMAINS__;
    for (const [key,values] of Object.entries(domains))
      if (Object.hasOwn(selected,key) && (typeof selected[key] === 'boolean' || !values.includes(String(selected[key]))))
        return {observed:false,reason:'unsupported_selected_option:'+key};
  }
  if (type === '38' && Object.hasOwn(data.userSelectedOptions,'productSpecificationId'))
    return {observed:false,reason:'unsupported_staggered_specification_context'};
  let manualControls;
  if (type === '38') {
    if (String(selected.proofType) !== '100' || String(selected.fileDelivery) !== '5' ||
        !['1','36','37','38'].includes(String(selected.manualProofType)) ||
        !['1','3'].includes(String(selected.productionTime)))
      return {observed:false,reason:'unsupported_staggered_service_scope'};
    const visible = el => el.getClientRects().length > 0 &&
      getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden';
    const control = (name,value) => {
      const checked = [...element.querySelectorAll('input[type="radio"][name="'+name+'"]')].filter(el => el.checked);
      if (checked.length !== 1 || String(checked[0].value) !== value || checked[0].disabled || !visible(checked[0])) return null;
      return {value,checked:true,enabled:true,visible:true};
    };
    manualControls = {
      proof_type:control('proofType','100'),
      manual_proof:control('proofTypeForManual',String(selected.manualProofType)),
      file_delivery:control('fileDeliveryForManual','5')
    };
    if (Object.values(manualControls).some(value => value === null))
      return {observed:false,reason:'staggered_manual_controls_not_retained'};
  }
  const prices = {};
  if (scalar(data.appliedPrices.productPrice)) prices.productPrice = data.appliedPrices.productPrice;
  for (const key of [...options,'proofType','designServices']) {
    const bucket = data.appliedPrices[key];
    if (bucket === undefined) continue;
    prices[key] = {};
    const value = key === 'proofType' ? selected.manualProofType : selected[key];
    if (bucket && typeof bucket === 'object' && Object.hasOwn(bucket,String(value)) && scalar(bucket[String(value)]))
      prices[key][String(value)] = bucket[String(value)];
  }
  const flags = {};
  for (const [out,key] of [['is_admin','isAdmin'],['waive_fees','waiveFees']])
    if (typeof data.others[key] === 'boolean') flags[out] = data.others[key];
  const present = value => value !== '' && value !== null && value !== undefined;
  if (Object.hasOwn(data.others,'itemId')) flags.has_item_id = present(data.others.itemId) && data.others.itemId !== 'null';
  if (Object.hasOwn(data.others,'editOrderItem') && data.others.editOrderItem && typeof data.others.editOrderItem === 'object')
    flags.has_edit_order_item = Object.keys(data.others.editOrderItem).length > 0;
  for (const [out,key] of [['has_shopping_cart_item','shoppingCartItemId'],['has_price_group','priceGroupId']])
    if (Object.hasOwn(data.userSelectedOptions,key)) flags[out] = present(data.userSelectedOptions[key]);
  const result = {observed:true, capture_kind:'controller', source_contract:expected,
    settled:settled === true, product_type:type, store_type:element.getAttribute('data-currentstoretype'),
    currency:'USD', userSelectedOptions:selected, appliedPrices:prices, other_context:flags,
    field_options:Array.isArray(c.fields?.[type]?.options) ? [...c.fields[type].options] : null};
  if (scope.source_url !== null) result.source_url = location.href;
  if (['39','38'].includes(type)) result.field_specs = [...c.fields[type].specs];
  if (type === '38') {
    result.manual_controls = manualControls;
    if (typeof data.others.itemId === 'string' && data.others.itemId === 'null')
      result.base_price_query = {mode:'source_literal_pair_v1',itemId:'null',cid:''};
  }
  if (scalar(c.totalPrice)) result.controller_total = c.totalPrice;
  const couponData = c.couponData;
  if (couponData && typeof couponData === 'object' && couponData.coupon && typeof couponData.coupon === 'object' && !Array.isArray(couponData.coupon)) {
    const coupon = couponData.coupon, parameters = {};
    for (const [out,key] of [['valid_for_specs','isCouponValidForSpecs'],['qualification_level','qualificationLevel']])
      if (Object.hasOwn(coupon,key) && scalar(coupon[key])) parameters[out] = coupon[key];
    if (coupon.status && Object.hasOwn(coupon.status,'id') && scalar(coupon.status.id)) parameters.status_id = coupon.status.id;
    const tier = Array.isArray(coupon.amountTiers) ? coupon.amountTiers[0] : null;
    if (tier && typeof tier === 'object') {
      if (Object.hasOwn(tier,'amount') && scalar(tier.amount)) parameters.amount_percent = tier.amount;
      if (Object.hasOwn(tier,'maxAmount') && scalar(tier.maxAmount)) parameters.max_amount = tier.maxAmount;
    }
    result.coupon = {observed:true,mode:'source_state',coupon_object_observed:true,
      coupon_object_key_count:Object.keys(coupon).length,parameters,source_quantity:selected.quantity};
    if (typeof couponData.isCouponSpecChanged === 'boolean') result.coupon.is_coupon_spec_changed = couponData.isCouponSpecChanged;
  } else if (type !== '38' && typeof c.getCouponPrice === 'function' && scalar(c.totalPrice)) {
    const couponResult = c.getCouponPrice(c.totalPrice);
    if (scalar(couponResult)) result.coupon = {observed:true, mode:'result_only', argument_subtotal:c.totalPrice, result:couponResult};
  }
  return result;
}""".replace("__REVIEWED_SOURCE_SCOPES__", json.dumps(REVIEWED_SOURCE_SCOPES))
    .replace("__OPTION_FIELDS__", json.dumps(OPTION_FIELDS))
    .replace("__BASE_FIELDS__", json.dumps(BASE_FIELDS))
    .replace("__SPECIAL_SHAPES_VALUE_DOMAINS__", json.dumps(SPECIAL_SHAPES_VALUE_DOMAINS))
)
