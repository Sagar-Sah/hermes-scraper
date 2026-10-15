"""Pure provisional rows for one explicitly resolved Printograph selection.

No option Cartesian product, network access, coupon default, singleton-price
extrapolation or legacy quote upgrade. Availability gaps and missing prices are
reported separately. These rows require independent configuration quote checks.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit

from .adapters.browser_tables import (
    TableSchemaError,
    parse_option_catalog,
    parse_option_quantity_support,
    parse_price_table,
    parse_quantity_support,
    resolve_option,
)
from .commerce import unit_price
from .price_contract import (
    BASE_FIELDS,
    OPTION_FIELDS,
    PRICED_OPTIONS,
    SERVICE_FIELDS,
    SOURCE_CONTRACT,
    calculate_source_total,
    normalize_price_context,
    round_source_component,
)
from .route_dependencies import (
    DependencyError,
    request_identity,
    request_key,
    validate_unique_lookup,
)


class RowGenerationError(ValueError):
    pass


GROUPS = {
    "roundedCorners": "2",
    "coating": "4",
    "spotCoating": "5",
    "raisedPrint": "50",
    "raisedFoilColor": "51",
    "windPosition": "12",
    "manualProofType": "1",
    "proofType": "1",
    "designServices": "13",
}


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RowGenerationError("Quantities must be explicit positive integer values")
    return value


def source_service_signature(capture):
    normalized = normalize_price_context(capture)
    if normalized["status"] != "complete":
        raise RowGenerationError(
            "Complete explicit pricing context required: "
            + ", ".join(normalized["reasons"])
        )
    return {
        field: normalized["context"]["selections"][field] for field in SERVICE_FIELDS
    }


def table_context_key(capture):
    """Fixed selection scope; quantity alone is supplied by availability tables."""
    normalized = normalize_price_context(capture)
    if normalized["status"] != "complete":
        raise RowGenerationError(
            "Complete explicit pricing context required: "
            + ", ".join(normalized["reasons"])
        )
    context = deepcopy(normalized["context"])
    context["selections"].pop("quantity")
    return _hash(context)


def bind_table_scope(capture, *, request_context, selection_evidence_ids):
    """Prepare metadata for *new* observations, not a historical inference.

    The caller must bind these metadata at request start after observing the
    complete selection. This helper cannot certify that supplied IDs are genuine.
    Unknown old request/service state must not be filled in retrospectively.
    """
    if not selection_evidence_ids or any(
        not isinstance(value, str) or not value for value in selection_evidence_ids
    ):
        raise RowGenerationError("Selection source evidence is required")
    context = deepcopy(request_context)
    if context.get("service_signature") != source_service_signature(capture):
        raise RowGenerationError(
            "Request service signature differs from explicit selections"
        )
    if (
        context.get("origin")
        not in {"https://www.printograph.com", "https://printograph.com"}
        or context.get("store_type") != "broker"
        or context.get("currency") != "USD"
    ):
        raise RowGenerationError("Unsupported source request context")
    context["pricing_scope"] = table_context_key(capture)
    request_identity({"path": "/service/rest/v1/products", "context": context})
    return {
        "schema_version": 1,
        "source_contract": dict(SOURCE_CONTRACT),
        "pricing_scope": context["pricing_scope"],
        "request_context": context,
        "selection_evidence_ids": sorted(set(selection_evidence_ids)),
        "expected_requests": {},
        "dependency_certificates": [],
    }


def expected_comparison_key(capture):
    """No key for legacy or partial controller state."""
    normalized = normalize_price_context(capture)
    return normalized["context_hash"] if normalized["status"] == "complete" else None


def _scalar_params(request):
    params = request.get("params", {})
    if not isinstance(params, dict) or any(
        not isinstance(value, (str, int, bool)) for value in params.values()
    ):
        raise RowGenerationError(
            "Exact scalar query parameters required; no duplicate/multi-value inference"
        )
    return {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in params.items()
    }


def _observation(observation, role, scope):
    if (
        not isinstance(observation, dict)
        or observation.get("status") != 200
        or observation.get("complete") is not True
        or not observation.get("response_id")
        or "data" not in observation
    ):
        raise RowGenerationError(f"Incomplete or missing source observation: {role}")
    request = request_identity(observation.get("request", {}))
    expected = request_identity(scope.get("expected_requests", {}).get(role, request))
    for identity in (request, expected):
        if (
            identity["context"] != scope["request_context"]
            or identity["method"] != "GET"
            or identity["body"] is not None
        ):
            raise RowGenerationError(f"Request scope/method mismatch: {role}")
    data = observation["data"]
    if isinstance(data, dict) and any(
        data.get(key) for key in ("next", "nextPage", "hasMore")
    ):
        raise RowGenerationError(f"Partial paginated observation: {role}")
    if request_key(request) != request_key(expected):
        certified = False
        for certificate in scope.get("dependency_certificates", []):
            payload = {key: value for key, value in certificate.items() if key != "id"}
            if (
                certificate.get("id") == _hash(payload)
                and certificate.get("scope") == "exact_tested_request_identities_only"
                and certificate.get("untested_values_covered") is False
                and certificate.get("global_dimension_independence") is False
                and {request_key(request), request_key(expected)}
                <= set(certificate.get("verified_members", []))
                and certificate.get("response_hash") == _hash(data)
            ):
                certified = True
                break
        if not certified:
            raise RowGenerationError(
                f"No exact-member response-equivalence certificate: {role}"
            )
    return expected, observation["response_id"]


def _price_query(request, proof_mode):
    params = _scalar_params(request)
    if set(params) - {"itemId", "cid", "designService", "quantities"}:
        raise RowGenerationError("Unknown price-affecting query field")
    if params.get("itemId", "null") not in {"", "null"} or params.get("cid", "") != "":
        raise RowGenerationError("Account/order-specific price query unsupported")
    if (proof_mode == "101") != (params.get("designService") == "true") or (
        "designService" in params and params["designService"] != "true"
    ):
        raise RowGenerationError(
            "Design-service price route differs from selected proof mode"
        )
    if "quantities" not in params:
        return None
    values = params["quantities"].split(",")
    if any(
        not value.isdigit() or str(int(value)) != value or int(value) <= 0
        for value in values
    ) or len(set(values)) != len(values):
        raise RowGenerationError("Invalid explicit quantities query")
    return {int(value) for value in values}


def _selected_catalog_items(selected, product_type, catalog):
    items = []
    for field in OPTION_FIELDS[product_type]:
        value = selected[field]
        if value and field in GROUPS:
            items.append(
                (
                    field,
                    value,
                    resolve_option(catalog, type_id=value, group_id=GROUPS[field]),
                    field in PRICED_OPTIONS,
                )
            )
    proof = selected["proofType"]
    service_field = {
        "31": "proofType",
        "100": "manualProofType",
        "101": "designServices",
    }[proof]
    value = selected[service_field]
    items.append(
        (
            service_field,
            value,
            resolve_option(catalog, type_id=value, group_id=GROUPS[service_field]),
            proof != "31",
        )
    )
    return items


def generate_table_rows(
    capture,
    *,
    lookup,
    catalog,
    base_table,
    option_tables,
    quantity_support,
    option_quantity_support,
    scope,
    product_url,
    title="",
    quantities=None,
):
    """Generate one fixed source-supported selection across finite quantities.

    Inputs use the route_dependencies observation shape: request, status=200,
    complete=True, response_id, data. option_tables maps catalog record IDs to
    observations. bind_table_scope prepares the exact request-start context.
    All exclusions/gaps are returned; a missing/null price is never unavailable.
    """
    try:
        return _generate(
            capture,
            lookup=lookup,
            catalog=catalog,
            base_table=base_table,
            option_tables=option_tables,
            quantity_support=quantity_support,
            option_quantity_support=option_quantity_support,
            scope=scope,
            product_url=product_url,
            title=title,
            quantities=quantities,
        )
    except (TableSchemaError, DependencyError, KeyError, TypeError) as exc:
        raise RowGenerationError(str(exc)) from None


def _generate(
    capture,
    *,
    lookup,
    catalog,
    base_table,
    option_tables,
    quantity_support,
    option_quantity_support,
    scope,
    product_url,
    title,
    quantities,
):
    normalized = normalize_price_context(capture)
    if normalized["status"] != "complete":
        raise RowGenerationError(
            "Incomplete pricing context: " + ", ".join(normalized["reasons"])
        )
    # Explicit caller parameters are permitted only with a separate per-quantity
    # applicability witness. Controller-empty state is the reusable normal case.
    coupon_mode = normalized["context"]["coupon"].get("mode")
    if coupon_mode != "empty_source_coupon" and not scope.get(
        "coupon_quantity_evidence"
    ):
        raise RowGenerationError(
            "No reusable empty coupon state or exact quantity applicability evidence"
        )
    if (
        scope.get("schema_version") != 1
        or scope.get("source_contract") != SOURCE_CONTRACT
        or scope.get("pricing_scope") != table_context_key(capture)
    ):
        raise RowGenerationError(
            "Table scope differs from complete selection/source contract"
        )
    checked_scope = bind_table_scope(
        capture,
        request_context=scope["request_context"],
        selection_evidence_ids=scope.get("selection_evidence_ids"),
    )
    if checked_scope["request_context"] != scope["request_context"]:
        raise RowGenerationError("Missing request-start pricing scope")
    parsed_url = urlsplit(product_url)
    if (
        f"{parsed_url.scheme}://{parsed_url.netloc}"
        != scope["request_context"]["origin"]
    ):
        raise RowGenerationError("Product URL differs from captured source origin")
    selected = normalized["context"]["selections"]
    product_type, mode = (
        normalized["context"]["product_type"],
        selected["productionTime"],
    )
    base_params = {
        "productType": product_type,
        **{key: selected[key] for key in BASE_FIELDS[product_type]},
    }
    lookup_request, lookup_id = _observation(lookup, "lookup", scope)
    validated = validate_unique_lookup({**lookup, "request": lookup_request})
    base_id = validated["base_product_id"]
    if base_id != selected["productId"] or _scalar_params(lookup_request) != {
        **base_params,
        "turnaround": mode,
    }:
        raise RowGenerationError(
            "Lookup/base/specification/turnaround binding mismatch"
        )
    catalog_request, catalog_id = _observation(catalog, "catalog", scope)
    if catalog_request[
        "path"
    ] != f"/service/rest/v1/products/{base_id}/options" or _scalar_params(
        catalog_request
    ) != {
        "requestValues": "true",
        "eddm": "false",
    }:
        raise RowGenerationError("Option catalog is not bound to the resolved base")
    parsed_catalog = parse_option_catalog(catalog["data"])
    selected_items = _selected_catalog_items(selected, product_type, parsed_catalog)
    support_request, support_id = _observation(
        quantity_support, "quantity_support", scope
    )
    if support_request[
        "path"
    ] != "/service/rest/v1/products/quantities" or _scalar_params(support_request) != {
        **base_params,
        "includeTurnaround": "true",
    }:
        raise RowGenerationError(
            "Quantity support differs from resolved base specifications"
        )
    supported = parse_quantity_support(quantity_support["data"])
    option_support = {}
    option_support_id = None
    if option_quantity_support is not None:
        support_request, option_support_id = _observation(
            option_quantity_support, "option_quantity_support", scope
        )
        params = _scalar_params(support_request)
        raw_ids = params.pop("optionIds", "").split(",")
        if (
            support_request["path"] != "/service/rest/v1/products/options/quantities"
            or params != {**base_params, "includeTurnaround": "true"}
            or not all(raw_ids)
            or len(raw_ids) != len(set(raw_ids))
        ):
            raise RowGenerationError(
                "Option quantity query differs from resolved specifications"
            )
        catalog_types = {item["type_id"] for item in parsed_catalog.values()}
        if not set(raw_ids) <= catalog_types:
            raise RowGenerationError(
                "Option quantity query contains unknown catalog types"
            )
        option_support = parse_option_quantity_support(option_quantity_support["data"])
        if not set(option_support) <= set(raw_ids):
            raise RowGenerationError(
                "Option support response contains unrequested types"
            )
    base_request, base_evidence = _observation(base_table, "base", scope)
    if base_request["path"] != f"/service/rest/v1/products/{base_id}/prices":
        raise RowGenerationError("Wrong base price route")
    base_requested = _price_query(base_request, selected["proofType"])
    _price_query(request_identity(base_table["request"]), selected["proofType"])
    base_prices = parse_price_table(base_table["data"])
    price_sources = {}
    for field, value, item, priced in selected_items:
        if not priced:
            continue
        observation = option_tables.get(item["id"])
        if observation is None:
            price_sources[field] = None
            continue
        request, evidence_id = _observation(observation, "option:" + item["id"], scope)
        if (
            request["path"]
            != f"/service/rest/v1/products/{base_id}/options/{item['id']}/prices"
        ):
            raise RowGenerationError("Wrong selected option price route")
        price_sources[field] = {
            "prices": parse_price_table(observation["data"]),
            "quantities": _price_query(request, selected["proofType"]),
            "evidence_id": evidence_id,
            "option_id": item["id"],
        }
        _price_query(request_identity(observation["request"]), selected["proofType"])
    requested = (
        sorted({_positive(value) for value in quantities})
        if quantities is not None
        else sorted(supported)
    )
    result = {
        "rows": [],
        "excluded_quantities": [],
        "unresolved_quantities": [],
        "scope_key": scope["pricing_scope"],
        "whole_product_complete": False,
        "status": "complete_for_requested_selection",
        "verification_status": "pending",
    }
    for quantity in requested:
        exclusions, gaps = [], []
        if quantity not in supported or mode not in supported[quantity]:
            exclusions.append("quantity_or_turnaround_not_supported")
        for field, value, item, priced in selected_items:
            if value in option_support:
                if (
                    quantity not in option_support[value]
                    or mode not in option_support[value][quantity]
                ):
                    exclusions.append(
                        f"selected_option_quantity_or_turnaround_not_supported:{field}:{value}"
                    )
            elif item.get("anyQuantityOrdered") is not True:
                if field not in {"manualProofType", "designServices"}:
                    gaps.append(f"selected_option_availability_unknown:{field}:{value}")
                # Manual/design service table rows are exact quantity evidence;
                # absence/null is reported below, never inferred included.
        if exclusions:
            result["excluded_quantities"].append(
                {"quantity": quantity, "reasons": exclusions}
            )
            continue
        next_capture = deepcopy(capture)
        next_capture.update(capture_kind="explicit_request")
        next_capture.pop("controller_total", None)
        next_capture.pop("displayed_total", None)
        next_capture["userSelectedOptions"]["quantity"] = str(quantity)
        next_capture["appliedPrices"] = {}
        ledger_sources = {}
        if coupon_mode != "empty_source_coupon":
            witness = scope.get("coupon_quantity_evidence", {}).get(str(quantity))
            if (
                not isinstance(witness, dict)
                or not witness.get("evidence_id")
                or witness.get("selection_scope") != scope["pricing_scope"]
                or "coupon" not in witness
            ):
                gaps.append("coupon_applicability_not_bound_for_quantity")
            else:
                next_capture["coupon"] = deepcopy(witness["coupon"])

        def assign(
            field, value, prices, requested_quantities, evidence_id, option_id=None
        ):
            if (
                requested_quantities is not None
                and quantity not in requested_quantities
            ):
                gaps.append(f"quantity_not_covered_by_price_request:{field}")
                return
            entry = prices.get(quantity)
            if entry is None or entry.markup_price is None:
                gaps.append(f"missing_exact_price:{field}")
                return
            try:
                amount = round_source_component(format(entry.markup_price, "f"))
            except ValueError:
                gaps.append(f"invalid_exact_price:{field}")
                return
            bucket = "proofType" if field == "manualProofType" else field
            if value is None:
                next_capture["appliedPrices"][bucket] = amount
            else:
                next_capture["appliedPrices"][bucket] = {value: amount}
            ledger_sources[bucket] = {
                "evidence_id": evidence_id,
                "option_id": option_id,
                "table_quantity": quantity,
                "raw_markup_price": format(entry.markup_price, "f"),
            }

        assign("productPrice", None, base_prices, base_requested, base_evidence)
        for field, value, item, priced in selected_items:
            if priced:
                source = price_sources[field]
                if source is None:
                    gaps.append(f"missing_option_price_capture:{field}")
                else:
                    assign(
                        field,
                        value,
                        source["prices"],
                        source["quantities"],
                        source["evidence_id"],
                        source["option_id"],
                    )
        if gaps:
            result["unresolved_quantities"].append(
                {"quantity": quantity, "reasons": sorted(set(gaps))}
            )
            continue
        calculated = calculate_source_total(next_capture)
        if calculated["status"] != "complete":
            result["unresolved_quantities"].append(
                {"quantity": quantity, "reasons": calculated["reasons"]}
            )
            continue
        ledger = [
            {**component, **ledger_sources[component["field"]]}
            for component in calculated["components"]
        ]
        evidence = {
            lookup_id,
            catalog_id,
            support_id,
            base_evidence,
            *scope["selection_evidence_ids"],
        }
        if option_support_id:
            evidence.add(option_support_id)
        evidence.update(component["evidence_id"] for component in ledger)
        if coupon_mode != "empty_source_coupon":
            evidence.add(
                scope["coupon_quantity_evidence"][str(quantity)]["evidence_id"]
            )
        non_additive = []
        for field in OPTION_FIELDS[product_type]:
            if field not in PRICED_OPTIONS or selected[field] == "":
                non_additive.append(
                    {
                        "field": field,
                        "selected_value": selected[field],
                        "effect": "no_additive_term_in_reviewed_formula",
                        "evidence_ids": sorted(
                            {catalog_id, *scope["selection_evidence_ids"]}
                        ),
                        "source_contract": dict(SOURCE_CONTRACT),
                    }
                )
        for field in (
            "fileDelivery",
            *(["proofType"] if selected["proofType"] == "31" else []),
        ):
            non_additive.append(
                {
                    "field": field,
                    "selected_value": selected[field],
                    "effect": "no_additive_term_in_reviewed_product_subtotal_formula",
                    "evidence_ids": sorted(
                        {catalog_id, *scope["selection_evidence_ids"]}
                    ),
                    "source_contract": dict(SOURCE_CONTRACT),
                }
            )
        result["rows"].append(
            {
                "row_schema": "printograph_calculated_v1",
                "source_product_id": base_id,
                "product_url": product_url,
                "title": title,
                "quantity": quantity,
                "currency": "USD",
                "configuration": {
                    key: selected[key]
                    for key in (
                        *BASE_FIELDS[product_type],
                        *OPTION_FIELDS[product_type],
                    )
                },
                "production": {"value": mode, "status": "requires_configuration_quote"},
                "pricing_context": calculated["context"],
                "expected_comparison_key": calculated["context_hash"],
                "subtotal": calculated["subtotal"],
                "unit_price": unit_price(calculated["subtotal"], quantity),
                "subtotal_before_coupon": calculated["subtotal_before_coupon"],
                "components": ledger,
                "non_additive_components": non_additive,
                "coupon": {
                    "state": calculated["context"]["coupon"],
                    "reduction": calculated.get("coupon_reduction", "0.00"),
                    "evidence_ids": (
                        [
                            scope["coupon_quantity_evidence"][str(quantity)][
                                "evidence_id"
                            ]
                        ]
                        if coupon_mode != "empty_source_coupon"
                        else list(scope["selection_evidence_ids"])
                    ),
                },
                "evidence_ids": sorted(evidence),
                "verification_status": "pending",
                "provenance": {
                    "kind": "source_table",
                    "scope_key": scope["pricing_scope"],
                    "source_contract": dict(SOURCE_CONTRACT),
                    "formula_reconciled": True,
                },
                "combination_validity": "source_availability_supported_pending_quote",
                "cart_verified": False,
            }
        )
    if result["unresolved_quantities"]:
        result["status"] = "partial_unresolved"
    return result


def compare_direct_quote(row, direct_capture):
    """Return comparison evidence, never mutate or upgrade either input row."""
    if (
        direct_capture.get("capture_kind") != "controller"
        or direct_capture.get("displayed_total") is None
    ):
        return {
            "status": "unresolved",
            "reason": "Independent complete controller and display evidence required",
        }
    direct = calculate_source_total(direct_capture)
    if direct["status"] != "complete" or direct["comparison"] != "matched":
        return {
            "status": "unresolved",
            "reason": "Direct quote lacks complete matching source arithmetic",
            "details": direct["reasons"],
        }
    if direct["context_hash"] != row.get("expected_comparison_key"):
        return {"status": "context_mismatch"}
    return {
        "status": (
            "matched" if direct["subtotal"] == row.get("subtotal") else "price_mismatch"
        ),
        "expected_subtotal": row.get("subtotal"),
        "observed_subtotal": direct["subtotal"],
        "verification_status_unchanged": True,
    }
