import asyncio
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

from crawler.adapters.browser import (
    BrowserAdapter,
    InactivityDeadline,
    SNAPSHOT_JS,
    browser_slot,
    field_key,
    filter_choices,
    is_navigation_cancellation,
    make_record,
    observed_amount,
    pricing_table_observation,
)
from crawler.adapters.printograph import AdapterError
from crawler.browser_session import BrowserSessionPool
from crawler.browser_discovery import resolve_titles

FIXTURE = b"""<!doctype html><title>Local product fixture</title><h1>Test cards</h1>
<main><div id="order-form">
<label for="size_abcdef">Size</label><select id="size_abcdef"><option value="" disabled selected>Select Size</option><option value="small">Small</option><option value="large">Large</option></select>
<label for="paper_abcdef">Paper</label><select id="paper_abcdef"><option value="gloss">Gloss</option><option value="matte">Matte</option></select>
<label for="quantity_abcdef">Quantity</label><select id="quantity_abcdef"><option value="100">100</option><option value="250">250</option></select>
<fieldset><legend>Production time</legend><input style="display:none" type="radio" id="regular" name="productionTime" value="1" checked><label for="regular">Standard</label>
<input style="display:none" type="radio" id="rush" name="productionTime" value="3"><label for="rush">Rush</label></fieldset>
</div></main><div class="row totalSection"><div class="col-8"><div class="subTotal">Subtotal (excludes shipping):</div></div><div class="col-4">$0.00</div></div>
<script>
const size=document.querySelector('#size_abcdef'),paper=document.querySelector('#paper_abcdef'),quantity=document.querySelector('#quantity_abcdef');
function price(){let amount=size.value?((size.value==='small'?10:20)+(paper.value==='gloss'?1:2)+Number(quantity.value)/10+(document.querySelector('#rush').checked?5:0)):0;document.querySelector('.col-4').textContent='$'+amount.toFixed(2);}
size.addEventListener('change',()=>{paper.innerHTML=size.value==='large'?'<option value="matte">Matte</option>':'<option value="gloss">Gloss</option><option value="matte">Matte</option>';price()});
document.querySelectorAll('input,select').forEach(e=>e.addEventListener('change',price));
</script>"""


class Limiter:
    async def acquire(self, origin, rps, concurrency, check):
        assert rps <= 2 and concurrency <= 2
        await check()
        return "fixture-permit"

    def release(self, *args):
        pass


class FixtureTransport:
    def __init__(self):
        self.limiter = Limiter()
        self.store = SimpleNamespace(
            config=SimpleNamespace(requests_per_second=2, source_concurrency=2)
        )
        self.observations = []
        self.checks = 0

    async def check(self):
        self.checks += 1

    async def record_observation(
        self, url, status, headers, body, context=None, count_request=True
    ):
        data = json.loads(body)
        self.observations.append({"data": data, "context": context, "headers": headers})
        return SimpleNamespace(
            data=data,
            response_id=f"fixture-{len(self.observations)}",
            status=status,
            cached=False,
        )


class BrowserHelpers(unittest.TestCase):
    def test_inactivity_wait_extends_only_for_new_progress(self):
        with patch("crawler.adapters.browser.time.monotonic", return_value=0) as now:
            deadline = InactivityDeadline(30)
            now.return_value = 20
            deadline.observe(("request", "/products/1/prices"))
            now.return_value = 40
            deadline.observe(("request", "/products/1/prices"))
            self.assertFalse(deadline.expired)
            now.return_value = 51
            self.assertTrue(deadline.expired)
            deadline.observe(("dom", "new option domain"))
            self.assertFalse(deadline.expired)

    def test_captured_table_event_counts_entries_without_publishing_or_url_query(self):
        observed = SimpleNamespace(
            data={"items": [{"quantity": 50}, {"quantity": 100}]},
            status=200,
            response_id="table-evidence",
        )
        event = pricing_table_observation(
            "https://example.com/products/1/prices?private=value", observed
        )
        self.assertEqual(event["url_path"], "/products/1/prices")
        self.assertEqual(event["tier_count"], 2)
        self.assertEqual(event["published_rows"], 0)
        self.assertEqual(event["count_basis"], "source_table_entries")
        self.assertEqual(event["evidence_id"], "table-evidence")
        observed.status = 401
        self.assertIsNone(
            pricing_table_observation("https://example.com/products/1/prices", observed)
        )

    def test_aliases(self):
        self.assertEqual(field_key("Run Size"), "quantity")
        self.assertEqual(field_key("Colorspec"), "color")
        self.assertEqual(field_key("Production time"), "productionTime")

    def test_only_navigation_aborts_are_recoverable(self):
        self.assertTrue(is_navigation_cancellation("net::ERR_ABORTED", 1, 2))
        self.assertTrue(
            is_navigation_cancellation("net::ERR_ABORTED", 1, 1, bootstrap=True)
        )
        self.assertFalse(is_navigation_cancellation("net::ERR_ABORTED", 1, 1))
        self.assertFalse(
            is_navigation_cancellation(
                "net::ERR_CONNECTION_RESET", 1, 2, bootstrap=True
            )
        )

    def test_empty_valid_choice_preserved_but_placeholder_rejected(self):
        control = {
            "kind": "select",
            "choices": [
                {"value": "", "label": "None", "placeholder": False},
                {"value": "0", "label": "Choose", "placeholder": True},
                {"value": "1", "label": "Yes", "disabled": True},
            ],
        }
        self.assertEqual(
            filter_choices(control),
            [{"value": "", "label": "None", "placeholder": False}],
        )

    def test_numeric_selection_range(self):
        control = {
            "kind": "number",
            "label": "Quantity",
            "selector": "#qty",
            "min": "10",
            "max": "100",
        }
        with self.assertRaises(AdapterError):
            filter_choices(control, 5)
        self.assertEqual(filter_choices(control, [25])[0]["value"], "25")

    def test_missing_zero_ambiguous_prices_fail(self):
        for value in (None, "$0.00", "$10.00 - $20.00", ""):
            with self.assertRaises(AdapterError):
                observed_amount({"price": value})
        self.assertEqual(str(observed_amount({"price": "$1,234.56"})), "1234.56")

    def test_direct_quote_provenance_is_not_independent_verification(self):
        row = make_record(
            "https://www.printograph.com/products/business-cards/order",
            {
                "title": "Cards",
                "price": "$20.00",
                "basis": "Subtotal (excludes shipping)",
                "images": [],
                "controls": [
                    {"key": "quantity", "value": "100", "selected_label": "100"}
                ],
            },
            ["source-response", "dom-observation"],
            set(),
            {},
        )
        self.assertEqual(row["provenance"], "direct_quote")
        self.assertEqual(row["verification_status"], "not_selected")
        self.assertEqual(row["quantity"], 100)

    def test_displayed_quote_unit_price_uses_shared_rounding_at_live_tie(self):
        row = make_record(
            "https://www.printograph.com/products/business-cards/order",
            {
                "title": "Business Cards",
                "price": "$510.65",
                "basis": "Subtotal (excludes shipping)",
                "images": [],
                "controls": [
                    {"key": "quantity", "value": "80000", "selected_label": "80000"}
                ],
            },
            ["dom-observation"],
            set(),
            {},
        )
        self.assertEqual(row["subtotal"], "510.65")
        self.assertEqual(row["unit_price"], "0.00638313")


class BrowserQueue(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_run_can_leave_queue_while_another_browser_is_open(self):
        lock = asyncio.Lock()
        await lock.acquire()
        checks = 0

        async def check():
            nonlocal checks
            checks += 1
            if checks > 1:
                raise InterruptedError("run was cancelled")

        with patch("crawler.adapters.browser._BROWSER_LOCK", lock):
            with self.assertRaises(InterruptedError):
                async with browser_slot(check):
                    self.fail(
                        "Cancelled queued run should not acquire another run's page slot"
                    )
        self.assertTrue(lock.locked())
        lock.release()


class BrowserLocalFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from playwright.async_api import async_playwright
            from pathlib import Path

            async with async_playwright() as p:
                if not Path(p.chromium.executable_path).exists():
                    self.skipTest("Playwright Chromium runtime is not installed")
        except ImportError:
            self.skipTest("Playwright is not installed")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                content_type = "text/html"
                response_status = 200
                content = (
                    b'<title>Authenticating</title><script>setTimeout(()=>location.href="/product",50)</script>'
                    if self.path == "/authenticate"
                    else FIXTURE
                )
                if self.path == "/delayed-domain":
                    content = b"""<!doctype html><h1>Delayed choices</h1><div id="order-form"><label for="size">Size</label><select id="size"><option value="">Please select an option</option></select></div><div id="subtotal"></div><script>setTimeout(()=>{document.querySelector('#size').innerHTML='<option value="small">Small</option>';document.querySelector('#order-form').insertAdjacentHTML('beforeend','<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select>');document.querySelector('#subtotal').textContent='$12.50';},1500);</script>"""
                if self.path == "/delayed-dependent-domain":
                    content = b"""<!doctype html><h1>Delayed paper</h1><div id="order-form"><label for="size">Size</label><select id="size"><option value="small">Small</option><option value="large">Large</option></select><label for="paper">Paper</label><select id="paper"><option value="gloss">Gloss</option></select><label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select></div><div id="subtotal">$10.00</div><script>document.querySelector('#size').addEventListener('change',()=>{const paper=document.querySelector('#paper');paper.innerHTML='<option value="">Please select paper</option>';setTimeout(()=>{paper.innerHTML='<option value="matte">Matte</option>';document.querySelector('#subtotal').textContent='$12.50';},1500);});</script>"""
                if self.path == "/blocked-control":
                    content = FIXTURE.replace(
                        b'style="display:none"', b'style="display:inline-block"'
                    )
                    content += b'<div style="position:fixed;inset:0;z-index:999;background:rgba(255,255,255,.1)">Fixture overlay</div>'
                if self.path == "/roll-wind-position":
                    radios = "".join(
                        f'<div style="position:relative;height:32px"><input style="position:absolute;opacity:0" type="radio" name="windPosition{i}" id="windPosition{i}" value="{value}" {"checked" if value == 54 else ""}><label style="position:absolute;inset:0;background:white" for="windPosition{i}">{label}</label></div>'
                        for i, (value, label) in enumerate(
                            [
                                (32, "Off Bottom"),
                                (33, "Off Left"),
                                (34, "Off Right"),
                                (35, "Off Top"),
                                (54, "Not Important"),
                            ]
                        )
                    )
                    content = (
                        '<!doctype html><h1>Roll Labels</h1><div id="order-form" data-printproducttype="36"><label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select>'
                        + radios
                        + '</div><div id="subtotal">$54.00</div><script>document.querySelectorAll("input[type=radio]").forEach(radio=>radio.addEventListener("change",()=>{document.querySelectorAll("input[type=radio]").forEach(other=>other.checked=other===radio);document.querySelector("#subtotal").textContent="$"+radio.value+".00";}));</script>'
                    ).encode()
                if self.path == "/session-product":
                    content += b'<script>document.querySelector("h1").textContent=localStorage.getItem("fixture_seen")?"Saved session page":"First session page";localStorage.setItem("fixture_seen","yes");</script>'
                if self.path == "/catalog":
                    content = b'<title>Catalog</title><a href="/product" id="product">Unavailable</a><script>if(localStorage.getItem("fixture_seen"))document.querySelector("#product").textContent="Test cards";</script>'
                if self.path == "/background-product":
                    content += b"<script>fetch('/account/poll');</script>"
                if self.path == "/failed-quote-product":
                    content += b"<script>size.value='small';price();fetch('/service/rest/v1/product/prices?private=fixture');</script>"
                if self.path.startswith("/service/rest/v1/product/prices"):
                    response_status = 503
                    content_type = "application/json"
                    content = b'{"error":"fixture quote failure"}'
                if self.path == "/account/poll":
                    self.server.poll_started.set()
                    self.server.release_poll.wait(15)
                    self.server.poll_finished = True
                    content = b"{}"
                    content_type = "application/json"
                if self.path == "/delayed-product":
                    content += b"""<div id="ajaxLoader">Loading</div><script>[500,1100,1700].forEach((ms,i)=>setTimeout(()=>fetch('/service/rest/v1/products?productType=fixture&stage='+i),ms));setTimeout(()=>document.querySelector('#ajaxLoader').remove(),2350);</script>"""
                if self.path.startswith(
                    "/service/rest/v1/products?productType=fixture&stage="
                ):
                    content_type = "application/json"
                    content = b'{"id":100000001}'
                if self.path == "/portal-product":
                    content = re.sub(
                        rb"<fieldset>.*?</fieldset>", b"", FIXTURE, flags=re.S
                    )
                    content = content.replace(
                        b"<script>",
                        b"""<div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a><a class="turnAround notActive" data-toggle="turnaround" data-title="3">Rush 2 Business Days</a></div>
                    <div id="large-gallery-img"><img src="/selected-product.jpg" data-src="/stale-product.jpg" alt="Test cards"></div>
                    <meta property="og:image" content="/logo.png">
                    <script>""",
                        1,
                    )
                    content = content.replace(
                        b"document.querySelector('#rush').checked",
                        b"document.querySelector('.turnAround.active').getAttribute('data-title')==='3'",
                    )
                    content += b"""<script>document.querySelectorAll('.turnAround').forEach(a=>a.addEventListener('click',()=>{document.querySelectorAll('.turnAround').forEach(b=>b.className='turnAround notActive');a.className='turnAround active';price();}));</script>"""
                    content += b"<script>fetch('/service/rest/v1/products/fixture/prices?context=fixture').then(r=>r.json());</script>"
                if self.path.startswith("/service/rest/v1/products/fixture/prices"):
                    content_type = "application/json"
                    content = json.dumps(
                        {
                            "items": [
                                {"quantity": 50, "markupPrice": "12"},
                                {"quantity": 100, "markupPrice": "21"},
                                {"quantity": 250, "markupPrice": "36"},
                            ]
                        }
                    ).encode()
                if self.path == "/unavailable-rush":
                    content = b"""<!doctype html><h1>Quantity-dependent Rush</h1>
<div id="order-form"><label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option><option value="5000">5000</option></select></div>
<div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a><a class="turnAround notActive" data-toggle="turnaround" data-title="3">Rush 2 Business Days</a></div>
<div id="subtotal">$10.00</div><script>
const quantity=document.querySelector('#quantity'),regular=document.querySelector('[data-title="1"]'),rush=document.querySelector('[data-title="3"]');
function quote(){document.querySelector('#subtotal').textContent='$'+(Number(quantity.value)/10+(rush.classList.contains('active')?5:0)).toFixed(2);}
quantity.addEventListener('change',()=>{regular.className='turnAround active';rush.className='turnAround notActive';rush.textContent=quantity.value==='5000'?'Rush Not Available':'Rush 2 Business Days';quote();});
document.querySelectorAll('.turnAround').forEach(a=>a.addEventListener('click',()=>{if(a===rush&&quantity.value==='5000'){fetch('/unavailable-rush-clicked');return;}document.querySelectorAll('.turnAround').forEach(b=>b.className='turnAround notActive');a.className='turnAround active';quote();}));
</script>"""
                if self.path == "/unavailable-rush-clicked":
                    self.server.unavailable_rush_clicks += 1
                    content = b"{}"
                    content_type = "application/json"
                if self.path == "/collapsed-process":
                    content = b"""<!doctype html><h1>Collapsed source services</h1>
<div id="order-form" data-printproducttype="1"><label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select></div><div id="subtotal">$10.00</div>
<button aria-expanded="true" aria-controls="collapseOne">Process Type +</button>
<div id="collapseOne" style="display:none"><input type="radio" name="proofType" id="instant" value="31" checked><label for="instant">Instantly Processed</label><input type="radio" name="proofType" id="manual" value="100"><label for="manual">Manually Processed</label><input type="radio" name="proofType" id="design" value="101"><label for="design">Let Us Design</label><input type="radio" name="fileDeliveryForIop" id="upload" value="5" checked><label for="upload">Upload Now</label><input type="radio" name="fileDeliveryForIop" id="online" value="22"><label for="online">Design Online</label><input type="hidden" name="privateFixtureField" value="not-a-real-secret"></div>"""
                self.send_response(response_status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                try:
                    self.wfile.write(content)
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.poll_started = threading.Event()
        self.server.release_poll = threading.Event()
        self.server.poll_finished = False
        self.server.unavailable_rush_clicks = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/product"

    async def asyncTearDown(self):
        if hasattr(self, "server"):
            self.server.release_poll.set()
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()

    async def test_product_pause_route_cannot_poison_shared_context_api(self):
        import contextlib
        import io
        from crawler.transport import RunInterrupted
        transport = FixtureTransport()
        paused = False
        async def check():
            if paused:
                raise RunInterrupted("Fixture product paused", "interrupted")
        transport.check = check
        class PauseAtRoute(Limiter):
            async def acquire(self, *args):
                nonlocal paused
                paused = True
                raise RunInterrupted("Fixture product paused", "interrupted")
        transport.limiter = PauseAtRoute()
        pool = BrowserSessionPool()
        transport.browser_pool = pool
        async def emit(*args):
            await transport.check()  # Match Engine.emit's stopped-product fence.
        errors = io.StringIO()
        try:
            with contextlib.redirect_stderr(errors), patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
                "os.environ", {"ATLAS_BROWSER_HEADLESS": "true", "ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""}
            ):
                with self.assertRaises(RunInterrupted):
                    _ = [row async for row in BrowserAdapter().extract({"url": self.url, "max_configurations": 1}, transport, emit)]
                # Independent DOMAIN requests call this exact native cookie API.
                context = next(iter(pool._contexts.values()))
                self.assertEqual(await context.cookies([self.url]), [])
                self.assertNotIn("Error occurred in event listener", errors.getvalue())
        finally:
            await pool.close()

    async def test_snapshot_treats_missing_document_body_as_transient_loading(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    "<html><head><title>Reloading</title></head><body></body></html>"
                )
                await page.evaluate("document.body.remove()")
                state = await page.evaluate(SNAPSHOT_JS, {"priceSelector": None})
                self.assertTrue(state["busy"])
                self.assertFalse(state["document_ready"])
                self.assertEqual(state["controls"], [])
                await page.evaluate(
                    "document.documentElement.appendChild(document.createElement('body'))"
                )
                ready = await page.evaluate(SNAPSHOT_JS, {"priceSelector": None})
                self.assertFalse(ready["busy"])
            finally:
                await browser.close()

    async def test_catalog_product_price_login_notice_is_access_required(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    '<main><nav>' + ('Catalog category ' * 120) + '</nav>'
                    '<article><h1>ONE CHOICE 10 ft. Circle Hanging Banner</h1>'
                    '<p>Please log in to see price or purchase this item</p>'
                    '<p>Includes a frame, printed graphic and carry bag.</p>'
                    '</article></main>'
                )
                state = await page.evaluate(SNAPSHOT_JS, {"priceSelector": None})
                self.assertTrue(state["login"])
                self.assertIsNone(state["price"])
                self.assertEqual(state["controls"], [])

                await page.set_content(
                    '<main><article><h1>Public product</h1>'
                    '<p>Log in to leave a review.</p></article></main>'
                )
                ordinary = await page.evaluate(SNAPSHOT_JS, {"priceSelector": None})
                self.assertFalse(ordinary["login"])
            finally:
                await browser.close()

    async def test_source_progress_extends_settle_past_inactivity_interval(self):
        async def emit(*args):
            pass

        transport = FixtureTransport()
        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
                "ATLAS_BROWSER_INACTIVITY_SECONDS": "1.2",
            },
        ):
            rows = [
                r
                async for r in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/delayed-product"),
                        "max_configurations": 1,
                        "source_metadata": {"currency": "USD"},
                        "selection_filters": {
                            "size": "Small",
                            "paper": "Gloss",
                            "quantity": 100,
                            "productionTime": "Standard",
                        },
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(rows[0]["subtotal"], "21.00")
        self.assertEqual(
            sum(
                o["context"]["lane"] == "browser_source" for o in transport.observations
            ),
            3,
        )

    async def test_placeholder_only_domain_waits_for_real_options(self):
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
                "ATLAS_BROWSER_INACTIVITY_SECONDS": "3",
            },
        ):
            rows = [
                row
                async for row in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/delayed-domain"),
                        "max_configurations": 1,
                        "source_metadata": {
                            "currency": "USD",
                            "price_selector": "#subtotal",
                        },
                    },
                    FixtureTransport(),
                    emit,
                )
            ]
        self.assertEqual(rows[0]["subtotal"], "12.50")
        discovery = next(payload for kind, payload in events if kind == "discovery")
        self.assertEqual(
            {control["key"] for control in discovery["controls"]}, {"size", "quantity"}
        )

    async def test_delayed_dependent_domain_does_not_skip_filtered_branch(self):
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                row
                async for row in BrowserAdapter().extract(
                    {
                        "url": self.url.replace(
                            "/product", "/delayed-dependent-domain"
                        ),
                        "max_configurations": 1,
                        "selection_filters": {
                            "size": "Large",
                            "paper": "Matte",
                            "quantity": 100,
                        },
                        "source_metadata": {
                            "currency": "USD",
                            "price_selector": "#subtotal",
                        },
                    },
                    FixtureTransport(),
                    emit,
                )
            ]
        self.assertEqual(rows[0]["configuration"]["paper"]["value"], "matte")
        self.assertEqual(rows[0]["subtotal"], "12.50")
        self.assertFalse(any(kind == "branch_skipped" for kind, _ in events))

    async def test_obstructed_control_timeout_identifies_public_choice(self):
        from playwright.async_api import Locator

        original_click = Locator.click

        async def short_click(locator, **kwargs):
            return await original_click(locator, **{**kwargs, "timeout": 600})

        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.object(
            Locator, "click", short_click
        ), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            with self.assertRaises(AdapterError) as error:
                [
                    row
                    async for row in BrowserAdapter().extract(
                        {
                            "url": self.url.replace("/product", "/blocked-control"),
                            "max_configurations": 1,
                            "selection_filters": {
                                "size": "Small",
                                "paper": "Gloss",
                                "quantity": 100,
                                "productionTime": "Rush",
                            },
                            "source_metadata": {"currency": "USD"},
                        },
                        FixtureTransport(),
                        emit,
                    )
                ]
        self.assertEqual(error.exception.code, "source_action_timeout")
        self.assertEqual(error.exception.details["kind"], "radio")
        self.assertEqual(error.exception.details["key"], "productionTime")
        self.assertEqual(error.exception.details["choice_value"], "3")
        self.assertTrue(error.exception.details["control_present"])
        self.assertTrue(error.exception.details["requested_choice_available"])
        self.assertFalse(error.exception.details["selector_changed"])
        self.assertNotIn("http", str(error.exception))
        self.assertEqual(
            len([kind for kind, _ in events if kind == "browser_action_error"]), 1
        )

    async def test_printograph_wind_radios_share_group_and_use_overlay_labels(self):
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                row
                async for row in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/roll-wind-position"),
                        "source_metadata": {
                            "currency": "USD",
                            "price_selector": "#subtotal",
                        },
                    },
                    FixtureTransport(),
                    emit,
                )
            ]
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {row["configuration"]["windPosition"]["value"] for row in rows},
            {"32", "33", "34", "35", "54"},
        )
        self.assertTrue(
            all(
                row["subtotal"] == row["configuration"]["windPosition"]["value"] + ".00"
                for row in rows
            )
        )
        discovery = next(payload for kind, payload in events if kind == "discovery")
        wind = [c for c in discovery["controls"] if c["key"] == "windPosition"]
        self.assertEqual(len(wind), 1)
        self.assertEqual(len(wind[0]["choices"]), 5)

    async def test_background_account_poll_does_not_block_a_product_quote(self):
        async def emit(*args):
            pass

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                r
                async for r in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/background-product"),
                        "max_configurations": 1,
                        "source_metadata": {"currency": "USD"},
                        "selection_filters": {
                            "size": "Small",
                            "paper": "Gloss",
                            "quantity": 100,
                            "productionTime": "Standard",
                        },
                    },
                    FixtureTransport(),
                    emit,
                )
            ]
        self.assertEqual(rows[0]["subtotal"], "21.00")
        self.assertTrue(self.server.poll_started.is_set())
        self.assertFalse(self.server.poll_finished)

    async def test_actual_product_failure_remains_fatal_with_safe_diagnostics(self):
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            with self.assertRaises(AdapterError) as error:
                [
                    r
                    async for r in BrowserAdapter().extract(
                        {
                            "url": self.url.replace(
                                "/product", "/failed-quote-product"
                            ),
                            "max_configurations": 1,
                            "source_metadata": {"currency": "USD"},
                        },
                        FixtureTransport(),
                        emit,
                    )
                ]
        self.assertEqual(error.exception.code, "source_unavailable")
        diagnostics = [data for kind, data in events if kind == "browser_request_error"]
        self.assertEqual(diagnostics[0]["url_path"], "/service/rest/v1/product/prices")
        self.assertEqual(diagnostics[0]["resource_type"], "fetch")
        self.assertNotIn("private", str(error.exception))

    async def test_browser_selects_filters_hidden_radio_and_portal_subtotal(self):
        transport = FixtureTransport()
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                r
                async for r in BrowserAdapter().extract(
                    {
                        "url": self.url,
                        "max_configurations": 1,
                        "source_metadata": {"currency": "USD"},
                        "selection_filters": {
                            "size": "Small",
                            "paper": "Gloss",
                            "quantity": 250,
                            "productionTime": "Rush",
                        },
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subtotal"], "41.00")
        self.assertEqual(rows[0]["production"]["value"], "3")
        self.assertEqual(rows[0]["configuration"]["size"]["value"], "small")
        self.assertEqual(transport.observations[-1]["data"]["subtotal_text"], "$41.00")
        self.assertTrue(transport.checks > 5)
        self.assertEqual(
            transport.observations[-1]["headers"], {"content-type": "application/json"}
        )

    async def test_dependencies_are_rediscovered_after_size_change(self):
        transport = FixtureTransport()

        async def emit(*args):
            pass

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                r
                async for r in BrowserAdapter().extract(
                    {
                        "url": self.url,
                        "max_configurations": 1,
                        "source_metadata": {"currency": "USD"},
                        "selection_filters": {
                            "size": "Large",
                            "paper": "Matte",
                            "quantity": 100,
                            "productionTime": "Standard",
                        },
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(rows[0]["subtotal"], "32.00")
        self.assertEqual(rows[0]["configuration"]["paper"]["value"], "matte")

    async def test_normal_authentication_shell_reload_is_allowed(self):
        async def emit(*args):
            pass

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                r
                async for r in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/authenticate"),
                        "max_configurations": 1,
                        "source_metadata": {"currency": "USD"},
                        "selection_filters": {
                            "size": "Small",
                            "paper": "Gloss",
                            "quantity": 100,
                            "productionTime": "Standard",
                        },
                    },
                    FixtureTransport(),
                    emit,
                )
            ]
        self.assertEqual(rows[0]["subtotal"], "21.00")

    async def test_pool_retains_normal_source_session_across_product_pages(self):
        pool = BrowserSessionPool()
        transport = FixtureTransport()
        transport.browser_pool = pool

        async def emit(*args):
            pass

        spec = {
            "url": self.url.replace("/product", "/session-product"),
            "max_configurations": 1,
            "source_metadata": {"currency": "USD"},
            "selection_filters": {
                "size": "Small",
                "paper": "Gloss",
                "quantity": 100,
                "productionTime": "Standard",
            },
        }
        try:
            with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
                "os.environ",
                {"ATLAS_BROWSER_HEADLESS": "true", "ATLAS_BROWSER_STORAGE_STATE": ""},
            ):
                first = [
                    r async for r in BrowserAdapter().extract(spec, transport, emit)
                ]
                second = [
                    r async for r in BrowserAdapter().extract(spec, transport, emit)
                ]
                with patch(
                    "crawler.browser_discovery.validate_url",
                    new=AsyncMock(return_value=urlparse(self.url)),
                ):
                    titles = await resolve_titles(
                        self.url.replace("/product", "/catalog"),
                        ["Test cards"],
                        browser_pool=pool,
                        rps=2,
                    )
            self.assertEqual(first[0]["title"], "First session page")
            self.assertEqual(second[0]["title"], "Saved session page")
            self.assertEqual(titles["matches"][0]["url"], self.url)
            self.assertEqual(titles["unresolved"], [])
            self.assertEqual(len(pool._contexts), 1)
            self.assertEqual(len(next(iter(pool._contexts.values())).pages), 0)
        finally:
            await pool.close()
        self.assertEqual(pool._contexts, {})

    async def test_portal_production_widget_and_product_gallery(self):
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                r
                async for r in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/portal-product"),
                        "max_configurations": 1,
                        "source_metadata": {"currency": "USD"},
                        "selection_filters": {
                            "size": "Small",
                            "paper": "Gloss",
                            "quantity": 100,
                            "productionTime": "3",
                        },
                    },
                    FixtureTransport(),
                    emit,
                )
            ]
        self.assertEqual(rows[0]["subtotal"], "26.00")
        self.assertEqual(rows[0]["production"]["value"], "3")
        self.assertEqual(rows[0]["production"]["label"], "Rush 2 Business Days")
        self.assertEqual(
            rows[0]["images"], [self.url.replace("/product", "/selected-product.jpg")]
        )
        tables = [data for kind, data in events if kind == "pricing_table_captured"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["tier_count"], 3)
        self.assertEqual(
            tables[0]["url_path"], "/service/rest/v1/products/fixture/prices"
        )
        self.assertEqual(tables[0]["published_rows"], 0)
        self.assertEqual(len(rows), 1)

    async def test_unavailable_rush_is_not_clicked_after_quantity_changes(self):
        events = []
        transport = FixtureTransport()

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                row
                async for row in BrowserAdapter().extract(
                    {
                        "url": self.url.replace("/product", "/unavailable-rush"),
                        "source_metadata": {
                            "currency": "USD",
                            "price_selector": "#subtotal",
                        },
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(self.server.unavailable_rush_clicks, 0)
        self.assertEqual(
            {(row["quantity"], row["production"]["value"], row["subtotal"]) for row in rows},
            {(100, "1", "10.00"), (100, "3", "15.00"), (5000, "1", "500.00")},
        )
        final_controls = transport.observations[-1]["data"]["controls"]
        production = next(c for c in final_controls if c["key"] == "productionTime")
        rush = next(c for c in production["choices"] if c["value"] == "3")
        self.assertEqual(rush["label"], "Rush Not Available")
        self.assertTrue(rush["disabled"])
        coverage = next(payload for kind, payload in events if kind == "coverage")
        self.assertEqual(coverage["invalid_branches"], 0)
        self.assertNotIn("source_auto_reset", coverage["reasons"])

    async def test_collapsed_source_services_are_separate_coverage_gaps(self):
        from playwright.async_api import async_playwright

        url = self.url.replace("/product", "/collapsed-process")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url)
                snapshot = await page.evaluate(SNAPSHOT_JS, {"priceSelector": "#subtotal"})
            finally:
                await browser.close()
        expected = {
            "collapsed_service_control:proofType",
            "collapsed_service_control:fileDeliveryForIop",
        }
        self.assertEqual(set(snapshot["coverage_gaps"]), expected)
        self.assertEqual(snapshot["unsupported"], [])
        self.assertNotIn("privateFixtureField", json.dumps(snapshot))
        self.assertNotIn("not-a-real-secret", json.dumps(snapshot))
        events = []
        transport = FixtureTransport()

        async def emit(kind, payload):
            events.append((kind, payload))

        with patch("crawler.transport.validate_url", new=AsyncMock()), patch.dict(
            "os.environ",
            {
                "ATLAS_BROWSER_HEADLESS": "true",
                "ATLAS_BROWSER_PROFILE_DIR": "",
                "ATLAS_BROWSER_STORAGE_STATE": "",
            },
        ):
            rows = [
                row
                async for row in BrowserAdapter().extract(
                    {
                        "url": url,
                        "source_metadata": {
                            "currency": "USD",
                            "price_selector": "#subtotal",
                        },
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subtotal"], "10.00")
        dom = next(
            observation["data"]
            for observation in transport.observations
            if observation["context"]["lane"] == "browser_dom_quote"
        )
        diagnostics = {entry["key"]: entry for entry in dom["service_context"]}
        self.assertEqual(diagnostics["proofType"]["selected"][0]["value"], "31")
        self.assertEqual(diagnostics["fileDeliveryForIop"]["selected"][0]["value"], "5")
        self.assertNotIn("privateFixtureField", json.dumps(dom))
        self.assertNotIn("not-a-real-secret", json.dumps(dom))
        self.assertNotIn("proofType", rows[0]["configuration"])
        self.assertEqual(rows[0]["coverage_status"], "partial")
        self.assertEqual(set(rows[0]["coverage_reasons"]), expected)
        coverage = next(payload for kind, payload in events if kind == "coverage")
        self.assertEqual(coverage["status"], "partial")
        self.assertFalse(coverage["count_is_exact"])


if __name__ == "__main__":
    unittest.main()
