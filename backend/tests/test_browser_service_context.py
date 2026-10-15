"""Observed service identity never becomes a guessed fee or traversed control."""

import json
from pathlib import Path

import pytest

from crawler.adapters.browser import SNAPSHOT_JS, make_record, normalize_snapshot


@pytest.mark.parametrize(
    "mode", ["selected", "unselected", "ambiguous", "visible", "nonproduct"]
)
@pytest.mark.asyncio
async def test_service_selection_diagnostics_preserve_raw_context_only(mode):
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")
        browser = await runtime.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("""<!doctype html><h1>Product fixture</h1>
<div id="order-form" data-printproducttype="1"><label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select></div>
<div id="subtotal">$13.00</div><div id="collapseOne" style="display:none">
<input type="radio" id="instant" name="proofType" value="31"><label for="instant">Instantly Processed</label>
<input type="radio" id="manual" name="proofType" value="100" checked><label for="manual">Manually Processed (+$3.00)</label>
<input type="radio" id="upload" name="fileDeliveryForIop" value="5" checked><label for="upload">Upload Now</label>
<input type="hidden" name="authorization" value="unrelated-private-value">
<input type="text" name="fileName" value="private-customer-file.pdf">
<input type="radio" name="unrelatedPrivateGroup" value="private-radio-value" checked>
</div>""")
            if mode == "unselected":
                await page.evaluate("document.querySelector('#manual').checked=false")
            elif mode == "ambiguous":
                await page.evaluate(
                    "Object.defineProperty(document.querySelector('#instant'),'checked',{get:()=>true})"
                )
            elif mode == "visible":
                await page.evaluate(
                    "document.querySelector('#collapseOne').style.display='' "
                )
            elif mode == "nonproduct":
                await page.evaluate(
                    "document.querySelector('#order-form').removeAttribute('data-printproducttype')"
                )
            snapshot = await page.evaluate(SNAPSHOT_JS, {"priceSelector": "#subtotal"})
        finally:
            await browser.close()
    encoded = json.dumps(snapshot)
    for private in (
        "authorization",
        "unrelated-private-value",
        "private-customer-file.pdf",
        "unrelatedPrivateGroup",
        "private-radio-value",
    ):
        assert private not in encoded
    original = snapshot["service_context"]
    state = normalize_snapshot(snapshot)
    assert state["service_context"] is not original
    assert [control["key"] for control in state["controls"]] == ["quantity"]
    if mode == "nonproduct":
        assert state["service_context"] == []
    else:
        context = {entry["key"]: entry for entry in state["service_context"]}
        proof = context["proofType"]
        assert proof["control_count"] == 2
        assert proof["checked_count"] == {"unselected": 0, "ambiguous": 2}.get(mode, 1)
        assert proof["selection_status"] == (
            mode if mode in {"unselected", "ambiguous"} else "selected"
        )
        assert all(value["checked"] is True for value in proof["selected"])
        assert all(
            value["visible"] is (mode == "visible") for value in proof["selected"]
        )
        if mode in {"selected", "visible"}:
            assert proof["selected"][0]["value"] == "100"
            assert proof["selected"][0]["label"] == "Manually Processed (+$3.00)"
        assert context["fileDeliveryForIop"]["selected"][0]["value"] == "5"
    record = make_record(
        "https://fixture.example/product",
        state,
        ["dom-evidence"],
        set(),
        {"currency": "USD"},
    )
    assert record["subtotal"] == "13.00"
    assert record["configuration"] == {}
    assert record["service_context"] == state["service_context"]
    assert record["record_schema_version"] == 2
    assert record["service_context_observed"] is (mode != "nonproduct")
    signature = record["pricing_context"]["service_context"]
    assert signature["status"] == (
        "unknown"
        if mode == "nonproduct"
        else "ambiguous" if mode == "ambiguous" else "observed"
    )
