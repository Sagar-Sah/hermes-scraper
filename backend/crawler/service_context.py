"""Canonical observed service selections; never infers a service or its fee.

An explicitly inspected, absent group is structural evidence. Missing legacy
diagnostics are unknown. Labels, visibility and disabled presentation do not
change the identity of an otherwise unambiguous checked raw value.
"""

from collections.abc import Mapping

SERVICE_CONTEXT_VERSION = 2
SERVICE_KEYS = ("proofType", "fileDeliveryForIop")


def _signature(groups, status=None):
    if status is None:
        states = {group["status"] for group in groups.values()}
        status = (
            "ambiguous"
            if "ambiguous" in states
            else "unknown" if "unknown" in states else "observed"
        )
    return {"version": SERVICE_CONTEXT_VERSION, "status": status, "groups": groups}


def _unknown():
    return _signature(
        {key: {"status": "unknown", "values": []} for key in SERVICE_KEYS}
    )


def _validated_signature(value):
    if (
        not isinstance(value, Mapping)
        or value.get("version") != SERVICE_CONTEXT_VERSION
    ):
        return _unknown()
    groups = value.get("groups")
    if not isinstance(groups, Mapping) or set(groups) != set(SERVICE_KEYS):
        return _unknown()
    cleaned = {}
    for key in SERVICE_KEYS:
        group = groups[key]
        if not isinstance(group, Mapping):
            return _unknown()
        state, values = group.get("status"), group.get("values")
        if (
            state not in {"selected", "absent", "unselected", "unknown", "ambiguous"}
            or not isinstance(values, list)
            or any(not isinstance(item, str) for item in values)
            or (state == "selected" and len(values) != 1)
            or (state in {"absent", "unselected", "unknown"} and values)
        ):
            return _unknown()
        cleaned[key] = {"status": state, "values": sorted(values)}
    result = _signature(cleaned)
    return result if value.get("status") == result["status"] else _unknown()


def canonical_service_context(source: Mapping) -> dict:
    """Read a snapshot/row without treating omitted diagnostics as absence."""
    context = source.get("pricing_context")
    if isinstance(context, Mapping) and "service_context" in context:
        signature = _validated_signature(context["service_context"])
        if "service_context" in source or "service_context_observed" in source:
            observed = canonical_service_context(
                {
                    key: value
                    for key, value in source.items()
                    if key != "pricing_context"
                }
            )
            if observed != signature:
                return _signature(
                    {key: {"status": "ambiguous", "values": []} for key in SERVICE_KEYS}
                )
        return signature
    if source.get("service_context_observed") is not True:
        return _unknown()
    raw = source.get("service_context")
    if not isinstance(raw, list):
        return _unknown()
    indexed = {key: [] for key in SERVICE_KEYS}
    for entry in raw:
        if not isinstance(entry, Mapping) or entry.get("key") not in indexed:
            return _signature(
                {key: {"status": "ambiguous", "values": []} for key in SERVICE_KEYS}
            )
        indexed[entry["key"]].append(entry)
    groups = {}
    for key, entries in indexed.items():
        if not entries:
            groups[key] = {"status": "absent", "values": []}
            continue
        if len(entries) != 1:
            groups[key] = {"status": "ambiguous", "values": []}
            continue
        entry = entries[0]
        selected = entry.get("selected")
        count = entry.get("control_count")
        valid = (
            isinstance(selected, list)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
            and all(
                isinstance(item, Mapping)
                and item.get("checked") is True
                and isinstance(item.get("value"), str)
                for item in selected
            )
            and entry.get("checked_count") == len(selected)
            and len(selected) <= count
        )
        values = sorted(item["value"] for item in selected) if valid else []
        expected = (
            "selected"
            if len(values) == 1
            else "unselected" if not values else "ambiguous"
        )
        state = (
            expected
            if valid and entry.get("selection_status") == expected
            else "ambiguous"
        )
        groups[key] = {"status": state, "values": values}
    return _signature(groups)


def service_context_is_bound(signature: Mapping) -> bool:
    cleaned = _validated_signature(signature)
    return cleaned["status"] == "observed" and all(
        group["status"] in {"selected", "absent"}
        for group in cleaned["groups"].values()
    )


def requires_service_context(row: Mapping) -> bool:
    """Scope browser price observations without imposing this source on adapters."""
    context = row.get("pricing_context") or {}
    return (
        "service_context" in row
        or "service_context_observed" in row
        or isinstance(context, Mapping)
        and (
            "service_context" in context
            or "source_product_type" in context
            and "store_type" in context
        )
    )
