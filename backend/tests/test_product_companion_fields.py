"""Field-overlap evidence tests. No supplier requests or browser sessions."""
from contextlib import asynccontextmanager
import csv
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace
import zipfile

import pytest

from crawler.product_companion_fields import (SCHEMA, CAPTURE_RENDERED_FIELDS_JS,
    duplicate_field_findings, normalize_field_inventory, static_field_inventory)
from crawler.product_companion_service import ProductCompanionService
from crawler.product_companion import compare_snapshots, storefront_html_snapshot
from test_product_companion import source_table, site_html, SOURCE, SITE


def control(name="quantity", **changes):
    return {"dom_ref": "purchase-0/control-0", "tag": "input", "type": "number", "id": "qty", "name": name,
            "label": "Quantity", "form_key": "purchase-0", "scopes": [], "visible": True,
            "enabled": True, "editable": True, **changes}


def inventory(*controls, headings=(), **changes):
    return normalize_field_inventory({"schema": SCHEMA, "observation_kind": "rendered_dom", "url": SITE,
        "product_id": "9532", "observed_at": "2026-10-29T10:00:00Z", "viewport": {"width": 1280, "height": 800},
        "controls": list(controls), "headings": list(headings), "truncated": False, **changes})


def test_same_binding_concurrent_editable_controls_are_a_proven_conflict_with_distinct_dom_references():
    observed = inventory(control(), control(dom_ref="purchase-0/control-1"))
    result = duplicate_field_findings(observed)
    assert result["status"] == "mismatch"
    finding, = result["findings"]
    assert finding["kind"] == "duplicate_bound_controls"
    assert {field["dom_ref"] for field in finding["observations"]} == {"purchase-0/control-0", "purchase-0/control-1"}
    assert finding["evidence"]["projection_sha256"] == observed["projection_sha256"]
    assert finding["proposed_correction"]["requires_review"] is True
    assert finding["proposed_correction"]["automatic"] is False


@pytest.mark.parametrize("name", ["print_option_grommets", "drill_hole", "pole_pockets", "custom[finish]", "super_attribute[999]"])
def test_any_named_product_axis_is_checked_without_a_known_label_alias(name):
    result = duplicate_field_findings(inventory(control(name, label="Custom supplier choice"),
        control(name, label="Custom supplier choice", dom_ref="purchase-0/control-1")))
    assert result["status"] == "mismatch"
    assert result["findings"][0]["kind"] == "duplicate_bound_controls"
    assert all(field["binding_key"] == "name:" + name for field in result["findings"][0]["observations"])


def test_same_named_binding_remains_duplicate_even_when_purpose_came_from_its_label():
    result = duplicate_field_findings(inventory(control("custom_quantity", label="Quantity"),
        control("custom_quantity", label="Quantity", dom_ref="purchase-0/control-1")))
    assert result["status"] == "mismatch"


def test_explicit_named_axis_binding_is_checked_without_native_name_or_known_label():
    result = duplicate_field_findings(inventory(control("", label="Supplier custom finish", binding="custom[finish]"),
        control("", label="Supplier custom finish", binding="custom[finish]", dom_ref="purchase-0/control-1")))
    assert result["status"] == "mismatch"
    assert result["findings"][0]["observations"][0]["binding_key"] == "declared:custom[finish]"


def test_array_or_multi_select_bindings_are_reviewed_instead_of_assuming_scalar_duplicates():
    result = duplicate_field_findings(inventory(control("custom[finish][]", label="Finishes", multiple=True),
        control("custom[finish][]", label="Finishes", multiple=True, dom_ref="purchase-0/control-1")))
    assert result["status"] == "unknown"


def test_native_woo_quantity_and_calculator_runsize_are_ambiguous_not_assumed_equivalent():
    result = duplicate_field_findings(inventory(control(), control("print_option_quantity", dom_ref="purchase-0/control-1", type="select")))
    assert result["status"] == "unknown"
    finding, = result["findings"]
    assert finding["kind"] == "ambiguous_field_overlap"
    assert finding["reason"] == "overlapping_purpose_requires_binding_review"


def primary_owner(identifier="9532"):
    return {"product_id": identifier, "verified": True, "primary": True,
            "basis": "explicit_product_region_or_form_id", "sources": ["product_region:product-" + identifier]}


def test_standalone_calculator_and_woo_form_for_same_primary_product_are_an_unknown_cross_form_overlap():
    observed = inventory(control("run_size", product_owner=primary_owner()),
        control("quantity", form_key="purchase-1", dom_ref="purchase-1/control-0", product_owner=primary_owner()))
    result = duplicate_field_findings(observed)
    finding, = result["findings"]
    assert result["status"] == finding["status"] == "unknown"
    assert finding["kind"] == "cross_form_purchase_field_overlap"
    assert finding["purchase_scope"]["product_id"] == "9532"
    assert {field["form_key"] for field in finding["observations"]} == {"purchase-0", "purchase-1"}
    assert all(field["product_owner"]["sources"] for field in finding["observations"])
    assert finding["proposed_correction"]["automatic"] is False


@pytest.mark.parametrize("scopes", [("legend:Front", "legend:Back"), ("data-set:set-1", "data-set:set-2"),
    ("variation_id:123", "variation_id:456"), ("row:0", "row:1")])
def test_cross_form_overlap_preserves_side_set_and_variation_scopes(scopes):
    result = duplicate_field_findings(inventory(control("run_size", product_owner=primary_owner(), scopes=[scopes[0]]),
        control("quantity", form_key="purchase-1", dom_ref="purchase-1/control-0", product_owner=primary_owner(), scopes=[scopes[1]])))
    assert result["findings"] == []


def test_other_product_or_unverified_upsell_owners_cannot_merge_with_the_primary_product():
    for other in [primary_owner("9999"), {"product_id": "9532", "verified": False, "primary": False, "basis": "non_primary_product_region"}, {}]:
        result = duplicate_field_findings(inventory(control("run_size", product_owner=primary_owner()),
            control("quantity", form_key="purchase-1", dom_ref="purchase-1/control-0", product_owner=other)))
        assert result["findings"] == []


def test_static_capture_proves_shared_primary_owner_across_separate_roots_but_not_visible_equivalence():
    html = '<body class="single-product postid-9532"><main><div id="product-9532"><div id="product_calculator_display"><select name="print_option_quantity" aria-label="Quantity"><option>100</option></select></div><form class="cart"><input name="add-to-cart" type="hidden" value="9532"><input name="quantity" type="number" aria-label="Quantity"></form></div></main></body>'
    observed = static_field_inventory(html, url=SITE)
    assert {field["form_key"] for field in observed["controls"]} == {"purchase-0", "purchase-1"}
    assert all(field["product_owner"]["primary"] for field in observed["controls"])
    result = duplicate_field_findings(observed)
    assert result["status"] == "unknown"
    assert result["findings"][0]["kind"] == "cross_form_purchase_field_overlap"


def test_static_cross_form_capture_keeps_outer_front_back_scopes_and_upsells_separate():
    html = '<body class="postid-9532"><main><div id="product-9532"><fieldset><legend>Front</legend><form class="cart"><input type="number" name="quantity"></form></fieldset><fieldset><legend>Back</legend><form class="cart"><input type="number" name="quantity"></form></fieldset></div><section class="upsells"><div class="product" id="product-9999"><form class="cart"><input type="number" name="quantity"></form></div></section></main></body>'
    observed = static_field_inventory(html, url=SITE)
    assert [field["scopes"] for field in observed["controls"][:2]] == [["legend:front"], ["legend:back"]]
    assert observed["controls"][2]["product_owner"]["verified"] is False
    assert duplicate_field_findings(observed)["findings"] == []


def test_product_identifier_on_the_form_does_not_erase_its_outer_side_scope():
    html = '<body class="postid-9532"><main><fieldset><legend>Front</legend><form class="cart" data-product_id="9532"><input type="number" name="quantity"></form></fieldset><fieldset><legend>Back</legend><form class="cart" data-product_id="9532"><input type="number" name="quantity"></form></fieldset></main></body>'
    observed = static_field_inventory(html, url=SITE)
    assert all(field["product_owner"]["primary"] for field in observed["controls"])
    assert [field["scopes"] for field in observed["controls"]] == [["legend:front"], ["legend:back"]]
    assert duplicate_field_findings(observed)["findings"] == []


def test_same_label_alone_is_not_a_confirmed_duplicate():
    result = duplicate_field_findings(inventory(control("custom_first"), control("custom_second", dom_ref="purchase-0/control-1")))
    assert result["status"] == "unknown"
    assert all(row["purpose_basis"] == "label_only" for row in result["findings"][0]["observations"])


def test_explicit_pieces_per_set_and_number_of_sets_are_distinct_purchase_axes():
    result = duplicate_field_findings(inventory(control("run_size", label="Quantity per artwork set"),
        control("sets", label="Number of sets", dom_ref="purchase-0/control-1")))
    assert result["status"] == "match" and result["findings"] == []
    # A second unqualified Woo quantity still needs a binding review.
    result = duplicate_field_findings(inventory(control("run_size", label="Quantity per artwork set"),
        control("quantity", dom_ref="purchase-0/control-1")))
    assert result["status"] == "unknown"


@pytest.mark.parametrize("scopes", [("legend:Front", "legend:Back"), ("data-set:set-1", "data-set:set-2"),
    ("row:0", "row:1")])
def test_front_back_artwork_sets_and_variation_rows_do_not_merge(scopes):
    result = duplicate_field_findings(inventory(control(scopes=[scopes[0]]),
        control(dom_ref="purchase-0/control-1", scopes=[scopes[1]])))
    assert result["status"] == "match" and result["findings"] == []


def test_disabled_conditional_hidden_responsive_and_readonly_proxy_are_inactive_not_active_conflicts():
    result = duplicate_field_findings(inventory(control(), control(dom_ref="purchase-0/control-1", visible=False),
        control(dom_ref="purchase-0/control-2", enabled=False), control(dom_ref="purchase-0/control-3", editable=False)))
    assert result["status"] == "match" and result["findings"] == []
    assert len(result["inactive_observations"]) == 3


def test_visible_declared_proxy_cannot_be_certified_without_binding_review():
    result = duplicate_field_findings(inventory(control(), control(dom_ref="purchase-0/control-1", proxy_for="qty")))
    assert result["status"] == "unknown"


def test_static_css_classes_do_not_prove_hidden_or_complete_rendered_state():
    html = '<form class="cart"><label for="a">Quantity</label><input id="a" name="quantity" type="number"><div class="mobile-hidden"><input aria-label="Quantity" name="quantity" type="number"></div><div style="display:none"><input name="quantity" type="number"></div></form>'
    observed = static_field_inventory(html, url=SITE)
    assert [field["visible"] for field in observed["controls"]] == [None, None, False]
    result = duplicate_field_findings(observed)
    assert result["status"] == "unknown" and len(result["findings"]) == 1


def test_product_options_tab_and_one_panel_heading_are_not_duplicate_headings():
    html = '<form class="cart"><button role="tab">Product Options</button><button><h2>Product Options</h2></button><section><h2>Product Options</h2><input type="number" name="quantity"></section></form>'
    observed = static_field_inventory(html, url=SITE)
    assert len(observed["headings"]) == 1
    assert duplicate_field_findings(observed)["findings"] == []


def test_two_product_options_headings_propose_layout_review_not_automatic_removal():
    headings = [{"dom_ref": f"purchase-0/heading-{i}", "tag": "h2", "label": "Product Options",
                 "form_key": "purchase-0", "scopes": [], "visible": True} for i in range(2)]
    result = duplicate_field_findings(inventory(control(), headings=headings))
    finding, = result["findings"]
    assert finding["kind"] == "repeated_product_options_heading" and finding["status"] == "unknown"
    assert finding["proposed_correction"]["automatic"] is False


@pytest.mark.parametrize("changes", [{"truncated": True}, {"observation_kind": "static_html"}])
def test_incomplete_capture_never_passes(changes):
    assert duplicate_field_findings(inventory(control(), **changes))["status"] == "unknown"
    assert duplicate_field_findings(inventory())["status"] == "unknown"


def test_entered_values_hidden_tokens_and_extra_capture_fields_are_not_retained():
    html = '<form class="cart"><input type="hidden" name="auth_token" value="secret"><input type="text" name="billing_address" value="private address"><label>Quantity<input type="number" name="quantity" value="99"></label></form>'
    observed = static_field_inventory(html, url=SITE)
    assert len(observed["controls"]) == 1
    assert all(text not in json.dumps(observed) for text in ["secret", "private address", "billing_address", "auth_token"])
    raw = inventory(control(value="do-not-retain", password="do-not-retain"), viewport={"width": 1200, "height": 700, "private": "do-not-retain"})
    assert "do-not-retain" not in json.dumps(raw)
    assert "localStorage" not in CAPTURE_RENDERED_FIELDS_JS and "fetch(" not in CAPTURE_RENDERED_FIELDS_JS


def test_duplicate_observations_enter_audit_with_evidence_and_review_only_proposals():
    site = storefront_html_snapshot(site_html([100]), url=SITE, product_id="9532")
    site["field_inventory"] = inventory(control(), control(dom_ref="purchase-0/control-1"))
    report = compare_snapshots(source_table([100]), site, source_freshness="current_exact_quotes", structure={"status": "match"})
    assert report["checks"]["storefront_duplicate_fields"] == "mismatch"
    assert report["status"] == "mismatch"
    proposal, = [item for item in report["proposed_changes"] if item["kind"] == "review_storefront_duplicate_fields"]
    assert proposal["requires_review"] is True and proposal["automatic"] is False
    assert proposal["finding_id"] == report["field_checks"]["storefront"]["findings"][0]["id"]


def test_old_green_audit_without_field_observations_is_reassessed_without_mutating_evidence():
    record = {"status": "green", "report": {"status": "green", "checks": {"options": "match"}}}
    view = ProductCompanionService._audit_read_view(record)
    assert view["status"] == "unknown"
    assert view["report"]["checks"]["storefront_duplicate_fields"] == "unknown"
    assert view["report"]["checks"]["supplier_duplicate_fields"] == "unknown"
    assert view["read_assessment"]["reason"] == "duplicate_field_observations_missing"
    assert record == {"status": "green", "report": {"status": "green", "checks": {"options": "match"}}}


@pytest.mark.asyncio
async def test_unsupported_retail_scope_still_gets_hermes_rendered_field_capture(tmp_path, monkeypatch):
    calls = []
    class Page:
        url = SITE
        async def goto(self, url, **kwargs): calls.append(("goto", url)); return SimpleNamespace(status=200)
        async def wait_for_function(self, *args, **kwargs): pass
        async def evaluate(self, script):
            assert script == CAPTURE_RENDERED_FIELDS_JS
            calls.append(("capture", SITE))
            return inventory(control())
    class Browser:
        async def new_page(self): return Page()
        async def close(self): calls.append(("close", None))
    async def launch(**kwargs): return Browser()
    @asynccontextmanager
    async def fake_playwright(): yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setattr("playwright.async_api.async_playwright", fake_playwright)
    service = ProductCompanionService(SimpleNamespace(root=Path(tmp_path)), None)
    site = storefront_html_snapshot(site_html([100]), url=SITE, product_id="9532")
    result = await service._rendered_retail_samples({"product_id": "9532", "storefront_url": SITE}, site)
    assert result["status"] == "unknown" and result["reason"] == "unsupported_rendered_calculator_scope"
    assert result["field_inventory"]["controls"][0]["name"] == "quantity"
    assert calls == [("goto", SITE), ("capture", SITE), ("close", None)]


def test_duplicate_field_export_carries_dom_evidence_and_cannot_become_a_write_operation(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from crawler.product_companion_routes import companion_router
    site = storefront_html_snapshot(site_html([100]), url=SITE, product_id="9532")
    site["field_inventory"] = inventory(control(), control(dom_ref="purchase-0/control-1"))
    report = compare_snapshots(source_table([100]), site, source_freshness="current_exact_quotes", structure={"status": "match"})
    record = {"id": "a" * 32, "product_id": "9532", "observed_at": time.time(), "report": report}
    service = SimpleNamespace(get_audit=lambda _: record, saved_source_evidence_for_audit=lambda _: {})
    app = FastAPI()
    app.include_router(companion_router(lambda: service, lambda: True))
    response = TestClient(app).get('/api/v1/companion/audits/' + record["id"] + '/export')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('duplicate-field-review.csv').decode())))
        assert rows[0]["location"] == "storefront" and rows[0]["status"] == "mismatch"
        assert json.loads(rows[0]["dom_references"]) == ["purchase-0/control-0", "purchase-0/control-1"]
        assert rows[0]["projection_sha256"] == site["field_inventory"]["projection_sha256"]
        assert rows[0]["review_required"] == "yes" and rows[0]["automatic"] == "no"
    companion = ProductCompanionService(SimpleNamespace(root=Path(tmp_path)), None)
    with pytest.raises(ValueError, match="proposal_requires_manual_content_review"):
        companion.writes._operations(record, {}, [0])


def as_rendered_markup(html):
    """Retain static scope extraction while supplying an explicit visibility witness."""
    captured = static_field_inventory(html, url=SITE)
    captured["observation_kind"] = "rendered_dom"
    for field in captured["controls"]:
        if field["visible"] is not False:
            field["visible"] = True
    return normalize_field_inventory(captured)


@pytest.mark.parametrize("attribute", ["data-layer", "data-layer-id", "data-layer-key", "data-artwork-layer", "data-artwork-layer-id"])
def test_independent_artwork_layer_scopes_preserve_same_named_controls(attribute):
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart">'
            f'<section {attribute}="layer-a"><label>Width<input type="number" name="width"></label></section>'
            f'<section {attribute}="layer-b"><label>Width<input type="number" name="width"></label></section>'
            '</form></div></main></body>')
    observed = as_rendered_markup(html)
    assert [field["scopes"] for field in observed["controls"]] == [[attribute + ":layer-a"], [attribute + ":layer-b"]]
    result = duplicate_field_findings(observed)
    assert result["status"] == "match" and result["findings"] == []
    assert attribute in CAPTURE_RENDERED_FIELDS_JS


def test_control_itself_can_declare_its_front_back_or_layer_scope():
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart">'
            '<input type="number" name="width" data-side="front" data-layer-key="a">'
            '<input type="number" name="width" data-side="back" data-layer-key="a">'
            '</form></div></main></body>')
    observed = as_rendered_markup(html)
    assert observed["controls"][0]["scopes"] == ["data-layer-key:a", "data-side:front"]
    assert observed["controls"][1]["scopes"] == ["data-layer-key:a", "data-side:back"]
    assert duplicate_field_findings(observed)["findings"] == []


def test_same_layer_with_two_same_bound_editable_controls_is_still_a_conflict():
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart"><section data-layer-key="a">'
            '<input type="number" name="width"><input type="number" name="width">'
            '</section></form></div></main></body>')
    result = duplicate_field_findings(as_rendered_markup(html))
    assert result["status"] == "mismatch"
    assert result["findings"][0]["kind"] == "duplicate_bound_controls"
    assert result["findings"][0]["purchase_scope"]["scopes"] == ["data-layer-key:a"]
    assert result["automatic_changes_allowed"] is False


def test_explicit_artwork_layer_legends_are_legitimate_separate_scopes():
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart">'
            '<fieldset><legend>Artwork layer A</legend><input type="number" name="width"></fieldset>'
            '<fieldset><legend>Artwork layer B</legend><input type="number" name="width"></fieldset>'
            '</form></div></main></body>')
    observed = as_rendered_markup(html)
    assert [field["scopes"] for field in observed["controls"]] == [["legend:artwork layer a"], ["legend:artwork layer b"]]
    assert duplicate_field_findings(observed)["findings"] == []


def test_unclassified_active_controls_cannot_disappear_into_a_match():
    observed = inventory(control(), control("", label="Flute directionVerticalHorizontal", dom_ref="purchase-0/control-1", tag="select"),
        control("", label="H-stakesNo H-stakes1 standard H-stake per sign", dom_ref="purchase-0/control-2", tag="select"))
    result = duplicate_field_findings(observed)
    assert result["status"] == "unknown"
    assert result["reason"] == "unclassified_active_purchase_controls"
    assert result["findings"] == []
    assert {field["dom_ref"] for field in result["unclassified_active_observations"]} == {"purchase-0/control-1", "purchase-0/control-2"}
    assert result["projection_sha256"] == observed["projection_sha256"]
    assert result["automatic_changes_allowed"] is False


def test_hidden_unclassified_controls_do_not_create_a_visible_conflict():
    observed = inventory(control(), control("", label="Flute direction", visible=False, dom_ref="purchase-0/control-1"))
    result = duplicate_field_findings(observed)
    assert result["status"] == "match"
    assert result["unclassified_active_observations"] == []
    assert result["findings"] == []


def test_current_rendered_match_does_not_claim_all_conditional_contexts_or_supplier_coverage():
    result = duplicate_field_findings(inventory(control()))
    assert result["status"] == "match"
    assert result["coverage"] == {"all_conditional_states": "unknown", "other_viewports": "unknown"}
    missing = duplicate_field_findings(None)
    assert missing["status"] == "unknown"
    assert missing["coverage"]["all_conditional_states"] == "unknown"
    assert missing["automatic_changes_allowed"] is False
    site = storefront_html_snapshot(site_html([100]), url=SITE, product_id="9532")
    site["field_inventory"] = inventory(control())
    report = compare_snapshots(source_table([100]), site, source_freshness="historical_saved_table", structure={"status": "unknown"})
    assert report["field_checks"]["storefront"]["coverage"]["all_conditional_states"] == "unknown"
    assert report["checks"]["supplier_duplicate_fields"] == "unknown"
    assert report["status"] != "green"


def test_reviewed_public_control_markers_provide_binding_without_private_payload_values():
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart">'
            '<label>Flute direction<select data-pigf-flute><option>Vertical</option></select></label>'
            '<label>H-stakes<select data-pigf-stakes><option>No H-stakes</option></select></label>'
            '<label>Quantity<input data-pigf-coroplast-quantity type="number" value="999999"></label>'
            '<input type="hidden" name="pigf_nonce" value="PRIVATE-NONCE">'
            '<input type="hidden" name="pigf_flute" value="PRIVATE-PAYLOAD">'
            '</form></div></main></body>')
    observed = as_rendered_markup(html)
    assert [row["binding"] for row in observed["controls"]] == ["pigf_flute", "pigf_stakes_per_sign", "pigf_run_size"]
    assert [row["purpose"] for row in observed["controls"]] == ["pigf_flute", "pigf_stakes_per_sign", "quantity"]
    result = duplicate_field_findings(observed)
    assert result["status"] == "match" and not result["unclassified_active_observations"]
    assert all(value not in json.dumps(observed) for value in ["PRIVATE-NONCE", "PRIVATE-PAYLOAD", "999999"])


def test_duplicate_public_marker_controls_are_real_binding_conflicts_not_label_guesses():
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart">'
            '<label>Flute<select data-pigf-flute></select></label>'
            '<label>Other title<select data-pigf-flute></select></label>'
            '</form></div></main></body>')
    finding, = duplicate_field_findings(as_rendered_markup(html))["findings"]
    assert finding["kind"] == "duplicate_bound_controls" and finding["status"] == "mismatch"
    assert all(field["binding_key"] == "declared:pigf_flute" for field in finding["observations"])
    assert finding["proposed_correction"]["automatic"] is False


def test_layer_scope_payloads_are_not_exposed_or_treated_as_confirmed_binding_identity():
    html = ('<body class="postid-9532"><main><div id="product-9532"><form class="cart">'
            '<section data-layer="https://private.invalid/UPLOADED-ARTWORK"><input type="number" name="width"><input type="number" name="width"></section>'
            '</form></div></main></body>')
    observed = as_rendered_markup(html)
    assert 'UPLOADED-ARTWORK' not in json.dumps(observed)
    assert observed['controls'][0]['scopes'] == ['data-layer:unverified-layer-scope:0']
    result = duplicate_field_findings(observed)
    assert result['status'] == 'unknown'
    assert result['unverified_scope_observations']
    assert all(finding['status'] == 'unknown' for finding in result['findings'])
    assert result['automatic_changes_allowed'] is False
