from datetime import datetime, timedelta, timezone
from copy import deepcopy

from crawler.verification import (
    compare_quote,
    freshness_status,
    select_verification_sample,
    verification_decision,
)


def branch_fixture():
    rows = []
    for production in ("regular", "rush"):
        quantities = (
            (50, 500, 2500, 10000) if production == "regular" else (50, 500, 2500)
        )
        finishes = ("", "raised UV") if production == "regular" else ("",)
        for qty in quantities:
            for finish in finishes:
                process = "digital" if qty < 500 else "offset"
                coatings = ("",) if process == "digital" else ("", "UV")
                for coating in coatings:
                    rows.append(
                        {
                            "product_url": "https://fixture/flyers",
                            "quantity": qty,
                            "currency": "USD",
                            "subtotal": "19.99",
                            "configuration": {
                                "production": production,
                                "process": process,
                                "finish": finish,
                                "coating": coating,
                                "size": "3 x 5",
                            },
                        }
                    )
    return rows


def test_deterministic_branch_sampling_covers_observed_interactions_only():
    rows = branch_fixture()
    report = select_verification_sample(rows)
    reversed_report = select_verification_sample(reversed(rows))
    assert report["coverage_complete"]
    assert report["uncovered"] == []
    assert report["sample_ids"] == reversed_report["sample_ids"]
    observed = {
        (r["quantity"], r["configuration"]["process"], r["configuration"]["coating"])
        for r in rows
    }
    sampled = {
        (r["quantity"], r["configuration"]["process"], r["configuration"]["coating"])
        for r in report["rows"]
    }
    assert sampled == observed
    assert all(
        r["configuration"]["finish"] == "" and r["quantity"] <= 2500
        for r in report["rows"]
        if r["configuration"]["production"] == "rush"
    )


def test_sampling_budget_reports_incomplete_without_discarding_requirements():
    report = select_verification_sample(branch_fixture(), budget=1)
    assert report["status"] == "sample_coverage_incomplete"
    assert report["selected_count"] == 1
    assert report["required_coverage"] > report["covered_coverage"]
    assert report["uncovered"]


def test_explicit_dependency_and_problematic_configuration_are_included():
    rows = [
        {
            "product_id": "label",
            "quantity": 100,
            "configuration": {"shape": shape, "size": size, "material": material},
            "previously_problematic": shape == "circle",
        }
        for shape, size in (("circle", "2"), ("rectangle", "2 x 4"))
        for material in ("vinyl", "paper")
    ]
    report = select_verification_sample(
        rows, dependency_groups=[("shape", "size", "material")]
    )
    assert report["coverage_complete"] and report["selected_count"] == 4


def test_quote_verification_preserves_valid_empty_values():
    expected = {
        "product_url": "https://fixture/cards",
        "configuration": {"coating": "", "corners": ""},
        "quantity": 500,
        "currency": "USD",
        "subtotal": "19.99",
        "tax_basis": "exclusive",
    }
    result = compare_quote(
        expected,
        {
            **expected,
            "quantity": "500",
            "subtotal": "19.990",
            "evidence_ids": ["cart-1"],
        },
    )
    assert result["matched"] and result["verification_status"] == "quote_verified"
    assert result["scope"] == "this_configuration_only"
    reset = compare_quote(
        expected,
        {**expected, "effective_configuration": {"coating": "UV", "corners": ""}},
    )
    assert reset["status"] == "configuration_mismatch"
    assert not reset["matched"]
    missing_empty = compare_quote(
        expected, {**expected, "effective_configuration": {"corners": ""}}
    )
    assert missing_empty["status"] == "configuration_mismatch"


def test_quote_verification_requires_same_known_services_even_when_totals_match():
    from test_service_context import service_observation

    expected = {
        "configuration": {"corners": {"value": "2", "label": "+$3"}},
        "quantity": 50,
        "currency": "USD",
        "subtotal": "5.60",
        **service_observation(),
    }
    observed = {**expected, **service_observation("100", delivery=False)}
    assert compare_quote(expected, observed)["status"] == "configuration_mismatch"
    assert not compare_quote(expected, observed)["matched"]
    assert compare_quote(expected, deepcopy(expected))["matched"]
    unknown = {**expected, "service_context_observed": False}
    assert compare_quote(unknown, unknown)["status"] == "inconclusive"
    assert not compare_quote(unknown, unknown)["matched"]


def test_quote_semantics_ignore_labels_verification_and_table_provenance_only():
    from test_service_context import service_observation
    from crawler.service_context import canonical_service_context

    expected = {
        "configuration": {"corners": {"value": "2", "label": "Corners"}},
        "quantity": 50,
        "currency": "USD",
        "subtotal": "5.60",
        "production": {"value": "1", "status": "requires_configuration_quote"},
        "pricing_context": {
            "service_context": canonical_service_context(service_observation()),
            "source_base_product_id": "123",
        },
    }
    observed = deepcopy(expected)
    observed["configuration"]["corners"]["label"] = "Corners (+$3)"
    observed["production"].update(label="Regular 3–5 days", status="source_displayed")
    observed["pricing_context"].pop("source_base_product_id")
    assert compare_quote(expected, observed)["matched"]
    observed["configuration"]["corners"]["value"] = ""
    assert compare_quote(expected, observed)["status"] == "configuration_mismatch"


def test_legacy_printograph_prices_cannot_be_verified_or_used_for_current_samples():
    row = {
        "product_url": "https://www.printograph.com/products/business-cards/order",
        "source_product_id": "1",
        "provenance": "direct_quote",
        "configuration": {},
        "quantity": 50,
        "subtotal": "5.60",
        "currency": "USD",
    }
    assert compare_quote(row, row)["status"] == "unresolved_scope"
    assert not compare_quote(row, row)["matched"]
    report = select_verification_sample([row])
    assert report["status"] == "unresolved_scope"
    assert report["excluded_legacy_unscoped"] == 1 and not report["rows"]
    assert not report["coverage_complete"]


def test_subtotal_mismatch_and_pricing_context_mismatch_are_distinct():
    expected = {
        "configuration": {"size": "2 x 3.5"},
        "quantity": 500,
        "currency": "USD",
        "subtotal": "19.99",
        "account_context": "broker",
    }
    assert (
        compare_quote(expected, {**expected, "subtotal": "20.00"})["status"]
        == "price_mismatch"
    )
    assert (
        compare_quote(expected, {**expected, "account_context": "retail"})["status"]
        == "configuration_mismatch"
    )
    assert (
        compare_quote(expected, {**expected, "subtotal": None})["status"]
        == "inconclusive"
    )
    # Formatting may round, but verification cannot conceal a source difference.
    assert (
        compare_quote(expected, {**expected, "subtotal": "19.991"})["status"]
        == "price_mismatch"
    )


def test_retry_policy_invalidates_only_correct_dependency_scope():
    first = verification_decision({"status": "price_mismatch", "matched": False})
    assert first["action"] == "refresh_and_retry_once"
    assert first["invalidate_scope"] == "product_snapshot"
    repeated = verification_decision(
        {"status": "price_mismatch", "matched": False},
        retried=True,
        shared_dependency=True,
    )
    assert repeated["action"] == "per_configuration_quote_fallback"
    assert repeated["invalidate_scope"] == "all_dependency_consumers"
    passed = verification_decision({"matched": True})
    assert passed["unsampled_rows"] == "provisional"


def test_staleness_uses_independent_quote_window_and_dependency_invalidation():
    observed = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    assert (
        freshness_status(observed, kind="table", now=observed + timedelta(minutes=31))[
            "status"
        ]
        == "fresh"
    )
    assert (
        freshness_status(observed, kind="quote", now=observed + timedelta(minutes=30))[
            "status"
        ]
        == "stale"
    )
    assert (
        freshness_status(observed, now=observed, dependency_changed=True)["reason"]
        == "dependency_changed"
    )
    assert (
        freshness_status(observed, now=observed, context_changed=True)["reason"]
        == "context_changed"
    )
