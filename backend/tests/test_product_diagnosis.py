"""Local intercepted Chromium only: no supplier pages, credentials or traffic."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from crawler.product_diagnosis import (
    DIAGNOSIS_JS,
    ProductDiagnosisManager,
    _loaded_bundle_contract,
    _printograph_bundle_key,
    _safe_controller_diagnostics,
    merge_dps_snapshot,
    normalize_snapshot,
)
from crawler.price_contract import SOURCE_CONTRACT


def test_printograph_bundle_verification_uses_only_loaded_static_scripts():
    assert _printograph_bundle_key("https://static.printograph.com/assets/orderForm.js?v=1") == "order_form_sha256"
    assert _printograph_bundle_key("https://static.printograph.com/assets/chunk-common.js") == "common_sha256"
    assert _printograph_bundle_key("https://other.example/assets/orderForm.js") is None
    observed = {key: {value} for key, value in SOURCE_CONTRACT.items()}
    assert _loaded_bundle_contract(observed) == SOURCE_CONTRACT
    observed["order_form_sha256"].add("different_script")
    assert _loaded_bundle_contract(observed) != SOURCE_CONTRACT


PRODUCT = """<!doctype html><main><h1>Cards</h1>
<div class="woocommerce-product-details__short-description">Printed cards.</div>
<div class="woocommerce-product-gallery"><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="Cards"></div>
<div id="order-form"><label for="size_dynamic123">Size</label>
<select id="size_dynamic123" name="size" required><option value="">Choose size</option><option value=" 101 " selected>3.5 x 2</option><option value="102" disabled>4 x 2</option></select>
<div class="form-group"><h3>Proof Processing</h3><input style="display:none" type="radio" id="instant" name="proofType" value="31" checked><label for="instant">Instantly Processed</label>
<input style="display:none" type="radio" id="manual" name="proofType" value="100"><label for="manual">Manually Processed</label></div>
<label for="qty">Quantity</label><input id="qty" name="quantity" type="number" min="1" max="1000" step="1" value="100">
<label for="coat">Coating</label><input id="coat" name="coat" type="checkbox" value="gloss" checked>
<label for="free">Notes</label><input id="free" value="PRIVATE_FREE_TEXT">
<input type="hidden" value="PRIVATE_HIDDEN">
<label for="upload">Artwork file</label><input id="upload" type="file">
<label for="news">Newsletter</label><input id="news" type="checkbox" value="PRIVATE_MARKETING">
<select id="no_label"><option value="private">Unlabelled</option></select></div>
<div class="row"><h4>Production time</h4><div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2–4 days</a><a class="turnAround disabled" data-toggle="turnaround" data-title="3" aria-disabled="true">Rush not available</a></div></div>
<form role="search"><label for="search">Search</label><input id="search" type="number" value="999"></form>
</main><footer><a href="/login">Sign in</a></footer>"""


def test_dps_calculator_projection_preserves_visible_options_and_range():
    raw = {"title": "Vinyl", "fields": [], "content": {"title": "Vinyl"},
           "session_status": "unknown"}
    calculator = {
        "width": {"id": "CustProductWidthHolder", "kind": "select", "value": "12",
                  "choices": [{"value": "12", "label": "12 in", "disabled": False}]},
        "height": {"id": "CustProductHeightHolder", "kind": "number", "value": "24",
                   "minimum": "1", "maximum": "120", "step": "1"},
        "quantity": {"id": "CustQuantityList", "kind": "number", "value": "10",
                     "minimum": "1", "maximum": "25000", "step": "1"},
        "options": [{"id": "19", "label": "Material", "value": "43",
                     "choices": [{"value": "43", "label": "White", "disabled": False},
                                 {"value": "44", "label": "Other product", "redirects": True}]}],
        "login_prompt": False,
    }
    fields = {f["label"]: f for f in normalize_snapshot(merge_dps_snapshot(raw, calculator))["fields"]}
    assert set(fields) == {"Width", "Height", "Quantity", "Material", "Product title"}
    assert fields["Quantity"]["max"] == "25000"
    assert fields["Width"]["choices"][0]["label"] == "12 in"
    assert fields["Material"]["selected_values"] == ["43"]
    assert fields["Material"]["choices"][1]["disabled"] is True


class FixturePage:
    def __init__(self, real, pool):
        self.real, self.pool = real, pool
        self.navigations = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def goto(self, *args, **kwargs):
        self.navigations += 1
        return await self.real.goto(*args, **kwargs)

    async def route(self, pattern, handler):
        async def intercepted(route):
            async def fixture_continue():
                self.pool.requests.append(route.request.url)
                response = self.pool.html.get(urlsplit(route.request.url).path, PRODUCT)
                if isinstance(response, dict):
                    await route.fulfill(**response)
                else:
                    await route.fulfill(status=200, content_type="text/html", body=response)
            proxy = SimpleNamespace(request=route.request, abort=route.abort, continue_=fixture_continue)
            await handler(proxy)
        await self.real.route(pattern, intercepted)


class FixturePool:
    def __init__(self, browser):
        self.browser = browser
        self.contexts, self.calls, self.requests, self.html = {}, [], [], {}
        self.closed = False

    @asynccontextmanager
    async def page(self, origin, *, headless=True):
        self.calls.append((origin, headless))
        if origin not in self.contexts:
            self.contexts[origin] = await self.browser.new_context(service_workers="block")
        real = await self.contexts[origin].new_page()
        wrapped = FixturePage(real, self)
        try:
            yield wrapped
        finally:
            if not real.is_closed():
                await real.close()


@pytest_asyncio.fixture
async def manager(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Local Chromium runtime unavailable")
        browser = await p.chromium.launch(headless=True)
        pool = FixturePool(browser)
        # All HTTP is intercepted above; DNS is deliberately not queried.
        monkeypatch.setattr("crawler.product_diagnosis.validate_url", AsyncMock())
        value = ProductDiagnosisManager(SimpleNamespace(root=tmp_path), pool)
        try:
            yield value, pool
        finally:
            await value.close()
            await browser.close()


async def completed(manager, url="https://www.printograph.com/products/business-cards/order"):
    record = await manager.start(url)
    assert record["status"] == "opening"
    await manager._tasks[record["id"]]
    return manager.get(record["id"])


@pytest.mark.asyncio
async def test_visible_fields_labels_raw_values_and_secret_exclusion(manager):
    m, pool = manager
    record = await completed(m)
    assert record["status"] == "ready"
    assert record["coverage"] == "current_page_only"
    assert record["supplier_id"] == "printograph"
    fields = {f["label"]: f for f in record["fields"]}
    assert set(fields) == {"Size", "Proof Processing", "Quantity", "Coating", "Production time", "Product title", "Description"}
    size = fields["Size"]
    assert size["source_name"] == "size"
    assert size["source_id"] == "size_dynamic123"
    assert size["selected_values"] == [" 101 "]
    assert size["choices"][2]["disabled"] is True
    assert size["required"] is True
    assert fields["Proof Processing"]["selected_values"] == ["31"]
    assert fields["Proof Processing"]["multiple"] is False
    assert fields["Coating"]["multiple"] is True
    assert size["multiple"] is False
    assert len(fields["Proof Processing"]["choices"]) == 2
    assert fields["Quantity"]["min"] == "1"
    assert fields["Quantity"]["max"] == "1000"
    assert fields["Quantity"]["step"] == "1"
    assert fields["Production time"]["selected_values"] == ["1"]
    assert fields["Production time"]["choices"][1]["disabled"] is True
    assert "PRIVATE_" not in json.dumps(record)
    assert "999" not in json.dumps(record)
    assert record["session_status"] == "unknown"  # Footer sign-in is no login form.
    assert pool.calls == [("https://www.printograph.com", False)]
    assert "price" not in record


@pytest.mark.asyncio
async def test_printograph_numbered_foil_radios_form_one_choice(manager):
    m, pool = manager
    foil = '<div class="form-group"><h3>Foil Colors</h3>' + ''.join(
        f'<input style="display:none" type="radio" id="raisedFoilColor{i}" name="raisedFoilColor{i}" value="{value}" {"checked" if i == 0 else ""}>'
        f'<label for="raisedFoilColor{i}">{label}</label>'
        for i, (value, label) in enumerate([('141', 'Gold'), ('142', 'Rose Gold'), ('143', 'Silver')])
    ) + '</div>'
    pool.html['/products/business-cards/order'] = PRODUCT.replace('<input type="hidden"', foil + '<input type="hidden"')
    record = await completed(m)
    fields = [field for field in record['fields'] if field['label'] == 'Foil Colors']
    assert len(fields) == 1
    assert fields[0]['source_name'] == 'raisedFoilColor'
    assert fields[0]['selected_values'] == ['141']
    assert [(choice['value'], choice['label']) for choice in fields[0]['choices']] == [
        ('141', 'Gold'), ('142', 'Rose Gold'), ('143', 'Silver')]


@pytest.mark.asyncio
async def test_printograph_custom_production_label_is_captured(manager):
    m, pool = manager
    pool.html['/products/business-cards/order'] = PRODUCT.replace(
        '<h4>Production time</h4>', '<span class="customheaderlabel">Production Time</span>')
    record = await completed(m)
    field = next(field for field in record['fields'] if field['label'] == 'Production Time')
    assert field['selected_values'] == ['1']
    assert field['choices'][1]['disabled'] is True


@pytest.mark.asyncio
async def test_observed_finish_choice_waits_for_source_turnaround_update(manager):
    m, pool = manager
    fixture = '''<label for="raisedPrint_1">Raised Print</label>
<select id="raisedPrint_1"><option value="135" selected>Raised UV</option>
<option value="138">Raised Foil</option></select>
<script>document.querySelector('#raisedPrint_1').addEventListener('change', () => {
  setTimeout(() => { document.querySelector('#radioBtn a').textContent =
    'Regular 5-7 days'; }, 250);
});</script>'''
    pool.html['/products/business-cards/order'] = PRODUCT.replace('<input type="hidden"', fixture + '<input type="hidden"')
    m._choice_limiter = SimpleNamespace(acquire=AsyncMock(return_value='permit'), release=MagicMock())
    record = await completed(m)
    finish = next(f for f in record['fields'] if f['label'] == 'Raised Print')
    result = await m.select_observed_choice(record['id'], record['revision'], finish['key'], '138')
    assert next(f for f in result['fields'] if f['label'] == 'Raised Print')['selected_values'] == ['138']
    assert '5-7 days' in next(f for f in result['fields'] if f['label'] == 'Production time')['choices'][0]['label']
    m._choice_limiter.acquire.assert_awaited_once()
    m._choice_limiter.release.assert_called_once()
    with pytest.raises(ValueError, match='diagnosis_revision_conflict'):
        await m.select_observed_choice(record['id'], record['revision'], finish['key'], '135')


@pytest.mark.asyncio
async def test_observed_choice_rejects_disabled_control_before_supplier_action(manager):
    m, pool = manager
    pool.html['/products/business-cards/order'] = PRODUCT.replace(
        '<input type="hidden"',
        '<label for="paper_choice">Paper</label><select id="paper_choice" disabled>'
        '<option value="">Choose</option><option value="10">16 pt. Premium Matte</option>'
        '</select><input type="hidden"', 1)
    m._choice_limiter = SimpleNamespace(acquire=AsyncMock(return_value='permit'), release=MagicMock())
    record = await completed(m)
    paper = next(f for f in record['fields'] if f['label'] == 'Paper')
    assert paper['disabled'] is True
    with pytest.raises(ValueError, match='observed_choice_control_disabled'):
        await m.select_observed_choice(record['id'], record['revision'], paper['key'], '10')
    m._choice_limiter.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_observed_foil_radio_choice_uses_the_rendered_group(manager):
    m, pool = manager
    foil = '<div id="foil-options"><h3>Foil Colors</h3>' + ''.join(
        f'<input type="radio" id="foil{i}" name="raisedFoilColor{i}" value="{value}"'
        f'{" checked" if i == 0 else ""}><label for="foil{i}">{label}</label>'
        for i, (value, label) in enumerate([('141', 'Gold'), ('142', 'Rose Gold'),
                                            ('143', 'Silver')])) + '''</div><script>
    for (const input of document.querySelectorAll('#foil-options input')) {
      input.addEventListener('click', () => {
        for (const other of document.querySelectorAll('#foil-options input'))
          if (other !== input) other.checked = false;
      });
    }</script>'''
    pool.html['/products/business-cards/order'] = PRODUCT.replace(
        '<input type="hidden"', foil + '<input type="hidden"', 1)
    m._choice_limiter = SimpleNamespace(acquire=AsyncMock(return_value='permit'), release=MagicMock())
    record = await completed(m)
    field = next(f for f in record['fields'] if f['label'] == 'Foil Colors')
    result = await m.select_observed_choice(record['id'], record['revision'], field['key'], '142')
    selected = next(f for f in result['fields'] if f['label'] == 'Foil Colors')['selected_values']
    assert selected == ['142']
    m._choice_limiter.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_scoped_price_requires_current_revision_and_observed_controller(manager):
    m, _ = manager
    record = await completed(m)
    result = await m.read_scoped_price(record['id'], record['revision'])
    assert result['status'] == 'unavailable'
    assert result['revision'] == record['revision']
    assert result['source_bundle_verified'] is False
    assert result['loaded_contract'] == {}
    with pytest.raises(ValueError, match='diagnosis_revision_conflict'):
        await m.read_scoped_price(record['id'], record['revision'] - 1)


def test_unsupported_controller_metadata_forwarder_excludes_unknown_private_fields():
    result = _safe_controller_diagnostics({
        "declared_type": "39", "controller_type": "39", "store_type": "broker",
        "option_names": ["coating", "paperFinish", "coating", {"email": "PRIVATE_SENTINEL"},
                         "PRIVATE_SENTINEL", "invalid-name", "x" * 65],
        "quantity": "50", "accountToken": "PRIVATE_SENTINEL", "subtotal": "123.45",
        "qualification": {"email": "PRIVATE_SENTINEL"},
    })
    assert result == {"declared_type": "39", "controller_type": "39", "store_type": "broker",
                      "option_names": ["coating", "paperFinish"]}
    assert _safe_controller_diagnostics(None) is None


@pytest.mark.asyncio
async def test_unsupported_scoped_price_awaits_loaded_hashes_without_quoting(manager, monkeypatch):
    m, pool = manager
    record = await completed(m)
    page = m._pages[record['id']]
    await page.evaluate("""() => {
        const element = document.querySelector('#order-form');
        element.setAttribute('data-printproducttype','39');
        element.setAttribute('data-currentstoretype','broker');
        const controller = {printProductType:39,fields:{39:{options:['coating','spotCoating','paperFinish']}},
          privateAccount:'PRIVATE_SENTINEL',jsonObj:{
            userSelectedOptions:{quantity:'50',email:'PRIVATE_SENTINEL'},
            appliedPrices:{productPrice:'123.45'},others:{oauth:'PRIVATE_SENTINEL'}},
          get totalPrice(){throw new Error('No unsupported price read')},
          getCouponPrice(){throw new Error('No unsupported qualification read')}};
        const child = {_: {subTree: {component: {refs: {orderFormInstance: controller}}}}};
        element._vnode = {component: {proxy: {$refs: {childComponents: child}}}};
    }""")
    monkeypatch.setattr(m, '_settle_initial', AsyncMock(return_value=True))
    observed = m._bundle_hashes[record['id']]

    async def finish_already_loaded_hashes():
        await asyncio.sleep(0.02)
        observed.update({key: {digest} for key, digest in SOURCE_CONTRACT.items()})

    pending = asyncio.create_task(finish_already_loaded_hashes())
    m._bundle_tasks[record['id']].add(pending)
    pending.add_done_callback(m._bundle_tasks[record['id']].discard)
    requests_before = list(pool.requests)
    result = await m.read_scoped_price(record['id'], record['revision'])
    assert result == {
        "status": "unavailable", "reason": "controller_product_mismatch",
        "diagnosis_id": record['id'], "revision": record['revision'],
        "source_bundle_verified": True, "loaded_contract": SOURCE_CONTRACT,
        "diagnostic_metadata": {
            "declared_type": "39", "controller_type": "39", "store_type": "broker",
            "option_names": ["coating", "spotCoating", "paperFinish"],
        },
    }
    assert pending.done()
    assert pool.requests == requests_before
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert not ({'capture', 'subtotal', 'reconciliation', 'qualification'} & result.keys())

    # A different actually loaded bundle remains visible but never verified.
    observed['order_form_sha256'] = {'0' * 64}
    unverified = await m.read_scoped_price(record['id'], record['revision'])
    assert unverified['status'] == 'unavailable'
    assert unverified['source_bundle_verified'] is False
    assert unverified['loaded_contract']['order_form_sha256'] == '0' * 64
    assert pool.requests == requests_before


@pytest.mark.asyncio
async def test_fourover_gallery_keeps_only_source_product_images(manager):
    m, pool = manager
    pool.html['/gator-board-signs'] = '''<!doctype html><main id="maincontent">
    <h1>Gator Board Signs</h1><div class="gallery-placeholder">
    <img alt="Gator Board Signs" src="https://4over.com/media/gator-a.jpg">
    <img alt="Gator Board Signs detail" src="https://cdn.4over.com/media/gator-b.jpg">
    <img alt="Untrusted" src="https://other.example/x.jpg"></div>
    <img alt="Site badge" src="https://4over.com/media/badge.jpg"></main>'''
    record = await completed(manager[0], 'https://4over.com/gator-board-signs')
    result = await m.read_observed_gallery(record['id'], record['revision'])
    assert [image['url'] for image in result['images']] == [
        'https://4over.com/media/gator-a.jpg', 'https://cdn.4over.com/media/gator-b.jpg']
    with pytest.raises(ValueError, match='diagnosis_revision_conflict'):
        await m.read_observed_gallery(record['id'], record['revision'] - 1)


@pytest.mark.asyncio
async def test_visible_price_login_gate_is_explicit_without_claiming_auth(manager):
    m, pool = manager
    pool.html['/products/business-cards/order'] = PRODUCT + '<p>Login To View Price</p>'
    record = await completed(m)
    assert record['pricing_access'] == 'login_required'
    assert 'supplier_price_login_required' in record['warnings']
    assert record['session_status'] == 'login_required'
    assert record['fields']


@pytest.mark.asyncio
async def test_wsdisplay_public_description_does_not_hide_supplier_login_blocker(manager):
    m, pool = manager
    pool.html['/ONE-CHOICE-10-ft.-Circle-Hanging-Banner'] = '''<!doctype html><main>
    <article><h1>ONE CHOICE 10 ft. Circle Hanging Banner</h1>
    <p>Please log in to see price or purchase this item</p>
    <div class="product-description"><p>Includes a frame, graphic and carry bag.</p></div>
    </article></main>'''
    record = await completed(m, 'https://www.wsdisplay.com/ONE-CHOICE-10-ft.-Circle-Hanging-Banner')
    assert record['status'] == 'login_required'
    assert record['pricing_access'] == 'login_required'
    assert record['title'] == 'ONE CHOICE 10 ft. Circle Hanging Banner'
    assert 'supplier_price_login_required' in record['warnings']
    assert 'no_visible_configurable_controls' not in record['warnings']


@pytest.mark.asyncio
async def test_refresh_keeps_page_revision_and_clears_stale_selection(manager):
    m, _ = manager
    first = await completed(m)
    identifier = first["id"]
    page = m._pages[identifier]
    saved = await m.save_selection(identifier, first["revision"], {"fields": [first["fields"][0]["key"]]})
    same = await m.refresh(identifier)
    assert same["revision"] == first["revision"]
    assert same["selection"] == saved["selection"]
    await page.locator("#qty").fill("250")
    changed = await m.refresh(identifier)
    assert changed["revision"] == first["revision"] + 1
    assert changed["selection"] is None
    assert "selection_cleared_after_page_change" in changed["warnings"]
    assert page.navigations == 1
    with pytest.raises(ValueError, match="diagnosis_revision_conflict"):
        await m.save_selection(identifier, first["revision"], {})
    on_disk = json.loads((m.root / f"{identifier}.json").read_text())
    assert on_disk == changed
    assert not list(m.root.glob("*.tmp"))


@pytest.mark.asyncio
async def test_login_explicit_signals_and_same_profile_reuse(manager):
    m, pool = manager
    pool.html["/user/login"] = '<main><h1>Sign in</h1><form><input type="password" value="SECRET"><input type="text" value="EMAIL"></form></main>'
    assert m.suppliers()[0]["session_status"] == "not_open"
    login = await m.open_supplier("printograph")
    assert login["session_status"] == "login_required"
    login_page = m._pages["supplier:printograph"]
    await login_page.real.set_content('<main><h1>Account</h1><a href="/user/logout">Log out</a></main>')
    assert (await m.check_supplier("printograph"))["session_status"] == "signed_in"
    assert (await m.open_supplier("printograph"))["session_status"] == "signed_in"
    assert login_page.navigations == 1
    row = await completed(m)
    assert m._pages[row["id"]].context is login_page.context
    assert len(pool.contexts) == 1
    assert "SECRET" not in json.dumps(m.suppliers())
    await m.close()
    assert login_page.is_closed()
    assert len(pool.contexts["https://www.printograph.com"].pages) == 0
    assert not pool.closed


@pytest.mark.asyncio
async def test_closed_page_and_restart_preserve_snapshot_without_reopening(manager):
    m, pool = manager
    row = await completed(m)
    await m._pages[row["id"]].close()
    assert m.get(row["id"])["status"] == "closed"
    assert (await m.refresh(row["id"]))["session_status"] == "closed"
    reopened = ProductDiagnosisManager(m.store, pool)
    try:
        restored = reopened.get(row["id"])
        assert restored["status"] == "closed"
        assert restored["fields"] == row["fields"]
        assert reopened.list()[0]["id"] == row["id"]
        assert "page_not_open" in restored["warnings"]
        assert len(pool.calls) == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_unknown_supplier_public_urls_and_navigation_guards(manager):
    m, pool = manager
    row = await completed(m, "https://fixture.example/items/card?size=1")
    assert row["supplier_id"] is None
    assert "?" not in row["url"]
    assert row["status"] == "ready"
    page = m._pages[row["id"]]
    with pytest.raises(Exception):
        await page.goto("https://elsewhere.example/account")
    assert "https://elsewhere.example/account" not in pool.requests
    for target in ("http://fixture.example/item", "https://fixture.example/cart", "https://fixture.example/checkout", "https://fixture.example/order", "https://fixture.example/orders/1", "https://fixture.example/wp-admin", "https://fixture.example/item?token=secret", "https://user:secret@fixture.example/item"):
        with pytest.raises(ValueError):
            await m.start(target)
    with pytest.raises(ValueError):
        await m.open_supplier("unknown")
    with pytest.raises(ValueError):
        m.get("../../secrets")


@pytest.mark.asyncio
async def test_redirect_to_login_can_refresh_after_user_signin(manager):
    m, pool = manager
    pool.html["/products/private/order"] = {"status":302,"headers":{"location":"/user/login"}}
    pool.html["/user/login"] = '<main><h1>Sign in</h1><form><input type="password"></form></main>'
    row = await completed(m, "https://www.printograph.com/products/private/order")
    assert row["status"] == "login_required"
    page = m._pages[row["id"]]
    await page.goto("https://www.printograph.com/products/cards/order")
    refreshed = await m.refresh(row["id"])
    assert refreshed["status"] == "ready"
    assert refreshed["revision"] > row["revision"]


@pytest.mark.asyncio
async def test_errors_do_not_persist_exception_payloads(manager, monkeypatch):
    m, pool = manager
    def fail(*args, **kwargs):
        raise RuntimeError("token=DO_NOT_SAVE password=DO_NOT_SAVE")
    monkeypatch.setattr(pool, "page", fail)
    row = await completed(m)
    assert row["status"] == "attention"
    assert row["warnings"] == ["page_unavailable"]
    assert "DO_NOT_SAVE" not in (m.root / f"{row['id']}.json").read_text()


def test_normalization_bounds_numeric_and_raw_identity():
    raw = {"fields": [{"kind":"number","label":" Width ","source_key":"raw id ","source_id":"raw id ","source_name":"", "selected_values":["secret","1e9999","12.5"],"min":"0","max":"secret","step":"any"},
                       {"kind":"select","label":"Too long","source_id":"x" * 2049}],
           "content":{"images":[{"url":"https://cdn.example/image.png?token=secret#secret","alt":" Image "}]}}
    normalized = normalize_snapshot(raw)
    field = normalized["fields"][0]
    assert field["source_id"] == "raw id "
    assert field["selected_values"] == ["12.5"]
    assert field["max"] is None
    assert field["step"] == "any"
    assert normalized["content"]["images"][0]["url"] == "https://cdn.example/image.png"
    assert "diagnosis_limits_reached" in normalized["warnings"]
    assert "Too long" not in json.dumps(normalized)


@pytest.mark.asyncio
async def test_initial_scan_waits_for_delayed_controls_and_preserves_multiselect(manager):
    m, pool = manager
    pool.html["/delayed"] = '''<main><h1>Delayed cards</h1><div id="order-form" aria-busy="true"></div></main>
    <script>setTimeout(()=>{
      const form=document.querySelector('#order-form');
      form.innerHTML='<label for="colors">Ink colors</label><select id="colors" name="colors" multiple><option value="cyan" selected>Cyan</option><option value="magenta" selected>Magenta</option><option value="yellow">Yellow</option></select>';
      form.removeAttribute('aria-busy');
    },100)</script>'''
    record = await completed(m, "https://fixture.example/delayed")
    assert record["status"] == "ready"
    colors = next(field for field in record["fields"] if field["label"] == "Ink colors")
    assert colors["multiple"] is True
    assert colors["selected_values"] == ["cyan", "magenta"]
    assert len(colors["choices"]) == 3
    assert "initial_snapshot_not_settled" not in record["warnings"]
    page = m._pages[record["id"]]
    assert page.navigations == 1
    assert pool.requests == ["https://fixture.example/delayed"]
    # Refresh is a current-page read, not a second initial readiness wait.
    m._settle_initial = AsyncMock(side_effect=AssertionError("Refresh must not settle again"))
    again = await m.refresh(record["id"])
    assert again["revision"] == record["revision"]
    m._settle_initial.assert_not_called()
