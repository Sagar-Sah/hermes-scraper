"""Clock-controlled operator windows; isolated directories and fake transports only."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from crawler.autonomous_companion import AutonomousCompanion, OPERATOR_WINDOW_ENDED
from crawler.autonomous_companion_routes import autonomous_router
from crawler.companion_autonomy_integration import IntegratedAutonomousCompanion


BASE = "https://printitprints.com"


class Clock:
    def __init__(self):
        self.now = 1793336400.0
    def __call__(self):
        return self.now


def item(identifier):
    return {"id": identifier, "name": f"Product {identifier}", "permalink": f"{BASE}/shop/product-{identifier}/",
        "categories": [{"id": 1, "name": "Standard Print"}]}


def verified():
    return {"status": "verified", "coverage": {"complete": True, "fresh": True},
        "required_checks": ["prices"], "checks": {"prices": "match"}, "evidence_ids": ["fixture-only"]}


@pytest.fixture
async def make(tmp_path):
    managers = []
    def build(*, rows=None, handler=None, director=None, subdir=None):
        rows = [item(1), item(2)] if rows is None else rows
        clock, calls = Clock(), []
        async def fetch(url):
            calls.append(url)
            parts = urlsplit(url)
            page = int(parse_qs(parts.query)["page"][0])
            values = ([{"id": 1, "name": "Standard Print", "slug": "standard-print", "parent": 0}]
                if parts.path.endswith("/categories") else rows)
            return 200, url, json.dumps(values[(page - 1) * 100:page * 100]).encode()
        async def default_handler(*args):
            return verified()
        store = SimpleNamespace(root=tmp_path / (subdir or str(len(managers))))
        service = AutonomousCompanion(store, fetch=fetch, clock=clock, product_handler=handler or default_handler,
            director_review=director)
        service.mapping_resolver = lambda row: {"supplier": "source" + str(int(row["product_id"]) % 4),
            "source_url": f'https://example.org/product-{row["product_id"]}'}
        managers.append(service)
        return service, clock, calls, store
    yield build
    for service in managers:
        await service.close()


async def ready(service, clock, **kwargs):
    created = service.create_cycle(stop_at=clock() + 60, **kwargs)
    return await service.inventory(created["id"])


@pytest.mark.parametrize("bad", [True, "1793336410", 0, -1, float("nan"), float("inf"), -float("inf")])
async def test_cutoff_requires_finite_future_numeric_manual_window(make, bad):
    service, clock, _, _ = make()
    with pytest.raises(ValueError, match="invalid_manual_cycle_stop_at"):
        service.create_cycle(stop_at=bad)
    assert not service.cycles


async def test_cutoff_bound_and_pin_leave_monthly_config_and_legacy_requests_unchanged(make):
    service, clock, _, _ = make()
    original = deepcopy(service.config)
    for stop in [clock(), clock() - 1, clock() + 86400.01]:
        with pytest.raises(ValueError, match="invalid_manual_cycle_stop_at"):
            service.create_cycle(stop_at=stop)
    with pytest.raises(ValueError, match="invalid_manual_cycle_stop_at"):
        service.create_cycle(kind="scheduled", stop_at=clock() + 60)
    created = service.create_cycle(stop_at=clock() + 86400)
    assert created["stop_at"] == clock() + 86400 and len(created["request_sha256"]) == 64
    assert service.list_cycles()[0]["stop_at"] == created["stop_at"]
    assert service.config == original and not service.config["schedule_enabled"]
    service.cycles[created["id"]]["stop_at"] += 1
    with pytest.raises(ValueError, match="cycle_request_changed"):
        await service.inventory(created["id"])
    legacy, _, _, _ = make()
    old = legacy.create_cycle()
    assert "stop_at" not in old and "request_sha256" not in old
    assert old["scope"] == {"category_slugs": None, "product_ids": None}


@pytest.mark.parametrize("excluded", [[], ["0"], ["1", "1"], [1], [str(i) for i in range(1, 101)]])
async def test_exclusion_scope_rejects_empty_duplicate_invalid_or_excessive_ids(make, excluded):
    service, _, _, _ = make()
    with pytest.raises(ValueError, match="invalid_manual_catalog_scope"):
        service.create_cycle(exclude_product_ids=excluded)
    assert not service.cycles


async def test_exclusions_are_explicit_pinned_and_checked_against_published_catalog(make):
    service, clock, _, _ = make()
    with pytest.raises(ValueError, match="manual_catalog_scope_overlap"):
        service.create_cycle(product_ids=["1"], exclude_product_ids=["1"])
    with pytest.raises(ValueError, match="scheduled_scope_must_cover_all_catalog"):
        service.create_cycle(kind="scheduled", exclude_product_ids=["1"])
    created = service.create_cycle(exclude_product_ids=["999"])
    failed = await service.inventory(created["id"])
    assert failed["inventory"]["reason"] == "catalog_excluded_product_missing" and not failed["products"]
    separate, other_clock, _, _ = make()
    inventory = await ready(separate, other_clock, product_ids=["1"], exclude_product_ids=["2"])
    assert [row["product_id"] for row in inventory["products"]] == ["1"]
    separate.cycles[inventory["id"]]["scope"]["exclude_product_ids"] = []
    with pytest.raises(ValueError, match="cycle_request_changed"):
        await separate.start(inventory["id"])


async def test_full_catalog_retains_99_working_99_next_without_requoting_excluded_pilot(make):
    source_calls = []
    async def source(cycle, product):
        source_calls.append(product["product_id"])
        await asyncio.Event().wait()
    rows = [item(identifier) for identifier in range(1, 206)] + [item(9532)]
    service, clock, _, _ = make(rows=rows, handler=source)
    inventory = await ready(service, clock, exclude_product_ids=["9532"])
    assert inventory["inventory"]["catalog_count"] == 206 and inventory["inventory"]["selected_count"] == 205
    assert len(inventory["working_window"]) == len(inventory["next_window"]) == 99
    assert all(row["product_id"] != "9532" for row in inventory["products"])
    await service.start(inventory["id"])
    await service.tick(inventory["id"])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(source_calls) == 4 and "9532" not in source_calls
    clock.now = inventory["stop_at"]
    await service.tick(inventory["id"])
    await asyncio.gather(*service._active.values(), return_exceptions=True)
    await service.tick(inventory["id"])
    assert len(source_calls) == 4
    current = service.get_cycle(inventory["id"])
    assert current["status"] == "paused" and current["fault"] == OPERATOR_WINDOW_ENDED
    assert current["summary"]["held"] == 4 and current["summary"]["queued"] == 201
    assert current["request_sha256"] == inventory["request_sha256"]
    assert current["inventory_baseline_sha256"] == inventory["inventory_baseline_sha256"]


async def test_bulk_manual_scope_keeps_99_windows_and_cutoff_without_collecting_unselected_products(make):
    source_calls = []
    async def source(cycle, product):
        source_calls.append(product["product_id"])
        await asyncio.Event().wait()
    service, clock, _, _ = make(rows=[item(identifier) for identifier in range(1, 311)], handler=source)
    identifiers = [str(identifier) for identifier in range(1, 301)]
    inventory = await ready(service, clock, product_ids=identifiers)
    assert inventory["scope"]["product_ids"] == identifiers
    assert inventory["inventory"]["catalog_count"] == 310 and inventory["inventory"]["selected_count"] == 300
    assert len(inventory["working_window"]) == len(inventory["next_window"]) == 99
    assert [row["product_id"] for row in inventory["products"]] == identifiers
    await service.start(inventory["id"])
    await service.tick(inventory["id"])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(source_calls) == 4
    clock.now = inventory["stop_at"]
    await service.tick(inventory["id"])
    await asyncio.gather(*service._active.values(), return_exceptions=True)
    await service.tick(inventory["id"])
    current = service.get_cycle(inventory["id"])
    assert current["status"] == "paused" and current["pause_reason"] == OPERATOR_WINDOW_ENDED
    assert len(source_calls) == 4 and current["summary"]["queued"] == 296


async def test_bulk_manual_scope_accepts_1000_but_not_1001_and_exclusions_stay_99(make):
    service, _, _, _ = make()
    with pytest.raises(ValueError, match="invalid_manual_catalog_scope"):
        service.create_cycle(product_ids=[str(identifier) for identifier in range(1, 1002)])
    with pytest.raises(ValueError, match="invalid_manual_catalog_scope"):
        service.create_cycle(exclude_product_ids=[str(identifier) for identifier in range(1, 101)])
    created = service.create_cycle(product_ids=[str(identifier) for identifier in range(1, 1001)])
    assert len(created["scope"]["product_ids"]) == 1000


async def test_expired_start_resume_and_inventory_do_not_request_or_release_supplier_holds(make):
    source_calls = []
    async def source(*args):
        source_calls.append(1)
        return verified()
    service, clock, catalog, _ = make(handler=source)
    inventory = await ready(service, clock)
    count = len(catalog)
    clock.now = inventory["stop_at"]
    record = service.cycles[inventory["id"]]
    record["supplier_blocks"]["source1"] = "supplier_login_required"
    for operation in [lambda: service.start(inventory["id"]), lambda: service.resume(inventory["id"], supplier="source1"), lambda: service.inventory(inventory["id"])]:
        assert (await operation())["pause_reason"] == OPERATOR_WINDOW_ENDED
    await service.tick(inventory["id"])
    assert not source_calls and len(catalog) == count and record["supplier_blocks"]


async def test_cutoff_cancels_source_and_director_preserves_unknown_effects_and_unrelated_tasks(make):
    source_started, director_started = asyncio.Event(), asyncio.Event()
    async def source(*args):
        source_started.set()
        await asyncio.Event().wait()
    async def director(*args):
        director_started.set()
        await asyncio.Event().wait()
    service, clock, _, _ = make(rows=[item(i) for i in range(1, 102)], handler=source, director=director)
    service.configure(director_enabled=True, director_provider_id="fixture-director")
    inventory = await ready(service, clock)
    record = service.cycles[inventory["id"]]
    for job in record["products"][:99]:
        job.update(status="needs_attention", reason="fixture_complete", ended_at=clock())
    await service.start(inventory["id"])
    await service.tick(inventory["id"])
    await asyncio.wait_for(source_started.wait(), 1)
    await asyncio.wait_for(director_started.wait(), 1)
    unrelated = asyncio.create_task(asyncio.sleep(60))
    try:
        service._review_tasks[("f" * 32, 0)] = unrelated
        clock.now = inventory["stop_at"]
        await service.tick(inventory["id"])
        owned = [task for (cid, _), task in [*service._active.items(), *service._review_tasks.items()] if cid == inventory["id"]]
        await asyncio.gather(*owned, return_exceptions=True)
        assert not unrelated.done() and not unrelated.cancelling()
        assert record["status"] == "paused" and record["pause_reason"] == OPERATOR_WINDOW_ENDED
        assert all(job["reason"] == "handler_interrupted_unknown_effect" for job in record["products"][99:])
        assert record["director_reviews"]["0"]["reason"] == "director_interrupted"
    finally:
        service._review_tasks.pop(("f" * 32, 0), None)
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


async def test_cutoff_excludes_caller_and_is_cheap_after_work_settles(make, monkeypatch):
    service, clock, _, _ = make()
    inventory = await ready(service, clock)
    clock.now = inventory["stop_at"]
    caller = asyncio.current_task()
    service._inventory_tasks[inventory["id"]] = caller
    try:
        await service.tick(inventory["id"])
        assert caller.cancelling() == 0
    finally:
        service._inventory_tasks.pop(inventory["id"], None)
    def forbidden(*args, **kwargs):
        raise AssertionError("Settled cutoff repeated a write or all-product timing walk")
    monkeypatch.setattr(service, "_sync_timing", forbidden)
    monkeypatch.setattr(service, "_save_cycle", forbidden)
    await service.tick(inventory["id"])
    await service.tick(inventory["id"])


async def test_restart_expired_cycle_pauses_before_any_new_dispatch(make):
    service, clock, _, store = make()
    inventory = await ready(service, clock)
    await service.start(inventory["id"])
    record = service.cycles[inventory["id"]]
    record["products"][0]["status"] = "running"
    service._save_cycle(record)
    await service.close()
    clock.now = inventory["stop_at"] + 1
    async def forbidden(*args):
        raise AssertionError("Expired restart performed I/O")
    restarted = AutonomousCompanion(store, clock=clock, fetch=forbidden, product_handler=forbidden)
    try:
        current = restarted.get_cycle(inventory["id"])
        assert current["status"] == "paused" and current["fault"] == OPERATOR_WINDOW_ENDED
        assert current["products"][0]["reason"] == "handler_interrupted_unknown_effect"
        await restarted.tick()
        await restarted.start(inventory["id"])
        await restarted.resume(inventory["id"])
        assert not restarted._active
    finally:
        await restarted.close()


async def test_expiry_during_catalog_stops_before_next_page_and_does_not_cancel_request_caller(make):
    service, clock, calls, _ = make()
    original = service.fetch
    async def fetch(url):
        response = await original(url)
        clock.now += 60
        return response
    service.fetch = fetch
    created = service.create_cycle(stop_at=clock() + 60)
    current = await service.inventory(created["id"])
    assert len(calls) == 1 and current["status"] == "paused"
    assert current["inventory"]["reason"] == OPERATOR_WINDOW_ENDED
    assert asyncio.current_task().cancelling() == 0


async def test_expiry_between_durable_dispatch_and_task_entry_never_calls_supplier(make, monkeypatch):
    calls = []
    async def source(*args):
        calls.append(1)
        return verified()
    service, clock, _, _ = make(handler=source)
    inventory = await ready(service, clock)
    await service.start(inventory["id"])
    original = service._save_cycle
    def save(record):
        original(record)
        if any(job["status"] == "running" for job in record["products"]):
            clock.now = inventory["stop_at"]
    monkeypatch.setattr(service, "_save_cycle", save)
    await service.tick(inventory["id"])
    await asyncio.gather(*service._active.values(), return_exceptions=True)
    await service.tick(inventory["id"])
    assert not calls and service.get_cycle(inventory["id"])["fault"] == OPERATOR_WINDOW_ENDED


async def test_late_handler_result_cannot_become_green_after_cutoff(make):
    clock_ref = []
    async def source(*args):
        clock_ref[0].now += 60
        return verified()
    service, clock, _, _ = make(rows=[item(1)], handler=source)
    clock_ref.append(clock)
    inventory = await ready(service, clock)
    await service.start(inventory["id"])
    await service.tick(inventory["id"])
    await asyncio.gather(*service._active.values(), return_exceptions=True)
    current = service.get_cycle(inventory["id"])
    assert current["status"] == "paused" and current["products"][0]["status"] == "held"
    assert current["products"][0]["result"] is None


async def test_expiry_during_review_admission_cannot_be_overwritten_by_cycle_completion(make, monkeypatch):
    service, clock, _, _ = make(rows=[item(1)])
    inventory = await ready(service, clock)
    record = service.cycles[inventory["id"]]
    record["products"][0].update(status="needs_attention", reason="fixture_complete", ended_at=clock())
    await service.start(inventory["id"])
    def reviews(cycle):
        clock.now = inventory["stop_at"]
        service._pause_at_cutoff(cycle)
    monkeypatch.setattr(service, "_maybe_reviews", reviews)
    await service.tick(inventory["id"])
    current = service.get_cycle(inventory["id"])
    assert current["status"] == "paused" and current["fault"] == OPERATOR_WINDOW_ENDED
    assert current["finished_at"] is None


async def test_active_supplier_timeout_is_capped_by_remaining_window_without_waiting_for_tick(make):
    cancelled = asyncio.Event()
    async def source(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    service, clock, _, _ = make(rows=[item(1)], handler=source)
    created = service.create_cycle(stop_at=clock() + .03)
    await service.inventory(created["id"])
    await service.start(created["id"])
    asyncio.get_running_loop().call_later(.03, setattr, clock, "now", created["stop_at"])
    await service.tick(created["id"])
    await asyncio.wait_for(asyncio.gather(*service._active.values(), return_exceptions=True), 1)
    current = service.get_cycle(created["id"])
    assert cancelled.is_set() and current["status"] == "paused" and current["fault"] == OPERATOR_WINDOW_ENDED
    assert current["products"][0]["reason"] == "handler_interrupted_unknown_effect"


async def test_director_receives_cutoff_and_timeout_cap_even_after_product_cycle_finishes(make):
    contexts, cancelled = [], asyncio.Event()
    async def director(context, digest):
        contexts.append(context)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    service, clock, _, _ = make(rows=[item(1)], director=director)
    service.configure(director_enabled=True, director_provider_id="fixture-director")
    created = service.create_cycle(stop_at=clock() + .03)
    await service.inventory(created["id"])
    await service.start(created["id"])
    await service.tick(created["id"])
    await asyncio.gather(*service._active.values(), return_exceptions=True)
    asyncio.get_running_loop().call_later(.03, setattr, clock, "now", created["stop_at"])
    await service.tick(created["id"])
    await asyncio.wait_for(asyncio.gather(*service._review_tasks.values(), return_exceptions=True), 1)
    assert contexts[0]["stop_at"] == created["stop_at"] and cancelled.is_set()
    current = service.get_cycle(created["id"])
    assert current["status"] == "paused" and current["director_reviews"]["0"]["reason"] == "director_interrupted"


async def test_connector_catalog_override_propagates_cutoff_and_stops_next_private_page(tmp_path):
    clock, calls = Clock(), []
    class Store:
        root = tmp_path
        def profiles(self, *args, **kwargs):
            return []
    async def public(url):
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        rows = [{"id": 1, "name": "Standard Print", "slug": "standard-print", "parent": 0}] if page == 1 else []
        return 200, url, json.dumps(rows).encode()
    service = IntegratedAutonomousCompanion(Store(), SimpleNamespace(pairs={}, campaigns=SimpleNamespace(list=lambda: [])), SimpleNamespace(), clock=clock, fetch=public)
    service.mapping_resolver = None
    service.environments.published_catalog_connection = lambda base: {"role": "read"}
    async def private(base, page):
        calls.append(page)
        document = {"schema": "hermes-companion-catalog/v1", "site_origin": BASE, "environment": "production",
            "items": [item(i) for i in range(1, 101)], "page": page, "total": 101, "pages": 2, "next_cursor": 2}
        clock.now += 60
        return 200, BASE + "/wp-json/hermes-companion/v1/catalog?page=" + str(page), json.dumps(document).encode(), {}
    service.environments.published_catalog_page = private
    try:
        created = service.create_cycle(stop_at=clock() + 60)
        current = await service.inventory(created["id"])
        assert calls == [1] and current["inventory"]["reason"] == OPERATOR_WINDOW_ENDED
        assert current["status"] == "paused"
    finally:
        await service.close()


def test_authenticated_cycle_api_accepts_scoped_cutoff_and_rejects_coercion(tmp_path):
    service = AutonomousCompanion(SimpleNamespace(root=tmp_path), clock=lambda: 1000)
    app = FastAPI()
    app.include_router(autonomous_router(lambda: service, lambda: True))
    with TestClient(app) as client:
        for value in [True, "1060"]:
            assert client.post("/api/v1/companion/autonomy/cycles", json={"stop_at": value}).status_code == 422
        assert client.post("/api/v1/companion/autonomy/cycles", json={"stop_at": 87401}).status_code == 409
        assert client.post("/api/v1/companion/autonomy/cycles", json={"product_ids": [str(identifier) for identifier in range(1, 1002)]}).status_code == 422
        assert client.post("/api/v1/companion/autonomy/cycles", json={"exclude_product_ids": [str(identifier) for identifier in range(1, 101)]}).status_code == 422
        result = client.post("/api/v1/companion/autonomy/cycles", json={"stop_at": 1060, "exclude_product_ids": ["9532"], "product_ids": [str(identifier) for identifier in range(1, 301)]})
        assert result.status_code == 200
        assert result.json()["stop_at"] == 1060 and result.json()["scope"]["exclude_product_ids"] == ["9532"]
        assert len(result.json()["scope"]["product_ids"]) == 300
    asyncio.run(service.close())
