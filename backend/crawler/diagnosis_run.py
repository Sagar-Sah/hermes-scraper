"""Compile an explicitly saved page policy for a future single browser run.

This module starts no jobs and makes no requests. Compiled binding metadata is
separate from the saved policy fields/fingerprint. Guarding a candidate's hidden
choices does not establish complete availability, production or final pricing.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from .diagnosis_policy import normalize_selection


_SERVICES = {"proofType", "fileDeliveryForIop", "fileDelivery", "manualProofType", "designServices"}


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("A fixed numeric choice must be finite")
    text = str(value)
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        raise ValueError("A fixed numeric choice must be finite")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("A fixed numeric choice must be finite") from None
    if not number.is_finite():
        raise ValueError("A fixed numeric choice must be finite")
    # format(..., 'f') avoids exponent notation without context-sensitive rounding.
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if number == 0 else result


def _binding(field, *, required):
    from .adapters.browser import field_key

    label = field["label"]
    if field.get("multiple") or field.get("kind") == "checkbox":
        if required:
            raise ValueError(f"Browser runs do not yet support fixed checkbox or multiple choices: {label}")
        return None
    kind = field.get("kind")
    if kind in {"select", "number"}:
        raw = field.get("source_id") or field.get("source_name") or field.get("source_data_id")
    elif kind == "radio":
        raw = field.get("source_name")
    else:
        if required:
            raise ValueError(f"Browser runs do not support this fixed control kind: {label}")
        return None
    if not isinstance(raw, str) or not raw:
        if required:
            raise ValueError(f"An observed control name or ID is required: {label}")
        return None
    data_id_fallback = not field.get("source_id") and not field.get("source_name") and raw == field.get("source_data_id")
    base = raw if data_id_fallback else re.sub(r"_[a-z0-9]{5,}$", "", raw, flags=re.I)
    normalized = field_key(base)
    if not normalized:
        raise ValueError(f"The observed control has no usable browser key: {label}")
    # Service context retains its observed camel-case keys; browser filters use
    # field_key on receipt. This is a literal source-name match, never a label.
    key = base if base in _SERVICES else normalized
    return {"browser_key": key, "normalized_key": normalized, "source_raw_key": raw,
            "numeric": kind == "number"}


def _one_filter(value, *, numeric, label):
    values = value if isinstance(value, list) else [value]
    if len(values) != 1 or isinstance(values[0], (Mapping, list, tuple, bool)) or values[0] is None:
        raise ValueError(f"Existing filter must contain exactly the fixed choice: {label}")
    if not isinstance(values[0], (str, int, float, Decimal)):
        raise ValueError(f"Existing filter has an unsupported value: {label}")
    return _number(values[0]) if numeric else str(values[0])


def compile_browser_policy(diagnosis, urls, adapter, filters):
    """Return a browser spec fragment from the current, explicitly saved policy."""
    from .adapters.browser import field_key
    from .product_finalization import same_product

    if not isinstance(diagnosis, Mapping) or diagnosis.get("status") != "ready":
        raise ValueError("The product diagnosis must be ready")
    saved = diagnosis.get("selection")
    if not isinstance(saved, Mapping) or not isinstance(saved.get("fields"), list):
        raise ValueError("Save the current field choices before starting this browser run")
    submitted = [{key: entry.get(key) for key in ("key", "include", "fixed_values")}
                 for entry in saved["fields"] if isinstance(entry, Mapping)]
    policy = normalize_selection(diagnosis, saved.get("revision"), submitted)
    if saved.get("fingerprint") != policy["fingerprint"]:
        raise ValueError("Saved field choices no longer match the current diagnosis")
    if adapter not in {"auto", "browser"}:
        raise ValueError("Diagnosed field choices currently support browser runs only")
    if (not isinstance(urls, list) or len(urls) != 1 or not isinstance(urls[0], str)
            or not same_product(urls[0], policy["source_url"])):
        raise ValueError("Field choices apply to exactly the diagnosed product URL")
    if filters is None:
        filters = {}
    if not isinstance(filters, Mapping) or any(not isinstance(key, str) for key in filters):
        raise ValueError("Browser filters must be a field mapping")
    result_filters = deepcopy(dict(filters))
    hidden_keys = set(policy["hidden_fixed"])
    bindings, seen = {}, {}
    # Inspect known raw bindings for collisions, including an included control
    # that would accidentally receive another control's hidden filter.
    for field in policy["fields"]:
        if not field["configurable"]:
            continue
        if field.get("kind") == "required_text":
            raise ValueError(f"Required product text cannot be guessed by a browser run: {field['label']}")
        if (field.get("binding_kind") not in {None, "native_control"}
                or field.get("value_origin") not in {None, "native_value"}
                or any(choice.get("value_origin") not in {None, "native_value"}
                       for choice in field["choices"])):
            raise ValueError(f"Browser runs need source option values, not display-label bindings: {field['label']}")
        if field.get("kind") not in {"select", "radio", "number"} or field.get("multiple"):
            raise ValueError(f"Browser runs cannot traverse this diagnosed control yet: {field['label']}")
        binding = _binding(field, required=field["key"] in hidden_keys)
        if binding is None:
            continue
        normalized = binding["normalized_key"]
        if normalized in seen:
            previous = seen[normalized]
            if field["key"] in hidden_keys or previous["key"] in hidden_keys:
                raise ValueError(f"Observed controls share one browser key: {previous['label']} / {field['label']}")
        seen[normalized] = field
        if field["key"] not in hidden_keys:
            continue
        fixed = field["fixed_values"]
        if len(fixed) != 1:
            raise ValueError(f"Browser runs require one explicit fixed choice: {field['label']}")
        fixed_value = _number(fixed[0]) if binding["numeric"] else fixed[0]
        for key, value in list(result_filters.items()):
            if field_key(key) != normalized and key != binding["source_raw_key"]:
                continue
            if _one_filter(value, numeric=binding["numeric"], label=field["label"]) != fixed_value:
                raise ValueError(f"Existing filter conflicts with the saved fixed choice: {field['label']}")
            del result_filters[key]
        result_filters[binding["browser_key"]] = fixed_value
        bindings[field["key"]] = {**binding, "fixed_value": fixed_value}
    compiled = deepcopy(policy)
    compiled["browser_binding_schema"] = "diagnosis-browser-bindings-v1"
    compiled["browser_bindings"] = bindings
    return {"field_policy": compiled, "filters": result_filters, "adapter": "browser"}


def validate_policy_row(policy, row):
    """Reject a hidden-choice mismatch; return the same full candidate unchanged."""
    from .product_finalization import bind_configuration, same_product
    from .service_context import canonical_service_context

    if not isinstance(policy, Mapping) or policy.get("browser_binding_schema") != "diagnosis-browser-bindings-v1":
        raise ValueError("A compiled browser field policy is required")
    if not isinstance(row, Mapping):
        raise ValueError("A captured row must be a mapping")
    if row.get("product_url") is not None and not same_product(row["product_url"], policy["source_url"]):
        raise ValueError("The captured row belongs to a different source product")
    bindings = policy.get("browser_bindings")
    hidden = policy.get("hidden_fixed")
    if not isinstance(bindings, Mapping) or not isinstance(hidden, Mapping) or set(bindings) != set(hidden):
        raise ValueError("Compiled hidden-field bindings are incomplete")
    # Use the shared source-name binder but limit this guard to hidden choices.
    # Unrelated unknown fields remain candidates for the separate final gate.
    bound_policy = deepcopy(dict(policy))
    bound_policy["fields"] = []
    services = canonical_service_context(row)
    pricing_context = row.get("pricing_context")
    explicit_service_context = ("service_context" in row or "service_context_observed" in row
                                or isinstance(pricing_context, Mapping) and "service_context" in pricing_context)
    for field in policy["fields"]:
        if field["key"] in hidden:
            copied = deepcopy(field)
            binding = bindings[field["key"]]
            copied["source_key"] = binding["normalized_key"]
            group = services["groups"].get(binding["browser_key"])
            if group and group["status"] == "ambiguous":
                raise ValueError(f"Observed service context is conflicting: {field['label']}")
            if group and explicit_service_context and group["status"] != "selected":
                raise ValueError(f"Required service selection was not observed: {field['label']}")
            bound_policy["fields"].append(copied)
    try:
        values, issues = bind_configuration(bound_policy, row)
    except (TypeError, AttributeError, KeyError):
        raise ValueError("Captured configuration has an unsupported binding schema") from None
    blocking = [issue for issue in issues if not issue.startswith("configuration_field_not_diagnosed:")]
    if blocking:
        raise ValueError("Hidden selection is missing or conflicting: " + "; ".join(blocking))
    for field in bound_policy["fields"]:
        actual = values.get(field["key"])
        if not isinstance(actual, list) or len(actual) != 1:
            raise ValueError(f"Hidden selection was not observed exactly once: {field['label']}")
        binding = bindings[field["key"]]
        actual_value = _number(actual[0]) if binding["numeric"] else actual[0]
        if actual_value != binding["fixed_value"]:
            raise ValueError(f"Captured choice differs from the saved fixed choice: {field['label']}")
    return row
