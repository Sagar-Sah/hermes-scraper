"""Customer CSV presentation; audit records and their identities stay unchanged."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .commerce import configuration, decimal_value
from .presentation import present_row
from .service_context import SERVICE_KEYS, canonical_service_context

LABEL_UNAVAILABLE = "Label unavailable"
BASE_COLUMNS = (
    "product_url", "title", "quantity", "currency", "subtotal", "unit_price",
    "verification_status", "coverage_status", "pricing_scope",
    "production_label", "production_status",
    *(name for key in SERVICE_KEYS for name in (f"service:{key}", f"service_status:{key}")),
)


def _internal_key(key):
    tokens = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(key)).lower()
    parts = set(re.split(r"[^a-z0-9]+", tokens))
    return bool(parts & {"id", "ids", "sku", "skus", "evidence", "provenance", "context", "internal"})


def _label(value):
    if isinstance(value, Mapping):
        for name in ("label", "selected_label", "name"):
            label = value.get(name)
            if isinstance(label, str) and label.strip():
                return label
        # An explicit empty option is preserved. An opaque value is never
        # translated to a customer-facing option name without source evidence.
        return "" if value.get("value") == "" else LABEL_UNAVAILABLE
    if isinstance(value, (list, tuple)):
        return "; ".join(_label(item) for item in value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _attributes(config):
    result = {}
    def add(key, value):
        if _internal_key(key):
            return
        if isinstance(value, Mapping) and not any(name in value for name in ("label", "selected_label", "name", "value", "id")):
            for child, item in value.items():
                add(f"{key}.{child}", item)
        else:
            result[f"attr:{key}"] = _label(value)
    for key, value in config.items():
        add(str(key), value)
    return result


def customer_row(incoming):
    """Allowlisted fields only: no raw records, IDs or nested JSON payloads.

    Plain numeric attributes remain legitimate user dimensions/counts. Only
    option objects require an explicit label; their raw ID/value is not output.
    """
    row = present_row(incoming)
    result = {key: _label(row.get(key)) for key in BASE_COLUMNS[:9]}
    result["verification_status"] = str(row.get("verification_status") or "provisional")
    for key in ("subtotal", "unit_price"):
        result[key] = "" if row.get(key) is None else format(decimal_value(row[key]), "f")
    production = row.get("production")
    result["production_label"] = _label(production) if production is not None else ""
    result["production_status"] = str(production.get("status") or "unknown") if isinstance(production, Mapping) else "unknown"
    signature = canonical_service_context(row)
    diagnostics = row.get("service_context")
    for key in SERVICE_KEYS:
        group = signature["groups"][key]
        label = ""
        if group["status"] == "selected":
            matches = [item for entry in diagnostics if isinstance(entry, Mapping) and entry.get("key") == key
                       for item in entry.get("selected", [])
                       if isinstance(item, Mapping) and item.get("checked") is True and item.get("value") in group["values"]] if isinstance(diagnostics, list) else []
            label = _label(matches[0]) if len(matches) == 1 else LABEL_UNAVAILABLE
        result[f"service:{key}"] = label
        result[f"service_status:{key}"] = group["status"]
    result.update(_attributes(configuration(row)))
    return result


def customer_columns(keys=()):
    return list(BASE_COLUMNS) + sorted(set(keys) - set(BASE_COLUMNS))


def format_schemas(csv_columns, parquet_columns):
    return {
        "customer_csv": {"version": "1.0", "columns": list(csv_columns),
                         "purpose": "Readable product options and prices; supplier/internal IDs and raw JSON are omitted.",
                         "unlabeled_option": LABEL_UNAVAILABLE},
        "audit_parquet": {"version": "1.0", "columns": list(parquet_columns),
                          "purpose": "Full source identities, configuration, provenance and evidence for audit and analytics."},
    }


def audit_record(flat):
    """Recover source records from old Parquet exports without assuming schema."""
    def decode(raw, default):
        if not isinstance(raw, str):
            return raw if raw is not None else default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    result = {key: flat.get(key) for key in BASE_COLUMNS[:9] if key in flat}
    config = decode(flat.get("configuration_json"), None)
    if not isinstance(config, Mapping):
        config = {key[5:]: decode(value, value) for key, value in flat.items() if key.startswith("attr:")}
    result["configuration"] = config
    for key in ("production", "pricing_context", "provenance"):
        result[key] = decode(flat.get(key + "_json"), None)
    full = decode(flat.get("record_json"), None)
    if isinstance(full, Mapping):
        result.update(full)
    return result


def write_customer_csv(records, target):
    """Discover columns and write every row with bounded memory, including legacy files."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns, count = set(), 0
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=target.parent) as spool:
        for record in records:
            projected = customer_row(record)
            columns.update(projected)
            spool.write(json.dumps(projected, ensure_ascii=False) + "\n")
            count += 1
        fields = customer_columns(columns)
        spool.seek(0)
        with target.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for line in spool:
                writer.writerow(json.loads(line))
            output.flush()
            os.fsync(output.fileno())
    return fields, count
