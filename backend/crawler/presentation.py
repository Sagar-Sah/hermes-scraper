"""Copy-only corrections for labels inherited by table-derived quantity rows."""

import re
from collections.abc import Mapping
from .record_scope import is_legacy_unscoped, requires_service_context

_CORNER_SURCHARGE = re.compile(
    r"^(Round Corners)\s*\(\s*\+\s*\$\s*"
    r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\s*\)\s*$"
)


def present_row(row: Mapping) -> dict:
    """Remove an anchor's price annotation without changing money or evidence.

    A table-derived row did not observe this label at its own quantity. Keep
    the copied text explicitly as source_label and use its quantity-neutral
    name for display. Copy only the modified path; never mutate stored rows.
    """
    result = dict(row)
    if requires_service_context(row):
        result["record_scope"] = (
            "legacy_unscoped" if is_legacy_unscoped(row) else "current"
        )
        if result["record_scope"] == "legacy_unscoped":
            result["scope_warning"] = (
                "Historical service selection is unresolved; excluded from current price comparisons and exports"
            )
    if row.get("provenance") != "source_table":
        return result
    config = row.get("configuration")
    if not isinstance(config, Mapping):
        return result
    corners = config.get("roundedCorners")
    if not isinstance(corners, Mapping) or str(corners.get("value")) != "2":
        return result
    label = corners.get("label")
    match = _CORNER_SURCHARGE.fullmatch(label) if isinstance(label, str) else None
    if match is None:
        return result
    normalized = {**corners, "label": match.group(1)}
    normalized.setdefault("source_label", label)
    result["configuration"] = {**config, "roundedCorners": normalized}
    return result
