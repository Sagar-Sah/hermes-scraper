import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

import crawler.transport as transport_module
from crawler.config import Settings
from crawler.store import Product, RequestLease, Response, ResponseObservation, Run, Store
from crawler.transport import ExtractionError, RunInterrupted, Transport


@pytest.fixture
def source(tmp_path, monkeypatch):
    store = Store(
        Settings(
            data_dir=tmp_path,
            s3_bucket="",
            requests_per_second=100,
            source_concurrency=4,
        )
    )
    with store.session() as session:
        run = Run(name="Transport fixture", status="running")
        session.add(run)
        session.flush()
        products = [
            Product(
                run_id=run.id,
                url="https://fixture.test/product",
                status="running",
                epoch=1,
            )
            for _ in range(2)
        ]
        session.add_all(products)
        session.flush()
        ids = run.id, [product.id for product in products]

    async def fixture_url(url):
        return urlparse(url)  # all I/O is httpx.MockTransport, never real DNS/network

    monkeypatch.setattr(transport_module, "validate_url", fixture_url)
    yield store, *ids
    store.engine.dispose()


@pytest.mark.asyncio
async def test_global_request_lease_deduplicates_separate_transport_instances(source):
    store, run_id, ids = source
    calls = []

    async def handle(request):
        calls.append(request)
        await asyncio.sleep(0.03)
        return httpx.Response(200, json={"price": "19.99"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        first, second = [
            Transport(store, run_id, product_id, client) for product_id in ids
        ]
        results = await asyncio.gather(
            first.fetch("https://fixture.test/data", context={"pricing_group": 1}),
            second.fetch("https://fixture.test/data", context={"pricing_group": 1}),
        )
    assert len(calls) == 1
    assert {result.cached for result in results} == {True, False}
    assert results[0].response_id == results[1].response_id
    with store.session() as session:
        assert list(session.scalars(select(RequestLease))) == []


@pytest.mark.asyncio
async def test_http_replay_telemetry_groups_without_changing_evidence_or_cache_keys(source):
    store, run_id, ids = source
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"price": "19.99"})
    )) as client:
        transport = Transport(store, run_id, ids[0], client)
        for generation, quantity in ((1, 100), (2, 100), (3, 500)):
            result = await transport.fetch(
                "https://fixture.test/prices", method="POST", json={"quantity": quantity},
                context={"navigation_generation": generation, "account_context": "retail"},
            )
            assert not result.cached
    with store.session() as session:
        observations = list(session.scalars(select(ResponseObservation).where(
            ResponseObservation.product_id == ids[0]).order_by(ResponseObservation.created_at)))
        responses = list(session.scalars(select(Response)))
    assert len(observations) == len(responses) == 3
    assert len({response.request_key for response in responses}) == 3
    assert observations[0].request_signature == observations[1].request_signature
    assert observations[2].request_signature != observations[0].request_signature


@pytest.mark.asyncio
async def test_conditional_304_reuses_bytes_with_fresh_evidence_record(source):
    store, run_id, ids = source
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200, json={"rows": [1, 2]}, headers={"ETag": '"fixture-v1"'}
            )
        assert request.headers["if-none-match"] == '"fixture-v1"'
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        normal = Transport(store, run_id, ids[0], client)
        original = await normal.fetch("https://fixture.test/data")
        refreshed = await Transport(store, run_id, ids[0], client, refresh=True).fetch(
            "https://fixture.test/data"
        )
        cached = await normal.fetch("https://fixture.test/data")
    assert len(calls) == 2
    assert refreshed.status == 304 and refreshed.data == original.data
    assert refreshed.response_id != original.response_id
    assert cached.cached and cached.response_id == refreshed.response_id
    with store.session() as session:
        records = list(session.scalars(select(Response).order_by(Response.created_at)))
    assert len(records) == 2 and records[0].object_path == records[1].object_path


@pytest.mark.asyncio
async def test_retry_preserves_form_body_and_cache_separates_attribute_values(source):
    store, run_id, ids = source
    calls = []

    async def handle(request):
        body = await request.aread()
        calls.append((request, body))
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"busy": True}
            )
        values = parse_qs(body.decode(), keep_blank_values=True)
        return httpx.Response(200, json={"variation_id": 1, "attributes": values})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = Transport(store, run_id, ids[0], client)
        first = await transport.fetch(
            "https://fixture.test/?wc-ajax=get_variation",
            method="POST",
            data={
                "product_id": "42",
                "attribute_color": "blue&silver",
                "attribute_finish": "",
            },
        )
        second = await transport.fetch(
            "https://fixture.test/?wc-ajax=get_variation",
            method="POST",
            data={"product_id": "42", "attribute_color": "red", "attribute_finish": ""},
        )
    assert len(calls) == 3
    assert calls[0][1] == calls[1][1]
    assert (
        calls[0][0]
        .headers["content-type"]
        .startswith("application/x-www-form-urlencoded")
    )
    assert first.data["attributes"]["attribute_color"] == ["blue&silver"]
    assert first.data["attributes"]["attribute_finish"] == [""]
    assert second.data["attributes"]["attribute_color"] == ["red"]
    assert not second.cached


@pytest.mark.asyncio
async def test_cached_http_200_challenge_still_reports_access_required(source):
    store, run_id, ids = source
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            text="<html><title>Just a moment</title>Verify you are human</html>",
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = Transport(store, run_id, ids[0], client)
        for _ in range(2):
            with pytest.raises(ExtractionError) as error:
                await transport.fetch("https://fixture.test/protected")
            assert error.value.code == "access_required"


@pytest.mark.asyncio
async def test_reclaimed_epoch_stops_request_before_network(source):
    store, run_id, ids = source
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = Transport(store, run_id, ids[0], client)
        transport.epoch = 1
        with store.session() as session:
            session.get(Product, ids[0]).epoch = 2
        with pytest.raises(RunInterrupted, match="superseded"):
            await transport.fetch("https://fixture.test/data")
    assert calls == []


@pytest.mark.asyncio
async def test_retry_exhaustion_is_recoverable_and_preserves_evidence(source):
    store, run_id, ids = source
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"busy": True}, headers={"Retry-After": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = Transport(store, run_id, ids[0], client)
        with pytest.raises(ExtractionError) as error:
            await transport.fetch("https://fixture.test/data")
    assert error.value.code == "retry_exhausted"
    assert len(calls) == 4
    with store.session() as session:
        assert len(list(session.scalars(select(Response)))) == 4
        assert list(session.scalars(select(RequestLease))) == []


@pytest.mark.asyncio
async def test_credential_profile_change_does_not_reuse_previous_account_response(
    source,
):
    store, run_id, ids = source
    profile_id = store.save_profile(
        "source",
        {"host": "fixture.test"},
        {"headers": {"Authorization": "Bearer fixture-one"}},
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "price": (
                    "10.00"
                    if request.headers["authorization"].endswith("one")
                    else "20.00"
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = Transport(store, run_id, ids[0], client)
        first = await transport.fetch("https://fixture.test/data")
        store.save_profile(
            "source",
            {"id": profile_id, "host": "fixture.test"},
            {"headers": {"Authorization": "Bearer fixture-two"}},
        )
        second = await transport.fetch("https://fixture.test/data")
    assert len(calls) == 2 and first.data != second.data
    assert not second.cached
