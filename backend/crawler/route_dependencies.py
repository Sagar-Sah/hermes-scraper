"""Pure, finite evidence comparisons for route dependency probes.

A certificate proves equal observed response content for its exact tested request
identities only. It is not permission to omit a context field, infer an untested
Cartesian product, or reuse an observation after the caller's freshness boundary.
Use opaque session/service scope identifiers, never cookies or credentials.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re


class DependencyError(ValueError):
    pass


REQUIRED_CONTEXT = {"origin", "session_scope", "source_epoch", "store_type", "currency", "service_signature"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def request_identity(request):
    """Preserve every supplied query, payload and context dimension, including unknowns."""
    context = request.get("context")
    if not isinstance(context, dict) or not REQUIRED_CONTEXT.issubset(context):
        raise DependencyError("Request lacks explicit session/source/store/currency/service context")
    if any(context[field] is None or context[field] == "" for field in REQUIRED_CONTEXT):
        raise DependencyError("Required request context must be explicitly resolved")
    method = request.get("method", "GET").upper()
    path = request.get("path", "")
    if not path.startswith("/") or "?" in path or "://" in path:
        raise DependencyError("Use a source-relative path and separate exact query parameters")
    return {"method": method, "path": path, "params": copy.deepcopy(request.get("params") or {}),
            "body": copy.deepcopy(request.get("body")), "context": copy.deepcopy(context)}


def request_key(request):
    return _hash(request_identity(request))


def _target(identity, dimension):
    parts = dimension.split(".")
    if len(parts) < 2 or parts[0] not in {"params", "body", "context"}:
        raise DependencyError("Probe dimension must name an existing request field")
    if parts[0] == "context" and parts[1] in REQUIRED_CONTEXT:
        raise DependencyError("Session, source and pricing scope cannot be normalized by a dependency probe")
    parent = identity
    for key in parts[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            raise DependencyError("Probe dimension is absent from the baseline request")
        parent = parent[key]
    if not isinstance(parent, dict) or parts[-1] not in parent:
        raise DependencyError("Probe dimension is absent from the baseline request")
    return parent, parts[-1]


def plan_dependency_probes(baseline, dimension, observed_values, *, discovery_complete=False,
                           lookup=None, catalog_option_ids=()):
    """Plan explicit single-axis comparisons; values must come from a discovered domain."""
    identity = request_identity(baseline)
    if "/prices" in identity["path"]:
        if not discovery_complete:
            raise DependencyError("Price probes remain locked until discovery is complete")
        plan_exact_quote_requests(lookup or {}, [identity], context=identity["context"],
                                  discovery_complete=True, catalog_option_ids=catalog_option_ids)
    parent, key = _target(identity, dimension)
    values = {_canonical(parent[key]): parent[key]}
    values.update({_canonical(value): value for value in observed_values})
    requests = []
    for value in values.values():
        variant = copy.deepcopy(identity)
        node, field = _target(variant, dimension)
        node[field] = value
        requests.append({"id": request_key(variant), "phase": "PROBE", "dimension": dimension,
                         "request": variant, "status": "unobserved"})
    return {"dimension": dimension, "requests": requests, "certificate": None,
            "claim": "Unexecuted finite comparisons; no dependency established"}


def _complete_data(observation):
    if observation.get("status") != 200 or observation.get("complete") is not True:
        raise DependencyError("Only explicit successful complete observations can be compared")
    if not observation.get("response_id") or "data" not in observation:
        raise DependencyError("Probe observation lacks response evidence or parsed content")
    data = observation["data"]
    if isinstance(data, dict):
        if any(data.get(key) for key in ("next", "nextPage", "hasMore")):
            raise DependencyError("A paginated partial response cannot certify equality")
        if isinstance(data.get("items"), list) and any(
            isinstance(data.get(key), int) and data[key] > len(data["items"])
            for key in ("total", "totalCount", "count")
        ):
            raise DependencyError("Incomplete item counts cannot certify equality")
    return _hash(data)


def compare_probe_observations(observations, dimension):
    """Return bounded equivalence evidence or an explicit dependency/confound result."""
    if len(observations) < 2:
        raise DependencyError("A dependency comparison requires at least two observations")
    members, response_hashes, projected = {}, set(), set()
    evidence_ids = []
    for observation in observations:
        identity = request_identity(observation["request"])
        digest = _complete_data(observation)
        full_key = _hash(identity)
        node, field = _target(identity, dimension)
        value = node.pop(field)
        projected.add(_hash(identity))
        if full_key in members and members[full_key]["response_hash"] != digest:
            return {"status": "unstable", "certificate": None, "reason": "Same complete request returned different content"}
        members[full_key] = {"value_hash": _hash(value), "response_hash": digest}
        response_hashes.add(digest)
        evidence_ids.append(observation["response_id"])
    if len(projected) != 1:
        return {"status": "confounded", "certificate": None,
                "reason": "More than the tested dimension changed; retain every dimension"}
    if len(members) < 2:
        raise DependencyError("Repeated identical request is not a dimension comparison")
    if len(response_hashes) != 1:
        return {"status": "dependent_for_observed_values", "certificate": None,
                "reason": "Changing the tested field changed the complete semantic response"}
    certificate = {
        "schema_version": 1, "dimension": dimension, "scope": "exact_tested_request_identities_only",
        "verified_members": sorted(members), "canonical_member": min(members),
        "response_hash": next(iter(response_hashes)), "evidence_ids": sorted(set(evidence_ids)),
        "untested_values_covered": False, "global_dimension_independence": False,
        "freshness": "caller_must_keep_same_resolved_source_epoch_and_enforce_validity_policy",
    }
    certificate["id"] = _hash(certificate)
    return {"status": "equal_for_tested_members", "certificate": certificate}


def finite_equivalence_key(request, certificate=None):
    """Alias only an exact tested member; never compose certificates or drop dimensions."""
    key = request_key(request)
    if certificate and certificate.get("scope") == "exact_tested_request_identities_only":
        if key in certificate.get("verified_members", ()):
            return certificate["canonical_member"]
    return key


def validate_unique_lookup(observation):
    """Bind a successful lookup ID to its complete request context; no historical shortcuts."""
    identity = request_identity(observation["request"])
    if identity["method"] != "GET" or identity["path"] != "/service/rest/v1/products":
        raise DependencyError("Expected the observed base product lookup route")
    digest = _complete_data(observation)
    items = observation["data"].get("items") if isinstance(observation["data"], dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise DependencyError("Lookup did not establish exactly one base product")
    product_id = str(items[0].get("id", ""))
    if not product_id.isdigit() or int(product_id) <= 0:
        raise DependencyError("Lookup returned no usable base product ID")
    product_type = str(identity["params"].get("productType", ""))
    required = {"productType", "turnaround", "size", "orientation", "paper", "color"}
    if product_type == "36":
        required |= {"shape", "finish"}
    if product_type not in {"1", "27", "36"} or any(not identity["params"].get(k) for k in required):
        raise DependencyError("Lookup is missing source-confirmed base selectors")
    return {"base_product_id": product_id, "request": identity, "request_key": _hash(identity),
            "response_hash": digest, "response_id": observation["response_id"], "validated": True}


def plan_exact_quote_requests(lookup, operations, *, context, discovery_complete, catalog_option_ids=()):
    """Accept explicit source-shaped quote operations only after validated discovery.

    Caller supplies exact observed params/body and resolved option IDs; this helper
    does not invent quantities, production formulas, selections, fees or defaults.
    """
    if discovery_complete is not True or lookup.get("validated") is not True:
        raise DependencyError("Exact quote requests require completed discovery and a unique validated lookup")
    if context != lookup["request"]["context"]:
        raise DependencyError("Quote context differs from the validated lookup context")
    base = re.escape(lookup["base_product_id"])
    option_ids = {str(value) for value in catalog_option_ids}
    requests = []
    for operation in operations:
        if "context" in operation and operation["context"] != context:
            raise DependencyError("Operation context differs from the validated lookup context")
        path = operation.get("path", "")
        option = re.fullmatch(rf"/service/rest/v1/products/{base}/options/(\d+)/prices", path)
        is_base = path == f"/service/rest/v1/products/{lookup['base_product_id']}/prices"
        if not is_base and (not option or option.group(1) not in option_ids):
            raise DependencyError("Quote route is not bound to this base and its discovered option catalog")
        request = request_identity({**operation, "context": context})
        if request["method"] != "GET":
            raise DependencyError("Only source-confirmed GET price tables are supported")
        requests.append({"id": request_key(request), "phase": "QUOTE", "request": request,
                         "lookup_evidence_id": lookup["response_id"], "status": "unobserved"})
    return {"phase": "QUOTE", "discovery_complete": True, "requests": requests, "prices_observed": False,
            "composition_verified": False, "exact_configuration_prices": []}
