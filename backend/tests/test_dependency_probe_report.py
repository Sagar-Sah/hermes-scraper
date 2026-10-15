"""Offline evidence gates: no network needed or permitted by this module."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from crawler.dependency_probe_report import build_report, compare_phases, markdown
from crawler.price_contract import BASE_FIELDS, OPTION_FIELDS, NORMAL_CONTEXT_FLAGS, SOURCE_CONTRACT


def capture(**changes):
    selections = {key: "1" for key in BASE_FIELDS["1"]}
    selections.update({key: "" for key in OPTION_FIELDS["1"]})
    selections.update(quantity="50", productId="100000009", productionTime="1",
                      proofType="31", manualProofType="1", designServices="42", fileDelivery="5")
    selections.update(changes)
    return {"capture_kind": "controller", "observed": True, "settled": True,
            "source_contract": dict(SOURCE_CONTRACT), "product_type": "1", "store_type": "broker",
            "currency": "USD", "userSelectedOptions": selections, "field_options": list(OPTION_FIELDS["1"]),
            "appliedPrices": {"productPrice": "5.60", "proofType": {"1": "0.00", "36": "0.00"}},
            "other_context": {key: False for key in NORMAL_CONTEXT_FLAGS},
            "coupon": {"observed": True, "mode": "source_state", "coupon_object_observed": True,
                       "coupon_object_key_count": 0, "parameters": {}, "source_quantity": "50", "is_coupon_spec_changed": False},
            "displayed_total": "5.60", "controller_total": "5.60"}


def paired():
    baseline = {"name": "baseline", "capture": capture(), "base_recheck": {"evidence_id": "base", "cached": False}}
    phase = {"name": "manual", "capture": capture(proofType="100"), "base_recheck": {"evidence_id": "manual", "cached": False}}
    report = {"loaded_contract": dict(SOURCE_CONTRACT), "captures": []}
    bodies = {}
    for item in (baseline, phase):
        evidence = item["base_recheck"]["evidence_id"]
        path = "/service/rest/v1/products/100000009/prices"
        report["captures"].append({"phase": item["name"], "kind": "explicit_base_recheck", "cached": False,
            "evidence_id": evidence, "url": "https://www.printograph.com" + path, "controller_context": deepcopy(item["capture"])})
        bodies[evidence] = {"available": True, "http_status": 200, "priced_quantity_table": True,
            "path": path, "quantity_count": 26, "full_json_sha256": "same"}
    return baseline, phase, report, bodies


def test_one_dimension_pair_is_observation_not_certificate():
    result = compare_phases(*paired())
    assert result["status"] == "observed_pair"
    assert result["full_base_bodies_equal"] is True
    assert list(result["changed_selections"]) == ["proofType"]
    assert result["global_proof"] is False and result["cache_certificate"] is False


@pytest.mark.parametrize("failure", ["cached", "missing_fresh", "unsettled", "wrong_bundle", "context_mismatch", "same_evidence", "filtered_table"])
def test_pair_requires_two_bound_complete_fresh_observations(failure):
    baseline, phase, report, bodies = paired()
    if failure == "cached":
        report["captures"][1]["cached"] = True
    elif failure == "missing_fresh":
        phase.pop("base_recheck")
    elif failure == "unsettled":
        phase["capture"]["settled"] = False
    elif failure == "wrong_bundle":
        report["loaded_contract"] = {}
    elif failure == "context_mismatch":
        report["captures"][1]["controller_context"]["userSelectedOptions"]["quantity"] = "100"
    elif failure == "filtered_table":
        report["captures"][1]["url"] += "?quantities=50"
    else:
        phase["base_recheck"]["evidence_id"] = "base"
    assert compare_phases(baseline, phase, report, bodies)["status"] == "unresolved"


def test_multiple_changed_selections_are_confounded_even_when_price_matches():
    baseline, phase, report, bodies = paired()
    phase["capture"]["userSelectedOptions"]["manualProofType"] = "36"
    result = compare_phases(baseline, phase, report, bodies)
    assert result["status"] == "confounded"
    assert set(result["changed_selections"]) == {"proofType", "manualProofType"}
    assert "full_base_bodies_equal" not in result


def test_full_body_difference_is_not_hidden_by_same_quantity_count():
    baseline, phase, report, bodies = paired()
    bodies["manual"]["full_json_sha256"] = "different"
    result = compare_phases(baseline, phase, report, bodies)
    assert result["status"] == "observed_pair" and result["full_base_bodies_equal"] is False


def saved_database(tmp_path):
    db_path = tmp_path / "evidence.db"
    db = sqlite3.connect(db_path)
    db.executescript('''CREATE TABLE probe_jobs (id TEXT,run_id TEXT,status TEXT,budget INTEGER,used INTEGER,created_at REAL,updated_at REAL,manifest TEXT,results TEXT,error TEXT);
    CREATE TABLE probe_attempts (job_id TEXT,sequence INTEGER,phase TEXT,method TEXT,status INTEGER);
    CREATE TABLE responses (id TEXT,status INTEGER,url TEXT,object_path TEXT);''')
    root = tmp_path / "data"
    (root / "objects").mkdir(parents=True)
    object_path = root / "objects" / "prices.response"
    object_path.write_text(json.dumps({"items": [{"quantity": 50, "markupPrice": "5.60"}, {"quantity": 100, "markupPrice": "8.00"}]}))
    url = "https://www.printograph.com/products/business-cards/order"
    manifest = {"prior_public_contract_requests": 2, "requests": [{"id": "table", "method": "GET", "path": "/service/rest/v1/products/100000009/prices", "params": {"quantities": "50,100", "cid": "DO-NOT-EXPORT", "authorization": "DO-NOT-EXPORT"}}]}
    incomplete = capture()
    incomplete["userSelectedOptions"]["quantity"] = "0"
    mismatch = capture()
    mismatch["displayed_total"] = "8.60"
    results = [
        {"id": "table", "status": "failed", "code": "network_error"},
        {"id": "table", "status": "observed", "response_id": "table-evidence", "cached": False},
        {"id": "browser-v1", "report": {"product_url": url, "status": "finished", "loaded_contract": SOURCE_CONTRACT,
            "captures": [{"phase": "baseline", "evidence_id": "table-evidence", "request_method": "GET"}],
            "phases": [{"name": "baseline", "status": "observed", "capture": incomplete},
                       {"name": "manual", "status": "observed", "capture": mismatch}]}},
        {"id": "browser-v2", "report": {"product_url": url, "status": "unresolved", "phases": [], "captures": []}}]
    db.execute("INSERT INTO probe_jobs VALUES (?,?,?,?,?,?,?,?,?,?)", ("job", "run", "needs_attention", 398, 3, 1, 2, json.dumps(manifest), json.dumps(results), "probe_interrupted"))
    db.executemany("INSERT INTO probe_attempts VALUES (?,?,?,?,?)", [("job",1,"bootstrap","GET",200),("job",2,"probe","GET",0),("job",3,"probe","GET",200)])
    db.execute("INSERT INTO responses VALUES (?,?,?,?)", ("table-evidence", 200, "https://www.printograph.com/service/rest/v1/products/100000009/prices", str(object_path)))
    db.commit()
    db.close()
    return db_path, root


def test_saved_summary_counts_attempts_issues_and_latest_product_without_secrets(tmp_path):
    db, root = saved_database(tmp_path)
    before = db.read_bytes()
    result = build_report(db, root)
    assert db.read_bytes() == before  # Reader cannot change saved runtime evidence.
    assert result["jobs"][0]["total_recorded_attempts_including_prior_gets"] == 5
    assert result["formula_summary"]["mismatch"] == 1
    assert result["formula_summary"]["unresolved"] == 1
    assert result["formula_summary"]["pass_rate"] == 0
    assert len(result["formula_issues"]) == 3
    assert result["products"][0]["latest_experiment_id"] == "browser-v2"
    assert result["products"][0]["baseline_status"] == "not_captured"
    assert result["routes"][0]["observed_quantity_counts"] == [2]
    assert result["routes"][0]["distinct_full_json_bodies"] == 1
    assert "DO-NOT-EXPORT" not in json.dumps(result)
    assert "0 matched, 1 mismatched, 1 unresolved" in markdown(result)


def test_unreadable_or_outside_response_never_becomes_body_evidence(tmp_path):
    db, root = saved_database(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE responses SET object_path=?", (str(tmp_path / "private.txt"),))
    connection.commit()
    connection.close()
    result = build_report(db, root)
    body = result["http_results"][1]["response"]
    assert body["available"] is False
    assert body["reason"] == "response_object_outside_object_store"
    assert result["routes"][0]["distinct_full_json_bodies"] == 0
