"""Campaign display uses local intercepted evidence only, never supplier traffic."""
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'


@pytest.mark.asyncio
async def test_campaign_inventory_stages_stale_polling_and_fixture_exclusion():
    playwright = pytest.importorskip('playwright.async_api')
    if not (DIST / 'index.html').exists():
        pytest.skip('Build frontend first')
    now = '2026-09-26T14:00:00Z'
    run = {'id': 'real-run', 'name': 'Supplier harvest', 'status': 'paused', 'created_at': now, 'updated_at': now,
           'total_products': 3, 'completed_products': 2, 'row_count': 15, 'request_count': 4,
           'cache_hits': 0, 'error_count': 0, 'coverage_status': 'partial', 'adapter': 'browser'}
    fixtures = [dict(run, id='fixture-flag', completed_products=100, fixture=True),
                dict(run, id='fixture-adapter', completed_products=102, adapter='fixture')]
    stages = ['queued', 'diagnosed', 'extracting', 'extracted', 'verified', 'finalized', 'blocked']
    products = [{'id': str(index + 1), 'title': f'Catalog product {index + 1}',
                 'storefront_url': f'https://shop.example/product-{index + 1}',
                 'supplier_url': f'https://supplier.example/product-{index + 1}' if index < 7 else None,
                 'supplier': 'B2Sign' if index < 7 else None, 'stage': stages[index] if index < 7 else 'queued',
                 'reason': 'Supplier adapter support required' if index == 6 else None,
                 'diagnosis_id': 'diagnosis' if index == 1 else None, 'run_id': 'real-run' if index == 2 else None,
                 'product_id': 'product' if index == 2 else None, 'finalization_id': 'a' * 32 if index == 5 else None,
                 'row_count': 13 if index == 2 else 0, 'updated_at': now} for index in range(89)]
    campaign = {'id': 'catalog89', 'name': '89 product repair', 'status': 'running', 'total_products': 89,
                'counts': {'diagnosed': 5, 'extracted': 3, 'verified': 2, 'finalized': 1, 'blocked': 1},
                'source_pages_collected': 1, 'updated_at': now, 'products': products}
    products[2].update(supplier_url='https://www.b2sign.com/pole-banner-set', source_collection_id='b' * 32, source_collection_status='page_collected')
    harvests = [
        {'id': 'pricing-active', 'title': 'Pole Banner finite set', 'url': 'https://www.b2sign.com/pole-banner-set', 'status': 'running',
         'requested_count': 10, 'completed_count': 2, 'row_count': 2, 'invalid_count': 0, 'quote_requests': 2,
         'finite_requested_set_exhausted': False, 'catalog_complete': False, 'verification_status': 'pending_independent_canaries'},
        {'id': 'pricing-complete', 'title': 'Earlier finite set', 'url': 'https://b2sign.com/pole-banner-set/', 'status': 'completed',
         'requested_count': 4, 'completed_count': 4, 'row_count': 4, 'invalid_count': 0, 'quote_requests': 4,
         'finite_requested_set_exhausted': True, 'catalog_complete': False, 'verification_status': 'pending_independent_canaries'},
        {'id': 'unrelated', 'title': 'Unrelated source excluded', 'url': 'https://www.b2sign.com/unrelated', 'status': 'running',
         'requested_count': 999, 'completed_count': 999, 'row_count': 999, 'invalid_count': 0, 'quote_requests': 999,
         'finite_requested_set_exhausted': True, 'catalog_complete': False, 'verification_status': 'pending'},
    ]
    state = {'mode': 'data', 'fail_detail': False, 'harvest_fail': False, 'harvest_reads': 0, 'detail_reads': 0, 'paths': []}
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip('Chromium not installed')
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1050})
        await page.clock.install()
        await page.add_init_script("sessionStorage.setItem('atlas_api_token','fixture-token')")

        async def intercept(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.hostname != 'campaign.fixture':
                assert parsed.hostname not in {'supplier.example', 'shop.example'}, 'Campaign view must not visit suppliers'
                return await route.abort()  # Existing optional web fonts are blocked too.
            path = parsed.path
            state['paths'].append(path)
            assert request.method == 'GET', 'Campaign page is read-only'
            if path.startswith('/api/'):
                assert request.headers.get('authorization') == 'Bearer fixture-token'
            if path == '/api/v1/health':
                result = {'authentication_required': False}
            elif path == '/api/v1/runs':
                result = {'items': [run, *fixtures]}
            elif path == '/api/v1/campaigns':
                if state['mode'] == 'unavailable':
                    return await route.fulfill(status=404, json={'detail': 'Campaign route not deployed'})
                result = {'items': [] if state['mode'] == 'empty' else [{key: value for key, value in campaign.items() if key != 'products'}]}
            elif path == '/api/v1/campaigns/catalog89':
                state['detail_reads'] += 1
                if state['fail_detail']:
                    return await route.fulfill(status=503, json={'detail': 'Campaign API temporarily unavailable'})
                result = campaign
            elif path == '/api/v1/finalizations/' + 'a' * 32 + '/report':
                return await route.fulfill(json={'examined_rows': 10, 'eligible_rows': 2, 'held_rows': 8, 'catalog_complete': False})
            elif path == '/api/v1/source-collections/' + 'b' * 32:
                return await route.fulfill(json={'status': 'completed', 'catalog_complete': False, 'mapping_verified': False})
            elif path == '/api/v1/b2sign-harvests':
                state['harvest_reads'] += 1
                if state['harvest_fail']:
                    return await route.fulfill(status=503, json={'detail': 'Pricing harvest temporarily unavailable'})
                result = {'items': harvests}
            elif path == '/api/v1/b2sign-harvests/pricing-active':
                return await route.fulfill(json=harvests[0])
            elif path.startswith('/api/'):
                pytest.fail(f'Unexpected campaign request: {path}')
            else:
                target = DIST / (path.lstrip('/') or 'index.html')
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    return await route.fulfill(body=target.read_bytes(), content_type='text/javascript' if target.suffix == '.js' else 'text/css' if target.suffix == '.css' else 'text/html')
                return await route.fulfill(status=404, body='Not found')
            await route.fulfill(json=result)

        await page.route('**/*', intercept)
        await page.goto('https://campaign.fixture/')
        metric = page.locator('.metric').filter(has=page.get_by_text('Products complete', exact=True))
        await playwright.expect(metric.locator('.metric-value')).to_have_text('2')
        await playwright.expect(metric).to_contain_text('Fixtures, partial and stopped attempts excluded')
        await page.get_by_role('button', name='Campaigns', exact=True).click()
        region = page.get_by_role('region', name='Campaign products', exact=True)
        rows = region.get_by_role('row')
        await playwright.expect(rows).to_have_count(90)
        counts = page.get_by_label('Campaign counts', exact=True)
        await playwright.expect(counts).to_contain_text('Diagnosed5 / 89')
        await playwright.expect(counts).to_contain_text('Finalized1 / 89')
        await playwright.expect(page.get_by_text('89 shown · 89 recorded entries · 89 planned products', exact=True)).to_be_visible()
        await playwright.expect(page.get_by_text('Extraction rows, verified prices, and finalized output are separate stages.', exact=False)).to_be_visible()
        await playwright.expect(page.get_by_text('1 / 89 supplier pages collected', exact=True)).to_be_visible()
        pricing = page.get_by_role('region', name='B2Sign pricing harvests', exact=True)
        await playwright.expect(pricing.get_by_role('article')).to_have_count(2)
        await playwright.expect(pricing.get_by_text('Unrelated source excluded', exact=True)).to_have_count(0)
        finite = pricing.get_by_role('article', name='Pole Banner finite set', exact=True)
        await playwright.expect(finite).to_contain_text('Requested configurations10')
        await playwright.expect(finite).to_contain_text('Quoted rows · held for verification2')
        await playwright.expect(pricing.get_by_text('Requested set processed', exact=True)).to_be_visible()
        await playwright.expect(pricing.get_by_text('Whole-product coverage and finalization are not established.', exact=False).first).to_be_visible()
        async with page.expect_download() as harvest_download:
            await finite.get_by_role('button', name='Pricing harvest report', exact=True).click()
        assert (await harvest_download.value).suggested_filename == 'hermes-b2-pricing-harvest.json'
        await page.get_by_label('Filter campaign stage', exact=True).select_option('blocked')
        await playwright.expect(rows).to_have_count(2)
        await playwright.expect(region.get_by_text('Supplier adapter support required', exact=True)).to_be_visible()
        await page.get_by_label('Filter campaign stage', exact=True).select_option('all')
        await page.get_by_label('Search campaign products', exact=True).fill('Catalog product 3')
        await playwright.expect(rows).to_have_count(12)  # Product 3 and 30–39.
        product_row = region.get_by_role('row').filter(has=page.get_by_text('Catalog product 3', exact=True))
        await playwright.expect(product_row.get_by_role('button', name='View harvest', exact=True)).to_be_enabled()
        await playwright.expect(product_row.get_by_role('link', name='View supplier page', exact=True)).to_have_attribute('href', 'https://www.b2sign.com/pole-banner-set')
        async with page.expect_download() as source_download:
            await product_row.get_by_role('button', name='Source page evidence', exact=True).click()
        assert (await source_download.value).suggested_filename == 'hermes-source-collection.json'
        await page.get_by_label('Search campaign products', exact=True).fill('')
        async with page.expect_download() as pending_download:
            await region.get_by_role('button', name='Finalization report', exact=True).click()
        assert (await pending_download.value).suggested_filename == 'hermes-finalization-report.json'
        # Explicit scope correction keeps both records, but removes them from active totals.
        products[87].update(stage='excluded', reason='Existing hero DTF product; user excluded this entry.',
                            exclusion_reason='Existing hero DTF product; user excluded this entry.',
                            supplier='B2Sign',supplier_url='https://www.b2sign.com/unrelated')
        products[88].update(stage='excluded', reason='Sample kits are outside this project.',
                            exclusion_reason='Sample kits are outside this project.')
        campaign.update(active_products=87,excluded_products=2,original_total_products=89)
        await page.get_by_role('button', name='Refresh campaigns', exact=True).click()
        await playwright.expect(counts).to_contain_text('Diagnosed5 / 87')
        await playwright.expect(page.get_by_text('87 active products · 89 original entries · 2 excluded by request.', exact=False)).to_be_visible()
        await playwright.expect(page.get_by_text('1 / 87 supplier pages collected', exact=True)).to_be_visible()
        await playwright.expect(rows).to_have_count(90)
        await playwright.expect(pricing.get_by_text('Unrelated source excluded', exact=True)).to_have_count(0)
        await page.get_by_label('Filter campaign stage', exact=True).select_option('excluded')
        await playwright.expect(rows).to_have_count(3)
        await playwright.expect(region.get_by_text('Sample kits are outside this project.', exact=True)).to_be_visible()
        await page.get_by_label('Filter campaign stage', exact=True).select_option('all')
        state['fail_detail'] = True
        state['harvest_fail'] = True
        await page.clock.fast_forward(5100)
        await playwright.expect(page.get_by_text('Live campaign updates unavailable · showing last-known data', exact=True)).to_be_visible()
        await playwright.expect(page.locator('.campaign-select').get_by_text('Last known: Running', exact=True)).to_be_visible()
        await playwright.expect(pricing.get_by_text('Pricing progress is last known; live updates are unavailable.', exact=False)).to_be_visible()
        await playwright.expect(finite.get_by_text('Last known: Running', exact=True)).to_be_visible()
        await playwright.expect(rows).to_have_count(90)
        await playwright.expect(page.get_by_label('Last-known campaign counts', exact=True)).to_contain_text('Finalized1 / 87')
        state['fail_detail'] = False
        state['harvest_fail'] = False
        harvests[0].update(status='completed', completed_count=10, row_count=9, invalid_count=1, finite_requested_set_exhausted=True)
        campaign['status'] = 'paused'
        await page.clock.fast_forward(5100)
        await playwright.expect(page.locator('.campaign-select').get_by_text('Paused', exact=True)).to_be_visible()
        await playwright.expect(finite).to_contain_text('Quoted rows · held for verification9')
        await playwright.expect(finite).to_contain_text('Unavailable responses1')
        await playwright.expect(page.get_by_label('Campaign counts', exact=True)).to_contain_text('Finalized1 / 87')
        reads = state['detail_reads']
        harvest_reads = state['harvest_reads']
        await page.clock.fast_forward(16000)
        assert state['detail_reads'] == reads, 'Inactive campaign must stop polling'
        assert state['harvest_reads'] == harvest_reads, 'Finished matching harvests stop polling even if unrelated jobs run'
        await page.set_viewport_size({'width': 390, 'height': 844})
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert not any('/pricing-pilots' in path for path in state['paths'])
        # Reloading has no dependency on browser-local campaign receipts.
        await page.reload()
        await page.get_by_role('button', name='Open navigation', exact=True).click()
        await page.get_by_role('button', name='Campaigns', exact=True).click()
        await playwright.expect(page.get_by_role('region', name='Campaign products', exact=True).get_by_role('row')).to_have_count(90)
        state['mode'] = 'empty'
        await page.get_by_role('button', name='Refresh campaigns', exact=True).click()
        await playwright.expect(page.get_by_text('No campaigns recorded yet', exact=True)).to_be_visible()
        state['mode'] = 'unavailable'
        await page.get_by_role('button', name='Refresh campaigns', exact=True).click()
        await playwright.expect(page.get_by_text('Campaign service unavailable', exact=True)).to_be_visible()
        await browser.close()
