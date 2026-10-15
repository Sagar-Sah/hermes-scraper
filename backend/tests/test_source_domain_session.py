"""Passive native-session setup and exact context-bound HTTP, entirely local."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

from crawler.config import Settings
from crawler.source_domain_session import DOMAIN_CONTROLLER_STATE_JS, SourceDomainSessions, public_controller_state
from crawler.source_domains import SourceDomainCoordinator, plan_known_reads
from crawler.store import Product, Run, Store
from crawler.transport import ExtractionError


URL = "https://www.printograph.com/products/roll-labels/order"
STATE = {"observed": True, "product_type": "36", "store_type": "broker", "currency": "USD",
         "userSelectedOptions": {"proofType": "31", "fileDelivery": "5", "designServices": "",
                                 "quantity": "100", "productId": "987", "shape": "5", "size": "742"},
         "other_context": {"is_admin": False, "waive_fees": False, "has_price_group": False},
         "field_options": ["windPosition", "proofType"]}


class Page:
    def __init__(self, context, url, state=None):
        self.context, self.url, self.state, self.closed = context, url, deepcopy(state or STATE), False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def evaluate(self, script):
        assert script == DOMAIN_CONTROLLER_STATE_JS
        return deepcopy(self.state)


class Context:
    def __init__(self):
        self.pages, self.listeners = [], {}
        self.cookie_reads, self.cookie_updates = [], []
        self.pages.append(Page(self, URL))

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        self.listeners[event].remove(listener)

    async def new_page(self):
        page = Page(self, "about:blank")
        self.pages.append(page)
        return page

    async def cookies(self, urls):
        self.cookie_reads.append(urls)
        return [{"name": "fixture_session", "value": "synthetic-native-cookie"}]

    async def add_cookies(self, cookies):
        self.cookie_updates.extend(cookies)

    async def close(self):
        for page in self.pages:
            page.closed = True
        for listener in list(self.listeners.get("close", [])):
            listener()

    def request(self, page=None, url="https://www.printograph.com/service/rest/v1/products/987/prices"):
        async def all_headers():
            return {"authorization": "synthetic-private-authorization", "x-source-scope": "synthetic-private-header",
                    "cookie": "must-not-copy-cookie-header", "accept": "application/json"}
        request = SimpleNamespace(url=url, method="GET", frame=SimpleNamespace(page=page or self.pages[0]), all_headers=all_headers)
        for listener in list(self.listeners.get("request", [])):
            listener(request)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    with store.session() as s:
        s.add(Run(id="run", name="Session fixture", status="running"))
        s.add(Product(id="roll", run_id="run", url=URL, status="paused"))
    async def local_validate(url):
        return urlparse(url)
    monkeypatch.setattr("crawler.transport.validate_url", local_validate)
    context = Context()
    yield store, context, SimpleNamespace(_contexts={"existing": context})
    store.engine.dispose()


async def initialize(manager, context):
    started = asyncio.create_task(manager.initialize("run", "roll", wait_seconds=.3))
    await asyncio.sleep(.01)
    context.request()
    return await started


def test_controller_diagnostic_never_claims_composition_or_exports_unknown_fields():
    raw = deepcopy(STATE)
    raw["userSelectedOptions"]["shoppingCartItemId"] = "private-cart-id"
    raw["userSelectedOptions"]["password"] = "private-password"
    raw["other_context"]["customer_id"] = "private-customer"
    raw["source_contract"] = {"claimed": "verified"}
    public = public_controller_state(raw)
    assert public["source_contract"] == {} and not public["composition_verified"]
    assert not public["source_contract_verified"] and not public["settled"]
    assert "private-" not in str(public)
    assert public["userSelectedOptions"]["proofType"] == "31"
    assert public["userSelectedOptions"]["fileDelivery"] == "5"
    raw["userSelectedOptions"].pop("fileDelivery")
    with pytest.raises(ExtractionError, match="explicitly"):
        public_controller_state(raw)


@pytest.mark.asyncio
async def test_passive_setup_no_source_http_and_only_owned_blank_duplicate_closes(setup):
    store, context, pool = setup
    calls = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request)))
    manager = SourceDomainSessions(store, pool, client_factory=lambda: client)
    result = await initialize(manager, context)
    assert result["status"] == "ready" and result["source_requests_issued"] == 0 and calls == []
    assert result["context"]["domain_controller_state"]["userSelectedOptions"]["fileDelivery"] == "5"
    assert "synthetic-private" not in str(result) and context.cookie_reads == []
    assert [page.url for page in context.pages] == [URL, "about:blank"]
    assert context.listeners["request"] == []
    session = manager.sessions[result["session_scope"]]
    assert session.headers["authorization"] == "synthetic-private-authorization"
    await manager.close()
    assert not context.pages[0].closed and context.pages[1].closed
    assert client.is_closed and session.headers == {}


@pytest.mark.asyncio
async def test_no_open_tab_or_no_ordinary_request_needs_attention_without_navigation(setup):
    store, context, pool = setup
    manager = SourceDomainSessions(store, pool)
    with pytest.raises(ExtractionError, match="No ordinary"):
        await manager.initialize("run", "roll", wait_seconds=.01)
    assert len(context.pages) == 1 and context.listeners["request"] == []
    pool._contexts.clear()
    with pytest.raises(ExtractionError, match="Exactly one"):
        await manager.initialize("run", "roll", wait_seconds=.01)
    await manager.close()


@pytest.mark.asyncio
async def test_unrelated_origin_or_page_cannot_seed_session(setup):
    store, context, pool = setup
    manager = SourceDomainSessions(store, pool)
    task = asyncio.create_task(manager.initialize("run", "roll", wait_seconds=.04))
    await asyncio.sleep(.005)
    context.request(url="https://evil.test/service/rest/v1/products/987/prices")
    context.request(page=Page(context, "about:blank"))
    with pytest.raises(ExtractionError, match="No ordinary"):
        await task
    assert not manager.sessions
    await manager.close()


@pytest.mark.asyncio
async def test_changed_service_context_during_observation_is_rejected(setup):
    store, context, pool = setup
    manager = SourceDomainSessions(store, pool)
    task = asyncio.create_task(manager.initialize("run", "roll", wait_seconds=.3))
    await asyncio.sleep(.01)
    context.pages[0].state["userSelectedOptions"]["proofType"] = "100"
    context.request()
    with pytest.raises(ExtractionError, match="changed"):
        await task
    assert len(context.pages) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_queue_uses_native_cookies_private_headers_shared_http_pool_and_domain_budget(setup):
    store, context, pool = setup
    calls = []
    async def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "synthetic-private-authorization"
        assert request.headers["cookie"] == "fixture_session=synthetic-native-cookie"
        assert "must-not-copy-cookie-header" not in str(request.headers)
        return httpx.Response(200, json={"items": [{"id": 987}]}, headers={"set-cookie": "rotated=synthetic-rotated; Path=/; Secure"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    manager = SourceDomainSessions(store, pool, client_factory=lambda: client)
    result = await initialize(manager, context)
    # The ordinary window can close a blank helper tab without ending the
    # native login context. Actual table reads must continue to use that context.
    await context.pages[1].close()
    assert manager.status(result["session_scope"])["status"] == "ready"
    coordinator = SourceDomainCoordinator(store, manager.factory, workers=2, poll_seconds=.01)
    plan = plan_known_reads("roll", "36", result["context"], include_specs=False, listing_offsets=(0, 25))
    job = coordinator.install("run", "native-domain", plan, budget=3)["job_id"]
    try:
        assert calls == []
        await coordinator.start(job)
        for _ in range(300):
            if coordinator.status(job)["status"] == "requests_drained":
                break
            await asyncio.sleep(.01)
        state = coordinator.status(job)
        assert state["completed_requests"] == state["attempts_used"] == len(calls) == 2
        assert len(context.cookie_reads) == 2 and len(context.cookie_updates) == 2
        assert not client.is_closed and not state["catalog_complete"]
        assert "synthetic-" not in str(state)
        with store.session() as s:
            assert s.get(Product, "roll").status == "paused"
    finally:
        await coordinator.close()
        await manager.close()


@pytest.mark.asyncio
async def test_factory_rejects_omitted_or_changed_initialized_context(setup):
    store, context, pool = setup
    manager = SourceDomainSessions(store, pool)
    result = await initialize(manager, context)
    public = deepcopy(result["context"])
    public.pop("domain_controller_state")
    claim = {"run_id": "run", "product_id": "roll", "request": {"context": public}}
    budget = SimpleNamespace(check=lambda: None)
    with pytest.raises(ExtractionError, match="not bound"):
        async with manager.factory(claim, budget):
            pass
    await manager.close()


@pytest.mark.asyncio
async def test_native_context_closure_invalidates_session_even_if_claim_context_matches(setup):
    store, context, pool = setup
    manager = SourceDomainSessions(store, pool)
    result = await initialize(manager, context)
    await context.close()
    assert manager.status(result["session_scope"])["status"] == "unavailable"
    claim = {"run_id": "run", "product_id": "roll", "request": {"context": result["context"]}}
    with pytest.raises(ExtractionError, match="Initialize"):
        async with manager.factory(claim, SimpleNamespace()):
            pass
    assert not context.cookie_reads
    await manager.close()


@pytest.mark.asyncio
async def test_session_and_stage_listing_api_is_authenticated_and_read_only(setup, monkeypatch):
    import crawler.app as api
    store, context, pool = setup
    manager = SourceDomainSessions(store, pool)
    coordinator = SourceDomainCoordinator(store, manager.factory)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api.app.state, "domain_sessions", manager, raising=False)
    monkeypatch.setattr(api.app.state, "source_domains", coordinator, raising=False)
    monkeypatch.setattr(api.settings, "api_token", "fixture-domain-api")
    headers = {"Authorization": "Bearer fixture-domain-api"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        path = "/api/v1/runs/run/source-domain-sessions"
        assert (await client.post(path, json={"product_id": "roll"})).status_code == 401
        task = asyncio.create_task(client.post(path, json={"product_id": "roll", "wait_seconds": .3}, headers=headers))
        await asyncio.sleep(.01)
        context.request()
        response = await task
        assert response.status_code == 200, response.text
        body = response.json()
        assert "synthetic-private" not in response.text
        plan = plan_known_reads("roll", "36", body["context"], include_specs=False)
        installed = coordinator.install("run", "list-domain", plan, budget=3)
        listing = await client.get("/api/v1/runs/run/source-domains", headers=headers)
        assert listing.status_code == 200 and listing.json()["items"][0]["job_id"] == installed["job_id"]
        assert listing.json()["items"][0]["status"] == "dormant"
        assert (await client.get("/api/v1/runs/missing/source-domains", headers=headers)).status_code == 404
    await coordinator.close()
    await manager.close()
