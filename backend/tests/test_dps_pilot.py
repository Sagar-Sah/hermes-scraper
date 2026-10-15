import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from crawler.dps_pilot import (DPSError, DPSPilotManager, bind_pricing_url, money_matches,
                               option_pairs, sanitize_options, sanitize_price, sanitize_quantities,
                               selected_source_value, source_schema)


@pytest.mark.parametrize('value', [True, None, '-2.00', '(2.00)', '$2.00', 'NaN', 'Infinity', '1,000.00'])
def test_reject_unsafe_or_ambiguous_prices(value):
    with pytest.raises(DPSError):
        sanitize_price({'price': value})


def test_projection_retains_only_contract_public_data():
    raw = {'price': 12.3, 'uploadPrice': 12.3, 'designPrice': None, 'customer': 'PRIVATE_CUSTOMER'}
    assert sanitize_price(raw) == {'price': '12.3', 'uploadPrice': '12.3'}
    assert 'PRIVATE' not in json.dumps(source_schema(raw))
    assert source_schema(raw)['unknown_field_count'] == 1
    options = [{'id': 1, 'name': 'Lamination', 'private': 'SECRET', 'productOptionItemValues': [
        {'id': 3, 'name': 'Matte', 'specialType': 2, 'productOptionItems': [
            {'id': 4, 'name': 'Nested', 'productOptionItemValues': [{'id': 5, 'name': 'Yes', 'specialType': 0}]}]}]}]
    safe = sanitize_options(options)
    assert safe[0]['choices'][0]['children'][0]['choices'][0]['id'] == '5'
    assert 'SECRET' not in json.dumps(safe)


def test_quantity_domain_is_not_price_breaks_and_preserves_range():
    assert sanitize_quantities({'staticQuantities': [{'quantity': 1}, {'quantity': 10}], 'startingQuantity': 1}) == {
        'kind': 'finite', 'values': ['1', '10'], 'starting_quantity': '1'}
    assert sanitize_quantities({'staticQuantities': [], 'minimumQuantity': 1, 'maximumQuantity': 100}) == {
        'kind': 'numeric_range', 'minimum': '1', 'maximum': '100', 'enumerated': False}
    with pytest.raises(DPSError):
        sanitize_quantities({'staticQuantities': [{'quantity': 1}, {'quantity': '1.0'}]})


def test_request_binding_exact_route_keys_and_encoding():
    base = 'https://www.digital-print-solutions.com/api'
    url = base + '/pricing/77?width=4&height=4.00&quantity=1&turnaroundId=0&options=1E3C4E5'
    bound = bind_pricing_url(url, base, '77')
    assert bound == {'product_id': '77', 'width': '4', 'height': '4', 'quantity': '1', 'turnaroundId': '0', 'options': '1E3C4E5'}
    for bad in [url+'&quantity=1', url+'&account=PRIVATE', url.replace('/77?', '/78?'), url.replace('1E3C4E5', 'bad'), url.replace('/api/', '/other/')]:
        with pytest.raises(DPSError):
            bind_pricing_url(bad, base, '77')
    assert money_matches('$1,000.01', '1000.005')
    assert not money_matches('Total $2.00', '2')


def test_requested_rendered_selection_rejects_missing_or_disabled_values():
    assert option_pairs('1E3C4E5') == [('1', '3'), ('4', '5')]
    for bad in ('', '1E3C1E4', '0E3', '1E3C', '1E3CPRIVATE'):
        with pytest.raises(DPSError):
            option_pairs(bad)
    control = {'kind': 'select', 'disabled': False, 'choices': [
        {'value': '', 'disabled': False}, {'value': '5.0', 'disabled': False},
        {'value': '6.0', 'disabled': True}]}
    assert selected_source_value(control, '5') == '5.0'
    with pytest.raises(DPSError):
        selected_source_value(control, '6')


HTML = '''<!doctype html><body><h1>Low Tack Vinyl</h1>
<form><input type=hidden id=PrimaryApiPath value="https://www.digital-print-solutions.com/api">
<input type=hidden id=ProductId value=77><input type=hidden id=ProductIdField value=77>
<input type=hidden id=CustProductIdField value=77><input type=hidden name=session value=PRIVATE_SESSION>
<input value=PRIVATE_JOBNAME><div id=ProductOptionsHolder></div>
<select id=CustProductWidthHolder><option value=4.00>4 inches</option></select>
<select id=CustProductHeightHolder><option value=4.00>4 inches</option></select>
<div id=quantityContainer></div><span id=CustProductPriceHolder>Loading</span></form>
<script src=/Scripts/Frontend.Product.js></script></body>'''

SCRIPT = '''async function quote(){
 const q=document.querySelector('#CustQuantityList').value;
 const response=await fetch('/api/pricing/77?width=4&height=4&quantity='+q+'&turnaroundId=0&options=1E3');
 const data=await response.json();document.querySelector('#CustProductPriceHolder').textContent='$'+(data.price+MISMATCH).toFixed(2);
}
async function initialize(){
 await fetch('/api/products/77/options').then(r=>r.json());
 document.querySelector('#ProductOptionsHolder').innerHTML='<div class=form-group><label>Lamination</label><select data-option-id=1 id=1><option value=3>Matte</option></select></div>';
 await fetch('/api/products/77/customizable').then(r=>r.json());
 document.querySelector('#quantityContainer').innerHTML='<select id=CustQuantityList><option value=1>1</option><option value=10>10</option></select>';
 document.querySelector('#CustQuantityList').addEventListener('change',quote);await quote();
}
fetch('/account/addresses').catch(()=>{});fetch('/cart',{method:'POST',body:'PRIVATE_CART'}).catch(()=>{});
initialize();'''


class FixturePage:
    def __init__(self, page, pool):
        self.page, self.pool = page, pool

    def __getattr__(self, name):
        return getattr(self.page, name)

    async def route(self, pattern, handler):
        async def intercept(route):
            async def continued():
                request = route.request
                path = urlsplit(request.url).path
                self.pool.forwarded.append(path)
                if path == '/low-tack-vinyl':
                    await route.fulfill(status=200, content_type='text/html', body=self.pool.html)
                elif path == '/Scripts/Frontend.Product.js':
                    await route.fulfill(status=200, content_type='text/javascript', body=self.pool.script)
                elif path == '/api/products/77/options':
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps([
                        {'id': 1, 'name': 'Lamination', 'productOptionItemValues': [{'id': 3, 'name': 'Matte', 'specialType': 0}]}]))
                elif path == '/api/products/77/customizable':
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps({
                        'staticQuantities': [{'quantity': 1}, {'quantity': 10}], 'startingQuantity': 1}))
                elif path == '/api/pricing/77':
                    q = int(parse_qs(urlsplit(request.url).query)['quantity'][0])
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps({
                        'price': q * 2.5, 'uploadPrice': q * 2.5, 'designPrice': q * 2.5 + 10, 'account': 'PRIVATE_ACCOUNT'}))
                else:
                    pytest.fail('unexpected source request: '+path)
            await handler(SimpleNamespace(abort=route.abort, continue_=continued), route.request)
        await self.page.route(pattern, intercept)


class FixturePool:
    def __init__(self, context):
        self.context, self.forwarded, self.script, self.pages = context, [], SCRIPT.replace('MISMATCH', '0'), []
        self.html = HTML

    @asynccontextmanager
    async def page(self, origin, *, headless):
        assert origin == 'https://digital-print-solutions.com' and headless is False
        page = await self.context.new_page()
        self.pages.append(page)
        try:
            yield FixturePage(page, self)
        finally:
            await page.close()


@pytest_asyncio.fixture
async def runner(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(service_workers='block')
        pool = FixturePool(context)
        manager = DPSPilotManager(SimpleNamespace(root=tmp_path), pool, timeout_seconds=5)
        manager.script_sha256 = hashlib.sha256(pool.script.encode()).hexdigest()
        monkeypatch.setattr('crawler.dps_pilot.validate_url', AsyncMock())
        try:
            yield manager, pool
        finally:
            await manager.close()
            await browser.close()


async def wait_done(manager, job):
    await manager._tasks[job['id']]
    return manager.get(job['id'])


@pytest.mark.asyncio
async def test_native_quote_and_quantity_domain_are_bound_without_private_fields(runner):
    manager, pool = runner
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl', [10]))
    assert report['status'] == 'completed', report
    assert report['quote_requests'] == 2 and report['completed_count'] == 2


@pytest.mark.asyncio
async def test_one_requested_selection_requires_retained_controls_and_fresh_native_price(runner):
    manager, pool = runner
    pool.html = HTML.replace('<option value=4.00>4 inches</option>',
                             '<option value=4.00>4 inches</option><option value=5.00>5 inches</option>', 1)
    pool.html = pool.html.replace('<div id=ProductOptionsHolder></div>',
                                  '<div id=ProductOptionsHolder></div><button type=button>Upload a File</button>')
    pool.script = pool.script.replace(
        "const q=document.querySelector('#CustQuantityList').value;",
        "const q=document.querySelector('#CustQuantityList').value;const w=document.querySelector('#CustProductWidthHolder').value;").replace(
        "'/api/pricing/77?width=4&height=4&quantity='+q",
        "'/api/pricing/77?width='+w+'&height=4&quantity='+q").replace(
        "document.querySelector('#CustQuantityList').addEventListener('change',quote);await quote();",
        "document.querySelector('#CustQuantityList').addEventListener('change',quote);"
        "document.querySelector('#CustProductWidthHolder').addEventListener('change',quote);await quote();")
    manager.script_sha256 = hashlib.sha256(pool.script.encode()).hexdigest()
    expected = {'width': '5', 'height': '4', 'quantity': '10',
                'turnaroundId': '0', 'options': '1E3'}
    report = await wait_done(manager, await manager.start(
        'https://www.digital-print-solutions.com/low-tack-vinyl', expected_request=expected))
    assert report['status'] == 'completed', report.get('warnings')
    assert report['selection_verified'] is True
    assert report['observations'][-1]['context']['width'] == '5'
    assert report['observations'][-1]['context']['quantity'] == '10'
    assert report['observations'][-1]['verification'] == 'fresh_native_quote_matches_scoped_display'
    assert report['availability'] == 'rendered_purchase_step_available_cart_unverified'
    assert report['final_eligible'] is False
    assert '/cart' not in pool.forwarded


@pytest.mark.asyncio
async def test_requested_quote_without_purchase_step_remains_unavailable(runner):
    manager, _pool = runner
    expected = {'width': '4', 'height': '4', 'quantity': '10',
                'turnaroundId': '0', 'options': '1E3'}
    report = await wait_done(manager, await manager.start(
        'https://www.digital-print-solutions.com/low-tack-vinyl', expected_request=expected))
    assert report['status'] == 'attention'
    assert report['warnings'] == ['rendered_purchase_step_unavailable']
    assert report['final_eligible'] is False


@pytest.mark.asyncio
async def test_visible_upload_price_layout_is_bound_to_native_quote(runner):
    manager, pool = runner
    pool.html = HTML.replace('CustProductPriceHolder', 'custUploadPrice')
    pool.script = pool.script.replace('CustProductPriceHolder', 'custUploadPrice')
    manager.script_sha256 = hashlib.sha256(pool.script.encode()).hexdigest()
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl', [10]))
    assert report['status'] == 'completed', report
    assert report['completed_count'] == 2
    assert report['last_readiness']['price_selector'] == '#custUploadPrice'
    assert [o['price']['price'] for o in report['observations']] == ['2.5', '25']
    assert report['source_contract_verified']
    assert report['price_breaks']['status'] == 'unresolved'
    assert report['catalog_complete'] is False and report['final_eligible'] is False
    assert '/account/addresses' not in pool.forwarded and '/cart' not in pool.forwarded
    assert 'PRIVATE_' not in json.dumps(report)
    assert all(p.is_closed() for p in pool.pages)
    assert (manager.root / (report['id']+'.json')).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_mismatched_dom_price_is_held(runner):
    manager, pool = runner
    pool.script = SCRIPT.replace('MISMATCH', '3')
    manager.script_sha256 = hashlib.sha256(pool.script.encode()).hexdigest()
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl'))
    assert report['status'] == 'attention'
    assert report['warnings'] == ['dom_price_mismatch_or_unresolved']
    assert report['observations'] == []


@pytest.mark.asyncio
async def test_explicit_rendered_out_of_stock_state_blocks_quote(runner):
    manager, pool = runner
    pool.html = HTML.replace('<div id=ProductOptionsHolder></div>',
                             '<div class=stock>Temporarily out of stock</div><div id=ProductOptionsHolder></div>')
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl'))
    assert report['status'] == 'attention'
    assert report['warnings'] == ['rendered_supplier_unavailable']
    assert report['observations'] == []


@pytest.mark.asyncio
async def test_purchase_step_is_observed_without_claiming_cart_eligibility(runner):
    manager, pool = runner
    pool.html = HTML.replace('<div id=ProductOptionsHolder></div>',
                             '<div id=ProductOptionsHolder></div><button type=button>Upload a File</button>')
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl'))
    assert report['status'] == 'completed'
    assert report['observations'][0]['availability_signals'] == {
        'explicit_unavailable': False,
        'purchase_actions': [{'text': 'Upload a File', 'disabled': False}],
    }
    assert report['final_eligible'] is False


@pytest.mark.asyncio
async def test_outside_quantity_is_not_transmitted(runner):
    manager, pool = runner
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl', [999]))
    assert report['warnings'] == ['requested_quantity_outside_observed_domain']
    assert report['quote_requests'] == 1 and report['completed_count'] == 1


@pytest.mark.asyncio
async def test_quantity_auto_reset_of_other_options_is_held(runner):
    manager, pool = runner
    pool.script = SCRIPT.replace('MISMATCH', '0').replace(
        "const q=document.querySelector('#CustQuantityList').value;",
        "const q=document.querySelector('#CustQuantityList').value;if(q==='10')document.querySelector('[data-option-id]').innerHTML='<option value=4>Gloss</option>';")
    manager.script_sha256 = hashlib.sha256(pool.script.encode()).hexdigest()
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl', [10]))
    assert report['warnings'] == ['nonquantity_configuration_changed']
    assert report['completed_count'] == 1


@pytest.mark.asyncio
async def test_contract_change_and_unsafe_urls_fail_closed(runner):
    manager, pool = runner
    for url in ['https://www.digital-print-solutions.com/cart', 'https://evil.test/low-tack-vinyl',
                'https://www.digital-print-solutions.com/low-tack-vinyl?customer=PRIVATE']:
        with pytest.raises(DPSError):
            await manager.start(url)
    manager.script_sha256 = '0'*64
    report = await wait_done(manager, await manager.start('https://www.digital-print-solutions.com/low-tack-vinyl'))
    assert report['warnings'] == ['source_script_contract_changed']
    assert not report['observations']
