"""Finite owned-session quote reads against a wholly intercepted local fixture."""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from crawler.b2sign_catalog import enumerate_catalog
from crawler.b2sign_harvest import (
    B2SignHarvestManager, HarvestError, SharedQuoteGate, make_payload,
    prepare_candidate, validate_observed_request, quote_only_candidates, bootstrap_open_size_match,
    display_only_contract_change,
    COMPATIBLE_RUNTIME_BUNDLES, verified_runtime_bundle,
)
from test_b2sign_catalog import catalog
from test_b2sign_pilot import PilotPool, response, diagnosis


def test_runtime_bundle_requires_reviewed_url_and_exact_bytes(monkeypatch):
    url = "https://www.b2sign.com/build/assets/app-bf29bac0.js"
    monkeypatch.setitem(COMPATIBLE_RUNTIME_BUNDLES, url, hashlib.sha256(b"reviewed").hexdigest())
    assert verified_runtime_bundle(url, b"reviewed")
    assert not verified_runtime_bundle(url, b"different")
    assert not verified_runtime_bundle("https://www.b2sign.com/build/assets/app-new.js", b"reviewed")


def candidates(count=3):
    cat = catalog()
    # Only an explicit quantity1, regular-v1 scope; no clock-dependent rush.
    iterator = enumerate_catalog(cat, quantities=[1])
    return cat, [next(iterator) for _ in range(count)]


def test_prepare_generic_values_and_exact_request_shape_no_price_independence():
    cat, rows = candidates()
    safe = prepare_candidate(cat, rows[0])
    payload = make_payload(cat, safe)
    observed = validate_observed_request(payload, cat, safe)
    assert payload["id"] == 31 and payload["values"]["_qty"] == "1"
    assert observed["configuration_bound"] is True
    assert observed["job"]["subjobs"] == [{"name": "", "quantity": "1"}]
    assert observed["job"]["shipping"] == ""
    assert safe["email_proof"] is False
    assert "price" not in safe
    other = prepare_candidate(cat, rows[1])
    assert other["candidate_id"] != safe["candidate_id"]


def test_only_hide_sqft_presentation_change_is_compatible():
    saved = catalog()
    live = copy.deepcopy(saved)
    live['contract']['display_metadata'] = {'hide_sqft': True}
    live['contract_sha256'] = 'changed'
    assert display_only_contract_change(saved, live)
    live['contract']['attributes'][0]['label'] = 'changed pricing control'
    assert not display_only_contract_change(saved, live)


@pytest.mark.parametrize("change", [
    lambda row: row.update(source_product_id="32"),
    lambda row: row.update(quantity=True),
    lambda row: row.update(contract_sha256="unknown"),
    lambda row: row["values"].update(material="unknown"),
    lambda row: row["values"].pop("hem"),
    lambda row: row.update(turnaround="v0"),  # unavailable without live source-clock context
])
def test_arbitrary_or_incomplete_candidates_rejected(change):
    cat, rows = candidates(1)
    row = copy.deepcopy(rows[0])
    change(row)
    with pytest.raises(HarvestError):
        prepare_candidate(cat, row)


def test_quote_guard_refuses_addresses_changed_jobs_and_unexpected_values():
    cat, rows = candidates(1)
    candidate = prepare_candidate(cat, rows[0])
    for change in [lambda p: p["values"].update(unknown="PRIVATE"),
                   lambda p: p["jobs"][0].update(shipping_to_id=100),
                   lambda p: p["jobs"][0].update(shipping="storepickup"),
                   lambda p: p["jobs"][0]["subjobs"][0].update(name="PRIVATE_JOB"),
                   lambda p: p["jobs"][0].update(turnaround="v0")]:
        payload = make_payload(cat, candidate)
        payload["jobs"] = json.loads(payload["jobs"])
        change(payload)
        with pytest.raises(HarvestError):
            validate_observed_request(payload, cat, candidate)


def test_bootstrap_allows_source_default_production_without_binding_a_quote():
    cat, rows = candidates(1)
    candidate = prepare_candidate(cat, rows[0])
    cat['production_rules'][0]['options'].append({'key': 'same-day', 'label': 'Same Day'})
    payload = make_payload(cat, candidate)
    payload['jobs'] = json.loads(payload['jobs'])
    payload['jobs'][0]['turnaround'] = 'same-day'
    assert validate_observed_request(payload, cat, candidate, allow_initializing=True)['configuration_bound'] is False
    with pytest.raises(HarvestError, match='turnaround_mismatch'):
        validate_observed_request(payload, cat, candidate)
    payload['jobs'][0]['turnaround'] = 'unrecognized'
    with pytest.raises(HarvestError, match='turnaround_mismatch'):
        validate_observed_request(payload, cat, candidate, allow_initializing=True)


def test_native_hardware_without_production_field_does_not_invent_a_turnaround():
    from crawler.b2sign_catalog import parse_public_product, PINNED_BUNDLE_SHA256
    cat=parse_public_product({'id':139,'title':'LED Light','url':'/led-light',
        'attrs':{'attrs':[],'designproof':False}},source_url='https://www.b2sign.com/led-light',
        source_bundle_sha256=PINNED_BUNDLE_SHA256)
    row=prepare_candidate(cat,quote_only_candidates(cat,[1])[0])
    request=make_payload(cat,row)
    assert row['turnaround'] is None and row['production_label'] is None
    assert row['production_status']=='not_exposed_no_lead_time_claim'
    assert 'turnaround' not in json.loads(request['jobs'])[0]
    assert validate_observed_request(request,cat,row)['configuration_bound'] is True
    invented=copy.deepcopy(row);invented['turnaround']='v1'
    with pytest.raises(HarvestError):prepare_candidate(cat,invented)
    normal=catalog()
    with pytest.raises(HarvestError):quote_only_candidates(normal,[1])


def test_explicit_open_size_can_be_quoted_only_within_source_bounds():
    from crawler.b2sign_catalog import parse_public_product, PINNED_BUNDLE_SHA256
    cat = parse_public_product({'id': 901, 'title': 'Open Size', 'url': '/open-size',
        'attrs': {'attrs': [{'key': '_size', 'label': 'Size', 'component': 'size',
            'options': [{'key': 'width', 'label': 'Width'}, {'key': 'height', 'label': 'Height'}],
            'component_props': {'min': [{'match': [], 'value': '6x6'}],
                                'max': [{'match': [], 'value': '48x36'}]}}],
            'turnaround': [{'match': [{'key': '_qty', 'op': '>', 'value': 0}],
                            'options': [{'key': 'v1', 'label': 'Next Day'}]}]}},
        source_url='https://www.b2sign.com/open-size', source_bundle_sha256=PINNED_BUNDLE_SHA256)
    rows = list(enumerate_catalog(cat, quantities=[1], explicit_sizes={'_size': ['24x36']}))
    assert len(rows) == 1
    safe = prepare_candidate(cat, rows[0])
    assert safe['values']['_size'] == '24x36'
    hundredths = copy.deepcopy(rows[0]); hundredths['values']['_size'] = '24.12x36.12'
    assert prepare_candidate(cat, hundredths)['values']['_size'] == '24.12x36.12'
    finer_than_source = copy.deepcopy(rows[0]); finer_than_source['values']['_size'] = '24.125x36.125'
    with pytest.raises(HarvestError, match='size_value_unavailable'):
        prepare_candidate(cat, finer_than_source)
    outside = copy.deepcopy(rows[0]); outside['values']['_size'] = '60x40'
    with pytest.raises(HarvestError, match='size_outside_bound'):
        prepare_candidate(cat, outside)


def test_native_open_size_must_match_requested_dimensions_before_saving_bootstrap_quote():
    cat = {'contract': {'attributes': [{'key': '_size', 'component': 'size',
        'options': [{'key': 'width'}, {'key': 'height'}]}]}}
    candidate = {'values': {'_size': '24.10x36.12'}}
    assert bootstrap_open_size_match(cat, candidate, {'size_inputs': ['24.1', '36.12']}) is True
    assert bootstrap_open_size_match(cat, candidate, {'size_inputs': ['24.11', '36.12']}) is False
    assert bootstrap_open_size_match(cat, candidate, {'size_inputs': None}) is None


def test_pickup_only_size_requires_explicit_source_fulfillment_mode():
    from crawler.b2sign_catalog import parse_public_product, PINNED_BUNDLE_SHA256
    cat = parse_public_product({'id': 902, 'title': 'Pickup Size', 'url': '/pickup-size',
        'attrs': {'attrs': [{'key': '_size', 'label': 'Size', 'component': 'size',
            'options': [{'key': 'width'}, {'key': 'height'}],
            'component_props': {'min': [{'match': [], 'value': '6x6'}],
                'max': [
                    {'match': [{'key': '_shipping', 'logic': 'and', 'op': '=',
                                'value': 'storepickup'}], 'value': '96x48'},
                    {'match': [{'key': '_shipping', 'logic': 'and', 'op': '=',
                                'value': 'dropship'}], 'value': '48x38'}]}}],
            'turnaround': [{'match': [{'key': '_qty', 'op': '>', 'value': 0}],
                            'options': [{'key': 'v1', 'label': 'Next Day'}]}]}},
        source_url='https://www.b2sign.com/pickup-size',
        source_bundle_sha256=PINNED_BUNDLE_SHA256)
    row = {'source_url': cat['url'], 'source_product_id': cat['source_product_id'],
           'contract_sha256': cat['contract_sha256'], 'values': {'_size': '49x38'},
           'quantity': 1, 'turnaround': 'v1'}
    with pytest.raises(HarvestError, match='size_outside_bound'):
        prepare_candidate(cat, row)
    pickup = copy.deepcopy(row)
    pickup['runtime_context'] = {'_shipping': 'storepickup'}
    safe = prepare_candidate(cat, pickup)
    assert safe['source_fulfillment_mode'] == 'storepickup'
    dropship = copy.deepcopy(row)
    dropship['runtime_context'] = {'_shipping': 'dropship'}
    with pytest.raises(HarvestError, match='size_outside_bound'):
        prepare_candidate(cat, dropship)
    assert safe['candidate_id'] != prepare_candidate(cat, dict(row, values={'_size': '48x38'}))['candidate_id']


class HarvestPage:
    def __init__(self, real, pool):
        self.real, self.pool = real, pool

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def route(self, pattern, handler):
        async def intercepted(route):
            async def continued():
                request = route.request
                self.pool.requests.append((request.method, request.url))
                path = urlsplit(request.url).path
                if path == "/pole-banner-set":
                    await route.fulfill(status=200, content_type="text/html", body=self.pool.html())
                elif path == "/build/assets/app-fixture.js":
                    await route.fulfill(status=200, content_type="text/javascript", body=self.pool.bundle)
                elif path == "/item/quote":
                    payload = request.post_data_json
                    self.pool.posted.append(payload)
                    assert request.headers["x-xsrf-token"] == "PRIVATE_CSRF_FIXTURE"
                    assert request.headers["x-inertia"] == "true"
                    if len(self.pool.posted) == self.pool.fail_at:
                        await route.fulfill(status=403, content_type="application/json", body=json.dumps({"private": "PRIVATE_ERROR"}))
                        return
                    quantity = int(json.loads(payload["jobs"])[0]["subjobs"][0]["quantity"])
                    body = response(quantity)
                    if payload["values"].get("windslit") == "1" and self.pool.unavailable_wind:
                        body["props"]["quoteResult"]["quote"] = [None]
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
                else:
                    await route.fulfill(status=404, body="")
            await handler(SimpleNamespace(request=route.request, abort=route.abort, continue_=continued))
        await self.real.route(pattern, intercepted)


class HarvestPool(PilotPool):
    def __init__(self, context, contract):
        super().__init__(context, contract)
        self.fail_at = -1
        self.unavailable_wind = False

    @asynccontextmanager
    async def page(self, origin, *, headless=True):
        self.calls.append((origin, headless))
        page = await self.context.new_page()
        self.pages.append(page)
        try:
            yield HarvestPage(page, self)
        finally:
            if not page.is_closed():
                await page.close()

    def html(self):
        return super().html().replace("headers:{'Content-Type':'application/json'}", "headers:{'Content-Type':'application/json','X-Inertia':'true','X-Inertia-Version':'fixture-v1','X-XSRF-Token':'PRIVATE_CSRF_FIXTURE','X-Requested-With':'XMLHttpRequest'}")


@pytest_asyncio.fixture
async def harvest(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from crawler.b2sign_pilot import load_contract
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Local Chromium unavailable")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        pool = HarvestPool(context, load_contract())
        monkeypatch.setattr("crawler.b2sign_harvest.validate_url", AsyncMock())
        manager = B2SignHarvestManager(SimpleNamespace(root=tmp_path), pool, requests_per_second=6, bootstrap_timeout=4)
        monkeypatch.setitem(COMPATIBLE_RUNTIME_BUNDLES,
                            "https://www.b2sign.com/build/assets/app-fixture.js",
                            hashlib.sha256(pool.bundle.encode()).hexdigest())
        try:
            yield manager, pool
        finally:
            await manager.close()
            await browser.close()


@pytest.mark.asyncio
async def test_chromium_one_bootstrap_multiple_finite_quotes_dedup_and_no_secrets(harvest):
    manager, pool = harvest
    cat, rows = candidates(3)
    user_page = await pool.context.new_page()
    await user_page.set_content('<h1>Original diagnosis unchanged</h1>')
    report = await manager.start(cat, rows + [rows[0]])
    await manager._tasks[report["id"]]
    done = manager.get(report["id"])
    assert done["status"] == "completed", done
    assert done["requested_count"] == done["completed_count"] == done["row_count"] == 3
    assert done["quote_requests"] == 3
    assert len(pool.calls) == 1 and len(pool.posted) == 3
    assert done["finite_requested_set_exhausted"] is True
    assert done["catalog_complete"] is False and done["final_eligible"] is False
    assert all(row["verification_status"] == "pending_independent_canaries" for row in done["results"])
    assert len({row["context_id"] for row in done["results"]}) == 1
    assert len({json.dumps(p["values"], sort_keys=True) for p in pool.posted}) == 3
    assert len(list((manager._path(report["id"]) / 'attempts').glob('*.json'))) == 3
    for path in manager.root.rglob('*.json'):
        assert 'PRIVATE' not in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
    assert not user_page.is_closed()
    assert all(page.is_closed() for page in pool.pages)
    assert all('/cart/' not in url and '/account/' not in url and 'third-party' not in url for _, url in pool.requests)


@pytest.mark.asyncio
async def test_resume_reuses_completed_evidence_without_relabeling_context(harvest):
    manager, pool = harvest
    cat, rows = candidates(3)
    pool.fail_at = 2
    report = await manager.start(cat, rows)
    await manager._tasks[report["id"]]
    paused = manager.get(report["id"])
    assert paused["status"] == "attention" and paused["completed_count"] == 1, paused
    original = paused["results"][0]
    path = manager._path(report["id"]) / 'results' / (original['candidate_id'] + '.json')
    before = path.read_bytes()
    pool.fail_at = -1
    await manager.resume(report["id"])
    await manager._tasks[report["id"]]
    resumed = manager.get(report["id"])
    assert resumed["status"] == "completed", resumed
    assert resumed["row_count"] == 3 and resumed["quote_requests"] == 4
    assert len(resumed["contexts"]) == 2
    assert resumed["contains_multiple_session_contexts"] is True
    assert path.read_bytes() == before
    assert len({row["context_id"] for row in resumed["results"]}) == 2
    attempts = [json.loads(path.read_text()) for path in (manager._path(report["id"]) / 'attempts').glob('*.json')]
    assert len(attempts) == 4
    assert any(attempt.get('http_status') == 403 for attempt in attempts)


@pytest.mark.asyncio
async def test_source_unavailable_is_preserved_without_inventing_price(harvest):
    manager, pool = harvest
    pool.unavailable_wind = True
    cat, rows = candidates(2)
    report = await manager.start(cat, rows)
    await manager._tasks[report["id"]]
    done = manager.get(report["id"])
    assert done["status"] == "completed", done
    assert done["completed_count"] == 2 and done["row_count"] == 1 and done["invalid_count"] == 1
    missing = next(row for row in done["results"] if row['status'] == 'source_unavailable')
    assert 'quote' not in missing
    assert missing['reason'] == 'source_unavailable_for_exact_configuration'


@pytest.mark.asyncio
async def test_saved_proof_policy_bound_and_conflict_rejected_before_browser(harvest):
    manager, pool = harvest
    cat, rows = candidates(1)
    record = diagnosis()
    record['selection']['fields'][-1]['fixed_values'] = ['']
    with pytest.raises(HarvestError, match='saved_policy_requires_unchecked_proof'):
        await manager.start(cat, rows, diagnosis=record)
    assert pool.calls == []
    record['selection']['fields'][-1]['fixed_values'] = []
    report = await manager.start(cat, rows, diagnosis=record)
    await manager._tasks[report['id']]
    final = manager.get(report['id'])
    assert final['status'] == 'completed', final
    assert final['saved_policy_binding']['diagnosis_id'] == record['id']
    assert final['proof_selection_basis'] == 'read_scope_requires_observed_unchecked_UI'


@pytest.mark.asyncio
async def test_shared_gate_orders_simultaneous_requests_without_supplier_calls():
    gate = SharedQuoteGate(6)
    timestamps = []
    async def enter():
        await gate.wait()
        timestamps.append(asyncio.get_running_loop().time())
    await asyncio.gather(enter(), enter(), enter())
    assert min(b - a for a, b in zip(timestamps, timestamps[1:])) >= .15


def test_harvest_and_canary_managers_can_share_one_supplier_gate(tmp_path):
    from crawler.b2sign_canary import B2SignCanaryManager
    gate = SharedQuoteGate(2)
    store = SimpleNamespace(root=tmp_path)
    pool = object()
    harvest = B2SignHarvestManager(store, pool, quote_gate=gate)
    canary = B2SignCanaryManager(store, pool, quote_gate=gate)
    assert harvest.gate is canary.gate is gate
