"""Source dependency and coverage regressions using ordinary local Chromium."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from crawler.adapters.browser import BrowserAdapter
from crawler.adapters.printograph import AdapterError
from crawler.browser_session import BrowserSessionPool
from test_browser_adapter import FixtureTransport


@pytest.fixture
async def local_page(monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")
    content = {"html": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/product" and content.get("document_visits") is not None:
                content["document_visits"].append(self.path)
            if self.path.startswith("/service/rest/") and content.get("response"):
                status, data = content["response"](self.path)
                body, content_type = json.dumps(data).encode(), "application/json"
            else:
                status, body, content_type = 200, content["html"].encode(), "text/html"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr("crawler.transport.validate_url", AsyncMock())
    monkeypatch.setenv("ATLAS_BROWSER_HEADLESS", "true")
    monkeypatch.setenv("ATLAS_BROWSER_PROFILE_DIR", "")
    monkeypatch.setenv("ATLAS_BROWSER_STORAGE_STATE", "")

    async def extract(
        html,
        *,
        filters=None,
        bulk=False,
        after_first=None,
        response=None,
        rows_out=None,
        document_visits=None,
        journal=None,
        on_row=None,
    ):
        content["html"] = html
        content["response"] = response
        content["document_visits"] = document_visits
        pool = BrowserSessionPool()
        transport = FixtureTransport()
        transport.browser_pool = pool
        if journal is not None:
            transport.browser_progress = journal
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))
            if after_first and kind == "progress" and payload["rows"] == 1:
                page = next(iter(pool._contexts.values())).pages[0]
                await after_first(page)

        try:
            rows = rows_out if rows_out is not None else []
            async for row in BrowserAdapter().extract(
                {
                    "url": f"http://127.0.0.1:{server.server_port}/product",
                    "selection_filters": filters or {},
                    "browser_bulk_quantities": bulk,
                    "source_metadata": {
                        "currency": "USD",
                        "price_selector": "#subtotal",
                    },
                },
                transport,
                emit,
            ):
                if on_row is not None:
                    await on_row(row)
                rows.append(row)
            return rows, events
        finally:
            await pool.close()

    try:
        yield extract
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


def orientation_page(product_type):
    return f"""<!doctype html><h1>Orientation fixture</h1><div id="order-form" data-printproducttype="{product_type}">
<label for="size">Size</label><select id="size"><option value="101">Horizontal size</option></select>
<label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select>
<input id="horizontal" name="orientation" type="radio" value="1" checked><label for="horizontal">Horizontal</label>
<input id="vertical" name="orientation" type="radio" value="2"><label for="vertical">Vertical</label>
</div><div id="subtotal">$5.00</div><script>
document.querySelectorAll('input[name=orientation]').forEach(radio=>radio.addEventListener('change',()=>{{
document.querySelector('#size').innerHTML=radio.value==='1'?'<option value="101">Horizontal size</option>':'<option value="102">Vertical size</option>';
document.querySelector('#subtotal').textContent=radio.value==='1'?'$5.00':'$6.00';}}));
</script>"""


def coverage(events):
    return next(payload for kind, payload in events if kind == "coverage")


@pytest.mark.asyncio
@pytest.mark.parametrize("product_type", ["1", "27", "36"])
async def test_orientation_precedes_orientation_specific_size_ids(
    local_page, product_type
):
    rows, events = await local_page(orientation_page(product_type))
    assert {
        (
            r["configuration"]["orientation"]["value"],
            r["configuration"]["size"]["value"],
        )
        for r in rows
    } == {("1", "101"), ("2", "102")}
    assert coverage(events)["status"] == "visible_domain_exhausted"
    assert coverage(events)["count_is_exact"]


@pytest.mark.asyncio
async def test_explicit_size_filter_excludes_other_orientation_without_a_gap(
    local_page,
):
    rows, events = await local_page(orientation_page("1"), filters={"size": "101"})
    assert len(rows) == 1
    assert coverage(events)["count_is_exact"]
    assert coverage(events)["filtered_branches"] == 1
    assert not coverage(events)["reasons"]


@pytest.mark.asyncio
async def test_low_quantity_restores_rush_after_regular_maximum(local_page):
    html = """<!doctype html><h1>Production fixture</h1><div id="order-form" data-printproducttype="1">
<label for="size">Size</label><select id="size"><option value="101">Standard</option></select>
<label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option><option value="100">100</option><option value="100000">100000</option></select></div>
<div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a><a id="rush" class="turnAround notActive" data-toggle="turnaround" data-title="3">Rush 2 Business Days</a></div>
<div id="subtotal">$5.00</div><script>
const quantity=document.querySelector('#quantity');
function price(){const mode=document.querySelector('.turnAround.active').dataset.title;document.querySelector('#subtotal').textContent='$'+(Number(quantity.value)/10+(mode==='3'?5:0)).toFixed(2);document.querySelector('#rush').style.display=Number(quantity.value)>100?'none':'';}
quantity.addEventListener('change',price);
document.querySelectorAll('.turnAround').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.turnAround').forEach(other=>other.className='turnAround notActive');button.className='turnAround active';const previous=quantity.value;quantity.innerHTML=(button.dataset.title==='3'?[50,100]:[50,100,100000]).map(q=>'<option value="'+q+'">'+q+'</option>').join('');if([...quantity.options].some(o=>o.value===previous))quantity.value=previous;price();}));
</script>"""
    rows, events = await local_page(html, bulk=True)
    assert {(r["production"]["value"], r["quantity"]) for r in rows} == {
        ("1", 50),
        ("1", 100),
        ("1", 100000),
        ("3", 50),
        ("3", 100),
    }
    assert not coverage(events)["reasons"]
    assert coverage(events)["count_is_exact"]


@pytest.mark.asyncio
async def test_disappeared_advertised_choice_marks_partial_coverage(local_page):
    html = """<!doctype html><h1>Changing domain</h1><div id="order-form"><label for="paper">Paper</label><select id="paper"><option value="a">A</option><option value="b">B</option></select><label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select></div><div id="subtotal">$5.00</div>"""

    async def remove_choice(page):
        await page.evaluate("document.querySelector('#paper option[value=b]').remove()")

    rows, events = await local_page(html, after_first=remove_choice)
    assert len(rows) == 1
    assert coverage(events)["status"] == "partial"
    assert "choice_disappeared" in coverage(events)["reasons"]
    assert coverage(events)["invalid_branches"] == 1
    assert not coverage(events)["count_is_exact"]


@pytest.mark.asyncio
async def test_unavailable_unfiltered_domain_marks_explicit_gap(local_page):
    html = """<!doctype html><h1>Missing dimension</h1><div id="order-form"><label for="paper">Paper</label><select id="paper"><option value="a">A</option><option value="b">B</option></select><label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select><div id="dimension" style="display:none"><label for="width">Width</label><input type="number" id="width" value=""></div></div><div id="subtotal">$5.00</div><script>document.querySelector('#paper').addEventListener('change',e=>document.querySelector('#dimension').style.display=e.target.value==='b'?'block':'none');</script>"""
    rows, events = await local_page(html)
    assert len(rows) == 1
    assert "no_matching_choice" in coverage(events)["reasons"]
    assert not coverage(events)["count_is_exact"]


@pytest.mark.asyncio
async def test_failed_parent_reloads_once_for_26_quantities_and_keeps_sibling(
    local_page,
):
    html = """<!doctype html><h1>Changing parent fixture</h1><div id="order-form">
<label for="paper">Paper</label><select id="paper"><option value="a">A</option><option value="b">B</option></select>
<label for="quantity">Quantity</label><select id="quantity"></select></div>
<div id="subtotal">$5.00</div><script>
const paper=document.querySelector('#paper'),quantity=document.querySelector('#quantity');
function update(){quantity.innerHTML=(paper.value==='a'?Array.from({length:26},(_,i)=>i+1):[100]).map(q=>'<option value="'+q+'">'+q+'</option>').join('');document.querySelector('#subtotal').textContent=paper.value==='a'?'$5.00':'$10.00';}
if(localStorage.getItem('parent-unavailable')){paper.querySelector('[value=a]').remove();paper.value='b';}
paper.addEventListener('change',update);update();
fetch('/service/rest/v1/products?paper='+paper.value);
</script>"""
    document_visits, lookups = [], []

    async def remove_parent(page):
        await page.evaluate("""() => {
            localStorage.setItem('parent-unavailable', 'yes');
            const paper=document.querySelector('#paper');
            paper.querySelector('[value=a]').remove();paper.value='b';
            paper.dispatchEvent(new Event('change'));
        }""")

    def respond(path):
        lookups.append(path)
        return 200, {"id": "fixture-product"}

    rows, events = await local_page(
        html,
        after_first=remove_parent,
        response=respond,
        document_visits=document_visits,
    )
    assert [
        (row["configuration"]["paper"]["value"], row["quantity"]) for row in rows
    ] == [
        ("a", 1),
        ("b", 100),
    ]
    assert len(document_visits) == 2  # Initial page plus one recovery reload.
    assert len(lookups) == 2
    report = coverage(events)
    assert report["status"] == "partial"
    assert report["invalid_branches"] == 1
    assert report["reasons"] == ["prefix_restore_failed"]
    assert not report["count_is_exact"]


def dependent_paper_page():
    return """<!doctype html><h1>Dependent production fixture</h1>
<div id="order-form" data-printproducttype="1">
<label id="paper-label" for="paper_initial">Paper</label><select id="paper_initial" data-dimension="paper"><option value="2">14 pt. Uncoated</option><option value="7">100 lb. Matte Cover</option></select>
<label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select></div>
<div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a><a id="rush" class="turnAround notActive" data-toggle="turnaround" data-title="3">Rush 3 Business Days</a></div>
<div id="subtotal">$5.00</div><div id="ajaxLoader" style="display:none">Loading</div><script>
let replacements=0;
function paper(){return document.querySelector('[data-dimension="paper"]');}
function mode(){return document.querySelector('.turnAround.active').dataset.title;}
async function quote(){
 document.querySelector('#ajaxLoader').style.display='block';
 const response=await fetch('/service/rest/v1/products?productType=1&paper='+paper().value+'&turnaround='+mode());
 if(response.ok){const result=await response.json();document.querySelector('#subtotal').textContent='$'+result.subtotal;}
 document.querySelector('#rush').textContent=paper().value==='7'?'Rush Not Available':'Rush 3 Business Days';
 document.querySelector('#ajaxLoader').style.display='none';
}
document.querySelector('#order-form').addEventListener('change',event=>{if(event.target.dataset.dimension==='paper')quote();});
document.querySelectorAll('.turnAround').forEach(button=>button.addEventListener('click',()=>{
 document.querySelectorAll('.turnAround').forEach(other=>other.className='turnAround notActive');button.className='turnAround active';
 // Changing production replaces the upstream node, as a reactive supplier UI
 // can do. A saved selector must not be reused after this transition.
 const previous=paper(), replacement=previous.cloneNode(true);replacement.id='paper_replaced_'+(++replacements);replacement.value=previous.value;previous.replaceWith(replacement);document.querySelector('#paper-label').htmlFor=replacement.id;
 quote();
}));
</script>"""


def paper_responses(requests, *, fail_regular=False):
    def response(path):
        query = parse_qs(urlparse(path).query)
        selected = (query["paper"][0], query["turnaround"][0])
        requests.append(selected)
        if selected == ("7", "3") or (fail_regular and selected == ("7", "1")):
            return 404, {"messageList": [{"resource": "data", "code": "not_found"}]}
        return 200, {
            "id": "fixture-" + "-".join(selected),
            "subtotal": (
                "7.00"
                if selected[0] == "7"
                else "8.00" if selected[1] == "3" else "5.00"
            ),
        }

    return response


@pytest.mark.asyncio
async def test_base_change_leaves_inherited_rush_before_selecting_new_paper(local_page):
    requests = []
    rows, events = await local_page(
        dependent_paper_page(), response=paper_responses(requests), bulk=True
    )
    assert {
        (
            row["configuration"]["paper"]["value"],
            row["production"]["value"],
            row["subtotal"],
        )
        for row in rows
    } == {("2", "1", "5.00"), ("2", "3", "8.00"), ("7", "1", "7.00")}
    assert requests == [("2", "3"), ("2", "1"), ("7", "1")]
    assert ("7", "3") not in requests
    assert coverage(events)["count_is_exact"]
    assert not coverage(events)["reasons"]


@pytest.mark.asyncio
async def test_rush_filter_allows_temporary_regular_but_never_publishes_it(local_page):
    requests = []
    rows, events = await local_page(
        dependent_paper_page(),
        response=paper_responses(requests),
        filters={"productionTime": "3"},
        bulk=True,
    )
    assert len(rows) == 1
    assert rows[0]["configuration"]["paper"]["value"] == "2"
    assert rows[0]["production"]["value"] == "3"
    assert requests == [("2", "3"), ("2", "1"), ("7", "1")]
    assert coverage(events)["filtered_branches"] == 1
    assert not coverage(events)["reasons"]


@pytest.mark.asyncio
async def test_real_lookup_404_remains_fatal_and_does_not_publish_stale_quote(
    local_page,
):
    requests, rows = [], []
    with pytest.raises(AdapterError, match="HTTP 404") as failure:
        await local_page(
            dependent_paper_page(),
            response=paper_responses(requests, fail_regular=True),
            rows_out=rows,
            bulk=True,
        )
    assert failure.value.code == "source_error"
    assert requests[-2:] == [("2", "1"), ("7", "1")]
    assert len(rows) == 2
    assert all(row["configuration"]["paper"]["value"] == "2" for row in rows)


def wind_acknowledgment_page(*, request_quote=False):
    return """<!doctype html><h1>Roll Labels</h1><div id="order-form" data-printproducttype="36">
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select>
<div style="position:relative;height:32px"><input style="position:absolute;opacity:0" type="radio" name="windPosition0" id="windPosition0" value="32"><label style="position:absolute;inset:0;background:white" for="windPosition0">Off Bottom</label></div>
<div style="position:relative;height:32px"><input style="position:absolute;opacity:0" type="radio" name="windPosition4" id="windPosition4" value="54" checked><label style="position:absolute;inset:0;background:white" for="windPosition4">Not Important</label></div>
</div><div id="subtotal">$54.00</div><script>
document.querySelectorAll('input[type=radio]').forEach(radio=>radio.addEventListener('change',()=>{
 document.querySelectorAll('input[type=radio]').forEach(other=>other.checked=other===radio);
 document.querySelector('#subtotal').textContent='$'+radio.value+'.00';
 REPLACE_QUOTE_REQUEST
}));
</script>""".replace(
        "REPLACE_QUOTE_REQUEST",
        "fetch('/service/rest/v1/products?productType=36');" if request_quote else "",
    )


def inject_label_acknowledgment_timeout(monkeypatch, *, takes_effect=True):
    from playwright.async_api import Locator, TimeoutError as PlaywrightTimeout

    original = Locator.click

    async def click(locator, *args, **kwargs):
        target = await locator.get_attribute("for")
        if target == "windPosition0":
            if takes_effect:
                await original(locator, *args, **kwargs)
            raise PlaywrightTimeout(
                "Local fixture: native action acknowledgment timed out"
            )
        return await original(locator, *args, **kwargs)

    monkeypatch.setattr(Locator, "click", click)


@pytest.mark.asyncio
async def test_roll_wind_acknowledgment_timeout_settles_before_normal_quote(
    local_page, monkeypatch
):
    inject_label_acknowledgment_timeout(monkeypatch)
    rows, events = await local_page(wind_acknowledgment_page())
    assert {
        (row["configuration"]["windPosition"]["value"], row["subtotal"]) for row in rows
    } == {("32", "32.00"), ("54", "54.00")}
    recovery = next(payload for kind, payload in events if kind == "action_recovered")
    assert recovery["published_rows"] == 0
    assert recovery["quote_validation"] == "pending_normal_extraction"
    assert all(row["provenance"] == "direct_quote" for row in rows)
    assert coverage(events)["count_is_exact"]


@pytest.mark.asyncio
async def test_roll_wind_unapplied_action_timeout_still_stops(local_page, monkeypatch):
    inject_label_acknowledgment_timeout(monkeypatch, takes_effect=False)
    rows = []
    with pytest.raises(AdapterError) as failure:
        await local_page(wind_acknowledgment_page(), rows_out=rows)
    assert failure.value.code == "source_action_timeout"
    assert failure.value.details["selected_value"] == "54"
    assert not rows


@pytest.mark.asyncio
async def test_roll_wind_effective_action_does_not_override_source_404(
    local_page, monkeypatch
):
    inject_label_acknowledgment_timeout(monkeypatch)
    rows = []
    with pytest.raises(AdapterError, match="HTTP 404") as failure:
        await local_page(
            wind_acknowledgment_page(request_quote=True),
            response=lambda path: (404, {"messageList": [{"code": "not_found"}]}),
            rows_out=rows,
        )
    assert failure.value.code == "source_error"
    assert not rows


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_key", ["raisedprint", "spotcoating"])
async def test_low_quantity_recovers_finishing_sibling_hidden_by_high_tier(local_page, finish_key):
    html = """<!doctype html><h1>Quantity-dependent finishing</h1>
<div id="order-form" data-printproducttype="1">
<label for="paper">Paper</label><select id="paper"><option value="10">Matte</option></select>
<div id="finishing"><label for="raisedprint">Raised print</label><select id="raisedprint"><option value="">None</option><option value="135">Raised front</option></select></div>
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option><option value="5000">5000</option></select>
</div><div id="subtotal">$5.00</div><script>
const quantity=document.querySelector('#quantity'),raised=document.querySelector('#raisedprint');
function update(){document.querySelector('#finishing').style.display=Number(quantity.value)>100?'none':'';document.querySelector('#subtotal').textContent=raised.value==='135'?'$8.00':'$5.00';}
quantity.addEventListener('change',update);raised.addEventListener('change',update);update();
</script>"""
    html = html.replace("raisedprint", finish_key)
    rows, events = await local_page(html, bulk=True)
    assert {
        (row['configuration'].get(finish_key, {}).get('value'), row['quantity'])
        for row in rows
    } == {('', 100), (None, 5000), ('135', 100)}
    # High-tier absence is retained; a paid finishing selection cannot disappear.
    assert next(row for row in rows if row['quantity'] == 5000)['subtotal'] == '5.00'
    assert coverage(events)['status'] == 'partial'
    assert not any(
        kind == 'branch_skipped'
        and payload.get('key') == finish_key
        and payload.get('reason') == 'dynamic_control_disappeared'
        for kind, payload in events
    )
