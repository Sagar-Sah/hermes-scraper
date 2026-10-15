"""Source ceilings are shared by ordinary browser and HTTP requests."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from crawler.adapters.browser import BrowserAdapter
from crawler.adapters.printograph import AdapterError
from crawler.config import Settings
from crawler.pacing import source_pacing
from crawler.store import Origin, Store
from crawler.transport import ExtractionError, RateLimiter, Transport
from test_browser_adapter import FixtureTransport
from test_transport import source

DEFAULTS = SimpleNamespace(requests_per_second=2, source_concurrency=2)


def test_pacing_defaults_and_explicit_profile_ceilings():
    assert source_pacing({}, DEFAULTS) == (2, 2)
    assert source_pacing(
        {"requests_per_second": 6, "max_concurrency": 4}, DEFAULTS
    ) == (6, 4)
    assert source_pacing(
        {"requests_per_second": "0.1", "max_concurrency": "1"}, DEFAULTS
    ) == (0.1, 1)
    assert source_pacing(
        {"requests_per_second": 100, "max_concurrency": 64}, DEFAULTS
    ) == (100, 64)
    assert source_pacing(
        {}, SimpleNamespace(requests_per_second=3, source_concurrency=5)
    ) == (3, 5)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        "bad",
        "nan",
        float("inf"),
        -float("inf"),
        0,
        -1,
        0.09,
        100.01,
        [],
    ],
)
def test_invalid_rate_is_rejected(value):
    with pytest.raises(ValueError, match="finite requests per second"):
        source_pacing({"requests_per_second": value}, DEFAULTS)


@pytest.mark.parametrize(
    "value",
    [None, True, False, "bad", "nan", float("inf"), -float("inf"), 0, -1, 1.5, 65, {}],
)
def test_invalid_concurrency_is_rejected(value):
    with pytest.raises(ValueError, match="whole concurrent requests"):
        source_pacing({"max_concurrency": value}, DEFAULTS)


@pytest.mark.parametrize("field", ["requests_per_second", "source_concurrency"])
def test_invalid_default_config_is_also_rejected(field):
    config = SimpleNamespace(requests_per_second=2, source_concurrency=2)
    setattr(config, field, float("nan"))
    with pytest.raises(ValueError):
        source_pacing({}, config)


@pytest.mark.parametrize("status", [429, 503])
def test_explicit_six_rps_still_shares_permits_and_retry_after(
    tmp_path, monkeypatch, status
):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    clock = [1000.0]
    monkeypatch.setattr(store, "now", lambda session: clock[0])
    first, second = RateLimiter(store), RateLimiter(store)
    origin = "https://local-fixture.invalid"
    rps, concurrency = source_pacing(
        {"requests_per_second": 6, "max_concurrency": 2}, DEFAULTS
    )
    try:
        token1, _ = first._grant(origin, rps, concurrency)
        assert token1
        assert second._grant(origin, rps, concurrency)[0] is None
        clock[0] += 0.17
        token2, _ = second._grant(origin, rps, concurrency)
        assert token2
        clock[0] += 0.17
        # Two separate products still share the same in-flight ceiling.
        assert first._grant(origin, rps, concurrency)[0] is None
        first.release(origin, token1, 0.2, status, 7, rps)
        second.release(origin, token2, 0.2, 200, 0, rps)
        with store.session() as session:
            state = session.get(Origin, origin)
            assert state.rps == 3
            assert state.backoff_until == pytest.approx(clock[0] + 7)
        clock[0] += 6.9
        assert second._grant(origin, rps, concurrency)[0] is None
        clock[0] += 0.2
        token3, _ = first._grant(origin, rps, concurrency)
        assert token3
        first.release(origin, token3, 0.2, 200, 0, rps)
    finally:
        store.engine.dispose()


class RecordingLimiter:
    def __init__(self):
        self.acquisitions = []
        self.caps = []

    async def acquire(self, origin, rps, concurrency, check):
        self.acquisitions.append((rps, concurrency))
        await check()
        return "fixture-permit"

    def release(self, origin, token, elapsed, status, retry_after, cap):
        self.caps.append(cap)


@pytest.mark.asyncio
async def test_http_transport_passes_profile_ceilings_to_shared_limiter(source):
    store, run_id, products = source
    store.save_profile(
        "source",
        {"host": "fixture.test", "requests_per_second": 6, "max_concurrency": 4},
        None,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
    ) as client:
        transport = Transport(store, run_id, products[0], client)
        transport.limiter = limiter = RecordingLimiter()
        await transport.fetch("https://fixture.test/pacing")
    assert limiter.acquisitions == [(6, 4)]
    assert limiter.caps == [6]


@pytest.mark.asyncio
async def test_http_invalid_pacing_does_not_send_request(source):
    store, run_id, products = source
    store.config.requests_per_second = float("nan")
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        transport = Transport(store, run_id, products[0], client)
        with pytest.raises(ExtractionError) as error:
            await transport.fetch("https://fixture.test/pacing")
    assert error.value.code == "invalid_source_pacing"
    assert calls == []


@pytest.mark.asyncio
async def test_browser_invalid_pacing_fails_before_opening_a_page(monkeypatch):
    monkeypatch.setattr("crawler.transport.validate_url", AsyncMock())
    page = AsyncMock(side_effect=AssertionError("Browser must not open"))
    monkeypatch.setattr("crawler.browser_session.browser_page", page)
    transport = FixtureTransport()
    transport.store.config.source_concurrency = float("inf")
    with pytest.raises(AdapterError) as error:
        async for _ in BrowserAdapter().extract(
            {"url": "https://fixture.test/product"}, transport, AsyncMock()
        ):
            pass
    assert error.value.code == "invalid_source_pacing"
    page.assert_not_called()


@pytest.mark.asyncio
async def test_browser_uses_explicit_profile_above_two_on_local_page(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<!doctype html><h1>Local pacing fixture</h1><div id="order-form"><label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select></div><div id="subtotal">$10.00</div>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr("crawler.transport.validate_url", AsyncMock())
    monkeypatch.setenv("ATLAS_BROWSER_HEADLESS", "true")
    monkeypatch.delenv("ATLAS_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.delenv("ATLAS_BROWSER_STORAGE_STATE", raising=False)
    transport = FixtureTransport()
    transport.limiter = limiter = RecordingLimiter()
    transport.store.profiles = lambda kind, secrets: [
        {"host": "127.0.0.1", "requests_per_second": 6, "max_concurrency": 4}
    ]
    try:
        rows = [
            row
            async for row in BrowserAdapter().extract(
                {
                    "url": f"http://127.0.0.1:{server.server_port}/product",
                    "max_configurations": 1,
                    "source_metadata": {
                        "currency": "USD",
                        "price_selector": "#subtotal",
                    },
                },
                transport,
                AsyncMock(),
            )
        ]
        assert len(rows) == 1 and rows[0]["subtotal"] == "10.00"
        assert limiter.acquisitions and set(limiter.acquisitions) == {(6, 4)}
        assert limiter.caps and set(limiter.caps) == {6}
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
