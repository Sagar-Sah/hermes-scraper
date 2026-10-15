"""Local sample planning and quote checks; no remote cart or checkout side effects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .commerce import (
    configuration,
    decimal_value,
    money,
    semantic_configuration,
    semantic_production,
    semantic_pricing_context,
)
from .service_context import (
    canonical_service_context,
    requires_service_context,
    service_context_is_bound,
)
from .record_scope import is_legacy_unscoped


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _key(name: str) -> str:
    normalized = name.strip().lower().replace(" ", "_").replace("-", "_")
    return {
        "runsize": "quantity",
        "run_size": "quantity",
        "printing_process": "process",
        "production_mode": "production",
        "turnaround": "production",
        "finishes": "finish",
        "paper": "stock",
    }.get(normalized, normalized)


DEFAULT_DEPENDENCIES = (
    ("quantity", "process", "coating"),
    ("production", "finish", "quantity"),
    ("production", "coating", "quantity"),
    ("shape", "size"),
    ("material", "finish"),
    ("material", "coating"),
    ("stock", "coating"),
)


def select_verification_sample(
    rows: Iterable[Mapping[str, Any]],
    *,
    budget: int | None = None,
    dependency_groups: Iterable[Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Deterministic greedy coverage over reachable observations, not cross joins.

    Covers each option value within each production/process branch; explicit
    observed interaction groups; branch quantity extrema; source-marked defaults,
    boundaries and problematic selections. Candidate memory scales with coverage
    tokens, not the complete catalog. None means fulfill all required coverage.
    A finite budget never silently converts incomplete coverage into success.
    """
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget < 0
    ):
        raise ValueError("budget must be a nonnegative integer or None")
    groups = [
        tuple(_key(str(k)) for k in group)
        for group in (
            DEFAULT_DEPENDENCIES if dependency_groups is None else dependency_groups
        )
    ]
    representatives: dict[str, tuple[str, dict[str, Any], set[str]]] = {}
    boundary_rows: dict[
        str, dict[str, tuple[Decimal, str, dict[str, Any], set[str]]]
    ] = {}
    scanned = 0
    excluded_legacy = 0
    for incoming in rows:
        row = dict(incoming)
        if is_legacy_unscoped(row):
            excluded_legacy += 1
            continue
        options = {
            _key(str(k)): value for k, value in semantic_configuration(row).items()
        }
        if row.get("quantity") is not None:
            options["quantity"] = str(decimal_value(row["quantity"]).normalize())
        production = row.get("production")
        if "production" not in options and production is not None:
            options["production"] = semantic_production(production)
        branch = {
            key: options[key] for key in ("production", "process") if key in options
        }
        identity = (
            row.get("product_url")
            or row.get("source_product_id")
            or row.get("product_id")
            or "unspecified-product"
        )
        scope = {
            "product": str(identity),
            "branch": branch,
            "pricing_context": semantic_pricing_context(row),
        }
        tokens = {
            _json({**scope, "kind": "value", "attribute": key, "value": value})
            for key, value in options.items()
        }
        for group in groups:
            if all(key in options for key in group):
                tokens.add(
                    _json(
                        {
                            **scope,
                            "kind": "dependency",
                            "values": {key: options[key] for key in group},
                        }
                    )
                )
        for flag in ("is_default", "is_boundary", "previously_problematic"):
            if row.get(flag):
                tokens.add(
                    _json(
                        {
                            **scope,
                            "kind": flag,
                            "configuration": configuration(row),
                            "quantity": options.get("quantity"),
                        }
                    )
                )
        if not tokens:
            tokens.add(_json({**scope, "kind": "base_configuration"}))
        stable_key = hashlib.sha256(_json(row).encode()).hexdigest()
        for token in tokens:
            current = representatives.get(token)
            if current is None or stable_key < current[0]:
                representatives[token] = (stable_key, row, tokens)
        if "quantity" in options:
            quantity = decimal_value(options["quantity"])
            if quantity <= 0:
                raise ValueError("Verification quantities must be positive")
            branch_key = _json(scope)
            extremes = boundary_rows.setdefault(branch_key, {})
            candidate = (quantity, stable_key, row, tokens)
            if "min" not in extremes or (quantity, stable_key) < extremes["min"][:2]:
                extremes["min"] = candidate
            if (
                "max" not in extremes
                or quantity > extremes["max"][0]
                or (quantity == extremes["max"][0] and stable_key < extremes["max"][1])
            ):
                extremes["max"] = candidate
        scanned += 1
    for scope, extremes in boundary_rows.items():
        for boundary, (_, key, row, tokens) in extremes.items():
            token = _json(
                {**json.loads(scope), "kind": "quantity_boundary", "boundary": boundary}
            )
            tokens.add(token)
            representatives[token] = (key, row, tokens)
    candidates = {key: (row, tokens) for key, row, tokens in representatives.values()}
    required = set(representatives)
    remaining = set(required)
    selected = []
    selected_ids = []
    while remaining and (budget is None or len(selected) < budget):
        key, (row, tokens) = min(
            candidates.items(), key=lambda item: (-len(item[1][1] & remaining), item[0])
        )
        covered = tokens & remaining
        if not covered:
            break
        selected.append(row)
        selected_ids.append(key)
        remaining.difference_update(covered)
        del candidates[key]
    return {
        "status": (
            "unresolved_scope"
            if excluded_legacy and not scanned
            else "sample_selected" if not remaining else "sample_coverage_incomplete"
        ),
        "rows": selected,
        "sample_ids": selected_ids,
        "selected_count": len(selected),
        "candidate_rows_scanned": scanned,
        "excluded_legacy_unscoped": excluded_legacy,
        "scope": "current_context_rows",
        "required_coverage": len(required),
        "covered_coverage": len(required) - len(remaining),
        "coverage_complete": not remaining and not (excluded_legacy and not scanned),
        "budget": budget,
        "uncovered": [json.loads(token) for token in sorted(remaining)],
        "verification_claim": "A planned sample is not verification; passing it does not verify unsampled rows",
    }


CONTEXT_KEYS = (
    "pricing_context",
    "account_context",
    "tax_basis",
    "discount_context",
    "services",
    "production",
    "quantity_unit",
    "units_per_pack",
    "snapshot_id",
)


def compare_quote(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    """Check effective configuration, quantity/context and equivalent subtotal.

    A provider adapter must read selections back after recalculation and supply a
    product subtotal with taxes/shipping/services consistently separated. Missing
    price data is inconclusive. No LLM values or additive pricing model is used.
    """
    expected_config = configuration(expected)
    observed_config = (
        dict(observed["effective_configuration"])
        if isinstance(observed.get("effective_configuration"), Mapping)
        else configuration(observed)
    )
    result: dict[str, Any] = {
        "status": "inconclusive",
        "verification_status": "provisional",
        "matched": False,
        "expected_configuration": expected_config,
        "effective_configuration": observed_config,
        "differences": [],
        "scope": "this_configuration_only",
    }
    if any(is_legacy_unscoped(row) for row in (expected, observed)):
        return {
            **result,
            "status": "unresolved_scope",
            "reason": "Legacy service selections were not captured; a fresh scoped observation is required",
        }
    if semantic_configuration(
        {"configuration": expected_config}
    ) != semantic_configuration({"configuration": observed_config}):
        result["differences"].append(
            {
                "field": "configuration",
                "expected": expected_config,
                "observed": observed_config,
                "reason": "Automatic reset or selection mismatch",
            }
        )
    for key in CONTEXT_KEYS:
        left, right = expected.get(key), observed.get(key)
        if key == "production":
            left, right = semantic_production(left), semantic_production(right)
        elif key == "pricing_context":
            left, right = semantic_pricing_context(expected), semantic_pricing_context(
                observed
            )
        if left != right:
            result["differences"].append(
                {
                    "field": key,
                    "expected": left,
                    "observed": right,
                }
            )
    for key in ("product_url", "source_product_id"):
        if expected.get(key) is not None and expected.get(key) != observed.get(key):
            result["differences"].append(
                {
                    "field": key,
                    "expected": expected.get(key),
                    "observed": observed.get(key),
                }
            )
    missing = [
        key
        for key in ("quantity", "currency", "subtotal")
        if expected.get(key) is None or observed.get(key) is None
    ]
    if missing:
        return {
            **result,
            "status": (
                "configuration_mismatch" if result["differences"] else "inconclusive"
            ),
            "verification_status": (
                "conflicting" if result["differences"] else "provisional"
            ),
            "missing_fields": missing,
        }
    try:
        expected_quantity, observed_quantity = decimal_value(
            expected["quantity"]
        ), decimal_value(observed["quantity"])
        if expected_quantity <= 0 or observed_quantity <= 0:
            raise ValueError("Quantity must be positive")
        if expected_quantity != observed_quantity:
            result["differences"].append(
                {
                    "field": "quantity",
                    "expected": str(expected["quantity"]),
                    "observed": str(observed["quantity"]),
                }
            )
        currency = str(expected["currency"]).upper()
        if currency != str(observed["currency"]).upper():
            result["differences"].append(
                {
                    "field": "currency",
                    "expected": expected["currency"],
                    "observed": observed["currency"],
                }
            )
        expected_total, observed_total = decimal_value(
            expected["subtotal"]
        ), decimal_value(observed["subtotal"])
        if expected_total < 0 or observed_total < 0:
            raise ValueError("Subtotal must be nonnegative")
        # Compare numeric source values exactly. Formatting precision is not an
        # unrequested tolerance that could hide a fractional price mismatch.
        if expected_total != observed_total:
            result["differences"].append(
                {
                    "field": "subtotal",
                    "expected": str(expected_total),
                    "observed": str(observed_total),
                }
            )
        result.update(
            {
                "currency": currency,
                "expected_subtotal": money(expected_total, currency),
                "observed_subtotal": money(
                    observed_total, str(observed["currency"]).upper()
                ),
            }
        )
    except ValueError as exc:
        return {**result, "status": "invalid_quote", "reason": str(exc)}
    if result["differences"]:
        only_price = all(d["field"] == "subtotal" for d in result["differences"])
        return {
            **result,
            "status": "price_mismatch" if only_price else "configuration_mismatch",
            "verification_status": "conflicting",
            "next_action": "refresh_evidence_and_retry_once_in_isolated_context",
        }
    if any(
        requires_service_context(row)
        and not service_context_is_bound(canonical_service_context(row))
        for row in (expected, observed)
    ):
        return {
            **result,
            "status": "inconclusive",
            "reason": "Service selections are unknown or ambiguous",
            "missing_fields": ["bound_service_context"],
        }
    return {
        **result,
        "status": "matched",
        "verification_status": "quote_verified",
        "matched": True,
        "evidence_ids": observed.get("evidence_ids", []),
    }


def verification_decision(
    report: Mapping[str, Any], *, retried: bool = False, shared_dependency: bool = False
) -> dict[str, Any]:
    """Publication/fallback decision after a local check, without remote actions."""
    if report.get("matched"):
        return {
            "action": "verify_exact_row",
            "unsampled_rows": "provisional",
            "invalidate_scope": None,
        }
    if report.get("status") in ("price_mismatch", "configuration_mismatch"):
        return {
            "action": (
                "per_configuration_quote_fallback"
                if retried
                else "refresh_and_retry_once"
            ),
            "invalidate_scope": (
                "all_dependency_consumers" if shared_dependency else "product_snapshot"
            ),
            "dependent_rows": "conflicting",
            "preserve_history": True,
        }
    return {
        "action": "collect_missing_evidence",
        "unsampled_rows": "provisional",
        "invalidate_scope": None,
    }


def freshness_status(
    observed_at: datetime | str,
    *,
    kind: str = "table",
    now: datetime | None = None,
    context_changed: bool = False,
    dependency_changed: bool = False,
) -> dict[str, Any]:
    """Operational freshness defaults, not a supplier price guarantee."""
    if kind not in ("table", "quote"):
        raise ValueError("kind must be table or quote")
    observed = (
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if isinstance(observed_at, str)
        else observed_at
    )
    current = now or datetime.now(timezone.utc)
    if observed.tzinfo is None or current.tzinfo is None:
        raise ValueError("Freshness timestamps must be timezone aware")
    if observed > current:
        return {"status": "invalid_timestamp", "reason": "Observation is in the future"}
    ttl = timedelta(hours=24) if kind == "table" else timedelta(minutes=30)
    expires = observed + ttl
    stale = context_changed or dependency_changed or current >= expires
    return {
        "status": "stale" if stale else "fresh",
        "expires_at": expires.isoformat(),
        "ttl_seconds": int(ttl.total_seconds()),
        "reason": (
            "context_changed"
            if context_changed
            else (
                "dependency_changed"
                if dependency_changed
                else "ttl_expired" if stale else None
            )
        ),
    }
