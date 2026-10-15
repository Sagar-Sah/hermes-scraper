import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import crawler.app as api
import crawler.dps_exact_quotes as exact


PILOT = Path(__file__).parent / "fixtures/dps-high-tack-pilot.json"
SOURCE_URL = "https://www.digital-print-solutions.com/vinyl-decals/3-mil-high-tack-vinyl-material"


@pytest.mark.asyncio
async def test_exact_quote_is_authenticated_active_group_only_and_not_finalization(monkeypatch):
    pilot = json.loads(PILOT.read_text())
    pilot.update({"id": "a" * 32, "url": SOURCE_URL})
    row = {"id": "19919", "supplier": "digital_print_solutions", "supplier_url": SOURCE_URL,
           "stage": "diagnosed", "dps_route_price_evidence": {"native_pilot_id": pilot["id"]}}
    campaign = {"products": [row]}
    def active(campaign_id, product_id):
        if product_id != "19919":
            raise ValueError("not_in_current_product_group")
        return campaign
    quote = AsyncMock(return_value={"price": {"price": "0.61"},
                                    "availability": "unverified", "price_scope": "exact_selection"})
    monkeypatch.setattr(api.app.state, "campaigns", SimpleNamespace(require_active_group=active), raising=False)
    monkeypatch.setattr(api.app.state, "dps_pilots", SimpleNamespace(get=lambda _: pilot), raising=False)
    monkeypatch.setattr(api.app.state, "dps_exact_quotes", SimpleNamespace(quote=quote), raising=False)
    monkeypatch.setattr(api.settings, "api_token", "fixture-token")
    body = {"width": 3, "height": 3, "quantity": 25,
            "selected_options": {"982": "4001", "703": "3083", "884": "3740"}}
    headers = {"Authorization": "Bearer fixture-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        path = "/api/v1/campaigns/c/dps-exact-quote/19919"
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.post(path.replace("19919", "queued"), json=body, headers=headers)).status_code == 409
        assert (await client.post(path, json={**body, "selected_options": {}}, headers=headers)).status_code == 422
        result = await client.post(path, json=body, headers=headers)
        assert result.status_code == 200
        assert result.json()["availability"] == "unverified"
        quote.assert_awaited_once()
        row["stage"] = "finalized"
        assert (await client.post(path, json=body, headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_rendered_quote_preflights_source_choice_and_starts_one_scoped_pilot(monkeypatch):
    pilot = json.loads(PILOT.read_text())
    pilot.update({"id": "a" * 32, "url": SOURCE_URL})
    row = {"id": "19919", "supplier": "digital_print_solutions", "supplier_url": SOURCE_URL,
           "stage": "diagnosed", "dps_route_price_evidence": {"native_pilot_id": pilot["id"]}}
    start = AsyncMock(return_value={"id": "b" * 32, "status": "opening",
                                    "availability": "unverified", "final_eligible": False})
    monkeypatch.setattr(api.app.state, "campaigns", SimpleNamespace(
        require_active_group=lambda _cid, _pid: {"products": [row]}), raising=False)
    monkeypatch.setattr(api.app.state, "dps_pilots", SimpleNamespace(
        get=lambda _: pilot, start=start), raising=False)
    monkeypatch.setattr(api.settings, "api_token", "fixture-token")
    headers = {"Authorization": "Bearer fixture-token"}
    body = {"width": 3, "height": 3, "quantity": 25,
            "selected_options": {"982": "4001", "703": "3083", "884": "3740"}}
    path = "/api/v1/campaigns/c/dps-rendered-quote/19919"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.post(path, json={**body, "width": 1000}, headers=headers)).status_code == 422
        assert start.await_count == 0
        result = await client.post(path, json=body, headers=headers)
        assert result.status_code == 200
        request = start.await_args.kwargs["expected_request"]
        assert request == {"width": "3", "height": "3", "quantity": "25",
                           "turnaroundId": "0", "options": "982E4001C703E3083C884E3740"}
        assert result.json()["final_eligible"] is False
        row["finalized_at"] = "now"
        assert (await client.post(path, json=body, headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_exact_quote_is_durable_and_reuses_fresh_scoped_response(tmp_path, monkeypatch):
    pilot = json.loads(PILOT.read_text())
    pilot.update({"id": "a" * 32, "url": SOURCE_URL})
    class FakeLimiter:
        def __init__(self, _store):
            self.acquires = 0
        async def acquire(self, *_args):
            self.acquires += 1
            return "token"
        def release(self, *_args):
            return None
    class FakeResponse:
        status_code = 200
        content = b'{"price":0.61,"uploadPrice":0.61,"designPrice":0.71}'
        headers = {}
        def json(self):
            return json.loads(self.content)
    class FakeClient:
        calls = 0
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def get(self, *_args, **_kwargs):
            type(self).calls += 1
            return FakeResponse()
    async def valid(_url):
        return None
    monkeypatch.setattr(exact, "RateLimiter", FakeLimiter)
    monkeypatch.setattr(exact, "validate_url", valid)
    monkeypatch.setattr(exact, "tls_context", lambda: True)
    monkeypatch.setattr(exact.httpx, "AsyncClient", FakeClient)
    manager = exact.DPSExactQuoteManager(SimpleNamespace(root=tmp_path))
    selection = {"982": "4001", "703": "3083", "884": "3740"}
    first = await manager.quote(pilot, width=3, height=3, quantity=25, selected_options=selection)
    second = await manager.quote(pilot, width=3, height=3, quantity=25, selected_options=selection)
    assert first["price"]["price"] == "0.61"
    assert first["availability"] == "unverified" and not first["cache_hit"]
    assert second["cache_hit"] and second["response_sha256"] == first["response_sha256"]
    assert FakeClient.calls == 1 and manager.limiter.acquires == 1
    assert len(list((tmp_path / "dps-exact-quotes").glob("*.json"))) == 1
    FakeClient.calls = 0
    second_manager = exact.DPSExactQuoteManager(SimpleNamespace(root=tmp_path / "concurrent"))
    results = await asyncio.gather(*[
        second_manager.quote(pilot, width=3, height=3, quantity=25,
                             selected_options=selection) for _ in range(10)
    ])
    assert FakeClient.calls == 1 and second_manager.limiter.acquires == 1
    assert sum(not item["cache_hit"] for item in results) == 1
