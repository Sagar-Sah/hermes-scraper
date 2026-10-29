import asyncio
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.product_companion_observed import observed_option_check, read_source_diagnosis
from crawler.product_companion_service import ProductCompanionService


SOURCE = "https://4over.com/4coro-coroplast-signs"
SITE = "https://printitprints.com/shop/coroplast/"
ID = "a" * 32


def observation():
    return {"id": ID, "url": SOURCE, "supplier_id": "fourover", "status": "ready",
            "session_status": "unknown", "created_at": datetime.now(timezone.utc).isoformat(),
            "fields": [{"key": "width", "label": "WIDTH *", "disabled": False,
                        "choices": [{"value": "", "label": "Choose width", "disabled": False},
                                    {"value": "6", "label": "6", "disabled": False}]}]}


def site_html():
    rows = [{"Stock": "4mm White Coroplast", "Colorspec": "4/0", "Width": "6", "Height": "6",
             "RunSize": "1", "TotalPrice": "0.41", "UnitPrice": "0.4125"}]
    return ("<html><body class='postid-6383'><h1 class='product_title'>Coroplast</h1>"
            "<div id='product_calculator_display'></div>"
            "<script>const current_product_id_is_this = 6383;</script>"
            f"<script>const csvdata = {json.dumps(rows)};</script></body></html>")


def service(tmp_path):
    product = {"id": "6383", "title": "4mm Coroplast", "supplier": "4over", "supplier_url": SOURCE,
               "storefront_url": SITE, "diagnosis_id": ID}
    campaign = {"products": [product]}
    store = SimpleNamespace(root=tmp_path)
    campaigns = SimpleNamespace(get=lambda identifier: campaign)
    (tmp_path / "diagnoses").mkdir()
    (tmp_path / "diagnoses" / (ID + ".json")).write_text(json.dumps(observation()))
    woo_url = "https://printitprints.com/wp-json/wc/store/v1/products/6383"
    woo = {"id": 6383, "permalink": SITE, "prices": {"price": "0", "currency_code": "USD"}, "images": []}
    async def fetch(url):
        documents = {SITE: site_html().encode(), SOURCE: b"<h1>4mm Coroplast Signs</h1>",
                     woo_url: json.dumps(woo).encode()}
        return 200, url, documents[url]
    value = ProductCompanionService(store, campaigns, fetch=fetch)
    async def rendered(pair, snapshot):
        return {"status": "unknown", "reason": "not_rendered_in_this_fixture", "samples": []}
    value._rendered_retail_samples = rendered
    value.add_pair({"product_id": "6383", "campaign_id": "test", "source_url": SOURCE, "storefront_url": SITE})
    return value, product


def test_attached_diagnosis_allows_partial_audit_without_inventing_source_rows(tmp_path):
    value, _ = service(tmp_path)
    audit = asyncio.run(value.audit("woo-6383"))
    assert audit["audit_mode"] == "observed_snapshot"
    assert audit["report"]["status"] == "unknown"
    assert audit["report"]["checks"]["observed_supplier_menu_values"] == "match"
    assert audit["report"]["checks"]["current_supplier_prices"] == "unknown"
    assert audit["report"]["checks"]["conditional_supplier_options"] == "unknown"
    assert audit["report"]["counts"]["source_rows"] == 0
    assert audit["report"]["counts"]["site_rows"] == 1
    assert audit["report"]["extra_site_rows"] == []
    assert audit["report"]["automatic_correction_eligible"] is False
    assert audit["automatic_changes_applied"] == 0
    assert value.history("woo-6383")[0]["id"] == audit["id"]
    assert not any(p["kind"] in {"review_stored_supplier_costs", "add_missing_configurations"}
                   for p in audit["report"]["proposed_changes"])


def test_verified_public_rule_and_scoped_rendered_match_cannot_complete_source_audit(tmp_path):
    value, _ = service(tmp_path)
    script_sha = "c" * 64
    async def rendered(pair, snapshot):
        return {"status": "match", "scope": "observed_public_js_selection_only",
                "samples": [{"selection": {"width": "6", "height": "6", "quantity": "1"},
                             "site_cost": "0.41", "expected_retail": "1.23", "displayed_retail": "1.23"}],
                "area_rule": {"public_rule_status": "verified", "backend_status": "unknown",
                              "current_supplier_prices": "unknown"},
                "observations": {"loaded_public_script": {"sha256": script_sha}}}
    value._rendered_retail_samples = rendered
    audit = asyncio.run(value.audit("woo-6383"))
    checks = audit["report"]["checks"]
    assert checks["loaded_public_retail_rule"] == checks["rendered_retail_samples"] == "match"
    assert checks["retail_rule"] == checks["current_supplier_prices"] == "unknown"
    assert checks["conditional_supplier_options"] == checks["current_supplier_quantity_domain"] == "unknown"
    assert audit["status"] == audit["report"]["status"] == "unknown"
    assert audit["report"]["source_domain_complete"] is False
    assert audit["report"]["automatic_correction_eligible"] is False
    assert audit["report"]["counts"]["source_rows"] == 0
    assert audit["report"]["cost_mismatches"] == []
    assert audit["evidence"]["retail_script_sha256"] == script_sha
    assert not any(p["kind"] in {"review_stored_supplier_costs", "add_missing_configurations"}
                   for p in audit["report"]["proposed_changes"])


def test_export_preserves_audited_observation_after_live_diagnosis_changes(tmp_path):
    value, _ = service(tmp_path)
    audit = asyncio.run(value.audit("woo-6383"))
    original = (tmp_path / "diagnoses" / (ID + ".json")).read_bytes()
    (tmp_path / "diagnoses" / (ID + ".json")).write_text("{}")
    files = value.saved_source_evidence_for_audit(audit)
    assert files["saved-supplier-page-observation.json"] == original


@pytest.mark.parametrize("field", ["fourover_rows_evidence", "fourover_option_map_evidence",
                                  "fourover_price_table_evidence"])
def test_observed_export_excludes_source_evidence_collected_after_audit(tmp_path, field):
    value, product = service(tmp_path)
    audit = asyncio.run(value.audit("woo-6383"))
    # The future object deliberately does not exist: exporting this historical
    # audit must neither read it nor claim it was part of the original evidence.
    product[field] = {"object_path": "later-source-object", "sha256": "b" * 64}
    files = value.saved_source_evidence_for_audit(audit)
    assert set(files) == {"saved-supplier-page-observation.json"}
    assert json.loads(files["saved-supplier-page-observation.json"])["id"] == ID


def test_observed_export_is_bound_to_its_saved_hash_not_current_mapping(tmp_path):
    value, product = service(tmp_path)
    audit = asyncio.run(value.audit("woo-6383"))
    product["supplier_url"] = "https://4over.com/another-product"
    files = value.saved_source_evidence_for_audit(audit)
    assert set(files) == {"saved-supplier-page-observation.json"}
    evidence = tmp_path / audit["source_diagnosis_evidence"]["object_path"]
    evidence.write_text("{}")
    with pytest.raises(ValueError, match="evidence_hash_changed"):
        value.saved_source_evidence_for_audit(audit)


def test_observed_export_requires_its_recorded_observation(tmp_path):
    value, _ = service(tmp_path)
    audit = asyncio.run(value.audit("woo-6383"))
    audit.pop("source_diagnosis_evidence")
    with pytest.raises(ValueError, match="audited_observation_reference_missing"):
        value.saved_source_evidence_for_audit(audit)


def test_observed_option_proposal_cannot_reach_write_connector(tmp_path):
    value, _ = service(tmp_path)
    path = tmp_path / "diagnoses" / (ID + ".json")
    record = observation()
    record["fields"][0]["choices"].append({"value": "7", "label": "7", "disabled": False})
    path.write_text(json.dumps(record))
    audit = asyncio.run(value.audit("woo-6383"))
    calls = []
    async def transport(*args):
        calls.append(args)
        raise AssertionError("read-only option proposals must not reach WordPress")
    value.writes.transport = transport
    assert audit["report"]["proposed_changes"][0]["kind"] == "review_observed_option_values"
    with pytest.raises(ValueError, match="proposal_requires_manual_content_review"):
        asyncio.run(value.writes.preview(audit["id"], [0]))
    assert calls == []
    assert audit["report"]["counts"]["source_rows"] == 0
    assert audit["report"]["cost_mismatches"] == []


def test_observed_source_control_proposals_stay_review_only_before_connector_access(tmp_path):
    value, _ = service(tmp_path)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "observed_controls_6383.json").read_text())
    source = observation()
    source["fields"] = copy.deepcopy(fixture["diagnosis"]["fields"])
    (tmp_path / "diagnoses" / (ID + ".json")).write_text(json.dumps(source))
    inventory = copy.deepcopy(fixture["storefront_inventory"])
    inventory.update(url=SITE, observed_at=datetime.now(timezone.utc).isoformat())
    async def rendered(pair, snapshot):
        return {"status": "unknown", "samples": [], "field_inventory": inventory}
    value._rendered_retail_samples = rendered
    audit = asyncio.run(value.audit("woo-6383"))
    proposals = audit["report"]["proposed_changes"]
    indexes = [i for i, p in enumerate(proposals) if p["kind"] == "review_observed_source_control"]
    assert {proposals[i]["purpose"] for i in indexes} == {"finish_options", "grommets"}
    assert audit["report"]["checks"]["observed_source_controls"] == "mismatch"
    calls = []
    async def transport(*args):
        calls.append(args)
        raise AssertionError("observed control review cannot reach WordPress")
    value.writes.transport = transport
    for index in indexes:
        with pytest.raises(ValueError, match="proposal_requires_manual_content_review"):
            asyncio.run(value.writes.preview(audit["id"], [index]))
    assert calls == []
    assert audit["report"]["source_domain_complete"] is False
    assert audit["report"]["automatic_correction_eligible"] is False
    assert audit["report"]["counts"]["source_rows"] == 0
    assert audit["report"]["missing_site_rows"] == audit["report"]["cost_mismatches"] == []


def test_unknown_source_combinations_do_not_become_missing_configurations():
    site = {"fields": ["width"], "rows": {("6",): {"selection": {"width": "6"}}}}
    result = observed_option_check(observation(), site)
    assert result["status"] == "match"
    assert "conditional_combinations_unverified" in result["reason"]
    assert "rows" not in result
    site["rows"][("7",)] = {"selection": {"width": "7"}}
    assert observed_option_check(observation(), site)["status"] == "mismatch"


@pytest.mark.parametrize("patch", [{"status": "login_required"}, {"session_status": "login_required"},
                                   {"created_at": "2001-01-01T00:00:00+00:00"}, {"created_at": "invalid"}])
def test_stale_or_signed_out_observation_stays_unknown(patch):
    site = {"fields": ["width"], "rows": {("6",): {"selection": {"width": "6"}}}}
    assert observed_option_check({**observation(), **patch}, site)["status"] == "unknown"


def test_missing_or_rebound_diagnosis_cannot_be_used(tmp_path):
    value, product = service(tmp_path)
    with pytest.raises(ValueError, match="source_table_or_attached_diagnosis_required"):
        read_source_diagnosis(tmp_path, {}, SOURCE)
    with pytest.raises(ValueError, match="identity_changed"):
        read_source_diagnosis(tmp_path, product, "https://4over.com/other")


@pytest.mark.parametrize("record", [None, [], "invalid", {**observation(), "fields": [None]},
                                   {**observation(), "fields": "invalid"}])
def test_malformed_saved_observation_is_rejected_without_attribute_error(tmp_path, record):
    _, product = service(tmp_path)
    (tmp_path / "diagnoses" / (ID + ".json")).write_text(json.dumps(record))
    with pytest.raises(ValueError, match="attached_source_diagnosis_identity_changed"):
        read_source_diagnosis(tmp_path, product, SOURCE)


@pytest.mark.parametrize("record", [None, [], {**observation(), "fields": [None]},
                                   {**observation(), "fields": [observation()["fields"][0], None]}])
def test_malformed_observed_fields_cannot_become_a_matching_menu(record):
    site = {"fields": ["width"], "rows": {("6",): {"selection": {"width": "6"}}}}
    result = observed_option_check(record, site)
    assert result == {"status": "unknown", "reason": "source_observation_fields_invalid", "fields": []}
