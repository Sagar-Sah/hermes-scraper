"""Pure finite-domain discovery planning from saved source specifications/code.

Every cross-axis tuple is a lookup candidate, never a declared valid product.
The returned manifest contains discovery operations only and cannot unlock prices.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re


class DiscoveryContractError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _function(source, name, containing=None):
    marker = name + ":function("
    positions = [match.start() for match in re.finditer(re.escape(marker), source)]
    if containing is not None:
        matches = []
        for position in positions:
            result = _function_at(source, position)
            if containing in result[1]:
                matches.append((position, result[1]))
        if len(matches) != 1:
            raise DiscoveryContractError(f"Expected one source function {name} containing {containing}")
        return matches[0]
    if len(positions) != 1:
        raise DiscoveryContractError(f"Expected one source function {name}")
    return _function_at(source, positions[0])


def _function_at(source, start):
    opening = source.index("{", start)
    depth, quote, escaped = 0, None, False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, source[start:index + 1]
    raise DiscoveryContractError(f"Unclosed source function {name}")


def source_contract(order_form_source, common_source):
    """Validate the specific saved frontend call pattern, not server capabilities."""
    functions = {}
    for file, source, names in (
        ("orderForm.js", order_form_source, ["getProductId", "processProductIdData", "getQuantityBySpec", "getOptionQuantityBySpec", "getOption", "autoDetectShapeForSize"]),
        ("chunk-common.js", common_source, ["getSpec", "getProductId", "getOption", "getOptionQuantityBySpec", "getOptionEnumById", "getTurnaround"]),
    ):
        for name in names:
            containing = "data:e" if file == "chunk-common.js" and name == "getTurnaround" else None
            offset, text = _function(source, name, containing)
            functions[file + ":" + name] = {"offset": offset, "source": text}
    lookup = functions["orderForm.js:getProductId"]["source"]
    consumer = functions["orderForm.js:processProductIdData"]["source"]
    if "/service/rest/v1/products?productType=" not in lookup or "&turnaround=" not in lookup:
        raise DiscoveryContractError("Frontend lookup query composition changed")
    if not re.search(r"1==\w+\.items\.length", consumer):
        raise DiscoveryContractError("Frontend does not establish an exact single-match consumer")
    for name in ("getSpec", "getProductId", "getOption", "getOptionQuantityBySpec"):
        if not re.search(r"\.get\(", functions["chunk-common.js:" + name]["source"]):
            raise DiscoveryContractError(f"Cannot confirm GET method for {name}")
    if 'method:"POST"' not in functions["chunk-common.js:getTurnaround"]["source"]:
        raise DiscoveryContractError("Cannot confirm turnaround rule request method")
    mappings = {}
    switch = functions["chunk-common.js:getOptionEnumById"]["source"]
    for cases, field in re.findall(r'((?:case \d+:)+)\w+="(\w+)"', switch):
        mappings.update({value: field for value in re.findall(r"case (\d+):", cases)})
    products = {}
    for product_type in (1, 27, 36):
        pattern = rf"(?<![\w]){product_type}:\{{specs:(\[[^\]]*\]),options:(\[[^\]]*\])"
        declarations = [
            {"offset": match.start(), "specs": json.loads(match.group(1)), "options": json.loads(match.group(2))}
            for match in re.finditer(pattern, order_form_source)
        ]
        fields = {tuple(value["specs"]) for value in declarations}
        if len(fields) != 1:
            raise DiscoveryContractError(f"No unambiguous base fields for product type {product_type}")
        products[str(product_type)] = {
            "base_fields": list(next(iter(fields))),
            "option_variants": [list(v) for v in sorted({tuple(d["options"]) for d in declarations})],
            "declaration_offsets": [d["offset"] for d in declarations],
            "store_variant_resolution": "required_before_option_enumeration" if len({tuple(d["options"]) for d in declarations}) > 1 else "same_in_observed_variants",
        }
    required_snippets = {
        "lookup": ("orderForm.js:getProductId", '/service/rest/v1/products?productType='),
        "single_result": ("orderForm.js:processProductIdData", "items.length"),
        "shape_dependency": ("orderForm.js:autoDetectShapeForSize", "r.properties.shape"),
        "option_type_batch": ("orderForm.js:getOptionQuantityBySpec", "optionIds="),
        "comma_separated_option_types": ("orderForm.js:getOption", '.type.id+","'),
        "specifications": ("chunk-common.js:getSpec", "/settings/product/specifications"),
        "option_catalog": ("chunk-common.js:getOption", "/options?requestValues=true"),
    }
    references = {}
    for key, (function, needle) in required_snippets.items():
        entry = functions[function]
        location = entry["source"].find(needle)
        if location < 0:
            raise DiscoveryContractError(f"Required source evidence missing: {key}")
        references[key] = {"function": function, "offset": entry["offset"] + location,
                           "excerpt": entry["source"][max(0, location - 45):location + 220]}
    return {
        "bundle_sha256": {"orderForm.js": hashlib.sha256(order_form_source.encode()).hexdigest(),
                          "chunk-common.js": hashlib.sha256(common_source.encode()).hexdigest()},
        "products": products, "option_type_fields": mappings, "evidence": references,
        "lookup_method": "GET", "lookup_consumer": "exactly_one_item_required",
        "lookup_pagination": "not_established_by_this_frontend_contract",
        "bulk_product_listing": "not_established",
        "omitted_filter_listing": "not_established",
        "multiple_product_ids": "not_established",
        "option_type_ids_batching": "frontend_constructs_comma_separated_optionIds",
        "server_required_parameters": "not_established; fields below are frontend-composed selectors",
        "turnaround_rules_method": "POST",
        "turnaround_rules_request_body": "not_in_historical_response_table; excluded_from_this_discovery_manifest",
    }


def _values(specifications, key):
    values = specifications.get(key)
    if not isinstance(values, list):
        raise DiscoveryContractError(f"Missing specification list {key}")
    result = {}
    for item in values:
        if not isinstance(item, dict) or item.get("disabled") is True or item.get("isHidden") is True:
            continue
        raw = str(item.get("id", ""))
        if raw.isdigit() and int(raw) > 0:
            if raw in result and result[raw] != item:
                raise DiscoveryContractError(f"Conflicting {key} ID {raw}")
            result[raw] = item
    if not result:
        raise DiscoveryContractError(f"No finite listed IDs in {key}")
    return result


def plan_discovery(product_type, specifications, contract, *, evidence_id, product_url,
                   historical_lookups=()):
    product_type = str(product_type)
    declaration = contract["products"].get(product_type)
    if declaration is None:
        raise DiscoveryContractError("Only the three source-confirmed product contracts are supported")
    fields = declaration["base_fields"]
    expected = {"size", "orientation", "paper", "color"} | ({"shape", "finish"} if product_type == "36" else set())
    if set(fields) != expected:
        raise DiscoveryContractError("Unsupported base selector contract")
    sizes = _values(specifications, "sizes")
    papers = _values(specifications, "papers")
    colors = _values(specifications, "colors")
    modes = _values(specifications, "turnarounds")
    finishes = _values(specifications, "finishes") if "finish" in fields else {None: None}
    shapes = _values(specifications, "shapes") if "shape" in fields else {}
    geometries = []
    for size_id, size in sizes.items():
        orientation = str((size.get("orientation") or {}).get("id", ""))
        if not orientation.isdigit() or int(orientation) <= 0:
            raise DiscoveryContractError(f"Size {size_id} has no explicit orientation binding")
        geometry = {"size": size_id, "orientation": orientation}
        if "shape" in fields:
            shape_id = str((size.get("properties") or {}).get("shape", ""))
            if shape_id not in shapes:
                raise DiscoveryContractError(f"Size {size_id} has no explicit listed shape binding")
            geometry["shape"] = shape_id
        geometries.append(geometry)
    historical = {}
    for lookup in historical_lookups:
        query = {k: str(v[0]) for k, v in lookup.get("query", {}).items() if isinstance(v, list) and len(v) == 1}
        if lookup.get("product") == product_url and len(lookup.get("base_product_ids", [])) == 1:
            historical[_digest(query)] = lookup
    requests = []
    for geometry, paper, color, finish, mode in itertools.product(geometries, papers, colors, finishes, modes):
        query = {"productType": product_type, "turnaround": mode, **geometry, "paper": paper, "color": color}
        if finish is not None:
            query["finish"] = finish
        prior = historical.get(_digest(query))
        requests.append({
            "id": _digest(["GET", "/service/rest/v1/products", query]),
            "phase": "DISCOVERY", "kind": "validate_base_candidate", "method": "GET",
            "path": "/service/rest/v1/products", "params": query,
            "candidate_status": "unvalidated", "valid_combination": None,
            "historical_single_match_evidence_ids": prior.get("evidence_ids", []) if prior else [],
            "completion_requires": "A fresh context-bound single match, explicit unavailable result, or terminally recorded unresolved error; ambiguity/pagination never implies validity",
        })
    return {
        "schema_version": 1, "phase": "DISCOVERY", "product_type": product_type,
        "product_url": product_url, "specification_evidence_id": evidence_id,
        "source_contract_sha256": contract["bundle_sha256"],
        "geometry_tuples": geometries,
        "domain_counts": {"size_orientation_shape_tuples": len(geometries), "papers": len(papers),
                          "colors": len(colors), "finishes": len(finishes), "production_modes": len(modes)},
        "candidate_lookup_count": len(requests), "validated_base_count": 0,
        "requests": requests, "discovery_complete": False, "pricing_unlocked": False,
        "conditional_discovery": [
            {"after": "context-bound unique lookup", "method": "GET", "path": "/service/rest/v1/products/{base_product_id}/options",
             "params": {"requestValues": "true", "eddm": "explicit current product context required"}},
            {"after": "context-bound unique lookup", "method": "GET", "path": "/service/rest/v1/products/quantities",
             "params_from": "exact validated base selectors without turnaround, plus includeTurnaround=true"},
            {"after": "complete option catalog and resolved frontend option variant", "method": "GET", "path": "/service/rest/v1/products/options/quantities",
             "params_from": "same base selectors plus includeTurnaround=true and comma-separated eligible OPTION TYPE IDs, not option record IDs"},
        ],
        "gates": [
            "Complete all finite candidate lookups and dependent discovery; retain errors/ambiguous results as unresolved.",
            "Detect pagination metadata and do not mark a partial response complete.",
            "Resolve store-specific option variants before declaring option-domain exhaustion.",
            "Bind observations to the authorized session/pricing/service context; never infer hidden service selection.",
            "No price endpoint may run until discovery is explicitly complete for its declared scope.",
        ],
        "coverage_limits": [
            "Cross-axis candidates are not asserted valid; server lookup must resolve compatibility.",
            "Only finite listed preset sizes are covered; arbitrary custom dimensions remain outside this scope.",
            "Frontend selectors are not a proof of server-required fields or bulk/pagination support.",
        ],
    }


def option_type_query(catalog, enabled_fields, contract):
    """Mirror the source's comma-separated eligible type IDs after catalog discovery."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise DiscoveryContractError("Expected complete option catalog items")
    if any(catalog.get(key) for key in ("hasMore", "next", "nextPage")) or any(
        isinstance(catalog.get(key), int) and catalog[key] > len(catalog["items"])
        for key in ("total", "totalCount", "count")
    ):
        raise DiscoveryContractError("Option catalog is paginated or incomplete")
    ids = set()
    for item in catalog["items"]:
        value = str((item.get("type") or {}).get("id", ""))
        if contract["option_type_fields"].get(value) in enabled_fields:
            ids.add(value)
    return ",".join(sorted(ids, key=int))
