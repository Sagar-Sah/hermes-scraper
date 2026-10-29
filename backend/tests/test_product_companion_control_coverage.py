"""Synthetic control/cost fixtures; no supplier or storefront requests."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from crawler.product_companion_control_coverage import observed_source_controls


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "observed_controls_6383.json").read_text())
SOURCE = "https://4over.com/4coro-coroplast-signs"
SITE = FIXTURE["storefront_inventory"]["url"]
NOW = datetime.fromisoformat("2026-10-29T15:40:00+00:00").timestamp()


def captures():
    return copy.deepcopy(FIXTURE["diagnosis"]), copy.deepcopy(FIXTURE["storefront_inventory"])


def compare(source=None, site=None, **kwargs):
    default_source, default_site = captures()
    source = default_source if source is None else source
    site = default_site if site is None else site
    return observed_source_controls(source, site, source_url=SOURCE, storefront_url=SITE,
        product_id="6383", diagnosis_sha256=hashlib.sha256(json.dumps(source).encode()).hexdigest(),
        now=kwargs.get("now", NOW))


def control(name, *, visible=True, scopes=None):
    _, site = captures()
    item = copy.deepcopy(site["controls"][0])
    item.update(dom_ref="purchase-0/control-extra-" + name, name="print_option_" + name,
                binding="", label=name, visible=visible, scopes=scopes or [])
    return item


def test_actual_branch_reports_only_finish_and_grommet_control_review_gaps():
    result = compare()
    assert result["status"] == "mismatch"
    assert {f["purpose"] for f in result["findings"]} == {"finish_options", "grommets"}
    found = {item["purpose"]: item for item in result["controls"]}
    assert found["flute_direction"]["status"] == found["h_stakes"]["status"] == "match"
    assert found["flute_direction"]["storefront_dom_references"] == ["purchase-0/control-6"]
    assert found["h_stakes"]["storefront_dom_references"] == ["purchase-0/control-7"]
    assert [option["label"] for option in found["grommets"]["source_choices"]] == [
        "None", "All 4 Corners +$2.00", "Top Corners Only +$3.00"]
    assert result["source_domain_complete"] is result["automatic_changes_allowed"] is False
    assert result["coverage"]["other_conditional_states"] == "unknown"
    assert result["coverage"]["merchant_selling_intent"] == "unknown"
    assert result["coverage"]["quantities"] == result["coverage"]["production"] == "unknown"
    assert result["coverage"]["non_select_controls"] == "unassessed"
    assert result["coverage"]["source_capture"] == "limited_observed_controls_only"
    assert "missing_site_rows" not in result
    assert all(f["requires_review"] and not f["automatic"] for f in result["findings"])


def test_matching_control_presence_does_not_assert_matching_choices_or_prices():
    source, site = captures()
    site["controls"].extend([control("grommets"), control("finish_options")])
    result = compare(source, site)
    assert result["status"] == "match"
    assert result["findings"] == []
    assert result["coverage"]["prices"] == "unknown"
    assert result["source_domain_complete"] is False
    assert all(item["conditional_equivalence"] == "unknown" for item in result["controls"])


def test_hidden_potential_equivalent_is_unknown_not_a_missing_field():
    source, site = captures()
    site["controls"].append(control("grommets", visible=False))
    result = compare(source, site)
    grommets = next(item for item in result["controls"] if item["purpose"] == "grommets")
    assert grommets["status"] == "unknown"
    assert grommets["inactive_storefront_dom_references"] == ["purchase-0/control-extra-grommets"]
    assert [f["purpose"] for f in result["findings"]] == ["finish_options"]


@pytest.mark.parametrize("scopes", [["data-side:front"], ["data-set-id:B"], ["data-layer-id:artwork-1"]])
def test_scoped_artwork_or_set_control_cannot_satisfy_a_product_axis(scopes):
    source, site = captures()
    site["controls"].append(control("grommets", scopes=scopes))
    result = compare(source, site)
    assert result["status"] == "unknown"
    assert result["findings"] == []
    assert next(item for item in result["controls"] if item["purpose"] == "grommets")["status"] == "unknown"


@pytest.mark.parametrize("patch", [{"visible": None}, {"name": "unmapped_control", "label": "Other choices"},
                                    {"product_owner": {"verified": False}}])
def test_unclassified_visible_or_unowned_control_prevents_an_absence_claim(patch):
    source, site = captures()
    item = control("grommets")
    item.update(patch)
    site["controls"].append(item)
    result = compare(source, site)
    assert result["status"] == "unknown"
    assert result["findings"] == []


@pytest.mark.parametrize("source_patch,site_patch", [
    ({"status": "attention"}, {}), ({"session_status": "login_required"}, {}),
    ({"url": "https://4over.com/another"}, {}), ({"supplier_id": "printograph"}, {}),
    ({"created_at": "2001-01-01T00:00:00+00:00"}, {}),
    ({"created_at": "2026-10-29T16:40:00+00:00"}, {}),
    ({"created_at": "2026-10-29T15:40:00"}, {}),
    ({}, {"truncated": True}), ({}, {"observation_kind": "static_html"}),
    ({}, {"product_id": "6669"}), ({}, {"url": "https://printitprints.com/another/"}),
    ({}, {"observed_at": "invalid"}), ({}, {"observed_at": "2001-01-01T00:00:00+00:00"}),
])
def test_unverified_or_stale_capture_stays_unknown(source_patch, site_patch):
    source, site = captures()
    source.update(source_patch); site.update(site_patch)
    result = compare(source, site)
    assert result["status"] == "unknown"
    assert result["findings"] == result["controls"] == []


def test_unrecognized_supplier_label_is_not_guessed():
    source, site = captures()
    source["fields"] = [{**source["fields"][0], "label": "Special Mystery Finish"}]
    result = compare(source, site)
    assert result["status"] == "unknown"
    assert result["controls"] == result["findings"] == []
    assert result["unclassified_source_select_labels"] == ["Special Mystery Finish"]


@pytest.mark.parametrize("patch", [{"disabled": True}, {"configurable": False},
                                    {"kind": "radio"}, {"choices": []}])
def test_non_configurable_or_unobserved_choice_domains_do_not_create_proposals(patch):
    source, site = captures()
    field = next(item for item in source["fields"] if item["label"] == "GROMMETS")
    source["fields"] = [{**field, **patch}]
    result = compare(source, site)
    assert result["status"] == "unknown"
    assert result["findings"] == []


def test_customer_values_and_private_control_payloads_are_not_copied():
    source, site = captures()
    site["controls"][0].update(value="CUSTOMER_SECRET", uploaded_artwork="PRIVATE_FILE")
    result = compare(source, site)
    assert "CUSTOMER_SECRET" not in json.dumps(result)
    assert "PRIVATE_FILE" not in json.dumps(result)
    assert result["evidence"]["source_diagnosis_revision"] == 3
