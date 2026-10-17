"""Snapshot read caches must never outlive one ordinary DOM observation."""

from pathlib import Path

import pytest

from crawler.adapters.browser import SNAPSHOT_JS, normalize_snapshot


@pytest.fixture
async def snapshot_page():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")
        browser = await runtime.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_snapshot_selector_paths_distinguish_siblings_and_shared_ancestors(snapshot_page):
    page = snapshot_page
    await page.set_content("""<!doctype html><h1>Fixture</h1><div id="order-form">
<div><span>Not a field</span><label><input type="radio" name="paper" value="matte" checked>Matte</label>
<label><input type="radio" name="paper" value="gloss">Gloss</label><span>Also not a field</span>
<div><label>Quantity<select name="quantity"><option value="100">100</option></select></label></div></div>
<div><label>Width<input type="number" name="width" value="12" min="1" max="48"></label></div>
</div><div id="subtotal">$12.00</div>""")
    first = normalize_snapshot(await page.evaluate(SNAPSHOT_JS, {"priceSelector": "#subtotal"}))
    assert first["price"] == "$12.00"
    assert {control["key"] for control in first["controls"]} == {"paper", "quantity", "width"}
    for control in first["controls"]:
        assert await page.locator(control["selector"]).count() == 1
        for choice in control["choices"]:
            if choice.get("selector"):
                element = page.locator(choice["selector"])
                assert await element.count() == 1
                assert await element.input_value() == choice["value"]
    paper = next(control for control in first["controls"] if control["key"] == "paper")
    assert paper["choices"][0]["selector"] == "#order-form > div:nth-of-type(1) > label:nth-of-type(1) > input:nth-of-type(1)"
    assert paper["choices"][1]["selector"] == "#order-form > div:nth-of-type(1) > label:nth-of-type(2) > input:nth-of-type(1)"
    assert paper["choices"][1]["label_selector"] == "#order-form > div:nth-of-type(1) > label:nth-of-type(2)"


@pytest.mark.asyncio
async def test_next_snapshot_reads_dynamic_visibility_labels_price_and_structure(snapshot_page):
    page = snapshot_page
    await page.set_content("""<!doctype html><h1>Fixture</h1><div id="order-form">
<div><label><input type="radio" name="paper" value="matte" checked>Matte</label>
<label><input type="radio" name="paper" value="gloss">Gloss</label></div>
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select>
</div><div id="subtotal">$12.00</div>""")
    first = normalize_snapshot(await page.evaluate(SNAPSHOT_JS, {"priceSelector": "#subtotal"}))
    await page.evaluate("""() => {
      const labels=document.querySelectorAll('#order-form > div label');
      labels[0].style.display='none';
      labels[1].lastChild.textContent='Gloss updated';
      labels[1].querySelector('input').checked=true;
      const before=document.createElement('label');before.textContent='New unrelated label';labels[1].before(before);
      document.querySelector('#quantity').innerHTML='<option value="250">250</option>';
      document.querySelector('#subtotal').textContent='$22.50';
    }""")
    second = normalize_snapshot(await page.evaluate(SNAPSHOT_JS, {"priceSelector": "#subtotal"}))
    assert first["price"] == "$12.00" and second["price"] == "$22.50"
    paper = next(control for control in second["controls"] if control["key"] == "paper")
    assert paper["selected_label"] == "Gloss updated"
    assert paper["choices"] == [{
        "value": "gloss", "label": "Gloss updated",
        "selector": "#order-form > div:nth-of-type(1) > label:nth-of-type(3) > input:nth-of-type(1)",
        "label_selector": "#order-form > div:nth-of-type(1) > label:nth-of-type(3)",
    }]
    assert next(control for control in second["controls"] if control["key"] == "quantity")["value"] == "250"


@pytest.mark.asyncio
async def test_snapshot_preserves_login_and_challenge_text_without_article(snapshot_page):
    page = snapshot_page
    await page.set_content("""<!doctype html><title>Fixture</title><h1>Fixture</h1>
<div>Please log in to see the prices.</div>""")
    state = await page.evaluate(SNAPSHOT_JS, {"priceSelector": None})
    assert state["login"] is True and state["challenge"] is False
    await page.set_content("""<!doctype html><title>Fixture</title><h1>Fixture</h1>
<div>Verify you are human</div>""")
    state = await page.evaluate(SNAPSHOT_JS, {"priceSelector": None})
    assert state["challenge"] is True and state["login"] is False
