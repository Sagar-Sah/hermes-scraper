"""Pure, fail-closed interpretation of an observed page diagnosis.

Diagnosis: ``status='ready', revision: str|nonnegative int, url: str, fields: list[dict]``.
Each field has an exact internal ``key``, source ``label``, ``configurable``
boolean, ``kind``, ``disabled``, ``multiple``, ``selected_values`` and observed
``choices`` (value/label/disabled, optional placeholder). Numeric controls carry
min/max/step strings or null. A submission contains exactly key/include/
fixed_values for every diagnosed field. This policy covers this page only.
Optional binding_kind/value_origin metadata is preserved, including a choice's
value_origin: a displayed label surrogate is not thereby a supplier option ID.

Finalization is separate from selecting columns. Existing matrix rows, including
rule_admissible rows with component totals, are not final by default. A collector
may attach ``finalization_evidence`` with availability, production and price
sections. EVERY section must contain ``selection_fingerprint`` from this module,
``full_selection=True`` and nonempty ``evidence_ids`` present in the row's evidence
list. Availability status is supported or source-proven invalid. Production
status is supported and its range has min/max (null max additionally requires
``open_ended=True``). Price status is reconciled or direct_quote, with subtotal
and currency matching the row. Reconciled prices additionally require the row's
pricing_composition_verified flag. A direct quote must have direct_quote
provenance. Pure code cannot verify evidence storage: the collector must verify
the referenced immutable responses before constructing this envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re


class SelectionPolicyError(ValueError):
    """The submitted policy is stale, incomplete or unsupported by the diagnosis."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SelectionPolicyError(f"{name} must be a list of strings")
    if len(set(value)) != len(value):
        raise SelectionPolicyError(f"{name} must not contain duplicate values")
    return list(value)


def _decimal(value):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Expected a finite decimal")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", str(value)):
        raise ValueError("Expected a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Expected a finite decimal") from None
    if not number.is_finite():
        raise ValueError("Expected a finite decimal")
    return number


def _numeric_valid(field, value):
    try:
        number = _decimal(value)
        minimum = _decimal(field["min"]) if field.get("min") is not None else None
        maximum = _decimal(field["max"]) if field.get("max") is not None else None
        if minimum is not None and maximum is not None and maximum < minimum:
            return False
        if minimum is not None and number < minimum or maximum is not None and number > maximum:
            return False
        step = field.get("step")
        if step not in (None, "any"):
            step = _decimal(step)
            if step <= 0:
                return False
            # Explicit source min is the numeric step base; no rounded float math.
            with localcontext() as context:
                context.prec = max(50, len(str(number)) + len(str(step)) + len(str(minimum)) + 10)
                if (number - (minimum if minimum is not None else Decimal(0))) % step:
                    return False
        return True
    except (ValueError, InvalidOperation):
        return False


def _multiple(field):
    return field.get("multiple") is True or field.get("kind") == "checkbox"


def _valid_values(field, values):
    if not _multiple(field) and len(values) != 1:
        return False
    if field.get("kind") in {"number", "numeric", "range"}:
        return len(values) == 1 and _numeric_valid(field, values[0])
    enabled = {choice["value"] for choice in field["choices"]
               if not choice["disabled"] and not choice.get("placeholder", False)}
    if not values:
        # An empty checkbox/multiple selection must have been observed explicitly.
        return _multiple(field) and field.get("selected_values") == []
    return all(value in enabled for value in values)


def normalize_selection(diagnosis, submitted_revision, submitted_fields):
    """Validate a complete user selection without inferring a hidden default."""
    if not isinstance(diagnosis, Mapping) or diagnosis.get("status") != "ready":
        raise SelectionPolicyError("Diagnosis is not ready")
    revision = diagnosis.get("revision")
    valid_revision = (isinstance(revision, str) and bool(revision)) or (type(revision) is int and revision >= 0)
    if not valid_revision or type(submitted_revision) is not type(revision) or submitted_revision != revision:
        raise SelectionPolicyError("Diagnosis revision is stale or missing")
    if not isinstance(diagnosis.get("url"), str) or not diagnosis["url"]:
        raise SelectionPolicyError("Diagnosis source URL is missing")
    source_fields = diagnosis.get("fields")
    if not isinstance(source_fields, list) or not isinstance(submitted_fields, list):
        raise SelectionPolicyError("Fields must be lists")
    source = {}
    for raw in source_fields:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("key"), str) or not raw["key"]:
            raise SelectionPolicyError("Diagnosed field key is missing")
        key = raw["key"]
        if key in source:
            raise SelectionPolicyError("Duplicate diagnosed field key")
        if not isinstance(raw.get("label"), str) or not raw["label"].strip():
            raise SelectionPolicyError(f"Source label is missing: {key}")
        label = raw["label"]
        if type(raw.get("configurable")) is not bool:
            raise SelectionPolicyError(f"Configurable flag is missing: {label}")
        field = {"key": key, "label": raw["label"], "configurable": raw["configurable"],
                 "kind": raw.get("kind", "content" if not raw["configurable"] else "select"),
                 "disabled": raw.get("disabled", False), "multiple": raw.get("multiple", False),
                 "choices": [], "selected_values": None,
                 **{bound: raw.get(bound) for bound in ("min", "max", "step")}}
        for source_key in ("source_name", "source_id", "source_key", "binding_kind", "value_origin"):
            if source_key in raw:
                if raw[source_key] is not None and not isinstance(raw[source_key], str):
                    raise SelectionPolicyError(f"Invalid source binding: {label}")
                field[source_key] = raw[source_key]
        if type(field["disabled"]) is not bool or type(field["multiple"]) is not bool:
            raise SelectionPolicyError(f"Invalid control flags: {label}")
        if "selected_values" in raw:
            field["selected_values"] = _strings(raw["selected_values"], f"Observed selection for {label}")
        choices = raw.get("choices", [])
        if not isinstance(choices, list):
            raise SelectionPolicyError(f"Choices must be a list: {label}")
        seen = {}
        for choice in choices:
            if (not isinstance(choice, Mapping) or not isinstance(choice.get("value"), str)
                    or not isinstance(choice.get("label"), str)
                    or type(choice.get("disabled", False)) is not bool
                    or type(choice.get("placeholder", False)) is not bool):
                raise SelectionPolicyError(f"Invalid observed choice: {label}")
            normalized_choice = {"value": choice["value"], "label": choice["label"],
                                 "disabled": choice.get("disabled", False),
                                 "placeholder": choice.get("placeholder", False)}
            if "value_origin" in choice:
                if choice["value_origin"] is not None and not isinstance(choice["value_origin"], str):
                    raise SelectionPolicyError(f"Invalid choice value origin: {label}")
                normalized_choice["value_origin"] = choice["value_origin"]
            if choice["value"] in seen:
                if normalized_choice != seen[choice["value"]]:
                    raise SelectionPolicyError(f"Ambiguous duplicate choice value: {label}")
                continue
            seen[choice["value"]] = normalized_choice
            field["choices"].append(normalized_choice)
        source[key] = field
    submitted = {}
    for entry in submitted_fields:
        if not isinstance(entry, Mapping) or set(entry) != {"key", "include", "fixed_values"}:
            raise SelectionPolicyError("Each selection must contain only key, include and fixed_values")
        key = entry["key"]
        if not isinstance(key, str) or key not in source or key in submitted:
            raise SelectionPolicyError("Unknown or duplicate submitted field")
        if type(entry["include"]) is not bool:
            raise SelectionPolicyError(f"Include must be boolean: {source[key]['label']}")
        submitted[key] = {"include": entry["include"],
                          "fixed_values": _strings(entry["fixed_values"], f"Fixed values for {source[key]['label']}")}
    if set(submitted) != set(source):
        raise SelectionPolicyError("Every diagnosed field requires exactly one selection")
    fields, included, omitted, hidden, labels = [], [], [], {}, set()
    for key, source_field in source.items():
        field = {**source_field, **submitted[key]}
        label = field["label"]
        fixed = field["fixed_values"]
        if field["include"]:
            if fixed:
                raise SelectionPolicyError(f"Included fields cannot have fixed values: {label}")
            if field["configurable"] and field["disabled"]:
                raise SelectionPolicyError(f"Disabled controls must retain their observed selection: {label}")
            if field["label"] in labels:
                raise SelectionPolicyError("Included fields have duplicate public labels")
            labels.add(field["label"])
            included.append(key)
        elif not field["configurable"]:
            if fixed:
                raise SelectionPolicyError(f"Content fields cannot have fixed values: {label}")
            omitted.append(key)
        else:
            if not _valid_values(field, fixed):
                raise SelectionPolicyError(f"Fixed selection is not an observed enabled choice or valid number: {label}")
            if field["disabled"] and (field["selected_values"] is None or set(fixed) != set(field["selected_values"])):
                raise SelectionPolicyError(f"Disabled control must keep its exact observed selection: {label}")
            hidden[key] = sorted(fixed) if _multiple(field) else list(fixed)
            field["fixed_values"] = hidden[key]
        fields.append(field)
    policy = {"schema": "diagnosis-selection-v1", "revision": revision,
              "source_url": diagnosis["url"], "coverage": "current_page_only",
              "fields": fields, "hidden_fixed": hidden,
              "included_field_keys": included, "omitted_content_keys": omitted}
    # Submission ordering does not change policy identity; source ordering stays visible.
    canonical = {**policy, "fields": sorted(fields, key=lambda field: field["key"]),
                 "included_field_keys": sorted(included), "omitted_content_keys": sorted(omitted)}
    policy["fingerprint"] = _hash(canonical)
    return policy


def _configuration_values(value):
    values = value if isinstance(value, list) else [value]
    if any(item is None or isinstance(item, (Mapping, list, bool)) for item in values):
        return None
    if any(not isinstance(item, (str, int, float, Decimal)) for item in values):
        return None
    result = [str(item) for item in values]
    if len(set(result)) != len(result):
        return None
    return result


def apply_selection(policy, configuration):
    """Filter actual rows and project labels; retain the complete internal row state."""
    if not isinstance(configuration, Mapping):
        raise SelectionPolicyError("Configuration must be a mapping")
    if not isinstance(policy, Mapping) or policy.get("schema") != "diagnosis-selection-v1":
        raise SelectionPolicyError("Unsupported selection policy")
    public, excluded, unresolved = {}, [], []
    known = {field["key"] for field in policy["fields"]}
    unresolved.extend(f"undiagnosed_configuration_field:{key}" for key in configuration if key not in known)
    for field in policy["fields"]:
        key = field["key"]
        if not field["configurable"] and not field["include"]:
            continue
        if key not in configuration:
            if field["configurable"]:
                unresolved.append(f"missing_required_field:{key}")
            continue
        values = _configuration_values(configuration[key])
        if values is None:
            unresolved.append(f"unsupported_value:{key}")
            continue
        if field["configurable"]:
            if not field["include"]:
                fixed = field["fixed_values"]
                matches = set(values) == set(fixed)
                if field.get("kind") in {"number", "numeric", "range"} and len(values) == len(fixed) == 1:
                    try:
                        matches = _decimal(values[0]) == _decimal(fixed[0])
                    except ValueError:
                        matches = False
                if not matches:
                    excluded.append(f"nonfixed_value:{key}")
                    continue
            if not _valid_values(field, values):
                unresolved.append(f"value_not_in_observed_domain:{key}")
                continue
        if field["include"]:
            choices = {choice["value"]: choice["label"] for choice in field["choices"]}
            labels = [choices.get(value, value) for value in values]
            if field["configurable"] and field.get("kind") not in {"number", "numeric", "range"} and any(not label for label in labels):
                unresolved.append(f"source_choice_label_missing:{key}")
                continue
            public[field["label"]] = labels if isinstance(configuration[key], list) or _multiple(field) else labels[0]
    return {"status": "excluded" if excluded else "unresolved" if unresolved else "included",
            "public_configuration": public, "internal_configuration": deepcopy(dict(configuration)),
            "reasons": sorted(set(excluded + unresolved))}


def selection_fingerprint(row):
    """Bind finalization evidence to full internal selections and price context."""
    raw_configuration = row.get("configuration", {})
    raw_configuration = {key: value.get("value") if isinstance(value, Mapping) and "value" in value else value
                         for key, value in raw_configuration.items()}
    configuration = row.get("selection")
    if not isinstance(configuration, Mapping):
        configuration = raw_configuration
    production = row.get("production") or {}
    mode = (production.get("value", production.get("mode", production.get("id")))
            if isinstance(production, Mapping) else None)
    return _hash({"schema": "full-selection-finalization-v1", "selection": configuration,
                  "configuration": raw_configuration,
                  "quantity": row.get("quantity", configuration.get("quantity")),
                  "production_mode": configuration.get("productionTime", mode),
                  "product_type": row.get("product_type", row.get("source_product_id")),
                  "base_product_id": row.get("base_product_id"),
                  "source_url": row.get("product_url", row.get("source_url")),
                  "pricing_context": row.get("pricing_context", row.get("price_dependencies", {}).get("context")),
                  "service_context": row.get("service_context"), "currency": row.get("currency"),
                  "pricing_scope": row.get("pricing_scope")})


def _evidence_ids(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and bool(item.strip()) for item in value))


def classify_finalization(row):
    """Only evidenced complete selections finalize; unknowns stay quarantined."""
    if not isinstance(row, Mapping):
        return {"status": "held", "reasons": ["invalid_row_schema"]}
    try:
        fingerprint = selection_fingerprint(row)
    except (TypeError, ValueError, AttributeError):
        return {"status": "held", "reasons": ["invalid_full_selection"]}
    envelope = row.get("finalization_evidence")
    envelope = envelope if isinstance(envelope, Mapping) else {}
    row_ids = row.get("evidence_ids")
    row_ids = set(row_ids) if _evidence_ids(row_ids) else set()

    def bound(section):
        return (isinstance(section, Mapping) and section.get("full_selection") is True
                and section.get("selection_fingerprint") == fingerprint
                and _evidence_ids(section.get("evidence_ids"))
                and set(section["evidence_ids"]) <= row_ids)

    availability = envelope.get("availability", {})
    if bound(availability) and availability.get("status") == "invalid":
        return {"status": "invalid", "reasons": ["source_proven_invalid_configuration"]}
    production = envelope.get("production", {})
    if bound(production) and production.get("status") == "invalid":
        return {"status": "invalid", "reasons": ["source_proven_disallowed_production"]}
    reasons = []
    if not bound(availability) or availability.get("status") != "supported":
        reasons.append("availability_not_supported_for_full_selection")
    if row.get("status") not in {None, "supported"} or row.get("unresolved"):
        reasons.append("unresolved_configuration_constraints")
    valid_range = False
    if bound(production) and production.get("status") == "supported":
        window = production.get("range")
        if isinstance(window, Mapping) and {"min", "max"} <= set(window):
            try:
                low = _decimal(window["min"])
                high = _decimal(window["max"]) if window["max"] is not None else None
                valid_range = low >= 0 and ((high is not None and high >= low)
                                          or (high is None and production.get("open_ended") is True))
            except ValueError:
                pass
    if not valid_range:
        reasons.append("complete_configuration_production_not_evidenced")
    price = envelope.get("price", {})
    verified_price = False
    if bound(price):
        if price.get("status") == "direct_quote":
            verified_price = row.get("provenance") == "direct_quote"
        elif price.get("status") == "reconciled":
            verified_price = row.get("pricing_composition_verified") is True
        try:
            amount, observed = _decimal(row.get("subtotal")), _decimal(price.get("subtotal"))
            currency = row.get("currency")
            verified_price = (verified_price and amount >= 0 and amount == observed
                              and isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) is not None
                              and currency == price.get("currency"))
        except ValueError:
            verified_price = False
    if not verified_price or row.get("verification_status") == "failed":
        reasons.append("final_source_price_not_verified")
    return {"status": "held" if reasons else "eligible", "reasons": reasons}
