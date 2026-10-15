"""Interception-only browser experiments: no supplier traffic or credentials."""
import json
import asyncio
import time
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from crawler.browser_dependency_probe import (
    ORIGIN, request_policy, probe_product, changed_selections, exception_diagnostic, page_error_diagnostic,
    blocked_request_diagnostic, PublicAssetCache, quote_ready, recheck_base_price, initialized_product_url, initialization_mismatches,
)
from crawler.config import Settings
from crawler.probe_jobs import ProbeBudget
from crawler.store import Store, Run, ProbeJob, ProbeAttempt
from crawler.transport import Transport, ExtractionError
from test_price_contract import captured

URL = ORIGIN + "/products/business-cards/order"
ROLL_URL = ORIGIN + "/products/roll-labels/order"
ROLL_INITIALIZATION = {"shape": 5, "size": 742, "paper": 13, "finish": 1, "color": 1,
                       "quantity": 100, "turnaround": 1, "windPosition": 32, "proof_type": 31, "file_delivery": 5}


@pytest.mark.parametrize("initialization", [
    {}, {"paper": 1}, {"size": 1, "cartId": 1}, {"size": 1, "orientation": 1},
    {"size": True}, {"size": 1.0}, {"size": "1&cartId=1"}, {"size": 0},
    {"size": 1000000000}, {"size": 1, "proof_type": 100, "file_delivery": 5},
    {"size": 1, "proof_type": 31}, {"size": 1, "productionTime": 1},
    {"size": 1, "turnaround": 2}, {"size": 1, "windPosition": 32},
])
def test_public_initialization_rejects_unproven_or_private_fields(initialization):
    with pytest.raises(ValueError):
        initialized_product_url(URL, initialization)


def test_source_query_aliases_and_manual_secondary_require_exact_readback():
    url = initialized_product_url(ROLL_URL, ROLL_INITIALIZATION)
    assert request_policy(url, "GET", "document", url) == "document"
    assert request_policy(url + "&cartId=1", "GET", "document", url + "&cartId=1") is None
    assert request_policy(url + "&size=1", "GET", "document", url + "&size=1") is None
    with pytest.raises(ValueError):
        initialized_product_url(url, ROLL_INITIALIZATION)
    capture = captured(product_type="36")
    capture["userSelectedOptions"].update({"shape": "5", "size": "742", "paper": "13", "quantity": "100"})
    assert not initialization_mismatches(ROLL_INITIALIZATION, capture)
    manual = {"size": 1, "proof_type": 36, "file_delivery": 5}
    assert initialization_mismatches(manual, capture)["proofType"] == {"requested": "100", "observed": "31"}
    capture["userSelectedOptions"].update(size="1", proofType="100", manualProofType="36")
    assert not initialization_mismatches(manual, capture)
    capture["userSelectedOptions"]["manualProofType"] = "1"
    assert initialization_mismatches(manual, capture) == {"manualProofType": {"requested": "36", "observed": "1"}}


@pytest.mark.parametrize("method,path,resource,body,expected", [
    ("GET", "/products/business-cards/order", "document", None, "document"),
    ("GET", "/js/orderForm.js", "script", None, "asset"),
    ("GET", "/service/rest/v1/users/self", "xhr", None, "startup"),
    ("GET", "/service/rest/v1/products/100/prices", "fetch", None, "public_source"),
    ("GET", "/service/rest/v1/products/100/prices?itemId=null&cid=", "fetch", None, "public_source"),
    ("GET", "/service/rest/v1/products/100/prices?itemId=123&cid=", "fetch", None, None),
    ("POST", "/service/rest/v1/i18n/lookup/resources", "xhr", {"nonEnums": "public.labels"}, "startup"),
    ("POST", "/service/rest/v1/i18n/lookup/resources", "xhr", {"nonEnums": "public.labels", "country": "US,CA", "state": "US.AZ"}, "startup"),
    ("GET", "/service/rest/v1/product/search", "xhr", None, "public_source"),
    ("POST", "/service/rest/v1/rules/turnarounds", "xhr", {"quantity": 100}, "public_source"),
    ("POST", "/service/rest/v1/users/self/cart", "xhr", {}, None),
    ("POST", "/service/rest/v1/users/self/orders", "xhr", {}, None),
    ("GET", "/service/rest/v1/users/self/cart?action=7", "xhr", None, None),
    ("POST", "/service/rest/v1/users/self/quotes", "xhr", {}, None),
    ("POST", "/service/rest/v1/rules/turnarounds", "xhr", {"headers": {"Authorization": "synthetic"}}, None),
    ("GET", "/service/rest/v1/products?cid=private", "xhr", None, None),
    ("GET", "/user/logout", "document", None, None),
    ("GET", "/pixel.png", "image", None, None),
])
def test_strict_readonly_request_policy(method, path, resource, body, expected):
    assert request_policy(ORIGIN + path, method, resource, URL, body) == expected
    assert request_policy("https://tracker.invalid" + path, method, resource, URL, body) is None


@pytest.mark.parametrize("path", ["/tl/css/cart/cart.css", "/tl/css/cart/cart-thumb.css", "/tl/css/cart/cart-preview.css", "/tl/css/order/order.css", "/tl/js/cart/presentation.js"])
def test_public_cart_and_order_presentation_assets_are_not_api_mutations(path):
    resource = "script" if path.endswith(".js") else "stylesheet"
    for origin in (ORIGIN, "https://static.printograph.com"):
        assert request_policy(origin + path, "GET", resource, URL) == "asset"
        assert request_policy(origin + path, "POST", resource, URL) is None
        assert request_policy(origin + path + "?token=private", "GET", resource, URL) is None
    assert request_policy(ORIGIN + "/service/rest/v1/users/self/cart/view.js", "GET", "script", URL) is None
    assert request_policy(ORIGIN + "/service/rest/v1/users/self/cart", "GET", "fetch", URL) is None
    assert request_policy(ORIGIN + "/service/rest/v1/users/self/orders", "GET", "fetch", URL) is None


def test_changed_selections_does_not_hide_dependent_resets():
    before = {"userSelectedOptions": {"proofType": "31", "quantity": "100"}}
    after = {"userSelectedOptions": {"proofType": "100", "quantity": "50"}}
    assert set(changed_selections(before, after)) == {"proofType", "quantity"}


@pytest.mark.parametrize("field,value", [("quantity", 0), ("quantity", ""), ("productId", ""), ("productId", 0), ("price", 0), ("price", "NaN")])
def test_unconfigured_calculator_cannot_be_a_settled_quote(field, value):
    capture = captured()
    if field == "price":
        capture["appliedPrices"]["productPrice"] = value
    else:
        capture["userSelectedOptions"][field] = value
    assert not quote_ready(capture)


@pytest.mark.asyncio
async def test_phase_base_read_has_fresh_identity_full_context_and_no_guessed_hash_on_error():
    calls = []
    class FreshTransport:
        async def fetch(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(response_id=str(len(calls)), data={"items": [{"price": 10}]}, cached=False)
    capture = captured()
    budget = SimpleNamespace(job_id="same-budget")
    one = await recheck_base_price(FreshTransport(), budget, "baseline", capture)
    two = await recheck_base_price(FreshTransport(), budget, "baseline", capture)
    assert one["semantic_hash"] == two["semantic_hash"] and one["evidence_id"] != two["evidence_id"]
    assert calls[0][0].endswith("/products/100000009/prices")
    assert calls[0][1]["params"] == {"itemId": "null", "cid": ""}
    assert calls[0][1]["context"]["controller_context"] == capture
    assert calls[0][1]["context"]["event_id"] != calls[1][1]["context"]["event_id"]
    capture["userSelectedOptions"]["quantity"] = 0
    with pytest.raises(ExtractionError, match="initialized"):
        await recheck_base_price(FreshTransport(), budget, "baseline", capture)
    assert len(calls) == 2
    class FailedTransport:
        async def fetch(self, *args, **kwargs):
            raise ExtractionError("Access expired", "access_required")
    with pytest.raises(ExtractionError, match="Access expired"):
        await recheck_base_price(FailedTransport(), budget, "proof", captured())


def test_exception_diagnostics_never_copy_message_or_private_url():
    result = exception_diagnostic(TimeoutError("https://private.invalid?token=synthetic"), "navigation")
    assert result == {"code": "navigation_timeout", "exception_type": "TimeoutError", "stage": "navigation"}
    assert "synthetic" not in json.dumps(result)


def test_page_error_classification_and_blocked_script_diagnostics_never_copy_private_values():
    assert page_error_diagnostic(RuntimeError("gpfields is not defined"), "baseline", "baseline_initialize")["code"] == "gpfields_not_defined"
    assert page_error_diagnostic(RuntimeError("gpfields is not defined; token=synthetic-private"), "baseline", "navigation")["code"] == "unclassified_page_error"
    blocked = blocked_request_diagnostic("https://static.printograph.com/js/gpfields.js?token=synthetic-private&v=123", "GET", "script", None)
    assert blocked["origin"] == "https://static.printograph.com"
    assert blocked["path"] == "/js/gpfields.js" and blocked["query_names"] == ["token", "v"]
    assert request_policy("https://static.printograph.com/js/gpfields.js?token=synthetic-private", "GET", "script", URL) is None
    other = blocked_request_diagnostic("https://untrusted.invalid/private-name.js?token=synthetic-private", "GET", "script", None)
    assert other["origin"] == "[other origin]" and other["path"] == "[non-API resource]"
    assert "synthetic-private" not in json.dumps([blocked, other])


@pytest.mark.parametrize("message,code", [
    ("Execution context was destroyed, most likely because of a navigation", "execution_context_destroyed"),
    ("Target page, context or browser has been closed", "target_closed"),
    ("strict mode violation: locator matched multiple", "strict_mode_violation"),
    ("Unexpected token while parsing css selector", "invalid_selector"),
    ("Navigation interrupted by another navigation", "navigation_interrupted"),
])
def test_known_browser_errors_are_classified_without_persisting_messages(message, code):
    diagnostic = exception_diagnostic(RuntimeError(message + " token=synthetic-private"), "baseline_initialize")
    assert diagnostic["code"] == code
    assert diagnostic["signals"][code] is True
    assert "synthetic-private" not in json.dumps(diagnostic)


@pytest.mark.parametrize("headers", [
    {}, {"cache-control": "max-age=0"}, {"cache-control": "max-age=60, private"},
    {"cache-control": "max-age=60, no-store"}, {"cache-control": "max-age=60, no-cache"},
    {"cache-control": "max-age=60", "vary": "Cookie"},
    {"cache-control": "max-age=60", "vary": "Authorization"},
    {"cache-control": "max-age=60", "set-cookie": "synthetic-private"},
])
def test_public_asset_cache_rejects_private_or_unapproved_storage(headers):
    cache = PublicAssetCache()
    assert not cache.put("https://static.printograph.com/orderForm.js?v=1", 200, headers, b"public")
    assert cache.size == 0


def test_public_asset_cache_exact_versions_expiry_server_age_and_memory_bound():
    now = [100.0]
    cache = PublicAssetCache(max_bytes=8, clock=lambda: now[0])
    url = "https://static.printograph.com/orderForm.js?v=1"
    headers = {"Cache-Control": "public, max-age=1200", "Age": "1100", "Content-Type": "text/javascript"}
    assert cache.put(url, 200, headers, b"1234")
    assert cache.get(url)["expires"] == 200
    assert cache.get(url.replace("v=1", "v=2")) is None
    assert not cache.put("https://www.printograph.com/api.js", 200, headers, b"no")
    assert not cache.put("https://static.printograph.com/index.html", 200, headers, b"no")
    assert not cache.put(url, 503, headers, b"no")
    now[0] = 200
    assert cache.get(url) is None and cache.size == 0
    assert cache.put(url, 200, {"cache-control": "max-age=3600"}, b"1234")
    assert cache.get(url)["expires"] == 800  # hard600second ceiling
    assert cache.put(url + "2", 200, {"cache-control": "max-age=60"}, b"56789")
    assert cache.size == 5 and cache.get(url) is None


def fixture_html(mode):
    empty = mode in {"empty", "reload_init"}
    data = captured(product_type="36" if mode.startswith("query_roll") else "1", base="10.00")
    selected = data["userSelectedOptions"]
    if empty:
        selected.update(paper="", color="", productId="", quantity=0)
    proof = "" if mode == "missing" else """
<div id="collapseOne">
<input type="radio" name="proofType" value="31" id="instant" checked><label for="instant">Instant</label>
<input type="radio" name="proofType" value="100" id="manual"><label for="manual">Manual</label>
<div id="manualChoices" style="display:none">
<input type="radio" name="proofTypeForManual" value="1" id="pdf" checked><label for="pdf">PDF proof</label>
<input type="radio" name="proofTypeForManual" value="36" id="none"><label for="none">No proof</label></div>
<input type="radio" name="fileDeliveryForIop" value="5" id="upload" checked><label for="upload">Upload</label>
<input type="radio" name="fileDeliveryForIop" value="22" id="design"><label for="design">Design online</label>
</div>"""
    html = """<!doctype html><html><head><title>Local calculator fixture</title>__ASSETS__</head><body>
<div id="order-form" data-printproducttype="1" data-currentstoretype="broker">
__BASE_CONTROLS__
<select id="quantity"><option value="50">50</option><option value="100">100</option></select>
__PROOF__
<input type="hidden" name="password" value="synthetic-private-password">
<input type="password" style="display:none" value="synthetic-hidden-modal-password">
</div>
<div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1" id="regular">Regular</a><a class="turnAround" data-toggle="turnaround" data-title="3" id="rush">Rush</a></div>
<div class="totalSection"><span class="subTotal">Subtotal</span><span class="col-4" id="price">$10.00</span></div>
<script>
const selected=__SELECTED__;
__QUERY_INIT__
const data={userSelectedOptions:selected,appliedPrices:{productPrice:'10.00',proofType:{'1':'3.00','36':'0.00'}},others:{isAdmin:false,waiveFees:false,itemId:'',editOrderItem:{}}};
__EMPTY_INIT__
selected.shoppingCartItemId='';selected.priceGroupId='';
const controller={jsonObj:data,printProductType:1,fields:{'1':{options:__OPTIONS__}},get totalPrice(){return String(10+(selected.proofType==='100'&&selected.manualProofType==='1'?3:0)+(selected.productionTime==='3'?2:0))},getCouponPrice:()=>0};
document.querySelector('#order-form')._vnode={component:{proxy:{$refs:{childComponents:{_:{subTree:{component:{refs:{orderFormInstance:controller}}}}}}}}};
const update=()=>{document.querySelector('#price').textContent='$'+Number(controller.totalPrice).toFixed(2);data.appliedPrices.productPrice=selected.productionTime==='3'?'12.00':'10.00';return fetch('/service/rest/v1/products/100/prices?quantity='+selected.quantity)};
document.addEventListener('change',e=>{const t=e.target;const key={proofType:'proofType',proofTypeForManual:'manualProofType',fileDeliveryForIop:'fileDelivery'}[t.name]||t.id;if(['proofType','manualProofType','fileDelivery','quantity'].includes(key)){selected[key]=t.value;const c=document.querySelector('#manualChoices');if(c)c.style.display=selected.proofType==='100'?'':'none';update();}});
for(const e of document.querySelectorAll('.turnAround'))e.addEventListener('click',()=>{selected.productionTime=e.dataset.title;for(const a of document.querySelectorAll('.turnAround'))a.classList.toggle('active',a===e);update();});
fetch('/service/rest/v1/users/self');
fetch('/service/rest/v1/users/self/cart',{method:'POST',body:'{}'}).catch(()=>{});
update();
__GPFIELDS__
__RELOAD__
__UNSTABLE__
</script></body></html>""".replace("__RELOAD__", "window.addEventListener('error',e=>{if(e.message==='Uncaught ReferenceError: gpfields is not defined'){location.reload();e.preventDefault();}});document.addEventListener('change',e=>{if(e.target.id==='paper')setTimeout(()=>{throw new ReferenceError('gpfields is not defined')},0)});" if mode == "reload_init" else "document.addEventListener('change',e=>{if(e.target.name==='proofType'&&e.target.value==='100')setTimeout(()=>location.reload(),0)});" if mode == "reload_phase" else "").replace("__ASSETS__", '<script src="https://static.printograph.com/orderForm.js?v=fixture"></script><script src="https://static.printograph.com/chunk-common.js?v=fixture"></script>' if mode == "cached" else "").replace("__BASE_CONTROLS__", '<label for="size">Size</label><select id="size"><option value="1">Small</option><option value="2">Large</option></select><label for="paper">Stock</label><select id="paper"><option value="">Select stock</option><option value="9">Observed stock</option></select><label for="color">Color</label><select id="color"><option value="">Select color</option><option value="3">Observed color</option></select>' if empty else "").replace("__EMPTY_INIT__", """
data.appliedPrices.productPrice=0;
const q=document.querySelector('#quantity');q.insertAdjacentHTML('afterbegin','<option value="0">Select quantity</option>');q.value='0';
document.addEventListener('change',e=>{if(['size','paper','color'].includes(e.target.id))selected[e.target.id]=e.target.value;if(selected.paper&&selected.color&&Number(document.querySelector('#quantity').value)>0){selected.productId='100000009';}else{selected.productId='';}});
""" if empty else "").replace("__GPFIELDS__", "window.gpfields={private:'synthetic-gpfields-secret'};" if mode == "gpfields_defined" else ("window.addEventListener('error',event=>event.preventDefault());" if mode == "gpfields_handled" else "") + "setTimeout(()=>{throw new ReferenceError('gpfields is not defined')},0);" if mode in {"gpfields", "gpfields_handled"} else "").replace("__PROOF__", proof).replace("__SELECTED__", json.dumps(selected)).replace("__OPTIONS__", json.dumps(data["field_options"])).replace("__UNSTABLE__", "setInterval(()=>{data.appliedPrices.productPrice=String(10+Math.random())},40);" if mode == "unstable" else "")

    query_init = """
for(const [key,value] of new URLSearchParams(location.search)){
 const mapped={turnaround:'productionTime',file_delivery:'fileDelivery',proof_type:'proofType'}[key]||key;
 selected[mapped]=value;
}
document.querySelector('#quantity').value=selected.quantity;
""" if mode.startswith("query_roll") else ""
    if mode == "query_roll_mismatch":
        query_init += "selected.windPosition='33';"
    html = html.replace("__QUERY_INIT__", query_init)
    if mode.startswith("query_roll"):
        html = html.replace('data-printproducttype="1"', 'data-printproducttype="36"').replace('printProductType:1', 'printProductType:36').replace("fields:{'1':", "fields:{'36':")
    if mode.startswith("disclosure_"):
        html = html.replace('<div id="collapseOne">', '<button id="proofDisclosure" type="button" aria-controls="collapseOne" disabled>Process Type +</button><div id="collapseOne" style="display:none">')
    return html



@pytest.mark.asyncio
@pytest.mark.parametrize("mode,budget_limit", [("normal", 80), ("missing", 80), ("unstable", 80), ("normal", 2), ("baseline", 80), ("paced", 80), ("navigation_timeout", 80), ("cached", 80), ("empty", 80), ("reload_init", 80), ("reload_phase", 80), ("gpfields", 80), ("gpfields_handled", 80), ("gpfields_defined", 80), ("query_roll", 80), ("query_roll_mismatch", 80), ("disclosure_timeout", 80), ("disclosure_baseline", 80)])
async def test_intercepted_probe_stability_phases_budget_and_no_cart_writes(tmp_path, mode, budget_limit, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    store = Store(Settings(data_dir=tmp_path, requests_per_second=100, source_concurrency=10))
    with store.session() as session:
        session.add(Run(id="run", name="fixture", status="running"))
        session.add(ProbeJob(id="probe", run_id="run", owner="test", status="running",
                             budget=budget_limit, used=0, manifest={}, results=[]))
    calls = []
    latency_samples = []
    if mode == "paced":
        class SlowQueue:
            def __init__(self, store):
                self.grants = {}

            async def acquire(self, origin, rps, concurrency, check):
                await check()
                await asyncio.sleep(0.6)
                token = str(len(self.grants))
                self.grants[token] = time.monotonic()
                return token

            def release(self, origin, token, elapsed, status, retry, cap):
                latency_samples.append((elapsed, time.monotonic() - self.grants[token]))
        monkeypatch.setattr("crawler.browser_dependency_probe.RateLimiter", SlowQueue)
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        context = await browser.new_context()

        class InterceptedPage:
            def __init__(self, page):
                self.page, self.handlers = page, {}

            def __getattr__(self, name):
                return getattr(self.page, name)

            def locator(self, selector):
                if mode == "disclosure_timeout" and selector == "#proofDisclosure":
                    async def disabled_click(**kwargs):
                        raise TimeoutError("Disabled optional disclosure")
                    return SimpleNamespace(click=disabled_click)
                return self.page.locator(selector)

            async def goto(self, *args, **kwargs):
                if mode == "navigation_timeout":
                    raise TimeoutError("Private source URL token=synthetic-error-secret")
                return await self.page.goto(*args, **kwargs)

            async def route(self, pattern, handler):
                async def wrapper(route):
                    async def continue_fixture():
                        request = route.request
                        calls.append((request.method, request.url))
                        if request.resource_type == "document":
                            await route.fulfill(status=200, content_type="text/html", body=fixture_html(mode))
                        elif request.resource_type == "script":
                            await route.fulfill(status=200, headers={"content-type": "application/javascript", "cache-control": "public, max-age=120"}, body="/*public fixture asset*/")
                        else:
                            payload = {"items": [{"quantity": 50, "price": 10}]} if "/products/" in request.url else {"private": "must-never-be-captured"}
                            await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
                    await handler(SimpleNamespace(request=route.request, continue_=continue_fixture, abort=route.abort, fulfill=route.fulfill))
                self.handlers[handler] = wrapper
                await self.page.route(pattern, wrapper)

            async def unroute(self, pattern, handler):
                await self.page.unroute(pattern, self.handlers[handler])

        class Pool:
            @asynccontextmanager
            async def page(self, *args, **kwargs):
                page = await context.new_page()
                try:
                    yield InterceptedPage(page)
                finally:
                    await page.close()

        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No HTTP allowed"))) as http:
                transport = Transport(store, "run", "", client=http)
                pool = Pool()
                report = await probe_product(pool=pool, transport=transport,
                    budget=ProbeBudget(store, "probe", "test"), product_url=ROLL_URL if mode.startswith("query_roll") else URL,
                    initialization=ROLL_INITIALIZATION if mode.startswith("query_roll") else None,
                    settle_timeout=1.5 if mode == "unstable" else 5, stability_seconds=0.21,
                    phases=() if mode in {"baseline", "paced", "cached", "gpfields", "gpfields_handled", "gpfields_defined", "query_roll", "query_roll_mismatch", "disclosure_baseline"} else None)
                if mode == "cached":
                    first = report
                    report = await probe_product(pool=pool, transport=transport,
                        budget=ProbeBudget(store, "probe", "test"), product_url=ORIGIN + "/products/club-flyers/order",
                        settle_timeout=5, stability_seconds=0.21, phases=())
                    assert first["public_asset_cache_hits"] == 0
                    assert report["public_asset_cache_hits"] == 2
                    expected = hashlib.sha256(b"/*public fixture asset*/").hexdigest()
                    assert report["loaded_contract"] == first["loaded_contract"] == {"order_form_sha256": expected, "common_sha256": expected}
                    assert len([url for _, url in calls if "static.printograph.com" in url]) == 2
        finally:
            await browser.close()
    assert all("/cart" not in url for _, url in calls)
    encoded = json.dumps(report)
    assert "synthetic-private-password" not in encoded
    assert "synthetic-hidden-modal-password" not in encoded
    assert "must-never-be-captured" not in encoded
    assert "synthetic-error-secret" not in encoded
    assert "synthetic-gpfields-secret" not in encoded
    assert report["report_evidence_id"]
    with store.session() as session:
        job = session.get(ProbeJob, "probe")
        attempts = list(session.scalars(select(ProbeAttempt)))
        assert job.used == len(attempts) == len(calls)
        assert job.used <= budget_limit
    if mode in {"reload_init", "reload_phase"}:
        expected = "source_navigation_during_initialization" if mode == "reload_init" else "source_navigation_during_phase"
        assert report["status"] == "unresolved" and report["reason"] == expected, report
        assert report["navigation_count"] == 2
        assert len([url for _, url in calls if url == URL]) == 1  # reload aborted before spending budget
        action = report["pending_action"]
        assert action["status"] == "unresolved" and action["reason"] == expected
        assert action["key"] == ("paper" if mode == "reload_init" else "proofType")
        assert action["kind"] == ("select" if mode == "reload_init" else "radio")
        assert action["selector"]
        if mode == "reload_init":
            assert [item["code"] for item in report["source_error_signals"]] == ["gpfields_not_defined"]
            assert report["page_errors"] == []  # supplier preventDefault suppresses this channel
            assert not report["phases"]
            assert report["actions"][0]["status"] == "completed"
        else:
            assert report["phases"][0]["name"] == "baseline"
            assert report["phases"][0]["status"] == "observed"
            assert all(not phase["name"].startswith("reset") for phase in report["phases"])
    elif mode == "disclosure_timeout":
        assert report["status"] == "unresolved" and report["reason"] == "browser_action_timeout"
        assert report["last_stage"] == "optional_disclosure"
        assert [phase["name"] for phase in report["phases"]] == ["baseline"]
        baseline = report["phases"][0]
        assert baseline["capture"]["settled"] is True and quote_ready(baseline["capture"])
        assert baseline["capture"]["displayed_total"] == "$10.00"
        assert baseline["evidence_ids"]
    elif mode == "query_roll_mismatch":
        assert report["status"] == "unresolved" and report["reason"] == "initialization_not_applied"
        assert report["initialization_mismatches"] == {"windPosition": {"requested": "32", "observed": "33"}}
        assert not report["phases"] and not report["actions"]
    elif mode == "navigation_timeout":
        assert report["reason"] == "navigation_timeout"
        assert report["failure"]["exception_type"] == "TimeoutError"
        assert report["last_phase"] == "baseline" and report["last_stage"] == "navigation"
        assert report["public_dom_flags"]["product_root_present"] is False
    elif budget_limit == 2:
        assert report["status"] == "unresolved"
        assert report["reason"] == "probe_budget_exhausted"
    elif mode == "unstable":
        assert report["status"] == "unresolved" and report["reason"] == "source_timeout"
        assert not report["phases"]
    else:
        assert report["status"] == "finished", report
        phases = {item["name"]: item for item in report["phases"]}
        assert phases["baseline"]["capture"]["settled"] is True
        assert phases["baseline"]["formula"]["comparison"] == "unresolved"  # fixture has no approved bundles
        if mode == "empty":
            assert report["initial_selected_map"]["quantity"] == 0
            assert report["initial_selected_map"]["paper"] == ""
            assert report["baseline_selected_map"]["paper"] == "9"
            assert report["baseline_selected_map"]["color"] == "3"
            assert report["baseline_selected_map"]["quantity"] == "50"
            assert {x["key"] for x in report["baseline_initialization"]} == {"size", "paper", "color", "quantity"}
        if mode in {"baseline", "paced", "cached", "gpfields", "gpfields_handled", "gpfields_defined", "query_roll", "disclosure_baseline"}:
            assert list(phases) == ["baseline"]
            if mode == "query_roll":
                assert report["requested_initialization"] == {k: str(v) for k, v in ROLL_INITIALIZATION.items()}
                assert not report["initialization_mismatches"] and not report["actions"]
                assert report["baseline_selected_map"]["windPosition"] == "32"
                assert report["baseline_selected_map"]["quantity"] == "100"
                assert calls[0][1] == initialized_product_url(ROLL_URL, ROLL_INITIALIZATION)
            if mode.startswith("gpfields"):
                flags = report["public_dom_flags"]
                assert flags["gpfields_exists"] is (mode == "gpfields_defined")
                assert flags["gpfields_type"] == ("object" if mode == "gpfields_defined" else "undefined")
                assert flags["gpfields_not_defined_observed"] is (mode != "gpfields_defined")
                assert [item["code"] for item in report["page_errors"]] == (["gpfields_not_defined"] if mode == "gpfields" else [])
                assert [item["code"] for item in report["source_error_signals"]] == ([] if mode == "gpfields_defined" else ["gpfields_not_defined"])
            if mode == "paced":
                assert report["max_queue_seconds"] >= 0.6
                assert latency_samples
                # A request cannot have spent more time on the wire than the
                # time since its pacing permit was granted. Queue time would.
                assert all(reported <= since_grant + 0.05 for reported, since_grant in latency_samples)
            store.engine.dispose()
            return
        elif mode == "missing":
            assert phases["manual_proof_1"]["status"] == "unsupported"
            assert phases["delivery"]["status"] == "unsupported"
        else:
            assert phases["manual_proof_1"]["capture"]["userSelectedOptions"]["manualProofType"] == "1"
            assert phases["manual_proof_36"]["capture"]["userSelectedOptions"]["manualProofType"] == "36"
            assert phases["delivery"]["changed_selections"] == {"fileDelivery": {"before": "5", "after": "22"}}
        assert phases["rush"]["capture"]["userSelectedOptions"]["productionTime"] == "3"
        assert all(item["controller_context"].get("capture_kind") == "controller" for item in report["captures"])
    store.engine.dispose()
