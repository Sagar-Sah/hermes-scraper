"""Offline finite probe comparisons never erase untested pricing dependencies."""

from copy import deepcopy

import pytest

from crawler.route_dependencies import (
    DependencyError, compare_probe_observations, plan_dependency_probes,
    plan_exact_quote_requests, request_key, finite_equivalence_key,
    validate_unique_lookup,
)


def request(quantity=50):
    return {"method": "GET", "path": "/service/rest/v1/products/quantities",
            "params": {"productType": "1", "size": "101", "paper": "1", "color": "2"},
            "context": {"origin": "https://example.test", "session_scope": "opaque-session-1",
                        "source_epoch": "capture-window-1", "store_type": "broker", "currency": "USD",
                        "service_signature": "opaque-service-1", "selection": {"quantity": quantity, "coating": "5"},
                        "unknown_account_price_class": "must-retain"}}


def observation(quantity=50, data=None):
    return {"request": request(quantity), "response_id": f"evidence-{quantity}", "status": 200,
            "complete": True, "data": data if data is not None else {"items": [{"quantity": 50}, {"quantity": 100}]}}


def lookup():
    result = observation()
    result["request"]["path"] = "/service/rest/v1/products"
    result["request"]["params"].update({"turnaround": "1", "orientation": "1"})
    result["data"] = {"items": [{"id": 123}]}
    return validate_unique_lookup(result)


def test_probe_plan_is_one_axis_and_preserves_all_other_context():
    baseline = request()
    plan = plan_dependency_probes(baseline, "context.selection.quantity", [50, 100, 250])
    assert len(plan["requests"]) == 3 and plan["certificate"] is None
    assert [row["request"]["context"]["selection"]["quantity"] for row in plan["requests"]] == [50, 100, 250]
    assert all(row["request"]["context"]["unknown_account_price_class"] == "must-retain" for row in plan["requests"])
    assert baseline == request()


def test_equal_content_certifies_exact_members_only_not_a_global_axis():
    result = compare_probe_observations([observation(50), observation(100)], "context.selection.quantity")
    certificate = result["certificate"]
    assert result["status"] == "equal_for_tested_members"
    assert not certificate["global_dimension_independence"] and not certificate["untested_values_covered"]
    assert finite_equivalence_key(request(50), certificate) == finite_equivalence_key(request(100), certificate)
    assert finite_equivalence_key(request(250), certificate) == request_key(request(250))
    changed = request(100)
    changed["context"]["selection"]["coating"] = "6"
    assert finite_equivalence_key(changed, certificate) == request_key(changed)


@pytest.mark.parametrize("field,value", [("session_scope", "another-session"), ("source_epoch", "next-window"),
                                        ("service_signature", "paid-proof"), ("unknown_account_price_class", "new-class")])
def test_scope_or_unproven_context_change_prevents_reuse(field, value):
    certificate = compare_probe_observations([observation(50), observation(100)], "context.selection.quantity")["certificate"]
    changed = request(100)
    changed["context"][field] = value
    assert finite_equivalence_key(changed, certificate) == request_key(changed)
    alternative = observation(100)
    alternative["request"] = changed
    assert compare_probe_observations([observation(50), alternative], "context.selection.quantity")["status"] == "confounded"


def test_changed_body_and_same_request_instability_cannot_certify():
    changed = observation(100, {"items": [{"quantity": 100}]})
    result = compare_probe_observations([observation(50), changed], "context.selection.quantity")
    assert result["status"] == "dependent_for_observed_values" and result["certificate"] is None
    changed["request"] = request(50)
    assert compare_probe_observations([observation(50), changed], "context.selection.quantity")["status"] == "unstable"


@pytest.mark.parametrize("mutation", ["incomplete", "http", "pagination", "total", "missing_evidence"])
def test_partial_failed_or_unattributed_probes_fail_closed(mutation):
    other = observation(100)
    if mutation == "incomplete":
        other["complete"] = False
    elif mutation == "http":
        other["status"] = 503
    elif mutation == "pagination":
        other["data"]["next"] = "page2"
    elif mutation == "total":
        other["data"]["totalCount"] = 3
    else:
        other.pop("response_id")
    with pytest.raises(DependencyError):
        compare_probe_observations([observation(50), other], "context.selection.quantity")


def test_protected_scope_cannot_be_the_varied_dependency():
    with pytest.raises(DependencyError, match="cannot be normalized"):
        plan_dependency_probes(request(), "context.session_scope", ["other"])
    with pytest.raises(DependencyError, match="absent"):
        plan_dependency_probes(request(), "context.selection.unobserved", [None])


def test_lookup_must_be_unique_complete_and_have_actual_selectors():
    assert lookup()["base_product_id"] == "123"
    invalid = observation()
    invalid["request"]["path"] = "/service/rest/v1/products"
    invalid["data"] = {"items": [{"id": 123}, {"id": 456}]}
    with pytest.raises(DependencyError, match="exactly one"):
        validate_unique_lookup(invalid)
    invalid["data"] = {"items": [{"id": 123}]}
    with pytest.raises(DependencyError, match="selectors"):
        validate_unique_lookup(invalid)


def test_quote_manifest_waits_for_discovery_lookup_and_exact_context():
    proof = lookup()
    operations = [{"path": "/service/rest/v1/products/123/prices", "params": {}},
                  {"path": "/service/rest/v1/products/123/options/456/prices", "params": {"quantities": "50"}}]
    plan = plan_exact_quote_requests(proof, operations, context=proof["request"]["context"],
                                    discovery_complete=True, catalog_option_ids=[456])
    assert len(plan["requests"]) == 2 and not plan["prices_observed"] and not plan["composition_verified"]
    with pytest.raises(DependencyError, match="completed discovery"):
        plan_exact_quote_requests(proof, operations, context=proof["request"]["context"], discovery_complete=False)
    with pytest.raises(DependencyError, match="catalog"):
        plan_exact_quote_requests(proof, operations, context=proof["request"]["context"], discovery_complete=True)
    other_context = deepcopy(proof["request"]["context"])
    other_context["service_signature"] = "different"
    with pytest.raises(DependencyError, match="context differs"):
        plan_exact_quote_requests(proof, operations, context=other_context, discovery_complete=True)
    wrong_base = [{"path": "/service/rest/v1/products/999/prices"}]
    with pytest.raises(DependencyError, match="bound"):
        plan_exact_quote_requests(proof, wrong_base, context=proof["request"]["context"], discovery_complete=True)


def test_price_probe_is_locked_before_discovery():
    price = request()
    price["path"] = "/service/rest/v1/products/123/prices"
    with pytest.raises(DependencyError, match="locked"):
        plan_dependency_probes(price, "context.selection.quantity", [100])


def test_price_probe_requires_valid_lookup_even_after_discovery():
    price = request()
    price["path"] = "/service/rest/v1/products/123/prices"
    with pytest.raises(DependencyError, match="unique validated lookup"):
        plan_dependency_probes(price, "context.selection.quantity", [100], discovery_complete=True)
    proof = lookup()
    plan = plan_dependency_probes(price, "context.selection.quantity", [100],
                                  discovery_complete=True, lookup=proof)
    assert len(plan["requests"]) == 2
