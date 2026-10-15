"""Bounded, source-hash-gated frontend constraints for Printograph 1/27/36.

Pure data validation only. It does not contact a source, infer price composition,
change service selections silently, or certify an entire catalog from samples.
"""

from __future__ import annotations

import hashlib
import json
import re

from .adapters.browser_tables import (
    TableSchemaError, parse_option_catalog, parse_option_quantity_support,
    parse_price_table, parse_quantity_support,
)

ORDER_HASH = "fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b"
COMMON_HASH = "577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d"
FIELDS = {"1": {"size", "orientation", "paper", "color"},
          "27": {"size", "orientation", "paper", "color"},
          "36": {"shape", "size", "orientation", "paper", "finish", "color"}}
OPTION_TYPES = {"roundedCorners": {"2"}, "coating": {"5", "6", "40"},
                "spotCoating": {"3", "4", "39"}, "raisedPrint": {str(n) for n in range(135, 141)},
                "raisedFoilColor": {"141", "142", "143"}, "windPosition": {"32", "33", "34", "35", "54"}}


class ConstraintError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _literal(source, marker):
    if source.count(marker) != 1:
        raise ConstraintError(f"Static source marker is not unique: {marker}")
    start = source.index(marker) + len(marker)
    opening, closing = source[start], "}" if source[start] == "{" else "]"
    if opening not in "{[":
        raise ConstraintError("Expected a static object or array")
    depth, quoted, escaped = 0, False, False
    for end in range(start, len(source)):
        char = source[end]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                text = source[start:end + 1]
                # These pinned literals contain only JSON data and bare identifier/numeric keys.
                text = re.sub(r'([,{])([A-Za-z_$][\w$]*|\d+):', r'\1"\2":', text)
                return json.loads(text), start
    raise ConstraintError("Unclosed static literal")


def extract_rule_contract(order_source, common_source):
    hashes = {"orderForm.js": hashlib.sha256(order_source.encode()).hexdigest(),
              "chunk-common.js": hashlib.sha256(common_source.encode()).hexdigest()}
    if hashes != {"orderForm.js": ORDER_HASH, "chunk-common.js": COMMON_HASH}:
        raise ConstraintError("Unknown source bundle; frontend rules need review")
    options, option_offset = _literal(order_source, "let v=")
    fields, field_offset = _literal(order_source, ",A=")
    validations, validation_offset = _literal(order_source, "let Al=")
    prices, price_offset = _literal(order_source, "optionsHavingPrices:")
    paper_finish, paper_finish_offset = _literal(order_source, "paperFinish:{list:")
    store_fields = {"100": {}, "other": {}}
    for product_type in FIELDS:
        declarations = list(re.finditer(r"(?<![\w])" + product_type + r":\{specs:(\[[^\]]*\]),options:(\[[^\]]*\])", order_source))
        if len(declarations) != 2:
            raise ConstraintError("Store field declarations changed")
        for key, declaration in zip(("100", "other"), declarations):
            store_fields[key][product_type] = json.loads(declaration.group(2))
    if "fields:100==this.store?M:h" not in order_source or "var M=m;let y=" not in order_source or "var h=y;" not in order_source:
        raise ConstraintError("Store field variant binding changed")
    for product_type in FIELDS:
        if f"{product_type}:{{specificValidation:[]}}" not in order_source:
            raise ConstraintError("Product-specific validation is no longer empty")
    references = {"option_visibility": option_offset, "field_visibility": field_offset,
                  "shared_corrections": validation_offset, "price_bearing_option_types": price_offset, "paper_finish_schema": paper_finish_offset,
                  "store_variant_selection": order_source.index("fields:100==this.store?M:h"),
                  "specChange": 1420856, "displayOptions": 1446199, "displaySpecs": 1600416,
                  "rushCondition": 1535009, "getTurnaround": 227516,
                  "turnaroundParam_common": 388971, "processTurnaround_common": 756731}
    for name in ("specChange", "displayOptions", "displaySpecs", "rushCondition", "getTurnaround"):
        if not order_source[references[name]:].startswith(name + ":function"):
            raise ConstraintError("Pinned source function moved")
    return {"schema_version": 1, "bundle_sha256": hashes, "display_options": options,
            "display_fields": fields, "validation_rules": validations,
            "option_fields_by_store": store_fields, "paper_finish_choices": paper_finish,
            "price_bearing_option_types": [str(v) for v in prices], "source_offsets": references,
            "scope": "classic product types1,27,36; no uploaded artwork/admin/edit/mailing/custom dimensions"}


def configuration_fingerprint(product_type, selection, context, base_product_id):
    return _digest({"product_type": str(product_type), "selection": {k: str(v) for k, v in selection.items()},
                    "context": context, "base_product_id": str(base_product_id)})


def validate_configuration(product_type, selection, *, contract, lookup, catalog, base_support,
                           option_support, option_prices, context, turnaround_rule=None):
    """Return supported/invalid/unresolved plus all detected corrections and evidence gaps.

    `lookup` is validate_unique_lookup output. Catalog/support/prices must be bound
    by the caller to that base and source context; `context.domains_bound` is an
    explicit prerequisite. Turnaround evidence is bound to the full selection
    fingerprint, not merely a same-valued response body. No corrections are quoted.
    """
    product_type = str(product_type)
    if not isinstance(selection, dict) or not isinstance(context, dict) or not isinstance(contract, dict):
        return {"status": "unresolved", "unresolved": ["malformed_selection_context_or_contract"], "changes": [], "resulting_selection": {}}
    lookup = lookup if isinstance(lookup, dict) else {}
    option_prices = option_prices if isinstance(option_prices, dict) else {}
    state = {key: str(value) for key, value in selection.items() if value is not None}
    resulting, changes, invalid, unresolved = dict(state), [], [], []
    def change(field, value, reason):
        value = str(value)
        if resulting.get(field) != value:
            changes.append({"field": field, "from": resulting.get(field), "to": value, "reason": reason})
            resulting[field] = value
    def reject(reason):
        if reason not in invalid:
            invalid.append(reason)
    if product_type not in FIELDS or contract.get("bundle_sha256") != {"orderForm.js": ORDER_HASH, "chunk-common.js": COMMON_HASH}:
        return {"status": "unresolved", "unresolved": ["unsupported_product_or_source_bundle"], "changes": [], "resulting_selection": resulting}
    required_options = {"windPosition"} if product_type == "36" else set(OPTION_TYPES) - {"windPosition"}
    paper_finish_visible = product_type in {"1", "27"} and state.get("paper") not in {str(v) for v in contract["display_fields"]["paperFinish"]["paper"]}
    required = FIELDS[product_type] | required_options | {"quantity", "productionTime", "proofType", "fileDelivery", "designServices"}
    if paper_finish_visible or "paperFinish" in state:
        required.add("paperFinish")
        if state.get("paperFinish") and state["paperFinish"] not in {str(item["id"]) for item in contract["paper_finish_choices"]}:
            reject("unknown_paper_finish_value")
    store_id = str(context.get("store_id", ""))
    if not store_id.isdigit() or int(store_id) <= 0:
        unresolved.append("missing_numeric_store_variant")
    else:
        enabled_fields = contract["option_fields_by_store"]["100" if store_id == "100" else "other"][product_type]
        for field in required_options:
            if state.get(field) and field not in enabled_fields:
                reject("option_disabled_by_store_variant:" + field)
    missing = sorted(required - state.keys())
    if missing:
        unresolved.append("missing_explicit_state:" + ",".join(missing))
    if set(state) - required:
        unresolved.append("unmodeled_selection_fields:" + ",".join(sorted(set(state) - required)))
    for flag in ("is_admin", "editing_existing_item", "images_uploaded", "mailing_opted", "custom_dimensions"):
        if context.get(flag) is not False:
            unresolved.append("unsupported_or_unknown_context:" + flag)
    for field in ("origin", "currency", "store_type", "price_group_id", "session_scope", "source_epoch", "service_signature"):
        if field not in context or context[field] is None:
            unresolved.append("missing_context:" + field)
    if context.get("domains_bound") is not True:
        unresolved.append("catalog_support_prices_not_bound_to_lookup_context")
    if state.get("proofType") not in {"31", "100"} or state.get("fileDelivery") != "5" or state.get("designServices") != "":
        unresolved.append("service_mode_requires_additional_validation")
    try:
        quantity = int(state.get("quantity", ""))
        if quantity <= 0 or str(quantity) != state["quantity"]:
            raise ValueError
    except ValueError:
        quantity = None
        unresolved.append("invalid_or_missing_discrete_quantity")

    # Declarative criteria are applied in the same order as the source. Changes
    # identify a non-stable requested tuple, never silently create a priced tuple.
    for index, rule in enumerate(contract["validation_rules"]):
        if all(key in resulting and resulting[key] in {str(v) for v in values} for key, values in rule["criteria"].items()):
            for field, values in rule["result"].items():
                if field in required:
                    change(field, values[0], f"shared_validation:{index}")
    for field, predicates in contract["display_fields"].items():
        if field not in required_options or not state.get(field):
            continue
        for dependency, values in predicates.items():
            if dependency in state and state[dependency] in {str(v) for v in values}:
                reject(f"field_hidden:{field}:{dependency}")
    for field, choices in contract["display_options"].items():
        if field not in state or not state[field]:
            continue
        for dependency, values in choices.get(state[field], {}).items():
            if dependency == "group":
                if all(k in state and state[k] in {str(v) for v in vv} for k, vv in values.items()):
                    reject(f"choice_hidden:{field}:group")
            elif dependency == "imageUploaded":
                unresolved.append("unmodeled_image_choice_guard")
            elif dependency in state and state[dependency] in {str(v) for v in values}:
                # Exact source override in displayOptions.
                if field == "spotCoating" and state.get("paper") == "10" and state.get("color") == "3" and state.get("coating") == "40" and state[field] in {"3", "4"}:
                    continue
                reject(f"choice_hidden:{field}:{dependency}")
    if state.get("raisedPrint") and (state.get("coating") or state.get("spotCoating")):
        reject("raised_print_and_coating_or_spot_are_mutually_reset")
    if state.get("color") in {"1", "2"} and state.get("coating") and state.get("spotCoating"):
        reject("single_sided_color_coating_and_spot_are_mutually_reset")
    if state.get("color") == "3" and state.get("coating") == "5" and state.get("spotCoating") in {"3", "39"}:
        reject("color3_coating5_hides_spot3_or39")
    if state.get("spotCoating") or state.get("raisedPrint"):
        if state.get("proofType") == "31":
            change("proofType", "100", "finish_changes_online_proof_service")
            change("fileDelivery", "5", "finish_changes_online_proof_service")
    if product_type == "27" and state.get("paper") == "4" and state.get("productionTime") == "3":
        change("productionTime", "1", "club_flyer_stock4_disallows_rush")
    if state.get("productionTime") == "3" and state.get("paper") in {"5", "6", "31", "32", "36", "56", "66"}:
        change("productionTime", "1", "rushCondition_stock_exclusion")
    if product_type == "36" and state.get("paper") == "12" and state.get("finish") not in {"1", "4"}:
        change("finish", "1", "roll_stock12_finish_correction")
    if state.get("color") == "3" and state.get("coating") and state.get("spotCoating"):
        # Some action sequences clear one field even when a static option is shown.
        unresolved.append("color3_coating_spot_action_order_requires_source_validation")
    if changes:
        reject("requested_state_is_changed_by_frontend")

    lookup_context = (lookup or {}).get("request", {}).get("context", {})
    if any(lookup_context.get(key) != context.get(key) for key in ("origin", "currency", "store_type", "store_id", "price_group_id", "session_scope", "source_epoch", "service_signature")):
        unresolved.append("lookup_pricing_context_mismatch")
    params = (lookup or {}).get("request", {}).get("params", {})
    if not lookup or lookup.get("validated") is not True or any(str(params.get(k, "")) != state.get(k, "") for k in FIELDS[product_type]) or str(params.get("productType", "")) != product_type or str(params.get("turnaround", "")) != state.get("productionTime"):
        unresolved.append("no_exact_full_selector_lookup")
    try:
        base_domain = parse_quantity_support(base_support)
        selected_domain = parse_option_quantity_support(option_support)
        options = parse_option_catalog(catalog)
        mode = state.get("productionTime")
        if quantity is not None and (quantity not in base_domain or mode not in base_domain[quantity]):
            reject("quantity_or_mode_not_in_base_support")
        for field in required_options:
            type_id = state.get(field)
            if not type_id:
                if field == "windPosition":
                    reject("wind_position_is_required")
                continue
            matches = [row for row in options.values() if row["type_id"] == type_id]
            if type_id not in OPTION_TYPES[field] or len(matches) != 1:
                unresolved.append("option_not_uniquely_bound:" + field)
                continue
            if type_id not in selected_domain:
                unresolved.append("missing_option_quantity_support:" + type_id)
            elif quantity is not None and (quantity not in selected_domain[type_id] or mode not in selected_domain[type_id][quantity]):
                reject("option_quantity_or_mode_unsupported:" + type_id)
            if type_id in contract["price_bearing_option_types"]:
                prices = option_prices.get(matches[0]["id"])
                if prices is None:
                    unresolved.append("missing_price_tier_domain:" + matches[0]["id"])
                elif quantity is not None:
                    table = parse_price_table(prices)
                    if quantity not in table:
                        if len(table) == 1:
                            unresolved.append("single_tier_option_visibility_requires_validation:" + type_id)
                        else:
                            reject("selected_option_has_no_exact_price_tier:" + type_id)
    except (TableSchemaError, TypeError, KeyError, ValueError):
        unresolved.append("unsupported_catalog_or_support_schema")

    production = None
    fingerprint = configuration_fingerprint(product_type, selection, context, (lookup or {}).get("base_product_id", ""))
    if not turnaround_rule or turnaround_rule.get("selection_fingerprint") != fingerprint or not turnaround_rule.get("evidence_id"):
        unresolved.append("full_option_combination_turnaround_rule_not_bound")
    else:
        items = turnaround_rule.get("data", {}).get("items")
        if not isinstance(items, list) or not all(isinstance(row, dict) for row in items) or [str(row.get("id")) for row in items] not in [["1"], ["3"], ["1", "3"]]:
            unresolved.append("unsupported_turnaround_rule_schema")
        else:
            selected = next((row for row in items if str(row["id"]) == state.get("productionTime")), None)
            if selected is None:
                reject("selected_mode_absent_from_combined_turnaround_rules")
            else:
                production = selected.get("range")
                if not isinstance(production, dict) or not isinstance(production.get("min"), int) or isinstance(production.get("min"), bool) or production["min"] < 0 or (production.get("max") is not None and (not isinstance(production["max"], int) or isinstance(production["max"], bool) or production["max"] < production["min"])):
                    production = None
                    unresolved.append("unsupported_turnaround_range")
    return {"status": "invalid" if invalid else "unresolved" if unresolved else "supported",
            "invalid": invalid, "unresolved": sorted(set(unresolved)), "changes": changes,
            "resulting_selection": resulting, "selection_fingerprint": fingerprint,
            "production": production, "source_rule_offsets": contract["source_offsets"],
            "validity_scope": "pinned_frontend_and_bound_domains_no_upload_standard_service_context",
            "pricing_composition_verified": False, "full_catalog_complete": False}
