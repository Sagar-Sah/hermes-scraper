"""Alignment proposals derive from bounded observations, never permissions."""
from copy import deepcopy
import json

import pytest

from crawler.companion_baseline_alignment import MAX_RECORDS, build_alignment_plan
from crawler.companion_environments import digest


def tree(sha="a" * 64):
    return {"status": "match", "complete": True, "sha256": sha, "files": 3, "bytes": 1234}


def plugin(version="1.0", sha="a" * 64, active=True):
    return {"version": version, "active": active, "code": tree(sha)}


def environment(name):
    return {"schema": "hermes-companion-environment-snapshot/v1", "environment": name,
            "site_origin": "https://" + ("shop.example" if name == "production" else "staging.example"),
            "plugins": {"catalog/catalog.php": plugin()},
            "themes": {"theme": {"version": "2.0", "code": tree()}},
            "mu_plugins": tree(), "custom_css": {"status": "match", "sha256": "a" * 64},
            "snippets": {"status": "match", "count": 2, "sha256": "a" * 64},
            "custom_calculator_files": {"status": "match", "files": {"42-printit.csv": "a" * 64}},
            "runtime_fingerprints": {"retail": "b" * 64, "shipping": "c" * 64},
            "staging_isolation": {"search_indexing": "disabled", "outbound_mail": "disabled",
                                  "payments": "sandbox", "scheduled_actions": "isolated", "outbound_http": "rates_only"}}


def seal(value):
    value = deepcopy(value)
    value.pop("id", None)
    value.pop("manifest_sha256", None)
    value["id"] = digest(value)
    value["manifest_sha256"] = digest(value)
    return value


def capture():
    return seal({"schema": "hermes-staging-baseline/v1", "captured_at": 123456789,
                 "production": environment("production"), "staging": environment("staging"),
                 "backups_verified": False, "runtime_parity_verified": False,
                 "isolation_verified": False, "ready": False})


def find(result, component, name):
    return next(item for item in result["records"] if item["component"] == component and item["name"] == name)


def test_equal_versions_and_code_never_claim_baseline_readiness():
    value = capture()
    # Even caller-supplied attestation fields do not authorize readiness.
    value.update(ready=True, backups_verified=True, runtime_parity_verified=True, isolation_verified=True)
    result = build_alignment_plan(seal(value))
    assert result["status"] == "review_required" and result["ready"] is False
    assert result["automatic_actions"] == [] and result["read_only"] is True
    assert result["records"] == [] and result["summary"]["match"] == 8
    assert {item["domain"] for item in result["unknowns"]} >= {"runtime_parity", "settings", "documents", "staging_isolation"}
    assert all(item["status"] == "declared_safe" and item["requires_functional_receipt"] for item in result["staging_isolation"])


def test_same_version_different_code_preserves_both_and_requires_review():
    value = capture()
    value["staging"]["plugins"]["catalog/catalog.php"]["code"] = tree("d" * 64)
    row = find(build_alignment_plan(seal(value)), "plugins", "catalog/catalog.php")
    assert row["status"] == "mismatch" and row["reason"] == "same_version_different_code"
    assert row["priority"] == "preserve_code_drift" and row["preserve_staging_before_alignment"]
    assert row["production"]["version"] == row["staging"]["version"] == "1.0"
    assert row["proposed_action"] == "preserve_both_code_trees_and_review_exact_diff"
    assert row["review_required"] is True


def test_version_change_with_equal_code_is_a_metadata_difference_not_runtime_pass():
    value = capture()
    value["staging"]["plugins"]["catalog/catalog.php"]["version"] = "0.9"
    row = find(build_alignment_plan(seal(value)), "plugins", "catalog/catalog.php")
    assert row["reason"] == "version_or_activation_differs" and row["status"] == "mismatch"
    assert row["production"]["code"]["sha256"] == row["staging"]["code"]["sha256"]


def test_missing_code_does_not_match_even_when_versions_match():
    value = capture()
    del value["staging"]["plugins"]["catalog/catalog.php"]["code"]
    row = find(build_alignment_plan(seal(value)), "plugins", "catalog/catalog.php")
    assert row["status"] == "unknown" and row["reason"] == "code_or_metadata_not_observed"


@pytest.mark.parametrize("field,bad", [("complete", False), ("files", True), ("bytes", -1), ("files", 10001), ("sha256", "not-a-sha")])
def test_partial_or_invalid_code_tree_never_counts_as_observed(field, bad):
    value = capture()
    value["staging"]["plugins"]["catalog/catalog.php"]["code"][field] = bad
    row = find(build_alignment_plan(seal(value)), "plugins", "catalog/catalog.php")
    assert row["status"] == "unknown" and row["staging"]["code"]["status"] == "unknown"


def test_staging_only_plugin_is_preserved_not_removed():
    value = capture()
    value["staging"]["plugins"]["editor/editor.php"] = plugin("0.3-qa")
    row = find(build_alignment_plan(seal(value)), "plugins", "editor/editor.php")
    assert row["production"] == {"present": False}
    assert row["reason"] == "staging_only_inventory_preserve" and row["preserve_staging_before_alignment"]
    assert row["proposed_action"] == "review_staging_only_work_before_any_removal_or_copy"


@pytest.mark.parametrize("folder", ["store-release", "store-draft-recovery", "importer", "store-qa-tool", "store-backup"])
def test_release_and_import_helpers_are_omitted_by_default(folder):
    value = capture()
    key = folder + "/main.php"
    value["production"]["plugins"][key] = plugin()
    row = find(build_alignment_plan(seal(value)), "plugins", key)
    assert row["copy_recommendation"] == "omit_by_default"
    assert row["proposed_action"] == "review_one_time_helper_scope_without_copying_by_default"


def test_missing_normal_plugin_only_proposes_reviewed_install_after_backups():
    value = capture()
    value["production"]["plugins"]["forms/forms.php"] = plugin()
    row = find(build_alignment_plan(seal(value)), "plugins", "forms/forms.php")
    assert row["copy_recommendation"] == "review_only" and row["review_required"]
    assert row["proposed_action"] == "review_selective_install_after_backups"


def test_activation_difference_stays_visible_even_with_exact_code():
    value = capture()
    value["staging"]["plugins"]["catalog/catalog.php"]["active"] = False
    row = find(build_alignment_plan(seal(value)), "plugins", "catalog/catalog.php")
    assert row["production"]["active"] is True and row["staging"]["active"] is False
    assert row["status"] == "mismatch"


def test_inactive_plugins_without_code_remain_unknown():
    value = capture()
    for name in ("production", "staging"):
        value[name]["plugins"]["inactive/plugin.php"] = {"version": "1.0", "active": False}
    row = find(build_alignment_plan(seal(value)), "plugins", "inactive/plugin.php")
    assert row["status"] == "unknown"


def test_real_wordpress_plugin_folders_with_spaces_remain_comparable():
    value = capture()
    key = "Merchant Custom Pricing/Merchant Custom Pricing.php"
    value["production"]["plugins"][key] = plugin("1.1")
    value["staging"]["plugins"][key] = plugin("1.0", "b" * 64)
    row = find(build_alignment_plan(seal(value)), "plugins", key)
    assert row["status"] == "mismatch" and row["reason"] == "version_or_code_drift"


def test_theme_same_version_code_change_and_custom_css_need_preservation():
    value = capture()
    value["staging"]["themes"]["theme"]["code"] = tree("d" * 64)
    value["staging"]["custom_css"]["sha256"] = "e" * 64
    result = build_alignment_plan(seal(value))
    assert find(result, "themes", "theme")["reason"] == "same_version_different_code"
    row = find(result, "custom_css", "custom_css")
    assert row["field_level_comparison_required"] and row["preserve_staging_before_alignment"]


def test_mu_tree_and_snippet_changes_require_exact_review_without_copying_content():
    value = capture()
    value["staging"]["mu_plugins"] = tree("d" * 64)
    value["staging"]["snippets"].update(sha256="e" * 64, secret="must-not-export")
    result = build_alignment_plan(seal(value))
    assert find(result, "mu_plugins", "mu_plugins")["status"] == "mismatch"
    assert find(result, "snippets", "snippets")["status"] == "mismatch"
    assert "must-not-export" not in json.dumps(result)


def test_calculator_file_diffs_do_not_invent_row_level_corrections():
    value = capture()
    value["production"]["custom_calculator_files"]["files"]["43-printit.csv"] = "b" * 64
    value["staging"]["custom_calculator_files"]["files"] = {"42-printit.csv": "c" * 64, "44-printit.csv": "d" * 64}
    result = build_alignment_plan(seal(value))
    changed = find(result, "custom_calculator_files", "42-printit.csv")
    assert changed["reason"] == "calculator_file_changed" and changed["field_level_comparison_required"]
    assert changed["proof_scope"] == "file_identity_not_row_correctness"
    assert find(result, "custom_calculator_files", "43-printit.csv")["reason"] == "production_only_calculator"
    assert find(result, "custom_calculator_files", "44-printit.csv")["preserve_staging_before_alignment"]


@pytest.mark.parametrize("files", [{"../42-printit.csv": "a" * 64}, {"42-printit.csv": "wrong"}, None])
def test_invalid_calculator_inventory_cannot_be_partially_compared(files):
    value = capture()
    value["staging"]["custom_calculator_files"]["files"] = files
    result = build_alignment_plan(seal(value))
    assert not any(row["domain"] == "custom_calculator_files" for row in result["records"])
    assert {"domain": "custom_calculator_files", "reason": "calculator_file_inventory_incomplete", "environment": "staging"} in result["unknowns"]


def test_php_empty_map_representations_are_supported():
    value = capture()
    for name in ("production", "staging"):
        value[name]["plugins"] = []
        value[name]["themes"] = []
        value[name]["custom_calculator_files"]["files"] = []
    result = build_alignment_plan(seal(value))
    assert not any(item["reason"] in {"inventory_missing_or_invalid", "calculator_file_inventory_incomplete"} for item in result["unknowns"])


def test_runtime_unknown_and_changes_are_reported_but_no_receipt_is_fabricated():
    value = capture()
    value["staging"]["runtime_fingerprints"] = {"retail": "unknown", "shipping": "d" * 64}
    result = build_alignment_plan(seal(value))
    assert find(result, "runtime_fingerprints", "retail")["status"] == "unknown"
    assert find(result, "runtime_fingerprints", "shipping")["status"] == "mismatch"
    assert "receipt_sha256" not in json.dumps(result) and result["ready"] is False


def test_unsafe_staging_states_do_not_get_copied_from_production():
    value = capture()
    value["staging"]["staging_isolation"].update(search_indexing="enabled", outbound_http="unknown", outbound_mail="secret@example.invalid")
    result = build_alignment_plan(seal(value))
    states = {item["control"]: item for item in result["staging_isolation"]}
    assert states["search_indexing"]["status"] == "unsafe"
    assert states["outbound_http"]["status"] == states["outbound_mail"]["status"] == "unknown"
    assert "secret@example.invalid" not in json.dumps(result)
    assert not any(row["domain"] == "staging_isolation" for row in result["records"])


def test_credentials_customer_data_and_raw_extra_fields_are_not_exported():
    value = capture()
    value["production"].update(token="private-token", customers=[{"email": "owner@example.invalid"}])
    value["staging"]["plugins"]["catalog/catalog.php"]["api_key"] = "private-key"
    value["staging"]["runtime_fingerprints"]["retail"] = {"status": "unknown", "secret": "private-service-key"}
    raw = json.dumps(build_alignment_plan(seal(value)))
    assert not any(secret in raw for secret in ["private-token", "owner@example.invalid", "private-key", "private-service-key"])


def test_capture_is_not_mutated_and_plan_is_deterministic_and_hashed():
    value = capture()
    original = deepcopy(value)
    result = build_alignment_plan(value)
    assert value == original and result == build_alignment_plan(value)
    claimed = result.pop("plan_sha256")
    assert digest(result) == claimed


@pytest.mark.parametrize("path", ["../bad.php", "/abs.php", "folder//bad.php", "folder/../../bad.php", "folder/main.php?secret=value"])
def test_invalid_plugin_paths_make_inventory_unknown_without_echoing_them(path):
    value = capture()
    value["staging"]["plugins"][path] = plugin()
    result = build_alignment_plan(seal(value))
    assert any(item["domain"] == "plugins" and item["reason"] == "inventory_missing_or_invalid" for item in result["unknowns"])
    assert not any(row["domain"] == "plugins" for row in result["records"])


def test_record_limit_is_explicit_and_does_not_drop_overall_counts():
    value = capture()
    value["production"]["plugins"] = {f"plugin-{i}/main.php": plugin() for i in range(MAX_RECORDS + 2)}
    value["staging"]["plugins"] = {}
    result = build_alignment_plan(seal(value))
    assert len(result["records"]) == MAX_RECORDS and result["records_truncated"]
    assert result["summary"]["review_records"] == MAX_RECORDS + 2
    assert result["summary"]["omitted_records"] == 2
    assert {"domain": "alignment_plan", "reason": "record_limit_reached_complete_review_required"} in result["unknowns"]


def test_capture_manifest_and_identity_are_both_validated():
    value = capture()
    value["production"]["plugins"]["catalog/catalog.php"]["version"] = "edited"
    with pytest.raises(ValueError, match="capture_hash_changed"):
        build_alignment_plan(value)
    value["manifest_sha256"] = digest({k: v for k, v in value.items() if k != "manifest_sha256"})
    with pytest.raises(ValueError, match="capture_identity_changed"):
        build_alignment_plan(value)


@pytest.mark.parametrize("origin", ["http://staging.example", "https://name:password@staging.example", "https://staging.example/?key=secret", "https://staging.example/subdir", "https://staging.example:444", "https://shop.example"])
def test_environment_origin_identity_must_be_distinct_unambiguous_https(origin):
    value = capture()
    value["staging"]["site_origin"] = origin
    with pytest.raises(ValueError, match="environment_identity_invalid|distinct_environments_required"):
        build_alignment_plan(seal(value))


def test_missing_or_invalid_capture_is_rejected_without_side_effects():
    for value in [None, [], {}, {"schema": "wrong"}, {"x": float("nan")}]:
        with pytest.raises(ValueError):
            build_alignment_plan(value)
