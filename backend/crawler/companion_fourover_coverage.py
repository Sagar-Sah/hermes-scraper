"""Join current 4over tables to complete, retained native-control evidence.

No browser requests occur here. Price tiers alone cannot prove See More was
expanded or conditional fields were inspected. Current collectors do not emit
``hermes-fourover-rendered-child-domain/v1`` yet; callers therefore receive an
explicit unknown with a driver requirement, never an inferred completion.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math
import re
import time

from .fourover_child_prices import _SCOPE, _scope, _url
from .fourover_options import _unique_object
from .product_companion import _value, current_child_table_snapshot


_ID = re.compile(r"[1-9][0-9]{0,12}")
_SHA = re.compile(r"[a-f0-9]{64}")
_CYCLE = re.compile(r"[a-f0-9]{32}")
_DRIVER = "fourover-rendered-domain-and-conditional-controls/v1"


class _CoverageError(ValueError):
    """Only locally declared diagnostic codes cross the evidence boundary."""



def _timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _money(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,18})?", value):
        raise _CoverageError("coverage_exact_source_price_required")
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise _CoverageError("coverage_exact_source_price_required")
    return result


class FourOverCoverageVerifier:
    """Trusted local evidence loader; AI/API clients cannot submit pass flags.

    ``load_object`` should be the existing Store.get_object method. A receipt
    reference is validated by bytes, SHA and the canonical content-addressed
    path before any of its claims are considered. No source/site equality is
    required: a verified source-price change is a valid correction candidate.
    """
    def __init__(self, load_object, *, now=time.time, price_dependency_verifiers=None):
        self.load_object, self.now = load_object, now
        # Reviewed collector code can prove a complete loaded-table dependency
        # without paying for a quote per row. No API/model response can register
        # callbacks, and a canary or a receipt's own boolean never enables one.
        self.price_dependency_verifiers = dict(price_dependency_verifiers or {})

    def _dependency_rows(self, native, collection, child, attributes, quantities, productions):
        reference = native.get("price_dependency")
        identifier = reference.get("verifier_id") if isinstance(reference, dict) else None
        verifier = self.price_dependency_verifiers.get(identifier)
        if not callable(verifier):
            raise _CoverageError("coverage_every_quantity_exact_quote_or_registered_dependency_required")
        proof = verifier(native=native, collection=collection, child_id=child, attributes=attributes,
                         load_evidence=self._evidence)
        if (not isinstance(proof, dict) or proof.get("schema") != "hermes-fourover-proven-price-dependency/v1"
                or proof.get("child_id") != child or proof.get("source_url") != collection["source_url"]
                or proof.get("snapshot_id") != collection["snapshot_id"] or proof.get("scope") != _SCOPE
                or not _sha(proof.get("binding_evidence_sha256")) or not isinstance(proof.get("rows"), list)):
            raise _CoverageError("coverage_registered_price_dependency_invalid")
        _scope(proof["scope"])
        rows = {row.get("quantity"): row for row in proof["rows"] if isinstance(row, dict)}
        if len(rows) != len(proof["rows"]) or set(rows) != set(quantities):
            raise _CoverageError("coverage_registered_price_dependency_incomplete")
        for quantity, row in rows.items():
            production = {key: productions[str(quantity)][0][key] for key in ("value", "label")}
            if type(quantity) is not int or row.get("production") != production:
                raise _CoverageError("coverage_registered_price_dependency_incomplete")
            _money(row.get("cost"))
        return rows, proof["binding_evidence_sha256"]

    def inspect_native_observations(self, *, option_map, collection, fields, cycle, quote_receipts):
        """Use actual automatic-profile observations without overstating them.

        These additive observations can prove that every child's quantity list
        was fully visible. They describe production at the pre-quote quantity
        only and cannot by themselves certify a full payable price domain.
        ``verify`` remains the complete-domain gate for supported corrections.
        """
        result = {"schema": "hermes-fourover-native-domain-join/v1", "status": "unknown",
                  "source_domain_complete": False, "source_price_evidence_complete": False,
                  "expanded_rendered_domain": "unknown", "source_quantity_domain_complete": False,
                  "checks": {"native_evidence_integrity": "unknown", "expanded_rendered_quantities": "unknown"},
                  "required_driver": _DRIVER, "supplier_requests_performed": 0, "evidence": []}
        try:
            from .fourover_automatic_profile import safe_native_domain_observation
            if (not isinstance(cycle, dict) or not isinstance(cycle.get("id"), str) or not _CYCLE.fullmatch(cycle["id"])
                    or not _timestamp(cycle.get("created_at")) or cycle["created_at"] > self.now()):
                raise _CoverageError("coverage_current_cycle_required")
            _scope(collection.get("scope"))
            source_url = collection.get("source_url")
            if option_map.get("source_url") != source_url or option_map.get("complete_child_mapping") is not True:
                raise _CoverageError("coverage_confirmed_option_mapping_required")
            projection = current_child_table_snapshot(option_map, collection, fields=fields, source_url=source_url)
            attributes = self._attributes(option_map)
            if not isinstance(quote_receipts, list) or not quote_receipts or len(quote_receipts) > 100000:
                raise _CoverageError("coverage_current_native_quote_receipts_required")
            observed_children, expanded_children = set(), set()
            raw_keys = {"status", "child_id", "observed_run_size", "selected_attributes", "attribute_controls", "scope_controls",
                        "additional_option_fields", "quantity_controls", "production_controls"}
            for ref in quote_receipts:
                quote = self._evidence(ref, "fourover-automatic-quote.json")
                _scope(quote.get("scope"))
                child = (quote.get("selection") or {}).get("child_id")
                if child not in attributes:
                    raise _CoverageError("coverage_current_native_quote_binding_required")
                native = self._evidence(quote.get("native_domain_evidence"), "fourover-native-domain.json")
                request = quote.get("request_context") or {}
                _scope(request.get("services"))
                if (quote.get("schema") != "fourover-automatic-source-quote/v1" or native != quote.get("native_domain_observation")
                        or (quote.get("association") or {}).get("source_url") != source_url
                        or (quote.get("catalog_reference") or {}).get("snapshot_id") != collection.get("snapshot_id")
                        or request.get("child_id") != child or request.get("attributes") != attributes[child]
                        or type(request.get("sets")) is not int or request["sets"] != 1
                        or native.get("snapshot_id") != collection.get("snapshot_id")
                        or native.get("schema") != "fourover-native-domain-observation/v1" or native.get("status") != "observed"
                        or native.get("source_url") != source_url or native.get("scope") != _SCOPE
                        or native.get("document_sha256") != quote.get("source_document_sha256")
                        or native.get("price_response_sha256") != quote.get("price_response_sha256")
                        or native.get("page_generation") != quote.get("page_generation")
                        or not _timestamp(native.get("observed_at")) or not _timestamp(quote.get("observed_at"))
                        or not cycle["created_at"] <= native["observed_at"] <= quote["observed_at"] <= self.now()
                        or self.now() - native["observed_at"] > 6 * 3600):
                    raise _CoverageError("coverage_current_native_quote_binding_required")
                table_row = next(row for row in collection["observations"] if row["child_id"] == child)
                if table_row.get("raw_response_sha256") != quote.get("price_response_sha256"):
                    raise _CoverageError("coverage_current_native_table_binding_required")
                raw = {key: native[key] for key in raw_keys}
                sanitized = safe_native_domain_observation(raw, source=source_url, selection=quote["selection"],
                    table={"sha256": table_row["raw_response_sha256"], "rows": table_row["package_prices"]},
                    document_sha256=quote["source_document_sha256"], page_generation=quote["page_generation"],
                    snapshot_id=collection["snapshot_id"], observed_at=native["observed_at"])
                if sanitized != native:
                    raise _CoverageError("coverage_native_observation_integrity_invalid")
                domain = projection["quantity_domains_by_child"][child]
                if int(native["observed_run_size"]) not in domain:
                    raise _CoverageError("coverage_native_observation_integrity_invalid")
                observed_children.add(child)
                if native["expanded_rendered_domain"] == "verified":
                    expanded_children.add(child)
                result["evidence"].append(quote["native_domain_evidence"]["sha256"])
            result["checks"]["native_evidence_integrity"] = "match" if observed_children == set(attributes) else "unknown"
            complete = expanded_children == set(attributes)
            result["checks"]["expanded_rendered_quantities"] = "match" if complete else "unknown"
            result.update(source_quantity_domain_complete=complete, expanded_rendered_domain="verified" if complete else "unknown",
                          reason="native_quantity_domain_verified_full_price_and_conditional_join_still_required" if complete
                                 else "all_current_children_expanded_rendered_quantities_required")
        except (ValueError, TypeError, KeyError, AttributeError, OSError, StopIteration) as exc:
            result["reason"] = str(exc) if isinstance(exc, _CoverageError) else "coverage_evidence_invalid"
        return result

    def _evidence(self, reference, suffix):
        if not isinstance(reference, dict) or set(reference) != {"object_path", "sha256"} or not _sha(reference.get("sha256")):
            raise _CoverageError("coverage_evidence_reference_required")
        sha = reference["sha256"]
        if reference["object_path"] != f"objects/{sha[:2]}/{sha}.{suffix}":
            raise _CoverageError("coverage_evidence_path_invalid")
        raw = self.load_object(reference["object_path"])
        if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha:
            raise _CoverageError("coverage_evidence_hash_changed")
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError):
            raise _CoverageError("coverage_evidence_json_invalid") from None
        if not isinstance(value, dict):
            raise _CoverageError("coverage_evidence_json_invalid")
        return value

    @staticmethod
    def _attributes(option_map):
        rows = {}
        for row in option_map.get("rows", []):
            if not isinstance(row, dict) or not _ID.fullmatch(str(row.get("child_id", ""))):
                raise _CoverageError("coverage_mapping_invalid")
            child = str(row["child_id"])
            fields = row.get("fields")
            if not isinstance(fields, list) or not fields or child in rows:
                raise _CoverageError("coverage_mapping_invalid")
            values = {}
            for field in fields:
                identifier, value = field.get("attribute_id"), field.get("value")
                if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or not isinstance(value, str) or not _ID.fullmatch(value) or identifier in values:
                    raise _CoverageError("coverage_mapping_invalid")
                values[identifier] = value
            rows[child] = values
        if not rows or any(set(row) != set(next(iter(rows.values()))) for row in rows.values()):
            raise _CoverageError("coverage_mapping_invalid")
        return rows

    def _retained_domain(self, value, *, child, attributes, all_attributes, quantities, cycle, collection, source_url):
        _scope(value.get("scope"))
        if (value.get("schema") != "hermes-fourover-rendered-child-domain/v1" or value.get("producer") != _DRIVER
                or value.get("source_url") != source_url or value.get("child_id") != child
                or value.get("cycle_id") != cycle["id"] or value.get("snapshot_id") != collection["snapshot_id"]
                or value.get("scope") != _SCOPE or not _timestamp(value.get("observed_at"))
                or not cycle["created_at"] <= value["observed_at"] <= self.now()
                or self.now() - value["observed_at"] > 6 * 3600
                or value.get("document_sha256") != collection.get("document_body_sha256")
                or not _sha(value.get("document_sha256"))
                or not _sha(value.get("price_response_sha256"))
                or value.get("price_response_sha256") != next(row["raw_response_sha256"] for row in collection["observations"] if row["child_id"] == child)
                or value.get("selected_attributes") != attributes or value.get("additional_option_fields") != []
                or value.get("scope_controls") != {
                    "job_sample": {"count": 1, "checked": False, "enabled": True},
                    "digital_proof": {"count": 1, "checked": False, "enabled": True},
                    "sets": {"count": 1, "value": "1", "enabled": True, "valid": True}}):
            raise _CoverageError("coverage_current_retained_child_controls_required")
        if any(type(value["scope_controls"][name]["count"]) is not int for name in ("job_sample", "digital_proof", "sets")):
            raise _CoverageError("coverage_current_retained_child_controls_required")
        controls = value.get("attribute_controls")
        if not isinstance(controls, dict) or set(controls) != set(attributes):
            raise _CoverageError("coverage_conditional_attribute_inventory_required")
        for identifier, selected in attributes.items():
            control = controls[identifier]
            compatible = {row[identifier] for row in all_attributes.values()
                          if all(row[other] == attributes[other] for other in attributes if other != identifier)}
            if (not isinstance(control, dict) or set(control) != {"count", "selected", "enabled", "visible", "implicit_fixed_dash", "options"}
                    or type(control["count"]) is not int or control["count"] != 1 or control["selected"] != selected or control["enabled"] is not True
                    or not isinstance(control["options"], list) or not control["options"]):
                raise _CoverageError("coverage_conditional_attribute_inventory_required")
            options = control["options"]
            if any(not isinstance(o, dict) or set(o) != {"value", "enabled", "label"}
                   or not isinstance(o["value"], str) or not _ID.fullmatch(o["value"])
                   or type(o["enabled"]) is not bool or not isinstance(o["label"], str) for o in options):
                raise _CoverageError("coverage_conditional_attribute_inventory_required")
            if len({o["value"] for o in options}) != len(options) or {o["value"] for o in options if o["enabled"]} != compatible:
                raise _CoverageError("coverage_conditional_choices_changed")
            implicit = control["implicit_fixed_dash"] is True and len(options) == 1 and options[0]["value"] == selected and options[0]["label"] == "-"
            if control["visible"] is not True and not implicit:
                raise _CoverageError("coverage_selected_attribute_not_visible")
        expanded = value.get("quantity_controls")
        if (not isinstance(expanded, dict) or set(expanded) != {"available", "row_count", "truncated", "remaining_expand_controls", "expansion"}
                or expanded["available"] != quantities or any(type(q) is not int for q in expanded["available"])
                or type(expanded["row_count"]) is not int or expanded["row_count"] != len(quantities)
                or expanded["truncated"] is not False or type(expanded["remaining_expand_controls"]) is not int or expanded["remaining_expand_controls"] != 0
                or not isinstance(expanded["expansion"], dict)):
            raise _CoverageError("coverage_expanded_rendered_quantity_domain_required")
        expansion = expanded["expansion"]
        if (set(expansion) != {"action", "before", "after"} or expansion["after"] != quantities
                or not isinstance(expansion["before"], list) or not set(expansion["before"]).issubset(set(quantities))
                or (expansion["action"] == "all_already_rendered" and expansion["before"] != quantities)
                or (expansion["action"] == "visible_see_more" and expansion["before"] == quantities)
                or expansion["action"] not in {"visible_see_more", "all_already_rendered"}):
            raise _CoverageError("coverage_expansion_action_not_proved")
        # A product with additional production choices needs a richer native
        # adapter. Do not call a default-only quote the full production domain.
        productions = value.get("production_by_quantity")
        if not isinstance(productions, dict) or set(productions) != {str(q) for q in quantities}:
            raise _CoverageError("coverage_production_domain_required")
        for quantity, choices in productions.items():
            if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
                    or set(choices[0]) != {"value", "label", "enabled", "selected", "visible"}
                    or not isinstance(choices[0]["value"], str) or not re.fullmatch(r"[0-9]{1,6}", choices[0]["value"])
                    or not isinstance(choices[0]["label"], str) or not re.search(r"\b(?:business\s+days?|days?|hours?)\b", choices[0]["label"], re.I)
                    or any(choices[0][key] is not True for key in ("enabled", "selected", "visible"))):
                raise _CoverageError("coverage_conditional_production_adapter_required")
        return productions

    def verify(self, *, option_map, collection, fields, cycle, rendered_child_receipts=None):
        result = {"schema": "hermes-fourover-coverage-review/v1", "status": "unknown", "source_domain_complete": False,
                  "source_quantity_domain_complete": False, "source_price_evidence_complete": False,
                  "checks": {"source_evidence_integrity": "unknown", "expanded_rendered_quantities": "unknown",
                             "conditional_options": "unknown", "retained_services": "unknown", "exact_quote_prices": "unknown"},
                  "eligible_source_rows": [], "evidence": [],
                  "required_driver": _DRIVER, "supplier_requests_performed": 0,
                  "storefront_cost_comparison": "separate_check_not_a_source_integrity_gate"}
        try:
            if (not isinstance(cycle, dict) or not isinstance(cycle.get("id"), str) or not _CYCLE.fullmatch(cycle["id"])
                    or not _timestamp(cycle.get("created_at")) or cycle["created_at"] > self.now()):
                raise _CoverageError("coverage_current_cycle_required")
            source_url = collection.get("source_url")
            _url(source_url)
            _scope(collection.get("scope"))
            if option_map.get("source_url") != source_url or option_map.get("complete_child_mapping") is not True:
                raise _CoverageError("coverage_confirmed_option_mapping_required")
            if not isinstance(collection.get("snapshot_id"), str) or not _CYCLE.fullmatch(collection["snapshot_id"]):
                raise _CoverageError("coverage_current_snapshot_required")
            projection = current_child_table_snapshot(option_map, collection, fields=fields, source_url=source_url)
            if projection["row_count"] > 100000:
                raise _CoverageError("coverage_source_domain_limit")
            attrs = self._attributes(option_map)
            if not isinstance(rendered_child_receipts, dict) or set(rendered_child_receipts) != set(attrs):
                raise _CoverageError("current_cycle_expanded_rendered_child_receipts_missing")
            source_rows = []
            for child, attributes in attrs.items():
                ref = rendered_child_receipts[child]
                native = self._evidence(ref, "fourover-rendered-domain.json")
                quantities = projection["quantity_domains_by_child"][child]
                productions = self._retained_domain(native, child=child, attributes=attributes, all_attributes=attrs,
                                                    quantities=quantities, cycle=cycle, collection=collection, source_url=source_url)
                quotes = native.get("quote_receipts")
                exact_quotes = isinstance(quotes, dict) and set(quotes) == {str(q) for q in quantities}
                dependency_rows, dependency_sha = ({}, None) if exact_quotes else self._dependency_rows(
                    native, collection, child, attributes, quantities, productions)
                price_rows = {row["quantity"]: row["price"] for observation in collection["observations"]
                              if observation["child_id"] == child for row in observation["package_prices"]}
                for quantity in quantities:
                    expected = {"child_id": child, "run_size": quantity, "attributes": attributes}
                    production = {key: productions[str(quantity)][0][key] for key in ("value", "label")}
                    if exact_quotes:
                        quote = self._evidence(quotes[str(quantity)], "fourover-automatic-quote.json")
                        _scope(quote.get("scope"))
                        request = quote.get("request_context") or {}
                        _scope(request.get("services"))
                        bound = (quote.get("schema") == "fourover-automatic-source-quote/v1" and quote.get("scope") == _SCOPE
                            and quote.get("selection") == expected and type((quote.get("selection") or {}).get("run_size")) is int
                            and (quote.get("association") or {}).get("source_url") == source_url
                            and quote.get("source_document_sha256") == native["document_sha256"]
                            and quote.get("page_generation") == native.get("page_generation") and bool(native.get("page_generation"))
                            and quote.get("price_response_sha256") == native["price_response_sha256"]
                            and _timestamp(quote.get("observed_at")) and cycle["created_at"] <= quote["observed_at"] <= self.now()
                            and self.now() - quote["observed_at"] <= 6 * 3600
                            and request.get("child_id") == child and request.get("attributes") == attributes
                            and type(request.get("run_size")) is int and request.get("run_size") == quantity
                            and type(request.get("sets")) is int and request.get("sets") == 1 and request.get("services") == _SCOPE
                            and request.get("production") == production and quote.get("production") == production
                            and _sha(quote.get("summary_response_sha256"))
                            and _money(quote.get("current_source_table_price")) == _money(price_rows[quantity])
                            and _money((quote.get("totals") or {}).get("subtotal")) == _money(price_rows[quantity])
                            and _money(quote.get("supplier_product_total_excluding_shipping")) == _money((quote.get("totals") or {}).get("grand_total")))
                        if not bound:
                            raise _CoverageError("coverage_native_exact_quote_binding_invalid")
                        price_evidence_sha, price_basis = quotes[str(quantity)]["sha256"], "native_exact_quote"
                    else:
                        if _money(dependency_rows[quantity]["cost"]) != _money(price_rows[quantity]):
                            raise _CoverageError("coverage_registered_dependency_table_price_changed")
                        price_evidence_sha, price_basis = dependency_sha, "registered_complete_price_dependency"
                    mapped = next(row for row in option_map["rows"] if str(row["child_id"]) == child)
                    selection = {re.sub(r"[^a-z0-9]", "", field["label"].casefold()): _value(field["choice"]) for field in mapped["fields"]}
                    selection["quantity"] = str(quantity)
                    # Use the canonical existing projection's normalized field
                    # values rather than rebuilding aliases for a write action.
                    verified = projection["rows"].get(tuple(selection[field] for field in fields))
                    if verified is None:
                        raise _CoverageError("coverage_source_selection_projection_changed")
                    source_rows.append({"selection": verified["selection"], "cost": verified["cost"],
                                        "production": production["label"], "price_evidence_sha256": price_evidence_sha,
                                        "price_basis": price_basis,
                                        **({"quote_sha256": price_evidence_sha} if exact_quotes else {})})
                result["evidence"].append(ref["sha256"])
            result["checks"] = {key: "match" for key in result["checks"]}
            result.update(status="verified_scoped_source", source_domain_complete=True, source_quantity_domain_complete=True,
                          source_price_evidence_complete=True, eligible_source_rows=source_rows,
                          reason="complete_current_cycle_one_set_services_off_source_evidence",
                          source_scope={**_SCOPE, "production": "all_native_choices_for_this_supported_single_choice_adapter"})
        except (ValueError, TypeError, KeyError, AttributeError, OSError, StopIteration) as exc:
            # Only fixed local codes are exposed. Transport messages or source
            # text are never copied into a compact director digest.
            code = str(exc) if isinstance(exc, _CoverageError) else "coverage_evidence_invalid"
            result["reason"] = code if re.fullmatch(r"[a-z0-9_]{1,120}", code) else "coverage_evidence_invalid"
        return result


def bind_source_operations(coverage, operations):
    """Validate corrections against source facts, irrespective of old site cost.

    This function does not authorize writes. Revision/CAS, staging baseline,
    typed repair preview, recheck and rollback gates remain the caller's job.
    """
    if (not isinstance(coverage, dict) or coverage.get("status") != "verified_scoped_source"
            or coverage.get("source_domain_complete") is not True
            or coverage.get("source_price_evidence_complete") is not True):
        raise _CoverageError("verified_source_coverage_required")
    rows = coverage.get("eligible_source_rows")
    if not isinstance(rows, list) or not isinstance(operations, list) or not operations or len(operations) > 1000:
        raise _CoverageError("bounded_source_operations_required")
    verified, seen = [], set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("kind") not in {"set_cost", "add_row"}:
            raise _CoverageError("supported_source_bound_operation_required")
        expected_keys = {"kind", "selection", "cost"} | ({"production"} if operation["kind"] == "add_row" else set())
        if set(operation) != expected_keys:
            raise _CoverageError("supported_source_bound_operation_required")
        selection = operation.get("selection")
        match = [row for row in rows if row.get("selection") == selection]
        if len(match) != 1 or _money(operation.get("cost")) != _money(match[0]["cost"]):
            raise _CoverageError("operation_not_bound_to_verified_source")
        key = json.dumps(selection, sort_keys=True)
        if key in seen:
            raise _CoverageError("duplicate_source_operation")
        seen.add(key)
        if operation["kind"] == "add_row" and operation.get("production") != match[0].get("production"):
            raise _CoverageError("operation_production_not_bound_to_verified_source")
        verified.append({"kind": operation["kind"], "selection": selection, "source_cost": match[0]["cost"],
                         "price_evidence_sha256": match[0]["price_evidence_sha256"], "price_basis": match[0]["price_basis"]})
    return {"source_bound": True, "operations": verified, "write_authorized": False}
