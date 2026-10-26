"""Bounded child evidence reads through fake browser transport; no suppliers."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.config import Settings
from crawler.fourover_child_prices import FourOverChildPriceCollector, parse_positive_prices
from crawler.store import Store
from crawler.transport import ExtractionError

URL = "https://4over.com/product-fixture"
SNAPSHOT = "a" * 32
SCOPE = {"sets": 1, "job_sample": False, "digital_proof": False}


def document(children=("101", "102", "103")):
    config = {child: {"sku": "PUBLIC-SKU-" + child} for child in children}
    return ('<h1>Fixture</h1><script type="text/x-magento-init">' + json.dumps({"*": {
        "Magento_Ui/js/core/app": {"components": {"child-sku": {"childSkuConfig": config}}}}}) + '</script>').encode()


def prices(amount="12.340001"):
    return ('[{"packagePrices":[{"qty":100,"price":' + amount + '},{"qty":250,"price":20.50}],"tierPrices":[],"accountToken":"do-not-retain-me"}]').encode()


class Response:
    status, url, headers = 200, URL, {}
    def __init__(self, body):
        self.raw = body
    async def body(self):
        return self.raw


class Route:
    def __init__(self, page, url, method="GET", resource_type="fetch", frame=None):
        self.request = SimpleNamespace(url=url, method=method, resource_type=resource_type,
                                       frame=page.main_frame if frame is None else frame)
        self.allowed = None
    async def continue_(self):
        self.allowed = True
    async def abort(self):
        self.allowed = False


class Page:
    main_frame = object()
    def __init__(self, fixture):
        self.fixture = fixture
        self.context = self
        self.cdp = []
    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler
    async def new_cdp_session(self, page):
        return self
    async def send(self, method, data):
        self.cdp.append((method, data))
    async def detach(self):
        pass
    async def goto(self, url, **kwargs):
        assert url == URL
        route = Route(self, url, resource_type="document")
        await self.handler(route)
        assert route.allowed is True
        self.fixture["documents"] += 1
        hold = self.fixture.get("hold_document")
        if hold is not None:
            self.fixture["document_entered"].set()
            await hold.wait()
        return Response(self.fixture.get("document_body", document(self.fixture.get("children", ("101", "102", "103")))))
    async def evaluate(self, script, params):
        assert "credentials:'same-origin'" in script and "redirect:'error'" in script
        route = Route(self, params["url"])
        await self.handler(route)
        assert route.allowed is True
        duplicate = Route(self, params["url"])
        await self.handler(duplicate)
        assert duplicate.allowed is False
        child = params["url"].split('/prices/')[1].split('?')[0]
        self.fixture["children_sent"].append(child)
        if child == self.fixture.get("cancel_child"):
            raise asyncio.CancelledError()
        status = self.fixture.get("statuses", {}).get(child, 200)
        raw = self.fixture.get("bodies", {}).get(child, prices())
        return {"status": status, "body_b64": base64.b64encode(raw).decode(), "retry_after": "8"}


class Pool:
    def __init__(self, fixture):
        self.fixture = fixture
    @asynccontextmanager
    async def page(self, origin, headless):
        assert origin == "https://4over.com" and headless is False
        page = Page(self.fixture)
        self.fixture["pages"].append(page)
        yield page


class Limiter:
    def __init__(self):
        self.acquired, self.released = [], []
    async def acquire(self, origin, rate, concurrency, check):
        await check()
        self.acquired.append((origin, rate, concurrency))
        return "fixture-token"
    def release(self, origin, token, elapsed, status, backoff, cap):
        self.released.append((origin, status, backoff, cap))


@pytest.fixture
async def collector(tmp_path, monkeypatch):
    import crawler.fourover_child_prices as module
    async def validate(url):
        assert url == URL
    monkeypatch.setattr(module, "validate_url", validate)
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    store.save_profile("source", {"host": "www.4over.com", "requests_per_second": 0.5,
                                  "max_concurrency": 1}, None)
    state = {"documents": 0, "children_sent": [], "pages": []}
    result = FourOverChildPriceCollector(store, Pool(state))
    result.limiter = Limiter()
    try:
        yield result, state
    finally:
        store.engine.dispose()


def test_exact_positive_source_prices_exclude_private_envelope():
    parsed = parse_positive_prices(prices())
    assert parsed == {"package_prices": [{"quantity": 100, "price": "12.340001"},
                                         {"quantity": 250, "price": "20.50"}], "tier_prices": []}
    assert "account" not in json.dumps(parsed)


@pytest.mark.parametrize("body", [prices("0"), prices("-1"), prices("NaN"), prices('"12.30"'),
    b'[{"packagePrices":[{"qty":0,"price":2}]}]', b'[{"packagePrices":[]}]',
    b'[{"packagePrices":[{"qty":1,"price":2},{"qty":1,"price":3}]}]',
    b'[{"tierPrices":[{"qty":1.5,"price":2}]}]', b'<html>Login</html>'])
def test_zero_malformed_duplicate_and_open_price_responses_fail_closed(body):
    with pytest.raises(ExtractionError, match="zero or malformed"):
        parse_positive_prices(body)


@pytest.mark.asyncio
async def test_document_option_bindings_are_preserved_without_account_material(collector):
    service, state = collector
    config = {"attributes": {"211": {"id": "211", "code": "size", "label": "Size",
              "options": [{"id": "87", "label": "4 x 6", "products": ["101"]},
                          {"id": "210", "label": "5 x 7", "products": ["102"]}]}},
              "index": {"101": {"211": "87"}, "102": {"211": "210"}},
              "accountToken": "private-value-never-persist"}
    state["document_body"] = (document(("101", "102")) +
        ('<script type="text/x-magento-init">' + json.dumps({
            "#product_addtocart_form": {"configurable": {"spConfig": config}}}) + '</script>').encode())
    result = await service.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE)
    assert result["option_contract"]["binding_complete"] is True
    assert {row["child_id"] for row in result["option_contract"]["rows"]} == {"101", "102"}
    assert result["final_eligible"] is False
    assert "private-value-never-persist" not in json.dumps(result)
    assert state["documents"] == 1 and state["children_sent"] == ["101"]


@pytest.mark.asyncio
async def test_discovery_only_uses_one_document_and_never_prices(collector):
    service, state = collector
    result = await service.collect(SNAPSHOT, URL, [], request_budget=1, scope=SCOPE)
    assert result["status"] == "discovered_source_children"
    assert result["declared_children"] == [{"child_id": child, "sku": "PUBLIC-SKU-"+child}
                                            for child in ("101", "102", "103")]
    assert state["documents"] == 1 and state["children_sent"] == []
    await service.collect(SNAPSHOT, URL, [], request_budget=1, scope=SCOPE)
    assert state["documents"] == 1


@pytest.mark.asyncio
async def test_success_is_durable_and_resume_does_not_repeat_children(collector):
    service, state = collector
    state["cancel_child"] = "102"
    with pytest.raises(asyncio.CancelledError):
        await service.collect(SNAPSHOT, URL, ["101", "102"], request_budget=3, scope=SCOPE)
    assert service.get(SNAPSHOT, URL)["observations"][0]["child_id"] == "101"
    state.pop("cancel_child")
    # A new collector simulates process restart against the same checkpoint.
    restarted = FourOverChildPriceCollector(service.store, Pool(state))
    restarted.limiter = Limiter()
    result = await restarted.collect(SNAPSHOT, URL, ["101", "102"], request_budget=2, scope=SCOPE)
    assert result["status"] == "completed_requested_subset"
    assert state["children_sent"] == ["101", "102", "102"]
    assert result["requests_this_call"] == 2 and result["cache_hits_this_call"] == 1
    assert result["observations"][0]["raw_response_sha256"] == hashlib.sha256(prices()).hexdigest()
    before = (state["documents"], list(state["children_sent"]))
    cached = await restarted.collect(SNAPSHOT, URL, ["101", "102"], request_budget=1, scope=SCOPE)
    assert cached["requests_this_call"] == 0 and cached["cache_hits_this_call"] == 2
    assert before == (state["documents"], state["children_sent"])
    assert len(service.limiter.acquired) == len(service.limiter.released) == 3
    assert all(item == ("https://4over.com", 0.5, 1) for item in service.limiter.acquired)
    assert all(not result[key] for key in ("catalog_complete", "final_eligible", "services_verified", "supplier_total_verified"))
    for path in service.root.rglob("*.json"):
        assert "do-not-retain-me" not in path.read_text()
    for item in result["observations"]:
        assert "do-not-retain-me" not in service.store.get_object(item["object_path"]).decode()


@pytest.mark.asyncio
async def test_budget_bounds_document_plus_prices_and_can_resume_remaining(collector):
    service, state = collector
    result = await service.collect(SNAPSHOT, URL, ["101", "102", "103"], request_budget=2, scope=SCOPE)
    assert result["error"]["code"] == "request_limit"
    assert state["children_sent"] == ["101"] and result["requests_this_call"] == 2
    assert [item["child_id"] for item in result["observations"]] == ["101"]
    result = await service.collect(SNAPSHOT, URL, ["101", "102", "103"], request_budget=3, scope=SCOPE)
    assert state["children_sent"] == ["101", "102", "103"]
    assert result["status"] == "completed_requested_subset"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, "access_required"), (403, "access_required"), (429, "source_http_error")])
async def test_auth_and_throttle_fail_without_retry_or_success_cache(collector, status, code):
    service, state = collector
    state["statuses"] = {"101": status}
    result = await service.collect(SNAPSHOT, URL, ["101", "102"], request_budget=3, scope=SCOPE)
    assert result["error"]["code"] == code and result["observations"] == []
    assert state["children_sent"] == ["101"]
    assert service.limiter.released[-1][1:3] == (status, 8.0)


@pytest.mark.asyncio
async def test_undeclared_child_never_receives_price_request(collector):
    service, state = collector
    result = await service.collect(SNAPSHOT, URL, ["999"], request_budget=2, scope=SCOPE)
    assert result["error"]["code"] == "undeclared_child"
    assert state["children_sent"] == []


@pytest.mark.asyncio
async def test_source_changed_during_resume_requires_new_snapshot(collector):
    service, state = collector
    first = await service.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE)
    state["children"] = ("101", "104")
    result = await service.collect(SNAPSHOT, URL, ["102"], request_budget=2, scope=SCOPE)
    assert result["error"]["code"] == "source_declaration_changed"
    assert state["children_sent"] == ["101"]
    assert result["option_contract"] == first["option_contract"]
    assert result["option_contract_document_sha256"] == first["document_body_sha256"]
    assert result["document_body_sha256"] != first["document_body_sha256"]
    assert service.get(SNAPSHOT, URL)["option_contract"] == first["option_contract"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"child_ids": ["101"]*11}, {"child_ids": ["101", "101"]},
    {"child_ids": [101]}, {"request_budget": True}, {"request_budget": 12},
    {"scope": {**SCOPE, "sets": True}}, {"scope": {**SCOPE, "digital_proof": True}},
    {"snapshot_id": "../escape"}, {"source_url": "https://4over.com/customer/account/"},
    {"source_url": "https://other.invalid/product-fixture"}])
async def test_invalid_requests_do_not_open_browser(collector, kwargs):
    service, state = collector
    values = {"snapshot_id": SNAPSHOT, "source_url": URL, "child_ids": ["101"],
              "request_budget": 2, "scope": SCOPE, **kwargs}
    with pytest.raises(ValueError):
        await service.collect(**values)
    assert not state["pages"]


@pytest.mark.asyncio
async def test_guard_allows_only_owned_get_and_blocks_writes_assets_redirects_subframes(collector):
    service, state = collector
    result = await service.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE)
    assert result["status"] == "completed_requested_subset"
    page = state["pages"][0]
    for route in [Route(page, URL, resource_type="document"),
                  Route(page, URL, method="POST", resource_type="document"),
                  Route(page, "https://4over.com/customer/account/", resource_type="document"),
                  Route(page, "https://4over.com/rest/default/V1/product/prices/102", resource_type="fetch"),
                  Route(page, "https://4over.com/script.js", resource_type="script"),
                  Route(page, URL, resource_type="document", frame=object()),
                  Route(page, "https://other.invalid/")]:
        await page.handler(route)
        assert route.allowed is False
    assert page.cdp == [("Emulation.setScriptExecutionDisabled", {"value": True}),
                        ("Emulation.setScriptExecutionDisabled", {"value": False})]


@pytest.mark.asyncio
async def test_cache_evidence_tampering_blocks_resume_without_supplier_calls(collector):
    service, state = collector
    first = await service.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE)
    evidence = first["observations"][0]["object_path"]
    (Path(service.store.root) / evidence).write_bytes(b'{}')
    with pytest.raises(ValueError, match="evidence changed"):
        await service.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE)
    assert state["documents"] == 1 and state["children_sent"] == ["101"]


@pytest.mark.asyncio
async def test_parallel_collectors_share_checkpoint_and_do_not_repeat_success(collector):
    service, state = collector
    second = FourOverChildPriceCollector(service.store, Pool(state))
    second.limiter = Limiter()
    results = await asyncio.gather(
        service.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE),
        second.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE))
    assert state["children_sent"] == ["101"] and state["documents"] == 1
    assert sorted(result["cache_hits_this_call"] for result in results) == [0, 1]


@pytest.mark.asyncio
async def test_intercepted_chromium_document_script_is_disabled_and_native_fetch_reads_exact_bytes(collector):
    """Real Chromium transport; every request is locally fulfilled or aborted."""
    playwright = pytest.importorskip("playwright.async_api")
    service, _ = collector
    requests, unsafe_scripts = [], []
    html = document() + b'''<script>window.unsafeCollectorScript=true;
      fetch('/checkout/forbidden',{method:'POST'});</script>
      <img src="https://untrusted-fixture.invalid/blocked.png">'''
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")
        browser = await runtime.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        try:
            class RealPool:
                @asynccontextmanager
                async def page(self, origin, headless):
                    page = await context.new_page()
                    original = page.route
                    async def install(pattern, guard):
                        async def intercepted(actual):
                            class Proxy:
                                request = actual.request
                                async def continue_(self):
                                    requests.append((self.request.method, self.request.url, "fulfilled"))
                                    if self.request.url == URL and self.request.method == "GET":
                                        await actual.fulfill(status=200, content_type="text/html", body=html)
                                    elif '/rest/default/V1/product/prices/101?' in self.request.url and self.request.method == "GET":
                                        await actual.fulfill(status=200, content_type="application/json", body=prices())
                                    else:
                                        raise AssertionError("Guard allowed an unexpected request")
                                async def abort(self):
                                    requests.append((self.request.method, self.request.url, "aborted"))
                                    await actual.abort()
                            await guard(Proxy())
                        await original(pattern, intercepted)
                    page.route = install
                    try:
                        yield page
                        unsafe_scripts.append(await page.evaluate("Boolean(window.unsafeCollectorScript)"))
                    finally:
                        await page.close()
            real = FourOverChildPriceCollector(service.store, RealPool())
            real.limiter = Limiter()
            result = await real.collect(SNAPSHOT, URL, ["101"], request_budget=2, scope=SCOPE)
            assert result.get("error") is None
            assert result["status"] == "completed_requested_subset"
            assert result["observations"][0]["raw_response_sha256"] == hashlib.sha256(prices()).hexdigest()
            assert result["observations"][0]["package_prices"][0]["price"] == "12.340001"
            assert unsafe_scripts == [False]
            assert len([item for item in requests if item[2] == "fulfilled"]) == 2
            assert all(item[0] == "GET" for item in requests)
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_declaration_failure_preserves_safe_hash_size_counts_and_exception_class(collector):
    service, state = collector
    state['document_body'] = b'<html><title>Fixture</title><div>do-not-save-account-text</div></html>'
    result = await service.collect(SNAPSHOT, URL, [], request_budget=1, scope=SCOPE)
    assert result['status'] == 'partial'
    assert result['error']['code'] == 'child_declaration_missing'
    assert result['error']['exception_type'] == 'ChildConfigurationError'
    assert result['document_body_sha256'] == hashlib.sha256(state['document_body']).hexdigest()
    assert result['document_body_bytes'] == len(state['document_body'])
    assert result['document_declaration']['child_map_count'] == 0
    assert state['children_sent'] == []
    for path in service.root.rglob('*.json'):
        assert 'do-not-save-account-text' not in path.read_text()


@pytest.mark.asyncio
async def test_document_declaration_over_bound_is_not_reported_as_generic_failure(collector):
    service, state = collector
    state['document_body'] = document(tuple(str(n) for n in range(1, 2002)))
    result = await service.collect(SNAPSHOT, URL, [], request_budget=1, scope=SCOPE)
    assert result['error']['code'] == 'child_declaration_limit'
    assert result['document_declaration']['declared_child_count'] == 2001
    assert result['document_body_bytes'] > 0 and state['children_sent'] == []


@pytest.mark.asyncio
async def test_live_page_uses_shared_source_admission_and_cache_hits_take_no_worker(collector, monkeypatch):
    import crawler.adapters.browser as browser_module
    import crawler.fourover_child_prices as child_module
    from crawler.browser_capacity import BrowserWorkload

    service, state = collector
    workload = BrowserWorkload()
    monkeypatch.setattr(browser_module, 'browser_workload', workload)
    monkeypatch.setattr(browser_module, '_BROWSER_LOCK', asyncio.Semaphore(4))
    await service.collect(SNAPSHOT, URL, ['101'], request_budget=2, scope=SCOPE)
    assert workload.snapshot() == {'active_browser_workers': 0, 'waiting_for_browser_workers': 0}
    state['hold_document'], state['document_entered'] = asyncio.Event(), asyncio.Event()
    active = asyncio.create_task(service.collect('b'*32, URL, ['102'], request_budget=2, scope=SCOPE))
    await asyncio.wait_for(state['document_entered'].wait(), timeout=1)
    queued = asyncio.create_task(service.collect('c'*32, URL, ['103'], request_budget=2, scope=SCOPE))
    try:
        for _ in range(20):
            if workload.snapshot()['waiting_for_browser_workers'] == 1:
                break
            await asyncio.sleep(0.01)
        # The source profile is concurrency=1; the second call must wait BEFORE
        # opening a Chromium page even though global capacity has room for four.
        assert workload.snapshot() == {'active_browser_workers': 1, 'waiting_for_browser_workers': 1}
        assert len(state['pages']) == 2  # initial completed call and active call
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert workload.snapshot() == {'active_browser_workers': 1, 'waiting_for_browser_workers': 0}
    finally:
        state['hold_document'].set()
        await active
        if not queued.done():
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
    assert workload.snapshot() == {'active_browser_workers': 0, 'waiting_for_browser_workers': 0}
    pages_before, requests_before = len(state['pages']), len(service.limiter.acquired)
    def forbidden_slot(*args, **kwargs):
        raise AssertionError('A completed child cache must not enter browser admission')
    monkeypatch.setattr(child_module, 'browser_slot', forbidden_slot)
    cached = await service.collect(SNAPSHOT, URL, ['101'], request_budget=1, scope=SCOPE)
    assert cached['requests_this_call'] == 0 and cached['cache_hits_this_call'] == 1
    assert len(state['pages']) == pages_before and len(service.limiter.acquired) == requests_before
    assert workload.snapshot() == {'active_browser_workers': 0, 'waiting_for_browser_workers': 0}
