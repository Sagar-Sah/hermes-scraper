"""Pure shipping evidence assessment; never fits a model or invents a quote."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import time


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("shipping_amount_requires_exact_decimal")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,8})?", str(value)):
        raise ValueError("shipping_amount_requires_exact_decimal")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("shipping_amount_requires_exact_decimal") from None
    if not number.is_finite() or number < 0 or number * 100 != (number * 100).to_integral_value():
        raise ValueError("shipping_amount_requires_nonnegative_decimal")
    return number


class CompanionShippingAudit:
    @staticmethod
    def assess(model_manifest, observations, runtime=None, *, now=None):
        now = time.time() if now is None else now
        if not isinstance(model_manifest, dict):
            return {"schema": "hermes-companion-shipping-audit/v1", "status": "unknown", "reason": "shipping_model_missing"}
        checks = {"model_identity": "unknown", "runtime_model": "unknown", "scope": "unknown",
                  "independent_holdouts": "unknown", "shipping_accuracy": "unknown"}
        result = {"schema": "hermes-companion-shipping-audit/v1", "status": "unknown", "checks": checks,
                  "scope": model_manifest.get("scope"), "cases": [], "automatic_model_changes_allowed": False,
                  "national_or_all_configuration_accuracy_verified": False}
        if not isinstance(observations, list) or not _sha(model_manifest.get("model_sha256")):
            result["reason"] = "shipping_model_or_observations_missing"
            return result
        checks["model_identity"] = "match"
        if isinstance(runtime, dict) and _sha(runtime.get("model_sha256")):
            checks["runtime_model"] = "match" if runtime.get("model_sha256") == model_manifest["model_sha256"] else "mismatch"
        scope = model_manifest.get("scope")
        if not isinstance(scope, dict) or not scope:
            result["reason"] = "shipping_scope_not_declared"
            return result
        checks["scope"] = "match"
        if model_manifest.get("policy") == "vendor_free":
            policy = model_manifest.get("free_shipping_evidence") or {}
            valid = (policy.get("vendor_policy_verified") is True and policy.get("retail_site_approved") is True
                     and policy.get("eligibility_verified") is True and _sha(policy.get("sha256"))
                     and isinstance(policy.get("valid_until"), (int, float)) and policy["valid_until"] >= now)
            checks["free_shipping_policy"] = "match" if valid else "unknown"
            checks["independent_holdouts"] = "not_applicable"
            checks["shipping_accuracy"] = "match" if valid else "unknown"
            invalid, mismatch = False, False
            for row in observations:
                observed = row.get("source_observed_at") if isinstance(row, dict) else None
                if (not isinstance(row, dict) or row.get("scope") != scope or not _sha(row.get("source_sha256"))
                        or not _sha(row.get("source_context_sha256")) or row["source_context_sha256"] != row.get("site_context_sha256")
                        or any(row.get(k) is not True for k in ("selection_retained", "destination_retained", "service_retained"))
                        or not isinstance(observed, (int, float)) or not 0 <= now - observed <= 31 * 86400):
                    invalid = True
                    continue
                try:
                    same = _money(row["source_total"]) == 0 and _money(row["site_total"]) == 0
                except (ValueError, KeyError):
                    invalid = True
                    continue
                if not same:
                    mismatch = True
            checks["shipping_accuracy"] = "mismatch" if mismatch else "unknown" if invalid or not observations or not valid else "match"
        else:
            declared = model_manifest.get("predeclared_holdout_ids")
            training = set(model_manifest.get("training_observation_ids") or [])
            contexts = model_manifest.get("holdout_contexts_sha256")
            frozen = model_manifest.get("frozen_at")
            freshness = model_manifest.get("max_quote_age_hours", 24 * 31)
            if (not isinstance(declared, list) or not declared or len(set(declared)) != len(declared)
                    or not isinstance(contexts, dict) or set(contexts) != set(declared)
                    or any(not _sha(value) for value in contexts.values())
                    or not isinstance(frozen, (float, int)) or not isinstance(freshness, (float, int)) or not 0 < freshness <= 31 * 24):
                result["reason"] = "independent_holdout_contract_missing"
                return result
            seen, source_hashes = set(), set()
            invalid = False
            for row in observations:
                if not isinstance(row, dict):
                    invalid = True
                    continue
                row_id, observed = row.get("id"), row.get("source_observed_at")
                bound = (row_id in declared and row_id not in training and row_id not in seen
                         and _sha(row.get("source_sha256")) and row["source_sha256"] not in source_hashes
                         and row.get("scope") == scope and row.get("model_sha256") == model_manifest["model_sha256"]
                         and row.get("source_context_sha256") == contexts.get(row_id)
                         and row.get("site_context_sha256") == contexts.get(row_id)
                         and isinstance(observed, (int, float)) and frozen <= observed <= now
                         and now - observed <= freshness * 3600
                         and row.get("selection_retained") is True and row.get("destination_retained") is True
                         and row.get("service_retained") is True)
                if not bound:
                    invalid = True
                    continue
                try:
                    source, estimate = _money(row["source_total"]), _money(row["site_total"])
                    if source <= 0:
                        raise ValueError("zero_quote_requires_verified_free_shipping_policy")
                except (ValueError, KeyError):
                    invalid = True
                    continue
                error = abs(estimate - source) / source
                result["cases"].append({"id": row_id, "source_sha256": row["source_sha256"],
                    "source_total": str(source), "site_total": str(estimate),
                    "relative_error": str(error), "within_two_percent": error <= Decimal("0.02")})
                seen.add(row_id)
                source_hashes.add(row["source_sha256"])
            complete = not invalid and seen == set(declared)
            checks["independent_holdouts"] = "match" if complete else "unknown"
            checks["shipping_accuracy"] = ("mismatch" if any(not row["within_two_percent"] for row in result["cases"])
                                            else "match" if complete else "unknown")
        result["status"] = "mismatch" if "mismatch" in checks.values() else "unknown" if "unknown" in checks.values() else "match"
        result["reason"] = "declared_scope_only_no_model_refit" if result["status"] == "match" else "shipping_evidence_or_drift_requires_review"
        return result
