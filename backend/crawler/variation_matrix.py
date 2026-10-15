"""Pure finite print-product inventories and conservative price-key factoring.

No HTTP, browser work or price composition. A rule-admissible matrix is not a
claim that unobserved combined turnaround rules or the entire catalog passed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from itertools import product
import hashlib
import json

from .adapters.browser_tables import (
    TableSchemaError, parse_option_catalog, parse_option_quantity_support,
    parse_price_table, parse_quantity_support,
)
from .printograph_constraints import (
    COMMON_HASH, ORDER_HASH, OPTION_TYPES, configuration_fingerprint,
    validate_configuration,
)
from .route_dependencies import request_key
from .price_contract import BASE_FIELDS as SOURCE_BASE_FIELDS, PRICED_OPTIONS, round_source_component

OPTIONS = ("roundedCorners", "coating", "spotCoating", "raisedPrint", "raisedFoilColor")
SERVICES = ("proofType", "fileDelivery", "designServices")
EXTRA_SERVICES = {"manualProofType"}
BASE_FIELDS = ("size", "orientation", "paper", "color")
OPTIONS_BY_TYPE = {"1": OPTIONS, "27": OPTIONS, "36": ("windPosition",)}
TURNAROUND_GAP = "full_option_combination_turnaround_rule_not_bound"
HASHES = {"orderForm.js": ORDER_HASH, "chunk-common.js": COMMON_HASH}


class MatrixError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _order(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def price_equivalence_key(selection, *, base_product_id, catalog, context, contract, product_type="1", _parsed_catalog=None):
    """Keep every unproven dependency; omission never follows equal sample prices.

    paperFinish is the reviewed source singleton Velvet, hasPrice=no, omitted by
    the source option serializer. No corner radius/position axes are supported.
    Full context remains in the key, including unknown account/coupon fields.
    """
    if contract.get("bundle_sha256") != HASHES:
        raise MatrixError("Unreviewed source contract")
    product_type = str(product_type)
    if product_type not in OPTIONS_BY_TYPE:
        raise MatrixError("Unsupported product type")
    options = _parsed_catalog if _parsed_catalog is not None else parse_option_catalog(catalog)
    selected_records, unresolved = [], []
    record_selections = {field: str(selection.get(field, "")) for field in OPTIONS_BY_TYPE[product_type]}
    if str(selection.get("proofType", "")) == "100":
        record_selections["manualProofType"] = str(selection.get("manualProofType", ""))
    elif str(selection.get("proofType", "")) == "31":
        record_selections["proofType"] = "31"
    for field, value in record_selections.items():
        if not value:
            continue
        matches = [item for item in options.values() if item["type_id"] == value]
        if len(matches) != 1:
            unresolved.append(field)
        else:
            selected_records.append({"field": field, "type_id": value, "option_record_id": matches[0]["id"],
                                     "price_bearing": value in contract["price_bearing_option_types"]})
    retained = {key: value for key, value in selection.items()
                if key != "paperFinish" or product_type == "36" or str(value) not in {"", "1"}}
    payload = {"schema": "bc-conservative-price-key-v1" if product_type == "1" else f"printograph-{product_type}-conservative-price-key-v1", "bundle_sha256": HASHES,
               "base_product_id": str(base_product_id), "selection": retained,
               "selected_option_records": selected_records, "context": deepcopy(context)}
    return {"key": _digest(payload), "dependencies": payload,
            "unresolved_option_records": unresolved,
            "omitted_fields": ["paperFinish"] if "paperFinish" in selection and "paperFinish" not in retained else [],
            "composition_verified": False}


def component_subtotal(row, baseentry, contract, *, _parsed=None):
    """Exact observed table arithmetic before coupons; never a final source quote.

    Each markup component follows parseFloat/toFixed(2), then the reviewed
    floating addition order base + manual proof + ordered option sum + design.
    Only proof31 and active manual1/36 are covered; missing/null tiers are not0.
    """
    gaps, components = [], []
    selection = row.get("selection", {})
    lookup = baseentry.get("lookup", {})
    params = lookup.get("request", {}).get("params", {})
    product_type = str(params.get("productType", ""))
    base_fields = SOURCE_BASE_FIELDS.get(product_type, ())
    option_fields = OPTIONS_BY_TYPE.get(product_type, ())
    if product_type not in OPTIONS_BY_TYPE or str(row.get("product_type", product_type)) != product_type:
        gaps.append("unsupported_or_mismatched_product_type")
    if contract.get("bundle_sha256") != HASHES:
        gaps.append("unreviewed_source_contract")
    if (lookup.get("validated") is not True or str(row.get("base_product_id")) != str(lookup.get("base_product_id")) or
            any(str(params.get(key, "")) != str(selection.get(key, "")) for key in base_fields) or
            str(params.get("turnaround", "")) != str(selection.get("productionTime", ""))):
        gaps.append("table_base_or_selector_binding_mismatch")
    optional_description = {"paperFinish"} if product_type in {"1", "27"} else set()
    if set(selection) - set(base_fields) - set(option_fields) - set(SERVICES) - EXTRA_SERVICES - {"quantity", "productionTime"} - optional_description:
        gaps.append("unmodeled_selection_price_dependency")
    try:
        quantity = int(selection["quantity"])
        if quantity <= 0 or str(quantity) != str(selection["quantity"]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        quantity = None
        gaps.append("invalid_exact_quantity")
    try:
        parsed = _parsed or {
            "catalog": parse_option_catalog(baseentry["catalog"]),
            "base_prices": parse_price_table(baseentry["base_prices"]) if baseentry.get("base_prices") is not None else None,
            "option_prices": {str(k): parse_price_table(v) for k, v in baseentry.get("option_prices", {}).items()},
        }
    except (KeyError, TypeError, TableSchemaError):
        parsed = {"catalog": {}, "base_prices": None, "option_prices": {}}
        gaps.append("unsupported_table_schema")
    evidence = baseentry.get("table_evidence", {})
    def component(field, type_id=None):
        option_id = None
        table, evidence_id = parsed["base_prices"], evidence.get("base_prices")
        if type_id is not None:
            matches = [item for item in parsed["catalog"].values() if item["type_id"] == type_id]
            if len(matches) != 1:
                gaps.append("option_record_not_uniquely_bound:" + field)
                return None
            option_id = matches[0]["id"]
            table = parsed["option_prices"].get(option_id)
            evidence_id = evidence.get("option_prices", {}).get(option_id)
        if table is None or quantity not in table:
            gaps.append("missing_exact_quantity_component:" + field)
            return None
        price = table[quantity].markup_price
        if price is None:
            gaps.append("null_price_component:" + field)
            return None
        try:
            rounded = round_source_component(format(price, "f"))
            if field == "base" and float(rounded) <= 0:
                raise ValueError
        except ValueError:
            gaps.append("invalid_price_component:" + field)
            return None
        components.append({"field": field, "type_id": type_id, "option_record_id": option_id,
                           "quantity": quantity, "markup_price": format(price, "f"),
                           "source_rounded_amount": rounded, "evidence_id": evidence_id})
        return float(rounded)
    base = component("base")
    manual, option_sum = 0.0, 0.0
    proof = str(selection.get("proofType", ""))
    if proof == "100":
        manual_type = str(selection.get("manualProofType", ""))
        if manual_type not in {"1", "36"}:
            manual = None
            gaps.append("unsupported_active_manual_proof")
        else:
            manual = component("manualProofType", manual_type)
    elif proof != "31":
        gaps.append("unsupported_active_service")
    if str(selection.get("fileDelivery", "")) != "5":
        gaps.append("unreviewed_file_delivery")
    for field in option_fields:
        selected = str(selection.get(field, ""))
        if field == "windPosition" and not selected:
            gaps.append("missing_required_wind_position")
        if selected and selected not in OPTION_TYPES[field]:
            gaps.append("unsupported_selected_option:" + field)
            continue
        if field not in PRICED_OPTIONS or not selected:
            continue
        if selected not in PRICED_OPTIONS[field]:
            gaps.append("unsupported_selected_price_option:" + field)
            continue
        amount = component(field, selected)
        if amount is not None:
            option_sum += amount
    subtotal = None
    if not gaps and base is not None and manual is not None:
        try:
            subtotal = round_source_component(str(base + manual + option_sum + 0.0))
        except ValueError:
            gaps.append("unreviewed_total_arithmetic")
    return {"table_subtotal": subtotal,
            "status": "calculated_pending_canary" if subtotal is not None else "unresolved",
            "components": components, "gaps": sorted(set(gaps)),
            "component_evidence_complete": bool(components) and all(item["evidence_id"] for item in components),
            "amount_scope": "Selected source-table components before coupon, shipping, tax or unmodeled context adjustments; not a final quote.",
            "source_context_verified_for_pricing": False, "cart_verified": False}


class VariationMatrix:
    """Stream source-rule candidates from exact lookups and captured finite domains.

    inventory: product_type='1'/'27'/'36', contract, context, service_states (explicit
    dictionaries containing proofType/fileDelivery/designServices), and bases.
    Each base supplies lookup, catalog, base_support, option_support,
    option_prices, optional labels/evidence_ids/turnaround_rules. lookup is the
    validated complete-selector/mode result; a listing ID is insufficient.
    completeness is caller evidence metadata, never inferred from row counts.
    """

    def __init__(self, inventory):
        self.inventory = deepcopy(inventory)
        inventory = self.inventory
        self.product_type = str(inventory.get("product_type"))
        if self.product_type not in OPTIONS_BY_TYPE:
            raise MatrixError("This matrix supports Business Cards, Club Flyers and Roll Labels")
        self.base_fields = SOURCE_BASE_FIELDS[self.product_type]
        self.option_fields = OPTIONS_BY_TYPE[self.product_type]
        self.contract = inventory.get("contract", {})
        if self.contract.get("bundle_sha256") != HASHES:
            raise MatrixError("Unreviewed source contract")
        self.context = inventory.get("context")
        if not isinstance(self.context, dict):
            raise MatrixError("An explicit complete source context is required")
        self.services = inventory.get("service_states", inventory.get("service_domains"))
        if not isinstance(self.services, list) or not self.services:
            raise MatrixError("Explicit finite service_states are required; no service defaults are invented")
        if any(not isinstance(state, dict) or not set(SERVICES) <= set(state) or set(state) - set(SERVICES) - EXTRA_SERVICES or
               any(not isinstance(value, (str, int)) or isinstance(value, bool) for value in state.values()) for state in self.services):
            raise MatrixError("Each service state must explicitly contain proofType, fileDelivery and designServices")
        self.services = [json.loads(value) for value in sorted({_json({key: str(value) for key, value in state.items()}) for state in self.services})]
        if not isinstance(inventory.get("bases"), list):
            raise MatrixError("bases must be a finite list")
        self.bases, seen = [], {}
        self.inventory_gaps = []
        for source in inventory["bases"]:
            lookup = source.get("lookup", {})
            params = lookup.get("request", {}).get("params", {})
            if (lookup.get("validated") is not True or not lookup.get("response_id") or
                    str(params.get("productType")) != self.product_type or
                    any(str(params.get(key, "")) == "" for key in (*self.base_fields, "turnaround"))):
                raise MatrixError("Every base needs a validated exact selector/mode lookup with evidence")
            if lookup.get("request", {}).get("context") != self.context:
                raise MatrixError("Base lookup context differs from matrix context")
            key = tuple(str(params[key]) for key in (*self.base_fields, "turnaround"))
            fingerprint = _digest(source)
            if key in seen:
                if seen[key] != fingerprint:
                    raise MatrixError("Conflicting inventory for the same selector/mode tuple")
                continue
            seen[key] = fingerprint
            try:
                parsed = {"catalog": parse_option_catalog(source["catalog"]),
                          "base_support": parse_quantity_support(source["base_support"]) if source.get("base_support") is not None else None,
                          "base_prices": parse_price_table(source["base_prices"]) if source.get("base_prices") is not None else None,
                          "option_support": parse_option_quantity_support(source["option_support"]),
                          "option_prices": {str(k): parse_price_table(v) for k, v in source.get("option_prices", {}).items()}}
            except (KeyError, TypeError, TableSchemaError) as exc:
                raise MatrixError("Inventory has an unsupported finite-domain schema") from exc
            self.bases.append((key, source, parsed))
        self.bases.sort(key=lambda entry: tuple(_order(v) for v in entry[0]))
        self.last_stats = {}

    def _validation_inputs(self, source, parsed, quantity):
        """A small exact projection of the already parsed tier domains, no prices invented."""
        def support_rows(support):
            return ([{"quantity": quantity, "turnaroundList": list(support[quantity])}] if quantity in support else [])
        prices = {}
        for option_id, table in parsed["option_prices"].items():
            if quantity in table:
                entry = table[quantity]
                prices[option_id] = {"items": [{"quantity": quantity,
                    "markupPrice": str(entry.markup_price) if entry.markup_price is not None else None}]}
            else:
                # Preserve the single-tier absence ambiguity exactly. Multi-tier
                # absent options were already excluded from the candidate axes.
                prices[option_id] = source["option_prices"][option_id]
        return {"lookup": source["lookup"],
                "catalog": {"items": [{"id": item["id"], "type": {"id": item["type_id"]}, "group": {"id": item["group_id"]}}
                                      for item in parsed["catalog"].values()]},
                "base_support": {"items": support_rows(parsed["base_support"])} if parsed["base_support"] is not None else None,
                "option_support": {key: support_rows(value) for key, value in parsed["option_support"].items()},
                "option_prices": prices, "context": self.context}

    def _quantity_domains(self, source, parsed, fixed, quantity):
        """Intersect known exclusions; absent evidence remains an unresolved branch."""
        mode = fixed["productionTime"]
        domains = []
        for field in self.option_fields:
            # Wind is mandatory; the other reviewed finite controls are optional.
            values = set() if field == "windPosition" else {""}
            for option in parsed["catalog"].values():
                type_id = option["type_id"]
                if type_id not in OPTION_TYPES[field]:
                    continue
                support = parsed["option_support"].get(type_id)
                if support is not None and mode not in support.get(quantity, ()):
                    continue
                prices = parsed["option_prices"].get(option["id"])
                if type_id in self.contract["price_bearing_option_types"] and prices is not None and quantity not in prices and len(prices) != 1:
                    continue
                values.add(type_id)
            # displaySpecs explicitly hides coating on these stocks below250.
            if field == "coating" and fixed["paper"] in {"3", "4", "5"} and quantity < 250:
                values = {""}
            domains.append(tuple(sorted(values, key=_order)))
        # Single-tier absence is deliberately unresolved in the source contract;
        # it must not borrow the validity result of a quantity that has a tier.
        price_presence = tuple((option_id, quantity in table) for option_id, table in sorted(parsed["option_prices"].items()))
        return tuple(domains), price_presence

    def iter_rows(self, *, include_unresolved=True):
        """No prices emitted. Static validation is reused across equal tier domains."""
        stats = Counter()
        self.inventory_gaps = []
        for key, source, parsed in self.bases:
            fixed = dict(zip(self.base_fields, key[:-1]))
            fixed["productionTime"] = key[-1]
            visible_velvet = self.product_type in {"1", "27"} and fixed["paper"] not in {str(v) for v in self.contract["display_fields"]["paperFinish"]["paper"]}
            if visible_velvet:
                fixed["paperFinish"] = "1"
            if parsed["base_support"] is not None:
                quantities = sorted(q for q, modes in parsed["base_support"].items() if key[-1] in modes)
            elif parsed["base_prices"] is not None:
                quantities = sorted(parsed["base_prices"])
            else:
                self.inventory_gaps.append({"base_product_id": str(source["lookup"]["base_product_id"]),
                                            "reason": "no_observed_quantity_domain"})
                continue
            grouped = defaultdict(list)
            for quantity in quantities:
                grouped[self._quantity_domains(source, parsed, fixed, quantity)].append(quantity)
            for (domains, _price_presence), tier_quantities in grouped.items():
                if any(not domain for domain in domains):
                    self.inventory_gaps.append({"base_product_id": str(source["lookup"]["base_product_id"]),
                        "quantities": tier_quantities, "production_mode": fixed["productionTime"],
                        "reason": "required_option_has_no_observed_supported_choice"})
                    continue
                validation = self._validation_inputs(source, parsed, tier_quantities[0])
                for options, service in product(product(*domains), self.services):
                    selection = {**fixed, **dict(zip(self.option_fields, options)), **service,
                                 "quantity": str(tier_quantities[0])}
                    checked_selection = {key: value for key, value in selection.items() if key not in EXTRA_SERVICES}
                    result = validate_configuration(self.product_type, checked_selection, contract=self.contract, **validation)
                    extra_gaps = (["manual_proof_mode_requires_additional_validation"] if "manualProofType" in service else [])
                    if parsed["base_support"] is None:
                        extra_gaps.append("base_quantity_mode_support_missing")
                    stats["finite_rule_evaluations"] += 1
                    stats["candidate_combinations_examined"] += len(tier_quantities)
                    if result["status"] == "invalid":
                        stats["known_invalid_combinations"] += len(tier_quantities)
                        continue
                    for quantity in tier_quantities:
                        selected = {**selection, "quantity": str(quantity)}
                        fingerprint = configuration_fingerprint(self.product_type, selected, self.context, source["lookup"]["base_product_id"])
                        current = result
                        combined = source.get("turnaround_rules", {}).get(fingerprint)
                        if combined:
                            current = validate_configuration(self.product_type, {key: value for key, value in selected.items() if key not in EXTRA_SERVICES}, contract=self.contract, lookup=source["lookup"],
                                catalog=source["catalog"], base_support=source["base_support"], option_support=source["option_support"],
                                option_prices=source.get("option_prices", {}), context=self.context, turnaround_rule=combined)
                            stats["combined_rule_validations"] += 1
                        if current["status"] == "invalid":
                            stats["known_invalid_combinations"] += 1
                            continue
                        gaps = sorted(set(current["unresolved"] + extra_gaps))
                        status = "supported" if not gaps else "rule_admissible" if set(gaps) == {TURNAROUND_GAP} else "unresolved"
                        if status == "unresolved" and not include_unresolved:
                            continue
                        priced = price_equivalence_key(selected, base_product_id=source["lookup"]["base_product_id"],
                            catalog=source["catalog"], context=self.context, contract=self.contract,
                            product_type=self.product_type, _parsed_catalog=parsed["catalog"])
                        labels = source.get("labels", source.get("source_labels", {}))
                        def selected_label(field, value):
                            label = labels.get(field)
                            if isinstance(label, dict):
                                return label.get(value)
                            if isinstance(label, str) and field in self.base_fields:
                                return label
                            if field in self.option_fields and value == "":
                                return "None"
                            if field in self.option_fields or field in {"manualProofType", "proofType"}:
                                matches = [item for item in parsed["catalog"].values() if item["type_id"] == value]
                                if len(matches) == 1:
                                    return matches[0].get("type", {}).get("label")
                            if field == "paperFinish" and value == "1":
                                return "Velvet"
                            return None
                        config = {field: {"value": value, "label": selected_label(field, value)}
                                  for field, value in selected.items() if field not in {"quantity", "productionTime"}}
                        row = {"product_type": self.product_type, "variation_key": fingerprint, "selection": selected, "configuration": config,
                               "base_product_id": str(source["lookup"]["base_product_id"]), "quantity": quantity,
                               "status": status, "unresolved": gaps, "production": current.get("production"),
                               "price_key": priced["key"], "price_dependencies": priced["dependencies"],
                               "price_key_omitted_fields": priced["omitted_fields"],
                               "evidence_ids": sorted(set(source.get("evidence_ids", []) + [source["lookup"]["response_id"]])),
                               "pricing_composition_verified": False}
                        arithmetic = component_subtotal(row, source, self.contract, _parsed=parsed)
                        row.update({"table_subtotal": arithmetic["table_subtotal"], "table_subtotal_status": arithmetic["status"],
                                    "table_subtotal_components": arithmetic["components"], "table_subtotal_gaps": arithmetic["gaps"],
                                    "component_evidence_complete": arithmetic["component_evidence_complete"],
                                    "table_subtotal_scope": arithmetic["amount_scope"], "source_context_verified_for_pricing": False})
                        yield row
        self.last_stats = dict(stats)

    def report(self):
        """Exact counts of this supplied finite inventory; no final catalog claim."""
        sizes, shapes, statuses, keys, gaps, rows = defaultdict(Counter), defaultdict(Counter), Counter(), set(), Counter(), 0
        subtotal_rows, subtotal_gaps = 0, Counter()
        targets, captured = set(), set()
        for row in self.iter_rows():
            rows += 1
            sizes[row["selection"]["size"]][row["status"]] += 1
            if "shape" in row["selection"]:
                shapes[row["selection"]["shape"]][row["status"]] += 1
            statuses[row["status"]] += 1
            keys.add(row["price_key"])
            gaps.update(row["unresolved"])
            subtotal_rows += row["table_subtotal"] is not None
            subtotal_gaps.update(row["table_subtotal_gaps"])
            base = row["base_product_id"]
            targets.add(f"/service/rest/v1/products/{base}/prices")
            for option in row["price_dependencies"]["selected_option_records"]:
                if option["price_bearing"]:
                    targets.add(f"/service/rest/v1/products/{base}/options/{option['option_record_id']}/prices")
        for _key, source, parsed in self.bases:
            base = str(source["lookup"]["base_product_id"])
            path = f"/service/rest/v1/products/{base}/prices"
            if source.get("base_prices") is not None:
                captured.add(path)
            for option_id in parsed["option_prices"]:
                captured.add(f"/service/rest/v1/products/{base}/options/{option_id}/prices")
        requests = self.inventory.get("price_requests")
        exact_requests = len({request_key(item) for item in requests}) if isinstance(requests, list) else None
        return {"product_type": self.product_type, "inventory_variation_count": rows,
                "candidate_rows": rows, "static_count": rows,
                "valid_rows": statuses["supported"] if not gaps and not self.inventory_gaps else None,
                "priced_rows": None,
                "table_subtotal_rows": subtotal_rows, "table_subtotal_gaps": dict(subtotal_gaps),
                "table_subtotal_scope": "Pre-coupon component arithmetic pending canary; not validated final prices.",
                "validity_counts": dict(statuses), "count_by_size": {size: dict(sizes[size]) for size in sorted(sizes, key=_order)},
                "count_by_shape": {shape: dict(shapes[shape]) for shape in sorted(shapes, key=_order)},
                "conservative_unique_price_keys": len(keys), "source_lookup_tuples": len(self.bases),
                "distinct_price_request_count": exact_requests,
                "component_table_target_count": len(targets), "captured_component_table_targets": len(targets & captured),
                "missing_component_table_targets": len(targets - captured),
                "request_count_scope": "Exact HTTP count requires explicit full-context price_requests. Component targets count distinct observed base/option table routes only, not retries, service contexts, support or turnaround requests.",
                "work": self.last_stats, "unresolved_counts": dict(gaps),
                "inventory_gaps": self.inventory_gaps,
                "input_completeness": deepcopy(self.inventory.get("completeness", {})),
                "catalog_complete": False, "fully_validated_configuration_count": statuses["supported"],
                "final_catalog_configuration_count": None, "pricing_composition_verified": False,
                "count_scope": "Supplied exact lookup tuples and finite captured domains; unsupported services and unobserved domains are not inferred.",
                "source_rule_offsets": self.contract["source_offsets"]}
