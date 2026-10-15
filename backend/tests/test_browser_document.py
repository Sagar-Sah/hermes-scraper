"""Bounded product-document capture: local fixtures only, no supplier requests."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from crawler.browser_document import capture_product_document
from crawler.browser_session import BrowserSessionPool
from crawler.transport import ExtractionError, RunInterrupted


class Limiter:
    def __init__(self):
        self.acquired, self.released = [], []

    async def acquire(self, origin, rate, concurrency, check):
        await check()
        self.acquired.append((origin, rate, concurrency))
        return "fixture-permit"

    def release(self, *args):
        self.released.append(args)


class Transport:
    def __init__(self, *, pool=None, max_requests=1, fail_check=None):
        self.browser_pool = pool
        self.calls, self.counts, self.checks, self.observed = 0, [], 0, []
        self.max_requests, self.fail_check = max_requests, fail_check
        self.limiter = Limiter()
        self.store = SimpleNamespace(
            profiles=lambda kind: [{"host": "example.test", "requests_per_second": .5, "max_concurrency": 1}],
            config=SimpleNamespace(requests_per_second=2, source_concurrency=2, max_response_bytes=65536),
        )

    async def check(self):
        self.checks += 1
        if self.fail_check and self.checks >= self.fail_check:
            raise RunInterrupted("Fixture worker superseded", "interrupted")

    def _count(self, cached):
        self.counts.append(cached)

    async def record_observation(self, url, status, headers, body, **kwargs):
        self.observed.append((url, status, body, kwargs))
        return SimpleNamespace(data=body.decode(), response_id="fixture-response", status=status)


class FakeRoute:
    def __init__(self, url, method="GET", resource_type="document"):
        self.request = SimpleNamespace(url=url, method=method, resource_type=resource_type)
        self.continued, self.aborted = False, False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        self.aborted = True


class FakePage:
    def __init__(self, *, body=b"<title>Product</title>", status=200, headers=None, goto_error=None, duplicate_document=False):
        self.body, self.status = body, status
        self.headers, self.goto_error = headers or {}, goto_error
        self.routes = []
        self.context, self.duplicate_document = self, duplicate_document
        self.closed = False

    async def new_cdp_session(self, page):
        return self

    def on(self, pattern, handler):
        self.handler = handler

    async def send(self, method, params=None):
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main"}}}
        if method == "Fetch.continueRequest":
            self.routes[int(params["requestId"])].continued = True
        elif method == "Fetch.failRequest":
            self.routes[int(params["requestId"])].aborted = True

    async def close(self):
        self.closed = True

    async def detach(self):
        pass

    async def goto(self, url, **kwargs):
        if self.goto_error:
            raise self.goto_error
        for args in [(url, "GET", "Document"), (url, "GET", "Document" if self.duplicate_document else "Fetch"), (url, "POST", "Fetch"), (url + "/asset", "GET", "Image")]:
            route = FakeRoute(*args)
            self.routes.append(route)
            self.handler({"requestId": str(len(self.routes)-1), "frameId": "main", "resourceType": route.request.resource_type,
                          "request": {"url": route.request.url, "method": route.request.method}})
            while not route.continued and not route.aborted:
                await asyncio.sleep(0)
        async def all_headers():
            return self.headers
        async def body():
            return self.body
        return SimpleNamespace(url=url, status=self.status, all_headers=all_headers, body=body)


@pytest.fixture
def setup(monkeypatch):
    async def validate(url):
        return urlparse(url)
    monkeypatch.setattr("crawler.browser_document.validate_url", validate)
    def install(page):
        @asynccontextmanager
        async def browser_page(*args, **kwargs):
            yield page
        monkeypatch.setattr("crawler.browser_document.browser_page", browser_page)
    return install


@pytest.mark.asyncio
async def test_single_get_blocks_duplicates_assets_and_writes_and_releases_permit(setup):
    page, transport = FakePage(), Transport()
    setup(page)
    result = await capture_product_document(transport, "https://example.test/product")
    assert result.response_id == "fixture-response"
    assert transport.calls == len(transport.counts) == 1
    assert page.routes[0].continued and all(route.aborted for route in page.routes[1:])
    assert transport.limiter.acquired == [("https://example.test", .5, 1)]
    assert len(transport.limiter.released) == 1 and transport.limiter.released[0][3] == 200
    assert transport.observed[0][3] == {"context": {"adapter": "printitprints", "lane": "browser_document", "capture": "exact_product_document", "subresources": "blocked"}, "count_request": False}


@pytest.mark.asyncio
async def test_exhausted_budget_and_superseded_worker_send_no_request(setup):
    page, transport = FakePage(), Transport()
    setup(page)
    transport.calls = 1
    with pytest.raises(ExtractionError) as error:
        await capture_product_document(transport, "https://example.test/product")
    assert error.value.code == "request_limit"
    assert not page.routes and not transport.limiter.acquired
    transport = Transport(fail_check=1)
    with pytest.raises(RunInterrupted):
        await capture_product_document(transport, "https://example.test/product")
    assert not page.routes and not transport.limiter.acquired


@pytest.mark.asyncio
async def test_worker_fence_at_dispatch_and_repeat_navigation_are_terminal_without_second_get(setup):
    page, transport = FakePage(), Transport(fail_check=3)
    setup(page)
    with pytest.raises(RunInterrupted):
        await capture_product_document(transport, "https://example.test/product")
    assert transport.calls == 0 and not transport.counts and not transport.observed
    assert page.closed and len(transport.limiter.released) == 1
    page, transport = FakePage(duplicate_document=True), Transport()
    setup(page)
    with pytest.raises(ExtractionError) as error:
        await capture_product_document(transport, "https://example.test/product")
    assert error.value.code == "source_error"
    assert transport.calls == 1 and not transport.observed and page.routes[1].aborted
    assert page.closed and len(transport.limiter.released) == 1


@pytest.mark.asyncio
async def test_budget_rechecked_after_waiting_for_permit(setup):
    page, transport = FakePage(), Transport()
    setup(page)
    original = transport.limiter.acquire
    async def acquire(*args):
        token = await original(*args)
        transport.calls = 1
        return token
    transport.limiter.acquire = acquire
    with pytest.raises(ExtractionError) as error:
        await capture_product_document(transport, "https://example.test/product")
    assert error.value.code == "request_limit"
    assert not transport.counts and not transport.observed and page.closed
    assert all(route.aborted for route in page.routes)
    assert len(transport.limiter.released) == 1


@pytest.mark.asyncio
async def test_cancellation_closes_owned_page_and_releases_permit(setup):
    page, transport = FakePage(), Transport()
    entered = asyncio.Event()
    async def goto(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    page.goto = goto
    setup(page)
    task = asyncio.create_task(capture_product_document(transport, "https://example.test/product"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert page.closed and len(transport.limiter.released) == 1
    assert not transport.calls and not transport.observed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,code", [(403, b"Denied", "access_required"), (200, b"<title>Just a moment...</title>", "access_required"), (404, b"Missing", "source_error")])
async def test_source_errors_retain_evidence_and_release_permit(setup, status, body, code):
    setup(FakePage(status=status, body=body))
    transport = Transport()
    with pytest.raises(ExtractionError) as error:
        await capture_product_document(transport, "https://example.test/product")
    assert error.value.code == code
    assert transport.calls == 1 and len(transport.observed) == 1
    assert len(transport.limiter.released) == 1 and transport.limiter.released[0][3] == status


@pytest.mark.asyncio
async def test_oversized_and_navigation_failure_release_permit(setup):
    setup(FakePage(headers={"content-length": "999999"}))
    transport = Transport()
    with pytest.raises(ExtractionError) as error:
        await capture_product_document(transport, "https://example.test/product")
    assert error.value.code == "response_too_large" and not transport.observed
    assert len(transport.limiter.released) == 1
    setup(FakePage(goto_error=RuntimeError("navigation failed")))
    transport = Transport()
    with pytest.raises(RuntimeError, match="navigation failed"):
        await capture_product_document(transport, "https://example.test/product")
    assert len(transport.limiter.released) == 1


@pytest.mark.asyncio
async def test_native_browser_blocks_subresource_writes_and_redirect_target(monkeypatch):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Local Chromium is not installed")
    requests = Counter()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests[("GET", self.path)] += 1
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
            body = b"<title>Product</title><img src='/image'><script src='/script'></script><script>fetch('/write',{method:'POST'});fetch('/read')</script>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            requests[("POST", self.path)] += 1
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    async def validate(url):
        return urlparse(url)
    monkeypatch.setattr("crawler.browser_document.validate_url", validate)
    pool = BrowserSessionPool()
    try:
        transport = Transport(pool=pool)
        result = await capture_product_document(transport, origin + "/product")
        assert "<title>Product</title>" in result.data
        assert requests == {("GET", "/product"): 1}
        assert transport.calls == 1 and len(transport.limiter.released) == 1
        transport = Transport(pool=pool)
        with pytest.raises(ExtractionError, match="Redirected"):
            await capture_product_document(transport, origin + "/redirect")
        assert requests == {("GET", "/product"): 1, ("GET", "/redirect"): 1}
        assert transport.calls == 1 and len(transport.limiter.released) == 1
    finally:
        await pool.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
