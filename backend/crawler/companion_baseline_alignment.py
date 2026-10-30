"""Deterministic, read-only alignment planning for a pinned environment capture.

This module compares observations; it never installs a package, copies a file,
changes a setting, approves a baseline, or turns matching version labels into
runtime evidence. Only bounded, non-secret inventory fields leave the planner.
Unreleased staging code and one-time release helpers need explicit review.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from urllib.parse import urlsplit

from .companion_environments import digest


SHA = re.compile(r"^[a-f0-9]{64}$")
NAME = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
CSV = re.compile(r"^[1-9][0-9]{0,11}-printit\.csv$")
HELPER = re.compile(r"(?:^|[-_])(?:drafts?|release|recovery|import(?:er)?|migration|qa|backup|seed|creator)(?:[-_]|$)")
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PLUGINS = 5000
MAX_FILES = 10000
MAX_RECORDS = 500
SAFE_ISOLATION = {
    "search_indexing": {"disabled"},
    "outbound_mail": {"disabled"},
    "payments": {"disabled", "sandbox"},
    "scheduled_actions": {"disabled", "isolated"},
    "outbound_http": {"disabled", "approved_rate_hosts", "rates_only"},
}


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _origin(value):
    if not isinstance(value, str) or len(value) > 300:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and parsed.path == "" and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _path(value, *, plugin=False):
    if not isinstance(value, str) or len(value) > 300:
        return False
    parts = value.split("/")
    return (all(NAME.fullmatch(part) and part not in {".", ".."} for part in parts)
            and (not plugin or value.endswith(".php")))


def _version(value):
    # Plugin version headers are untrusted display data, never commands/HTML.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._+ -]{1,80}", value) else None


def _fingerprint(value, *, tree=False):
    if isinstance(value, str):
        return {"status": "observed", "sha256": value} if _sha(value) and not tree else {"status": "unknown"}
    if (not isinstance(value, dict) or value.get("status") not in {"match", "verified", "passed"}
            or not _sha(value.get("sha256")) or tree and value.get("complete") is not True):
        return {"status": "unknown"}
    result = {"status": "observed", "sha256": value["sha256"]}
    if tree:
        for key in ("files", "bytes"):
            number = value.get(key)
            if type(number) is not int or number < 0 or key == "files" and number > MAX_FILES:
                return {"status": "unknown"}
            result[key] = number
        result["complete"] = True
    return result


def _metadata(value, *, plugin=False):
    if value is None:
        return {"present": False}
    if not isinstance(value, dict):
        return {"present": True, "status": "unknown"}
    result = {"present": True, "version": _version(value.get("version")),
              "code": _fingerprint(value.get("code"), tree=True)}
    if plugin:
        result["active"] = value.get("active") if type(value.get("active")) is bool else None
    return result


def _code_status(left, right):
    if left.get("status") != "observed" or right.get("status") != "observed":
        return "unknown"
    return "match" if left["sha256"] == right["sha256"] else "mismatch"


def _capture(value):
    if not isinstance(value, dict):
        raise ValueError("baseline_alignment_capture_invalid")
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise ValueError("baseline_alignment_capture_invalid") from None
    if len(raw) > MAX_CAPTURE_BYTES:
        raise ValueError("baseline_alignment_capture_too_large")
    value = deepcopy(value)
    if value.get("schema") != "hermes-staging-baseline/v1":
        raise ValueError("baseline_alignment_capture_invalid")
    claimed = value.pop("manifest_sha256", None)
    if not _sha(claimed) or digest(value) != claimed:
        raise ValueError("baseline_alignment_capture_hash_changed")
    identifier = value.pop("id", None)
    if not _sha(identifier) or digest(value) != identifier:
        raise ValueError("baseline_alignment_capture_identity_changed")
    for environment in ("production", "staging"):
        observed = value.get(environment)
        if (not isinstance(observed, dict) or observed.get("schema") != "hermes-companion-environment-snapshot/v1"
                or observed.get("environment") != environment or not _origin(observed.get("site_origin"))):
            raise ValueError("baseline_alignment_environment_identity_invalid")
    if value["production"]["site_origin"] == value["staging"]["site_origin"]:
        raise ValueError("baseline_alignment_distinct_environments_required")
    value["id"], value["manifest_sha256"] = identifier, claimed
    return value


class _Planner:
    def __init__(self, capture):
        self.capture = capture
        self.records, self.unknowns, self.counts = [], [], {"match": 0, "mismatch": 0, "unknown": 0}
        self.review_count = self.preserve_count = self.helper_count = 0

    def unknown(self, domain, reason, *, environment=None):
        value = {"domain": domain, "reason": reason}
        if environment:
            value["environment"] = environment
        if value not in self.unknowns:
            self.unknowns.append(value)

    def record(self, domain, key, left, right, *, status, reason, priority, action,
               preserve=False, helper=False, field_review=False, proof_scope="captured_inventory_only"):
        self.counts[status] += 1
        if status == "match":
            return
        self.review_count += 1
        self.preserve_count += int(preserve)
        self.helper_count += int(helper)
        if len(self.records) >= MAX_RECORDS:
            self.unknown("alignment_plan", "record_limit_reached_complete_review_required")
            return
        self.records.append({
            "id": digest({"domain": domain, "key": key})[:24], "domain": domain, "key": key,
            "component": domain, "name": key,
            "production": left, "staging": right, "status": status, "reason": reason,
            "priority": priority, "proposed_action": action,
            "preserve_staging_before_alignment": preserve,
            "copy_recommendation": "omit_by_default" if helper else "review_only",
            "review_required": True, "field_level_comparison_required": field_review,
            "proof_scope": proof_scope,
        })

    def inventories(self, domain, *, plugins=False):
        values = {}
        for environment in ("production", "staging"):
            raw = self.capture[environment].get(domain)
            if raw == []:
                raw = {}
            maximum = MAX_PLUGINS if plugins else 50
            if (not isinstance(raw, dict) or len(raw) > maximum
                    or any(not _path(key, plugin=plugins) or not isinstance(item, dict) for key, item in raw.items())):
                self.unknown(domain, "inventory_missing_or_invalid", environment=environment)
                return
            values[environment] = raw
        lefts, rights = values["production"], values["staging"]
        for key in sorted(lefts.keys() | rights.keys()):
            left, right = _metadata(lefts.get(key), plugin=plugins), _metadata(rights.get(key), plugin=plugins)
            code = _code_status(left.get("code", {}), right.get("code", {}))
            helper = plugins and HELPER.search(key.split("/")[0]) is not None
            if not left["present"]:
                status, reason, priority = "mismatch", "staging_only_inventory_preserve", "preserve_unreleased_staging"
                action, preserve = "review_staging_only_work_before_any_removal_or_copy", True
            elif not right["present"]:
                status, reason, priority = "mismatch", "production_only_inventory", "review_selective_alignment"
                action, preserve = "review_selective_install_after_backups", False
            elif code == "mismatch" and left.get("version") == right.get("version") and left.get("version") is not None:
                status, reason, priority = "mismatch", "same_version_different_code", "preserve_code_drift"
                action, preserve = "preserve_both_code_trees_and_review_exact_diff", True
            elif code == "mismatch":
                status, reason, priority = "mismatch", "version_or_code_drift", "review_selective_alignment"
                action, preserve = "preserve_staging_then_review_selective_code_update", True
            elif left.get("version") != right.get("version") or plugins and left.get("active") != right.get("active"):
                status, reason, priority = "mismatch", "version_or_activation_differs", "review_metadata_and_runtime"
                action, preserve = "review_activation_and_version_without_assuming_runtime_parity", True
            elif code == "match" and left.get("version") is not None and (not plugins or left.get("active") is not None):
                status, reason, priority, action, preserve = "match", "version_and_code_equal", "observed", "none", False
            else:
                status, reason, priority = "unknown", "code_or_metadata_not_observed", "collect_missing_evidence"
                action, preserve = "capture_loaded_code_and_metadata_before_alignment", True
            if helper and status != "match":
                action = "review_one_time_helper_scope_without_copying_by_default"
            self.record(domain, key, left, right, status=status, reason=reason, priority=priority,
                        action=action, preserve=preserve, helper=helper)

    def fingerprints(self, domain, *, tree=False, preserve=True):
        left = _fingerprint(self.capture["production"].get(domain), tree=tree)
        right = _fingerprint(self.capture["staging"].get(domain), tree=tree)
        status = _code_status(left, right)
        self.record(domain, domain, left, right, status=status,
                    reason="captured_fingerprint_differs" if status == "mismatch" else "fingerprint_not_observed",
                    priority="preserve_custom_work" if status == "mismatch" else "collect_missing_evidence",
                    action="preserve_both_and_review_selective_merge" if status == "mismatch" else "capture_missing_fingerprint",
                    preserve=preserve, field_review=status == "mismatch")

    def calculators(self):
        inventories = {}
        for environment in ("production", "staging"):
            value = self.capture[environment].get("custom_calculator_files")
            files = value.get("files") if isinstance(value, dict) else None
            if files == []:
                files = {}
            if (not isinstance(value, dict) or value.get("status") != "match" or not isinstance(files, dict)
                    or len(files) > MAX_FILES or any(not isinstance(key, str) or not CSV.fullmatch(key)
                                                   or not _sha(sha) for key, sha in files.items())):
                self.unknown("custom_calculator_files", "calculator_file_inventory_incomplete", environment=environment)
                return
            inventories[environment] = files
        lefts, rights = inventories["production"], inventories["staging"]
        for key in sorted(lefts.keys() | rights.keys()):
            left = {"present": key in lefts, "sha256": lefts.get(key)}
            right = {"present": key in rights, "sha256": rights.get(key)}
            status = "match" if left == right else "mismatch"
            reason = ("staging_only_calculator_preserve" if key not in lefts else
                      "production_only_calculator" if key not in rights else "calculator_file_changed")
            self.record("custom_calculator_files", key, left, right, status=status, reason=reason,
                        priority="preserve_product_data", action="compare_exact_rows_and_revisions_after_backups",
                        preserve=right["present"], field_review=True, proof_scope="file_identity_not_row_correctness")

    def runtime(self):
        for key in ("retail", "shipping"):
            observations = []
            for environment in ("production", "staging"):
                value = self.capture[environment].get("runtime_fingerprints")
                observations.append(_fingerprint(value.get(key) if isinstance(value, dict) else None))
            left, right = observations
            status = _code_status(left, right)
            self.record("runtime_fingerprints", key, left, right, status=status,
                        reason="runtime_fingerprint_changed" if status == "mismatch" else "runtime_fingerprint_unknown",
                        priority="verify_effective_runtime", action="register_actual_runtime_and_run_storefront_fixtures",
                        preserve=status == "mismatch", proof_scope="fingerprint_identity_not_functional_parity")
        self.unknown("runtime_parity", "matching_versions_or_fingerprints_do_not_prove_customer_flow")
        self.unknown("settings", "effective_settings_not_fully_inventoried_in_environment_capture")
        self.unknown("documents", "per_product_document_inventory_requires_product_snapshots")

    def isolation(self):
        raw = self.capture["staging"].get("staging_isolation")
        observations = []
        for key, allowed in SAFE_ISOLATION.items():
            value = raw.get(key) if isinstance(raw, dict) else None
            valid = value in allowed if isinstance(value, str) else False
            # Retain only the finite connector state vocabulary, never raw data.
            safe_value = value if isinstance(value, str) and value in set.union(*SAFE_ISOLATION.values(), {"enabled", "unknown"}) else "unknown"
            observations.append({"control": key, "observed_state": safe_value,
                                 "status": "declared_safe" if valid else "unknown" if safe_value == "unknown" else "unsafe",
                                 "requires_functional_receipt": True,
                                 "proposed_action": "verify_existing_protection" if valid else "restore_or_verify_staging_protection"})
            if not valid:
                self.unknown("staging_isolation", key + "_not_verified_safe", environment="staging")
        self.unknown("staging_isolation", "upstream_preview_and_direct_sdk_isolation_require_independent_evidence", environment="staging")
        return observations

    def plan(self):
        self.inventories("plugins", plugins=True)
        self.inventories("themes")
        self.fingerprints("mu_plugins", tree=True)
        self.fingerprints("custom_css")
        self.fingerprints("snippets")
        self.calculators()
        self.runtime()
        isolation = self.isolation()
        result = {
            "schema": "hermes-companion-baseline-alignment/v1",
            "capture_id": self.capture["id"], "capture_manifest_sha256": self.capture["manifest_sha256"],
            "production_origin": self.capture["production"]["site_origin"],
            "staging_origin": self.capture["staging"]["site_origin"],
            "status": "review_required", "ready": False, "read_only": True,
            "automatic_actions": [], "records": self.records,
            "unknowns": self.unknowns, "staging_isolation": isolation,
            "summary": {**self.counts, "review_records": self.review_count, "shown_records": len(self.records),
                        "omitted_records": self.review_count - len(self.records),
                        "unknown_domains": len({item["domain"] for item in self.unknowns}), "unknown_checks": len(self.unknowns),
                        "preserve_staging_records": self.preserve_count,
                        "helpers_omitted_by_default": self.helper_count},
            "records_truncated": self.review_count > len(self.records), "record_limit": MAX_RECORDS,
            "required_order": ["verify_full_backups_both_environments", "preserve_unreleased_staging_work",
                               "review_selective_file_and_setting_alignment", "refresh_capture_after_changes",
                               "verify_actual_runtime_isolation_and_storefront_receipts", "human_baseline_review"],
            "prohibitions": ["no_database_clone_or_replacement", "no_automatic_install_copy_or_removal",
                             "no_credentials_or_customer_data_in_plan", "no_production_promotion",
                             "no_green_from_versions_or_unknown_evidence"],
        }
        result["plan_sha256"] = digest(result)
        return result


def build_alignment_plan(capture):
    """Return a deterministic proposal bound to a verified, immutable capture.

    The caller should load the capture through StagingBaseline._capture first.
    Hash checks are repeated here so a detached or edited capture cannot be
    treated as evidence. Plans intentionally never satisfy baseline approval.
    """
    return _Planner(_capture(capture)).plan()


# Descriptive alias for offline callers; the server uses build_alignment_plan.
plan_baseline_alignment = build_alignment_plan
