"""Local-only planner stages: immutable preflight, real Transport, no live source."""

import asyncio
from copy import deepcopy
import time

import httpx
import pytest
from sqlalchemy import func, select

from crawler.config import Settings
from crawler.harvest_queue import QueueError
from crawler.source_domains import (DomainAttempt, DomainBudget, DomainJob, DomainQueue,
    DomainTask, SourceDomainCoordinator, normalize_manifest, plan_known_reads, preflight)
from crawler.store import Event, Product, Response, ResponseObservation, Run, Store
from crawler.transport import ExtractionError, Transport


CONTEXT = {"origin": "https://www.printograph.com", "session_scope": "fixture-session",
           "source_epoch": "fixture-epoch", "store_type": "broker", "currency": "USD",
           "service_signature": "fixture-services"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from urllib.parse import urlparse
    async def local_validation(url):
        return urlparse(url)
    monkeypatch.setattr("crawler.transport.validate_url", local_validation)
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    with store.session() as s:
        s.add(Run(id="run", name="Domain fixture", status="running"))
        s.add(Product(id="product", run_id="run", url="https://www.printograph.com/products/business-cards/order", status="paused"))
        s.add(Product(id="second", run_id="run", url="https://www.printograph.com/products/club-flyers/order", status="paused"))
    yield store
    store.engine.dispose()


def manifest(offsets=(0,), *, specs=False):
    return plan_known_reads("product", "1", CONTEXT, include_specs=specs, listing_offsets=offsets)


async def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Domain stage did not reach the expected state"
        await asyncio.sleep(.01)


class Factory:
    def __init__(self, store, callback=None):
        self.store, self.callback, self.calls = store, callback, []

    def __call__(self, claim, budget):
        async def handle(request):
            self.calls.append(str(request.url))
            if self.callback:
                await self.callback(request)
            return httpx.Response(200, json={"items": [{"id": 100000021}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        transport = Transport(self.store, claim["run_id"], claim["product_id"], client=client)
        transport.owns_client = True
        # This paused epoch would fail the normal product-walker check. The
        # separately explicit domain scope must not resume or mutate that product.
        transport.epoch = 42
        return transport


def test_known_reads_count_is_not_configuration_or_total_budget():
    value = manifest((0, 25), specs=True)
    value["requests"].append(deepcopy(value["requests"][0]))
    value["requests"][-1]["product_id"] = "second"
    result = preflight(value, budget=10, measured_rps=2)
    assert result["planned_unique_requests"] == 3
    assert result["request_seconds_at_measured_rate"] == 1.5
    assert result["final_configuration_count"] is None
    assert not result["catalog_complete"] and not result["discovery_complete"] and not result["pricing_unlocked"]
    assert not result["is_end_to_end_eta"] and len(result["unresolved"]) == 3
    assert any(len(entry["products"]) == 2 for entry in normalize_manifest(value)["requests"])


def test_unproven_context_and_price_purpose_cannot_be_silently_normalized():
    value = manifest()
    extra = deepcopy(value["requests"][0])
    extra["request"]["context"]["selection"] = {"coating": "5"}
    value["requests"].append(extra)
    assert preflight(value, budget=2)["planned_unique_requests"] == 2
    value["requests"][0]["request"]["path"] = "/service/rest/v1/products/123/prices"
    with pytest.raises(QueueError, match="tier_domain"):
        normalize_manifest(value)
    value["requests"][0]["purpose"] = "tier_domain"
    assert normalize_manifest(value)["catalog_complete"] is False
    value["discovery_complete"] = True
    with pytest.raises(QueueError, match="completed"):
        normalize_manifest(value)


@pytest.mark.parametrize("mutation", ["secret", "origin", "cart", "customer"])
def test_only_public_read_routes_are_installed(mutation):
    value = manifest()
    request = value["requests"][0]["request"]
    if mutation == "secret":
        request["context"]["password"] = "forbidden"
    elif mutation == "origin":
        request["context"]["origin"] = "https://example.test"
    elif mutation == "cart":
        request["path"] = "/service/rest/v1/cart"
    else:
        request["params"]["cid"] = "123"
    with pytest.raises(ValueError):
        normalize_manifest(value)


def test_lookup_bound_known_catalog_and_tiers_do_not_validate_option_combinations():
    lookup = {"status": 200, "complete": True, "response_id": "lookup", "data": {"items": [{"id": 123}]},
              "request": {"method": "GET", "path": "/service/rest/v1/products", "context": CONTEXT,
                          "params": {"productType": "1", "size": "101", "paper": "1", "color": "2", "orientation": "1", "turnaround": "1"}}}
    planned = plan_known_reads("product", "1", CONTEXT, include_specs=False, listing_offsets=(),
                               lookup_observations=[lookup], include_base_tiers=True)
    assert {entry["purpose"] for entry in planned["requests"]} == {"option_catalog", "base_quantity_support", "tier_domain"}
    assert preflight(planned, budget=3)["planned_unique_requests"] == 3
    support = next(entry for entry in planned["requests"] if entry["purpose"] == "base_quantity_support")
    assert "turnaround" not in support["request"]["params"]
    lookup["data"]["items"].append({"id": 124})
    with pytest.raises(ValueError, match="exactly one"):
        plan_known_reads("product", "1", CONTEXT, lookup_observations=[lookup])


@pytest.mark.asyncio
async def test_dormant_install_and_preflight_publish_before_any_http(store):
    publishing, release = asyncio.Event(), asyncio.Event()
    published = False
    async def publish(run, kind, payload):
        nonlocal published
        assert kind == "source_domain_preflight"
        with store.session() as s:
            assert s.scalar(select(func.count()).select_from(Event).where(Event.type == kind)) == 1
        publishing.set()
        await release.wait()
        published = True
    async def before(request):
        assert published
    factory = Factory(store, before)
    coordinator = SourceDomainCoordinator(store, factory, publish=publish, poll_seconds=.01)
    job = coordinator.install("run", "stage", manifest(), budget=2, measured_rps=2)["job_id"]
    try:
        await asyncio.sleep(.03)
        assert not factory.calls
        starting = asyncio.create_task(coordinator.start(job))
        await publishing.wait()
        assert not factory.calls
        release.set()
        await starting
        await until(lambda: coordinator.status(job)["status"] == "requests_drained")
        result = coordinator.status(job)
        assert result["attempts_used"] == result["completed_requests"] == 1
        assert not result["catalog_complete"] and result["final_configuration_count"] is None
        assert "limit=1000" in factory.calls[0]
        with store.session() as s:
            assert s.get(Product, "product").status == "paused"
            assert s.scalar(select(func.count()).select_from(DomainAttempt)) == 1
            assert s.scalar(select(Response)).url == factory.calls[0]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_busy_queue_does_not_sleep_poll_interval_and_still_uses_source_pacing(store):
    dispatches, third = [], asyncio.Event()
    async def observe(request):
        dispatches.append(time.monotonic())
        if len(dispatches) == 3:
            third.set()
    factory = Factory(store, observe)
    # A deliberately long idle interval makes any per-task polling regression
    # observable without benchmarking machine-dependent database timings.
    coordinator = SourceDomainCoordinator(store, factory, workers=1, poll_seconds=60)
    job = coordinator.install("run", "busy-pump", manifest((0, 25, 50)), budget=3)["job_id"]
    try:
        await coordinator.start(job)
        await asyncio.wait_for(third.wait(), 4)
        await until(lambda: coordinator.status(job, include_timing=False)["status"] == "requests_drained")
        assert len(factory.calls) == coordinator.status(job)["completed_requests"] == 3
        # The fixture's existing two-per-second source limiter remains active.
        assert all(b - a >= .45 for a, b in zip(dispatches, dispatches[1:]))
        with store.session() as s:
            assert s.get(Product, "product").status == "paused"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["delayed_retry", "foreign_lease", "empty_active_set"])
async def test_unclaimable_queue_keeps_idle_poll_and_does_not_spin(store, monkeypatch, blocked):
    import crawler.source_domains as module
    coordinator = SourceDomainCoordinator(store, Factory(store), workers=1, poll_seconds=.1)
    job = coordinator.install("run", "idle-pump", manifest(), budget=3)["job_id"]
    with store.session() as s:
        row = s.get(DomainJob, job)
        row.status, row.owner, row.epoch = "running", coordinator.owner, 1
        request = s.scalar(select(DomainTask).where(DomainTask.job_id == job))
        if blocked == "delayed_retry":
            request.not_before = time.time() + 60
        else:
            request.status, request.owner, request.job_epoch = "leased", "other-worker", 1
            request.lease_until = time.time() + 60
    if blocked != "empty_active_set":
        coordinator._active.add(job)
    claims = []
    original = module.execute_worker
    async def counted(*args, **kwargs):
        result = await original(*args, **kwargs)
        claims.append(result["claimed"])
        return result
    monkeypatch.setattr(module, "execute_worker", counted)
    worker = asyncio.create_task(coordinator._pump(0))
    try:
        await asyncio.sleep(.04)
        assert claims == ([] if blocked == "empty_active_set" else [0])
        assert not coordinator.transport_factory.calls
        assert coordinator.status(job, include_timing=False)["attempts_used"] == 0
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await coordinator.close()


@pytest.mark.asyncio
async def test_failed_publication_and_uninstalled_factory_never_start(store):
    coordinator = SourceDomainCoordinator(store)
    job = coordinator.install("run", "stage", manifest(), budget=2)["job_id"]
    with pytest.raises(QueueError, match="dormant"):
        await coordinator.start(job)
    factory = Factory(store)
    coordinator.transport_factory = factory
    async def fail(*args):
        raise RuntimeError("publisher unavailable")
    coordinator.publish = fail
    with pytest.raises(RuntimeError, match="publisher"):
        await coordinator.start(job)
    assert not factory.calls and coordinator.status(job)["status"] == "preflight_failed"
    await coordinator.close()


@pytest.mark.asyncio
async def test_pause_and_restart_keep_completed_response_checkpoint(store):
    factory = Factory(store)
    coordinator = SourceDomainCoordinator(store, factory, workers=1, poll_seconds=.01)
    job = coordinator.install("run", "stage", manifest(), budget=4)["job_id"]
    await coordinator.start(job)
    await until(lambda: coordinator.status(job)["known_stage_requests_complete"])
    await coordinator.close()
    replacement = SourceDomainCoordinator(store, factory, poll_seconds=.01)
    try:
        assert replacement.status(job)["completed_requests"] == 1
        await replacement.start(job)
        await until(lambda: replacement.status(job)["status"] == "requests_drained")
        assert len(factory.calls) == 1
        with pytest.raises(QueueError, match="Immutable"):
            replacement.install("run", "stage", manifest((0, 25)), budget=4)
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_actual_http_attempt_budget_is_reserved_before_dispatch(store):
    coordinator = SourceDomainCoordinator(store)
    job_id = coordinator.install("run", "bounded", manifest((0, 25)), budget=1)["job_id"]
    with store.session() as s:
        job = s.get(DomainJob, job_id)
        job.status, job.owner, job.epoch = "running", "owner", 1
    budget = DomainBudget(store, job_id, "owner", 1)
    first = budget.reserve("bootstrap", "GET", CONTEXT["origin"] + "/products/business-cards/order")
    budget.finish(first, 200, .1)
    with pytest.raises(ExtractionError, match="exhausted"):
        budget.reserve("retry", "GET", CONTEXT["origin"] + "/service/rest/v1/products")
    assert coordinator.status(job_id)["attempts_used"] == 1
    with store.session() as s:
        s.get(Run, "run").status = "paused"
    with pytest.raises(ExtractionError, match="paused"):
        await budget.check()
    assert DomainQueue(store, job_id).claim("run", "worker") is None
    await coordinator.close()


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_discard_last_successful_inflight_response(store):
    factory = Factory(store)
    coordinator = SourceDomainCoordinator(store, factory, workers=2, poll_seconds=.01)
    job = coordinator.install("run", "bounded", manifest((0, 25)), budget=1)["job_id"]
    try:
        await coordinator.start(job)
        await until(lambda: coordinator.status(job)["status"] == "budget_exhausted")
        state = coordinator.status(job)
        assert len(factory.calls) == state["completed_requests"] == state["attempts_used"] == 1
        assert state["remaining_unique_requests"] == 1 and not state["known_stage_requests_complete"]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_run_pause_fences_inflight_response_and_product_pause_is_preserved(store):
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(request):
        entered.set()
        await release.wait()
    factory = Factory(store, hold)
    coordinator = SourceDomainCoordinator(store, factory, workers=1, poll_seconds=.01)
    job = coordinator.install("run", "paused", manifest(), budget=3)["job_id"]
    try:
        await coordinator.start(job)
        await entered.wait()
        await coordinator.pause(job)
        release.set()
        await asyncio.sleep(.05)
        assert coordinator.status(job)["completed_requests"] == 0
        with store.session() as s:
            assert s.get(Product, "product").status == "paused"
        factory.callback = None
        await coordinator.start(job)
        await until(lambda: coordinator.status(job)["known_stage_requests_complete"])
        # The paused fetch persisted immutable evidence but could not finish the
        # task. Resume may reuse that exact, fresh full-context Transport cache.
        assert len(factory.calls) == 1
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_domain_api_is_authenticated_and_dry_run_does_not_install_or_start(store, monkeypatch):
    import crawler.app as api
    coordinator = SourceDomainCoordinator(store)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api.app.state, "source_domains", coordinator, raising=False)
    monkeypatch.setattr(api.settings, "api_token", "domain-fixture-token")
    payload = {"manifest": manifest(), "budget": 3, "measured_rps": 2}
    headers = {"Authorization": "Bearer domain-fixture-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        endpoint = "/api/v1/runs/run/source-domains"
        assert (await client.post(endpoint + "/preflight", json=payload)).status_code == 401
        response = await client.post(endpoint + "/preflight", json=payload, headers=headers)
        assert response.status_code == 200 and response.json()["planned_unique_requests"] == 1
        with store.session() as s:
            assert s.scalar(select(func.count()).select_from(DomainJob)) == 0
        response = await client.post(endpoint, json={**payload, "scope": "api-fixture"}, headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "dormant"
        job = response.json()["job_id"]
        detail = (await client.get(f"/api/v1/source-domains/{job}", headers=headers)).json()
        assert detail["completed_requests"] == 0 and detail["observed_timing_included"] is True
        listed = (await client.get(endpoint, headers=headers)).json()["items"]
        assert len(listed) == 1 and listed[0]["observed_timing_included"] is False
        assert listed[0]["observed_stage_timing"] is None
        assert (await client.post(f"/api/v1/source-domains/{job}/start", headers=headers)).status_code == 409
        assert (await client.post(f"/api/v1/source-domains/{job}/pause", headers=headers)).json()["status"] == "paused"
    await coordinator.close()


@pytest.mark.asyncio
async def test_http_boolean_query_is_validated_using_wire_encoding(store):
    value = manifest()
    value["requests"][0]["request"]["params"]["requestValues"] = True
    factory = Factory(store)
    coordinator = SourceDomainCoordinator(store, factory, poll_seconds=.01)
    job = coordinator.install("run", "bool", value, budget=2)["job_id"]
    try:
        await coordinator.start(job)
        await until(lambda: coordinator.status(job)["status"] == "requests_drained")
        assert coordinator.status(job)["completed_requests"] == 1 and "requestValues=true" in factory.calls[0]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_domain_stage_keeps_run_active_with_paused_walkers_then_settles(store):
    from crawler.engine import Engine
    engine = Engine(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(request):
        entered.set()
        await release.wait()
    factory = Factory(store, hold)
    coordinator = SourceDomainCoordinator(store, factory, workers=1, poll_seconds=.01,
                                           on_stage_finished=engine.finish_run)
    engine.source_domains = coordinator
    job = coordinator.install("run", "separate-scope", manifest(), budget=2)["job_id"]
    try:
        await coordinator.start(job)
        await entered.wait()
        assert not engine.finish_run("run")
        assert store.status("run") == "running"
        release.set()
        await until(lambda: store.status("run") == "paused")
        assert coordinator.status(job)["known_stage_requests_complete"]
        with store.session() as s:
            assert s.get(Product, "product").status == s.get(Product, "second").status == "paused"
    finally:
        release.set()
        await coordinator.close()
        await engine.client.aclose()


@pytest.mark.asyncio
async def test_explicit_global_pause_wins_and_foreign_domain_owner_does_not_hold_run(store):
    from crawler.engine import Engine
    engine = Engine(store)
    coordinator = SourceDomainCoordinator(store, on_stage_finished=engine.finish_run)
    engine.source_domains = coordinator
    job = coordinator.install("run", "owner-fence", manifest(), budget=2)["job_id"]
    try:
        with store.session() as s:
            row = s.get(DomainJob, job)
            row.owner, row.status = coordinator.owner, "running"
            s.get(Run, "run").status = "paused"
        engine.finish_run("run")
        assert store.status("run") == "paused"
        with store.session() as s:
            s.get(Run, "run").status = "running"
            s.get(DomainJob, job).owner = "stale-owner"
        engine.finish_run("run")
        assert store.status("run") == "paused"
    finally:
        await coordinator.close()
        await engine.client.aclose()


def test_operational_queries_do_not_load_manifest_or_unclaimed_request_bodies(store):
    import re
    from sqlalchemy import event
    coordinator = SourceDomainCoordinator(store)
    job = coordinator.install("run", "small-reads", manifest(tuple(range(100))), budget=105)["job_id"]
    statements = []
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(store.engine, "before_cursor_execute", record)
    try:
        state = coordinator.status(job, include_responses=False)
        assert state["planned_unique_requests"] == 100 and not state["responses_included"]
        with store.session() as s:
            s.get(DomainJob, job).status = "running"
        claim = DomainQueue(store, job).claim("run", "worker")
        assert claim
    finally:
        event.remove(store.engine, "before_cursor_execute", record)
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert not any("source_domain_jobs.manifest " in statement or "source_domain_jobs.manifest," in statement for statement in selects)
    body_reads = [statement for statement in selects if re.search(r"source_domain_tasks\.request(?:\s|,)", statement)]
    assert len(body_reads) == 1 and "WHERE source_domain_tasks.id =" in body_reads[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["domain_session_unavailable", "domain_context_changed", "worker_error"])
async def test_session_factory_failure_stops_entire_918_request_stage(store, code):
    factory_calls, finished = [], []
    def factory(claim, budget):
        factory_calls.append(claim)
        raise ExtractionError("Shared session requires attention", code)
    coordinator = SourceDomainCoordinator(store, factory, workers=2, poll_seconds=.01,
                                           on_stage_finished=finished.append)
    job = coordinator.install("run", "shared-session-failure", manifest(tuple(range(918))), budget=1050)["job_id"]
    try:
        await coordinator.start(job)
        await until(lambda: bool(finished))
        state = coordinator.status(job, include_responses=False)
        assert state["status"] == "needs_attention" and state["attempts_used"] == 0
        assert state["task_counts"] == {"pending": 918, "leased": 0, "succeeded": 0, "failed": 0}
        assert 1 <= len(factory_calls) <= 2 and finished == ["run"]
        with store.session() as s:
            assert s.scalar(select(func.count()).select_from(Event).where(Event.type == "source_domain_attention")) == 1
            assert sum(s.scalars(select(DomainTask.attempts).where(DomainTask.job_id == job))) <= 2
        await asyncio.sleep(.04)
        assert len(factory_calls) <= 2  # no next-ID cascade after shared failure
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_source_access_error_is_stage_wide(store):
    calls = []
    def factory(claim, budget):
        async def handle(request):
            calls.append(request)
            return httpx.Response(403, json={"error": "session expired"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        transport = Transport(store, claim["run_id"], claim["product_id"], client=client)
        transport.owns_client = True
        return transport
    coordinator = SourceDomainCoordinator(store, factory, workers=1, poll_seconds=.01)
    job = coordinator.install("run", "expired", manifest((0, 25, 50)), budget=5)["job_id"]
    try:
        await coordinator.start(job)
        await until(lambda: coordinator.status(job)["status"] == "needs_attention")
        state = coordinator.status(job)
        assert state["attempts_used"] == len(calls) == 1
        assert state["task_counts"]["pending"] == 3 and state["task_counts"]["failed"] == 0
        assert state["task_errors"][0]["error"].startswith("access_required:")
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_explicit_domain_only_resume_does_not_resume_products_or_override_cancel(store):
    factory = Factory(store)
    coordinator = SourceDomainCoordinator(store, factory, poll_seconds=.01)
    job = coordinator.install("run", "explicit-planner-resume", manifest(), budget=3)["job_id"]
    try:
        with store.session() as s:
            s.get(Run, "run").status = "paused"
        with pytest.raises(QueueError, match="paused"):
            await coordinator.start(job)
        await coordinator.start(job, resume_run=True)
        await until(lambda: coordinator.status(job)["known_stage_requests_complete"])
        with store.session() as s:
            assert s.get(Product, "product").status == "paused"
            assert s.get(Run, "run").status == "running"
            s.get(Run, "run").status = "cancelled"
        await coordinator.pause(job)
        with pytest.raises(QueueError, match="cancelled"):
            await coordinator.start(job, resume_run=True)
        assert store.status("run") == "cancelled"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_targeted_failed_retry_rechecks_context_and_preserves_successes_budget_and_manifest(store):
    fail = True
    calls, validated = [], []
    def factory(claim, budget):
        async def handle(request):
            calls.append(str(request.url))
            return httpx.Response(404 if fail and request.url.params["offset"] == "0" else 200,
                                  json={"items": [{"id": 123}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        transport = Transport(store, claim["run_id"], claim["product_id"], client=client)
        transport.owns_client = True
        return transport
    def validate(claim):
        assert claim["request"]["context"] == CONTEXT
        validated.append(claim)
    coordinator = SourceDomainCoordinator(store, factory, workers=1, poll_seconds=.01, context_validator=validate)
    job = coordinator.install("run", "targeted-retry", manifest((0, 25)), budget=5)["job_id"]
    try:
        await coordinator.start(job)
        await until(lambda: coordinator.status(job)["status"] == "needs_attention")
        before = coordinator.status(job)
        with store.session() as s:
            original_manifest = s.get(DomainJob, job).manifest_hash
            failed = s.scalar(select(DomainTask).where(DomainTask.job_id == job, DomainTask.status == "failed"))
            key = failed.request_key
            s.get(Run, "run").status = "paused"
        with pytest.raises(QueueError, match="paused"):
            await coordinator.retry_failed(job, [key])
        fail = False
        await coordinator.retry_failed(job, [key], resume_run=True)
        await until(lambda: coordinator.status(job)["known_stage_requests_complete"])
        after = coordinator.status(job)
        assert len(validated) == 1 and len(calls) == after["attempts_used"] == 3
        assert after["attempt_budget"] == before["attempt_budget"] == 5
        assert before["responses"][0] in after["responses"]
        with store.session() as s:
            assert s.get(DomainJob, job).manifest_hash == original_manifest
            assert s.get(Product, "product").status == "paused"
        with pytest.raises(QueueError, match="must be failed"):
            await coordinator.retry_failed(job, [key])
    finally:
        await coordinator.close()

@pytest.mark.asyncio
async def test_source_404_remains_request_specific(store):
    calls = []
    def request_specific_factory(claim, budget):
        async def handle(request):
            calls.append(request)
            return httpx.Response(404 if request.url.params["offset"] == "0" else 200,
                                  json={"items": [{"id": 123}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        transport = Transport(store, claim["run_id"], claim["product_id"], client=client)
        transport.owns_client = True
        return transport
    coordinator = SourceDomainCoordinator(store, request_specific_factory, workers=1, poll_seconds=.01)
    job = coordinator.install("run", "one-missing", manifest((0, 25)), budget=4)["job_id"]
    try:
        await coordinator.start(job)
        await until(lambda: coordinator.status(job)["status"] == "needs_attention")
        state = coordinator.status(job)
        assert state["attempts_used"] == len(calls) == 2
        assert state["task_counts"]["failed"] == state["completed_requests"] == 1
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_lifecycle_timestamps_follow_real_progress_and_fence_late_finishes(store, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(store, "now", lambda session: clock[0])
    coordinator = SourceDomainCoordinator(store, Factory(store), context_validator=lambda claim: None)
    job_id = coordinator.install("run", "timestamps", manifest((0, 25)), budget=5)["job_id"]
    # Exercise the real lifecycle while controlling when each queue operation
    # runs; no background consumer or supplier request is needed.
    coordinator._workers = [asyncio.create_task(asyncio.Event().wait())]
    queue = DomainQueue(store, job_id)
    def stamp():
        return coordinator.status(job_id, include_responses=False)["updated_at"]
    try:
        assert stamp() == 100
        clock[0] = 110
        await coordinator.start(job_id)
        assert stamp() == 110
        clock[0] = 120
        claim = queue.claim("run", "worker")
        assert stamp() == 120
        budget = DomainBudget(store, job_id, claim["job_owner"], claim["job_epoch"])
        clock[0] = 125
        attempt = budget.reserve("domain", "GET", CONTEXT["origin"] + claim["request"]["path"])
        assert stamp() == 125
        clock[0] = 126
        budget.finish(attempt, 200, 1)
        assert stamp() == 126
        clock[0] = 127
        assert queue.heartbeat(claim["task_id"], "worker", claim["epoch"])
        assert stamp() == 126  # A lease renewal is not new request progress.
        clock[0] = 128
        assert queue.retry(claim["task_id"], "worker", claim["epoch"], "source_error: missing", retryable=False)
        assert stamp() == 128
        clock[0] = 130
        await coordinator.retry_failed(job_id, [claim["request_key"]])
        assert stamp() == 130
        clock[0] = 131
        claim = queue.claim("run", "worker")
        request = claim["request"]
        with store.session() as s:
            s.add(Response(id="timed-response", status=200,
                           url=str(httpx.URL(CONTEXT["origin"] + request["path"], params=request["params"]))))
            s.add(ResponseObservation(id="timed-binding", run_id="run", product_id="product",
                response_id="timed-response", kind="harvest", request_signature=claim["request_key"], created_at=132))
        clock[0] = 133
        assert queue.complete(claim["task_id"], "worker", claim["epoch"], "timed-response")
        assert stamp() == 133
        clock[0] = 140
        claim = queue.claim("run", "worker")
        assert queue.retry(claim["task_id"], "worker", claim["epoch"], "domain_session_unavailable: closed")
        assert stamp() == 140
        assert coordinator.status(job_id)["status"] == "needs_attention"
        clock[0] = 150
        await coordinator.pause(job_id)
        assert stamp() == 150
        clock[0] = 160
        budget.finish(attempt, 200, 35)
        assert stamp() == 150  # Old callbacks cannot rewrite the pause timestamp.
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_terminal_and_preflight_failure_timestamps_are_durable(store, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(store, "now", lambda session: clock[0])
    async def handle(request):
        clock[0] = 300
    coordinator = SourceDomainCoordinator(store, Factory(store, handle), workers=1, poll_seconds=.01)
    job_id = coordinator.install("run", "terminal-timestamp", manifest(), budget=2)["job_id"]
    try:
        clock[0] = 200
        await coordinator.start(job_id)
        await until(lambda: coordinator.status(job_id)["status"] == "requests_drained")
        assert coordinator.status(job_id)["updated_at"] == 300
        with store.session() as s:
            assert s.scalar(select(Event.created_at).where(Event.type == "source_domain_preflight")) == 200
        clock[0] = 400
        failed = coordinator.install("run", "failed-timestamp", manifest(), budget=2)["job_id"]
        async def fail(*args):
            clock[0] = 500
            raise RuntimeError("publication failed")
        coordinator.publish = fail
        with pytest.raises(RuntimeError, match="publication"):
            await coordinator.start(failed)
        assert coordinator.status(failed)["status"] == "preflight_failed"
        assert coordinator.status(failed)["updated_at"] == 500
    finally:
        await coordinator.close()


@pytest.mark.parametrize("evidence", ["complete", "missing", "preexisting"])
def test_observed_rate_uses_first_preflight_and_exact_response_bindings(store, evidence):
    coordinator = SourceDomainCoordinator(store)
    job_id = coordinator.install("run", "observed-rate", manifest((0, 25)), budget=5, measured_rps=100)["job_id"]
    assert coordinator.status(job_id)["observed_stage_timing"]["requests_per_second"] is None
    with store.session() as s:
        job = s.get(DomainJob, job_id)
        job.status, job.updated_at = "requests_drained", 99999
        # Two starts deliberately preserve the initial window across a pause.
        for timestamp in (100, 130):
            s.add(Event(run_id="run", type="source_domain_preflight", created_at=timestamp, payload={"job_id": job_id}))
        s.add(Event(run_id="run", type="source_domain_preflight", created_at=1, payload={"job_id": "another-stage"}))
        tasks = list(s.scalars(select(DomainTask).where(DomainTask.job_id == job_id).order_by(DomainTask.id)))
        for i, task in enumerate(tasks):
            task.status, task.response_id = "succeeded", f"response-{i}"
            timestamp = 90 if evidence == "preexisting" and i == 0 else 120 + i * 20
            if evidence != "missing" or i != 0:
                s.add(ResponseObservation(id=f"bound-{i}", run_id="run", product_id="product",
                    response_id=task.response_id, kind="harvest", request_signature=task.request_key, created_at=timestamp))
            # URL/response similarity and a different consumer are insufficient.
            s.add(ResponseObservation(id=f"other-{i}", run_id="run", product_id="second",
                response_id=task.response_id, kind="harvest", request_signature=task.request_key, created_at=9999))
    state = coordinator.status(job_id, include_responses=False)
    timing = state["observed_stage_timing"]
    assert state["supplied_preflight_requests_per_second"] == state["measured_requests_per_second"] == 100
    assert timing["first_preflight_at"] == 100
    assert timing["last_response_at"] == 140 and state["updated_at"] == 99999
    assert timing["complete_timing_coverage"] is (evidence == "complete")
    if evidence == "complete":
        assert timing["elapsed_seconds"] == 40 and timing["requests_per_second"] == .05
        replacement = SourceDomainCoordinator(store)
        assert replacement.status(job_id)["observed_stage_timing"] == timing
    else:
        assert timing["requests_per_second"] is None and timing["elapsed_seconds"] is None
    assert coordinator.status(job_id, include_timing=False)["observed_stage_timing"] is None


def test_timing_uses_selective_bounded_response_reads_across_large_histories(store):
    from sqlalchemy import event
    coordinator = SourceDomainCoordinator(store)
    job_id = coordinator.install("run", "indexed-timing", manifest(tuple(range(1030))), budget=1100)["job_id"]
    with store.session() as s:
        s.add(Event(run_id="run", type="source_domain_preflight", created_at=100, payload={"job_id": job_id}))
        tasks = list(s.scalars(select(DomainTask).where(DomainTask.job_id == job_id).order_by(DomainTask.id)))
        for index, task in enumerate(tasks):
            task.status, task.response_id = "succeeded", f"response-{index}"
            s.add(ResponseObservation(id=f"binding-{index}", run_id="run", product_id="product",
                response_id=task.response_id, request_signature=task.request_key, kind="harvest", created_at=101+index))
        # The same product/run's unrelated history must never be scanned per task.
        for index in range(5000):
            s.add(ResponseObservation(id=f"unrelated-{index}", run_id="run", product_id="product",
                response_id=f"old-{index}", request_signature="old-signature", kind="harvest", created_at=1))
        # Similar response ID, but wrong full binding, must still be excluded.
        s.add(ResponseObservation(id="wrong-binding", run_id="different-run", product_id="product",
            response_id="response-0", request_signature=tasks[0].request_key, kind="harvest", created_at=999999))
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM response_observations" in statement:
            queries.append((statement, parameters))
    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        result = coordinator.status(job_id, include_responses=False)["observed_stage_timing"]
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert result["bound_completed_requests"] == 1030
    assert result["first_response_at"] == 101 and result["last_response_at"] == 1130
    assert result["requests_per_second"] == 1
    assert len(queries) == 3 and all(len(params) <= 512 for _, params in queries)
    with store.engine.connect() as connection:
        for query, params in queries:
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + query, params).all()
            assert any("ix_response_observations_response_id" in str(step) for step in plan)
            assert not any("ix_response_observations_product_id" in str(step) for step in plan)
