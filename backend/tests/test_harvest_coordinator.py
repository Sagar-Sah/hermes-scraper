"""Source-free coordinator integration: preflight gate precedes actual queue pumping."""

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import func, select

from crawler.config import Settings
from crawler.harvest_coordinator import HarvestCoordinator, HarvestCoordinatorState, normalize_quote_manifest
from crawler.harvest_queue import HarvestTask, QueueError
from crawler.store import Event, Product, Response, Run, Store, digest


@pytest.fixture
def store(tmp_path):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    with store.session() as session:
        session.add(Run(id="run", name="Coordinator fixture", status="running"))
        session.add(Product(id="product", run_id="run", url="https://example.test/cards", status="running", epoch=1))
    yield store
    store.engine.dispose()


def request(base="123"):
    return {"method": "GET", "path": f"/service/rest/v1/products/{base}/prices", "params": {},
            "context": {"origin": "https://example.test", "session_scope": "opaque-session", "source_epoch": "epoch1",
                        "store_type": "broker", "currency": "USD", "service_signature": "opaque-service"}}


def manifest(*bases):
    return {"phase": "QUOTE", "discovery_complete": True,
            "requests": [{"phase": "QUOTE", "request": request(base), "lookup_evidence_id": "lookup"} for base in bases]}


async def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Coordinator did not reach expected state"
        await asyncio.sleep(0.01)


class Factory:
    def __init__(self, store, before=None):
        self.store, self.before = store, before
        self.calls, self.closed = [], []
        self.active = self.maximum = 0

    def __call__(self, claim):
        factory = self
        class Transport:
            async def fetch(self, url, **kwargs):
                factory.calls.append(claim)
                factory.active += 1
                factory.maximum = max(factory.maximum, factory.active)
                try:
                    if factory.before:
                        await factory.before(claim)
                    data = {"items": [{"quantity": 50, "markupPrice": "4.20"}]}
                    path = factory.store.put_object(json.dumps(data).encode(), "response")
                    if kwargs["params"]:
                        url += "?" + urlencode(kwargs["params"], doseq=True)
                    rid = digest([claim["task_id"], claim["epoch"]])
                    with factory.store.session() as session:
                        session.add(Response(id=rid, status=200, url=url, object_path=path,
                                             semantic_hash=digest(data), request_key=claim["request_key"]))
                    return SimpleNamespace(response_id=rid, status=200, data=data)
                finally:
                    factory.active -= 1
            async def close(self):
                factory.closed.append(claim["task_id"])
        return Transport()


@pytest.mark.asyncio
async def test_no_first_quote_until_preflight_is_durable_and_published(store):
    publishing, allow_publication = asyncio.Event(), asyncio.Event()
    publication_finished = False
    async def publish(run_id, kind, payload):
        nonlocal publication_finished
        if kind == "harvest_preflight":
            with store.session() as session:
                assert session.scalar(select(func.count()).select_from(Event).where(Event.type == kind)) == 1
            publishing.set()
            await allow_publication.wait()
            publication_finished = True
    async def before(claim):
        assert publication_finished
        with store.session() as session:
            assert session.scalar(select(func.count()).select_from(Event).where(Event.type == "harvest_preflight")) == 1
    factory = Factory(store, before)
    coordinator = HarvestCoordinator(store, factory, publish=publish, workers=2, poll_seconds=0.01)
    try:
        await coordinator.install("run", "product", "scope", manifest("1", "2"),
                                  discovery_summary={"candidate_count": 3, "resolved_count": 3, "validated_base_count": 2})
        await asyncio.sleep(0.03)
        assert factory.calls == []
        start = asyncio.create_task(coordinator.start("run", measured_rps=2))
        await publishing.wait()
        assert factory.calls == [] and not start.done()
        allow_publication.set()
        preflight = await start
        assert preflight["requests"]["manifest_unique_tasks"] == 2
        await until(lambda: coordinator.status("run")["coordinator_status"] == "requests_drained")
        assert len(factory.calls) == len(factory.closed) == 2
        status = coordinator.status("run")
        assert status["requests"]["succeeded"] == 2 and not status["catalog_complete"]
        assert status["products"][0]["discovery"]["candidate_count"] == 3
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_publication_failure_keeps_all_quotes_locked(store):
    async def fail(*args):
        raise RuntimeError("Publisher unavailable")
    factory = Factory(store)
    coordinator = HarvestCoordinator(store, factory, publish=fail, poll_seconds=0.01)
    try:
        await coordinator.install("run", "product", "scope", manifest("1"))
        with pytest.raises(RuntimeError, match="Publisher"):
            await coordinator.start("run", measured_rps=2)
        await asyncio.sleep(0.03)
        assert not factory.calls
        assert coordinator.status("run")["coordinator_status"] == "preflight_unpublished"
        coordinator.publish = None
        await coordinator.start("run", measured_rps=2)
        await until(lambda: coordinator.status("run")["requests"]["succeeded"] == 1)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_unresolved_discovery_and_missing_active_product_manifest_block_start(store):
    with store.session() as session:
        session.add(Product(id="second", run_id="run", url="https://example.test/other", status="running", epoch=1))
    factory = Factory(store)
    coordinator = HarvestCoordinator(store, factory)
    try:
        await coordinator.record_discovery("run", "second", {"candidate_count": 10, "resolved_count": 3,
                                                            "unresolved_count": 7, "discovery_complete": False})
        await coordinator.install("run", "product", "scope", manifest("1"))
        with pytest.raises(QueueError, match="Every active product"):
            await coordinator.start("run", measured_rps=2)
        status = coordinator.status("run")
        assert status["discovery_unresolved_product_ids"] == ["second"]
        assert status["products"][1]["discovery"]["unresolved_count"] == 7
        assert factory.calls == []
        with pytest.raises(QueueError, match="Unresolved"):
            await coordinator.install("run", "second", "scope", manifest("2"), discovery_summary={"unresolved_count": 7})
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_worker_limit_is_shared_across_runs_and_start_is_explicit(store):
    with store.session() as session:
        session.add(Run(id="other-run", name="Other run", status="running"))
        session.add(Product(id="other-product", run_id="other-run", url="https://example.test/other", status="running", epoch=1))
    async def slow(claim):
        await asyncio.sleep(0.03)
    factory = Factory(store, slow)
    coordinator = HarvestCoordinator(store, factory, workers=2, poll_seconds=0.01)
    try:
        await coordinator.install("run", "product", "scope", manifest("1", "2", "3"))
        await coordinator.install("other-run", "other-product", "scope", manifest("4", "5", "6"))
        await coordinator.start("run", measured_rps=2)
        await until(lambda: len(factory.calls) > 0)
        assert {call["run_id"] for call in factory.calls} == {"run"}
        await coordinator.start("other-run", measured_rps=2)
        await until(lambda: coordinator.status("run")["requests"]["succeeded"] == 3)
        await until(lambda: coordinator.status("other-run")["requests"]["succeeded"] == 3)
        assert factory.maximum <= 2 and len(factory.calls) == 6
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_external_run_pause_cancels_fetch_and_resume_republishes_preflight(store):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def pause_first(claim):
        if claim["attempt"] == 1:
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
    factory = Factory(store, pause_first)
    coordinator = HarvestCoordinator(store, factory, workers=1, poll_seconds=0.01)
    try:
        await coordinator.install("run", "product", "scope", manifest("1"))
        await coordinator.start("run", measured_rps=2)
        await started.wait()
        with store.session() as session:
            session.get(Run, "run").status = "paused"
        await until(lambda: cancelled.is_set())
        await until(lambda: coordinator.status("run")["coordinator_status"] == "paused")
        assert coordinator.status("run")["requests"]["succeeded"] == 0
        with store.session() as session:
            session.get(Run, "run").status = "running"
        await coordinator.start("run", measured_rps=2)
        await until(lambda: coordinator.status("run")["requests"]["succeeded"] == 1)
        with store.session() as session:
            assert session.scalar(select(func.count()).select_from(Event).where(Event.type == "harvest_preflight")) == 2
        assert len(factory.calls) == 2
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_and_new_coordinator_resume_never_refetch_checkpointed_response(store):
    factory = Factory(store)
    first = HarvestCoordinator(store, factory, workers=1, poll_seconds=0.01)
    await first.install("run", "product", "scope", manifest("1", "2"))
    await first.start("run", measured_rps=2)
    await until(lambda: first.status("run")["requests"]["succeeded"] == 2)
    await first.stop()
    second = HarvestCoordinator(store, factory, workers=1, poll_seconds=0.01)
    try:
        await second.start("run", measured_rps=2)
        await until(lambda: second.status("run")["coordinator_status"] == "requests_drained")
        assert len(factory.calls) == 2
        assert not second.status("run")["catalog_complete"]
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_explicit_coordinator_pause_stops_then_resumes_pending_tasks(store):
    started = asyncio.Event()
    async def wait_first(claim):
        if claim["attempt"] == 1:
            started.set()
            await asyncio.sleep(10)
    factory = Factory(store, wait_first)
    coordinator = HarvestCoordinator(store, factory, workers=1, poll_seconds=0.01)
    try:
        await coordinator.install("run", "product", "scope", manifest("1"))
        await coordinator.start("run", measured_rps=2)
        await started.wait()
        with pytest.raises(QueueError, match="Pause"):
            await coordinator.install("run", "product", "different", manifest("2"))
        await coordinator.pause("run")
        assert factory.active == 0 and coordinator.status("run")["coordinator_status"] == "paused"
        await coordinator.start("run", measured_rps=2)
        await until(lambda: coordinator.status("run")["requests"]["succeeded"] == 1)
    finally:
        await coordinator.stop()


def test_normalizer_keeps_completion_gate_and_full_unknown_context():
    flat = request()
    flat.update({"lookup_evidence_id": "lookup", "phase": "QUOTE"})
    flat["context"]["unproven_dimension"] = "retain"
    result = normalize_quote_manifest({"phase": "QUOTE", "discovery_complete": True, "requests": [flat]})
    assert result["requests"][0]["request"]["context"]["unproven_dimension"] == "retain"
    with pytest.raises(QueueError, match="completed QUOTE"):
        normalize_quote_manifest({"phase": "DISCOVERY", "discovery_complete": False, "requests": []})
    invalid = manifest("1")
    invalid["requests"][0]["request"]["path"] = "/unproven/prices"
    with pytest.raises(QueueError, match="source price-table"):
        normalize_quote_manifest(invalid)


@pytest.mark.asyncio
async def test_coordinator_with_real_transport_retains_query_and_full_context_binding(store, monkeypatch):
    import httpx
    from urllib.parse import urlparse
    import crawler.transport as transport_module
    from crawler.route_dependencies import request_key
    from crawler.store import ResponseObservation
    from crawler.transport import Transport

    async def local_validation(url):
        return urlparse(url)  # MockTransport only; no real DNS or network.
    monkeypatch.setattr(transport_module, "validate_url", local_validation)
    requests = []
    async def handle(http_request):
        requests.append(http_request)
        with store.session() as session:
            assert session.scalar(select(func.count()).select_from(Event).where(Event.type == "harvest_preflight")) == 1
        assert http_request.url.params["quantities"] == "50"
        return httpx.Response(200, json={"items": [{"quantity": 50, "markupPrice": "4.20"}]})
    quote = manifest("123")
    quote["requests"][0]["request"]["params"] = {"quantities": "50"}
    expected_identity = request_key(quote["requests"][0]["request"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        def factory(claim):
            transport = Transport(store, claim["run_id"], claim["product_id"], client=client)
            transport.epoch = 1
            return transport
        coordinator = HarvestCoordinator(store, factory, workers=1, poll_seconds=0.01)
        try:
            await coordinator.install("run", "product", "scope", quote)
            await coordinator.start("run", measured_rps=2)
            await until(lambda: coordinator.status("run")["requests"]["succeeded"] == 1)
            assert len(requests) == 1
            with store.session() as session:
                saved = session.scalar(select(Response))
                binding = session.scalar(select(ResponseObservation).where(ResponseObservation.kind == "harvest"))
                assert saved.url.endswith("/123/prices?quantities=50")
                assert binding.response_id == saved.id and binding.request_signature == expected_identity
            await coordinator.pause("run")
            await coordinator.start("run", measured_rps=2)
            await until(lambda: coordinator.status("run")["coordinator_status"] == "requests_drained")
            assert len(requests) == 1
        finally:
            await coordinator.stop()
