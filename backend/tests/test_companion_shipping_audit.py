import copy

from crawler.companion_shipping_audit import CompanionShippingAudit as Audit


def fixture():
    scope = {"product_id": "6669", "material": "pvc_3mm", "service": "Ground", "destinations": ["AZ", "FL", "NY"]}
    manifest = {"policy": "estimated", "model_sha256": "a" * 64, "scope": scope,
                "frozen_at": 100, "training_observation_ids": ["training"], "predeclared_holdout_ids": ["az", "fl", "ny"],
                "holdout_contexts_sha256": {key: char * 64 for key, char in [("az", "e"), ("fl", "f"), ("ny", "0")]}}
    rows = [{"id": key, "scope": scope, "source_sha256": char * 64, "model_sha256": "a" * 64,
             "source_observed_at": 200, "selection_retained": True, "destination_retained": True,
             "service_retained": True, "source_total": "100.00", "site_total": "102.00"}
            for key, char in [("az", "b"), ("fl", "c"), ("ny", "d")]]
    for row in rows:
        row["source_context_sha256"] = row["site_context_sha256"] = manifest["holdout_contexts_sha256"][row["id"]]
    return manifest, rows


def test_independent_holdout_gate_exact_two_percent_no_national_claim():
    manifest, rows = fixture()
    result = Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)
    assert result["status"] == "match"
    assert all(case["within_two_percent"] for case in result["cases"])
    assert result["national_or_all_configuration_accuracy_verified"] is False
    rows[-1]["site_total"] = "102.01"
    assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)["status"] == "mismatch"


def test_training_duplicate_stale_wrong_scope_missing_runtime_never_pass():
    for change in (lambda m, r: r.pop(), lambda m, r: r[0].update(id="training"),
                   lambda m, r: r[0].update(source_sha256=r[1]["source_sha256"]),
                   lambda m, r: r[0].update(scope={"service": "Air"}),
                   lambda m, r: r[0].update(selection_retained=False),
                   lambda m, r: r[0].update(site_context_sha256="8" * 64),
                   lambda m, r: m.update(frozen_at=250)):
        manifest, rows = fixture()
        change(manifest, rows)
        assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)["status"] == "unknown"
    manifest, rows = fixture()
    assert Audit.assess(manifest, rows, now=300)["status"] == "unknown"
    assert Audit.assess(manifest, rows, {"model_sha256": "e" * 64}, now=300)["status"] == "mismatch"
    assert Audit.assess(manifest, rows, {}, now=300)["status"] == "unknown"
    assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=40 * 86400)["status"] == "unknown"


def test_free_ground_requires_verified_policy_and_selection_eligibility():
    manifest = {"policy": "vendor_free", "scope": {"service": "Ground", "destination_scope": "us48"}, "model_sha256": "a" * 64,
                "free_shipping_evidence": {"vendor_policy_verified": True, "retail_site_approved": True,
                    "eligibility_verified": True, "sha256": "b" * 64, "valid_until": 500}}
    rows = [{"scope": manifest["scope"], "source_sha256": "c" * 64, "source_total": "0.00", "site_total": "0.00",
             "source_context_sha256": "d" * 64, "site_context_sha256": "d" * 64, "source_observed_at": 200,
             "selection_retained": True, "destination_retained": True, "service_retained": True}]
    assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)["status"] == "match"
    manifest["free_shipping_evidence"]["eligibility_verified"] = False
    assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)["status"] == "unknown"
    manifest["free_shipping_evidence"]["eligibility_verified"] = True
    rows[0]["site_total"] = "5.00"
    assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)["status"] == "mismatch"
    rows.append({"scope": {}})
    assert Audit.assess(manifest, rows, {"model_sha256": "a" * 64}, now=300)["status"] == "mismatch"
