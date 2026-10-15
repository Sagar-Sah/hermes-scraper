"""Pause fencing and contextual telemetry; all traffic is a local mock or capture."""

import json

import httpx
import pytest
from sqlalchemy import select

from crawler.engine import Engine
from crawler.store import (
    Event,
    Product,
    ProductRecordScope,
    ResponseObservation,
    Run,
    TrafficWindow,
)
from crawler.transport import RunInterrupted, Transport, observation_signature
from test_api import auth, service


def job(store, statuses=("running", "running"), *, requests=0, rows=0):
    with store.session() as session:
        run = Run(
            name="Independent product controls",
            status="running",
            spec={"adapter": "browser"},
        )
        session.add(run)
        session.flush()
        products = [
            Product(
                run_id=run.id,
                status=status,
                epoch=1,
                owner="old-worker",
                url=f"https://fixture.invalid/{i}",
                title=f"Product {i}",
                request_count=requests,
                row_count=rows,
                cache_hits=10,
            )
            for i, status in enumerate(statuses)
        ]
        session.add_all(products)
        session.flush()
        return run.id, [p.id for p in products]


def saved_row(key):
    return {
        "id": key,
        "title": "Fixture",
        "quantity": 100,
        "configuration": {"paper": "1"},
        "subtotal": "10.00",
        "currency": "USD",
    }


@pytest.mark.asyncio
async def test_product_pause_fences_old_worker_and_preserves_rows_and_sibling(service):
    client, store = service
    rid, (pid, sibling) = job(store)
    store.append_rows(pid, 1, [saved_row("saved")])
    paused = await client.post(
        f"/api/v1/runs/{rid}/products/{pid}/pause", headers=auth()
    )
    assert paused.status_code == 200
    by_id = {p["id"]: p for p in paused.json()["products"]}
    assert by_id[pid]["status"] == "paused" and by_id[pid]["row_count"] == 1
    assert by_id[sibling]["status"] == "running"
    assert paused.json()["status"] == "running"
    assert not store.heartbeat(pid, 1)
    with pytest.raises(RuntimeError, match="superseded"):
        store.append_rows(pid, 1, [saved_row("stale")])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Network must not run"))
    ) as http:
        transport = Transport(store, rid, pid, http)
        transport.epoch = 1
        with pytest.raises(RunInterrupted, match="superseded"):
            await transport.check()
    with store.session() as session:
        p = session.get(Product, pid)
        assert (p.epoch, p.owner, p.lease_until) == (2, "", 0)
        events = list(session.scalars(select(Event).where(Event.run_id == rid)))
    assert (
        events[-1].type == "product_pause" and events[-1].payload["preserved_rows"] == 1
    )
    assert [row["id"] for row in store.iter_rows(product_id=pid)] == ["saved"]
    # Repeating a pause is idempotent with respect to the fencing epoch.
    await client.post(f"/api/v1/runs/{rid}/products/{pid}/pause", headers=auth())
    with store.session() as session:
        assert session.get(Product, pid).epoch == 2


@pytest.mark.asyncio
async def test_individual_resume_leaves_other_products_paused_and_only_target_claimable(
    service,
):
    client, store = service
    rid, (pid, sibling) = job(store, statuses=("paused", "paused"))
    with store.session() as session:
        session.get(Run, rid).status = "paused"
    response = await client.post(
        f"/api/v1/runs/{rid}/products/{pid}/resume", headers=auth()
    )
    assert response.status_code == 200 and response.json()["status"] == "queued"
    states = {p["id"]: p["status"] for p in response.json()["products"]}
    assert states == {pid: "queued", sibling: "paused"}
    claim = store.claim("new-worker")
    assert claim["id"] == pid and claim["epoch"] == 2
    assert store.claim("another-worker") is None
    with store.session() as session:
        assert session.get(Product, sibling).status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses", [("paused", "paused"), ("completed", "paused")])
async def test_global_resume_never_exports_individually_paused_products(
    service, statuses
):
    client, store = service
    rid, ids = job(store, statuses=statuses)
    with store.session() as session:
        session.get(Run, rid).status = "paused"
    response = await client.post(f"/api/v1/runs/{rid}/resume", headers=auth())
    assert response.status_code == 200
    assert response.json()["status"] == "paused"
    assert {p["id"]: p["status"] for p in response.json()["products"]} == dict(
        zip(ids, statuses)
    )
    assert store.claim("must-not-run") is None
    engine = Engine(store)
    try:
        assert not engine.finish_run(rid)
    finally:
        await engine.client.aclose()
    with store.session() as session:
        assert session.get(Run, rid).status == "paused"
        assert all(not session.get(Product, pid).export_manifest for pid in ids)


@pytest.mark.asyncio
async def test_global_resume_still_queues_runnable_sibling_but_preserves_product_pause(
    service,
):
    client, store = service
    rid, (pid, sibling) = job(store, statuses=("queued", "paused"))
    with store.session() as session:
        session.get(Run, rid).status = "paused"
    response = await client.post(f"/api/v1/runs/{rid}/resume", headers=auth())
    assert response.json()["status"] == "queued"
    assert store.claim("allowed-worker")["id"] == pid
    with store.session() as session:
        assert session.get(Product, sibling).status == "paused"


@pytest.mark.asyncio
async def test_product_controls_reject_cross_run_and_invalid_action(service):
    client, store = service
    rid, (pid,) = job(store, statuses=("running",))
    other, _ = job(store, statuses=("running",))
    assert (
        await client.post(f"/api/v1/runs/{other}/products/{pid}/pause", headers=auth())
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/runs/{rid}/products/{pid}/destroy", headers=auth())
    ).status_code == 422
    assert (
        await client.post(f"/api/v1/runs/{rid}/products/{pid}/pause")
    ).status_code == 401
    with store.session() as session:
        assert session.get(Product, pid).status == "running"


@pytest.mark.asyncio
async def test_efficiency_window_excludes_historical_traffic_and_rows(service):
    client, store = service
    rid, (pid,) = job(store, statuses=("running",), requests=1000, rows=500)
    with store.session() as session:
        session.add(ProductRecordScope(product_id=pid, legacy_row_count=450))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No network"))
    ) as http:
        transport = Transport(store, rid, pid, http)
        transport._count(False)  # Establish baseline before the first new request.
        store.append_rows(pid, 1, [saved_row("fresh-one"), saved_row("fresh-two")])
        transport._count(False)
        transport._count(True)  # A cache hit cannot add a network request.
        await transport.record_observation(
            "https://fixture.invalid/data",
            200,
            {},
            b'{"price":1}',
            context={"lane": "browser_source", "request_controls": {"paper": "1"}},
            count_request=False,
        )
        await transport.record_observation(
            "https://fixture.invalid/data",
            200,
            {},
            b'{"price":1}',
            context={"lane": "browser_source", "request_controls": {"paper": "1"}},
            count_request=False,
        )
        await transport.record_observation(
            "https://fixture.invalid/data",
            503,
            {},
            b'{"error":"busy"}',
            context={"lane": "browser_source"},
            count_request=False,
        )
        await transport.record_observation(
            "https://fixture.invalid/product",
            200,
            {},
            b'{"price":1}',
            context={"lane": "browser_dom"},
            count_request=False,
        )
    result = (await client.get(f"/api/v1/runs/{rid}/efficiency", headers=auth())).json()
    assert result["network_requests"] == 2 and result["new_rows"] == 2
    assert result["rows_per_request"] == 1 and result["current_rows"] == 52
    assert result["source_response_observations"] == 2
    assert (
        result["distinct_response_keys"] == result["distinct_semantic_responses"] == 1
    )
    assert result["requests_per_unique_response"] == 2
    assert (
        result["attribution_scope"] == "since_telemetry_start" and result["started_at"]
    )
    assert result["products"][0]["network_requests"] == 2
    with store.session() as session:
        window = session.get(TrafficWindow, pid)
        assert (window.requests_start, window.cache_hits_start, window.rows_start) == (
            1000,
            10,
            500,
        )
        assert session.get(Product, pid).request_count == 1002


@pytest.mark.asyncio
async def test_same_url_and_body_with_distinct_pricing_contexts_remain_distinct(
    service,
):
    client, store = service
    rid, (pid,) = job(store, statuses=("running",))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No network"))
    ) as http:
        transport = Transport(store, rid, pid, http)
        for paper in ("1", "1", "2"):
            await transport.record_observation(
                "https://fixture.invalid/rules/turnarounds",
                200,
                {},
                b'{"days":2}',
                context={
                    "lane": "browser_source",
                    "request_controls": {"paper": paper},
                    "request_service_context": {"version": 2, "status": "observed"},
                },
            )
    result = (await client.get(f"/api/v1/runs/{rid}/efficiency", headers=auth())).json()
    assert result["source_response_observations"] == 3
    assert result["distinct_response_keys"] == 2
    assert result["distinct_semantic_responses"] == 2
    assert result["requests_per_unique_response"] == 1.5


@pytest.mark.asyncio
async def test_replayed_capture_identity_does_not_inflate_distinct_responses(service):
    client, store = service
    rid, (pid,) = job(store, statuses=("running",))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No network"))
    ) as http:
        transport = Transport(store, rid, pid, http)
        for generation in (1, 2):
            await transport.record_observation(
                "https://fixture.invalid/prices?quantity=100", 200, {},
                b'{"price":2}',
                context={
                    "lane": "browser_source", "navigation_generation": generation,
                    "navigation": generation, "selection_epoch": generation,
                    "generation": generation, "probe_job": f"probe-{generation}",
                    "event_id": generation, "request_id": str(generation),
                    "request_controls": {"paper": "2"},
                    "requested_selection": {"quantity": "100"},
                    "request_service_context": {"proofType": "31"},
                },
            )
    result = (await client.get(f"/api/v1/runs/{rid}/efficiency", headers=auth())).json()
    assert result["source_response_observations"] == 2
    assert result["distinct_response_keys"] == result["distinct_semantic_responses"] == 1
    assert result["requests_per_unique_response"] == 2
    with store.session() as session:
        observations = list(session.scalars(select(ResponseObservation).where(
            ResponseObservation.product_id == pid)))
    # Full capture context and individual response evidence are retained.
    assert len({item.response_id for item in observations}) == 2
    assert {item.context["navigation_generation"] for item in observations} == {1, 2}


@pytest.mark.parametrize("field,before,after", [
    ("request_controls", {"paper": "1"}, {"paper": "2"}),
    ("requested_selection", {"quantity": "100"}, {"quantity": "500"}),
    ("request_service_context", {"proofType": "31"}, {"proofType": "100"}),
    ("account_context", {"price_group": "retail"}, {"price_group": "trade"}),
    ("session_generation", "session-one", "session-two"),
    ("unknown_dimension", "one", "two"),
    ("nested", {"generation": 1}, {"generation": 2}),
])
def test_telemetry_signature_preserves_material_and_unknown_context(field, before, after):
    assert observation_signature("https://fixture.invalid/prices", {field: before}) != (
        observation_signature("https://fixture.invalid/prices", {field: after})
    )


def test_http_telemetry_signature_keeps_method_payload_and_auth_generation():
    base = {"method": "POST", "json": {"quantity": 100}, "credential_generation": "one"}
    signature = observation_signature("https://fixture.invalid/prices", {}, **base)
    for update in ({"method": "GET"}, {"json": {"quantity": 500}}, {"credential_generation": "two"}):
        assert observation_signature("https://fixture.invalid/prices", {}, **{**base, **update}) != signature


@pytest.mark.asyncio
async def test_telemetry_context_removes_nested_credential_values(service):
    _, store = service
    rid, (pid,) = job(store, statuses=("running",))
    sensitive = {
        "Authorization": "synthetic-auth",
        "cookie": "synthetic-cookie",
        "api_key": "synthetic-api-key",
        "X-API-Key": "synthetic-header-key",
        "password": "synthetic-password",
        "access_token": "synthetic-token",
        "client_secret": "synthetic-secret",
        "session_state": "synthetic-state",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No network"))
    ) as http:
        transport = Transport(store, rid, pid, http)
        await transport.record_observation(
            "https://fixture.invalid/prices",
            200,
            {},
            b'{"price":2}',
            context={
                "lane": "browser_source",
                "request_controls": {"paper": "2"},
                "nested": [sensitive],
                "headers": {"Other": "synthetic-header"},
            },
        )
    with store.session() as session:
        observation = session.scalar(
            select(ResponseObservation).where(ResponseObservation.product_id == pid)
        )
        serialized = json.dumps(observation.context)
    assert observation.context["request_controls"] == {"paper": "2"}
    assert all(
        secret not in serialized for secret in [*sensitive.values(), "synthetic-header"]
    )


@pytest.mark.asyncio
async def test_missing_telemetry_scope_stays_unknown_without_fabricated_ratios(service):
    client, store = service
    rid, _ = job(store, statuses=("paused",), requests=900, rows=80)
    result = (await client.get(f"/api/v1/runs/{rid}/efficiency", headers=auth())).json()
    assert result["network_requests"] == result["new_rows"] == 0
    assert result["started_at"] is None and result["current_rows"] is None
    assert (
        result["rows_per_request"] is None
        and result["requests_per_unique_response"] is None
    )
    assert (await client.get(f"/api/v1/runs/{rid}/efficiency")).status_code == 401
    assert (
        await client.get("/api/v1/runs/missing/efficiency", headers=auth())
    ).status_code == 404


@pytest.mark.asyncio
async def test_product_pause_during_export_cannot_publish_completed_product(
    service, monkeypatch
):
    from crawler import exporter

    _, store = service
    rid, (pid,) = job(store, statuses=("queued",))
    with store.session() as session:
        session.get(Run, rid).spec = {
            "adapter": "fixture",
            "fixture_rows_per_product": 3,
        }

    def paused_export(rows, output_dir, **kwargs):
        count = sum(1 for _ in rows)
        store.control_product(rid, pid, "pause")
        return {"items": [], "row_count": count}

    monkeypatch.setattr(exporter, "export_rows", paused_export)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        with store.session() as session:
            product = session.get(Product, pid)
            assert product.status == "paused" and product.row_count == 3
            assert not product.export_manifest
            assert not session.scalar(
                select(Event).where(
                    Event.run_id == rid, Event.type == "product_finished"
                )
            )
        assert not engine.finish_run(rid)
        assert store.status(rid) == "paused"
    finally:
        await engine.client.aclose()
