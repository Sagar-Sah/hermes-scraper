"""Retry classification never converts a failed source response into a quote."""

import asyncio
import time

import pytest

from crawler.adapters.printograph import AdapterError
from test_browser_traversal import local_page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, "source_unavailable"),
        (502, "source_unavailable"),
        (503, "source_unavailable"),
        (504, "source_unavailable"),
        (404, "source_error"),
        (401, "access_required"),
        (403, "access_required"),
        (429, "rate_limited"),
    ],
)
async def test_source_http_status_preserves_terminal_and_retryable_classes(
    local_page, status, expected
):
    rows = []
    html = """<!doctype html><h1>HTTP error fixture</h1>
<div id="order-form" data-printproducttype="1"><label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select></div>
<div id="subtotal">$5.00</div><script>fetch('/service/rest/v1/products?fixture=true').catch(()=>{});</script>"""
    with pytest.raises(AdapterError, match=f"HTTP {status}") as error:
        await local_page(html, response=lambda _: (status, {"message": "fixture error"}), rows_out=rows)
    assert error.value.code == expected
    assert error.value.details["status"] == status
    assert error.value.details["url_path"] == "/service/rest/v1/products"
    assert rows == []


@pytest.mark.asyncio
async def test_service_signature_settles_after_response_without_await_cycle(local_page):
    html = """<!doctype html><h1>Service settlement fixture</h1>
<div id="order-form" data-printproducttype="1"><label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select></div>
<div id="subtotal">$5.00</div><div id="collapseOne" style="display:none">
<input id="instant" type="radio" name="proofType" value="31" checked><label for="instant">Instant</label>
<input id="manual" type="radio" name="proofType" value="100"><label id="proof-label" for="manual">Manual</label></div>
<script>
fetch('/service/rest/v1/products?fixture=true').then(r=>r.json()).then(()=>{
 document.querySelector('#manual').checked=true;document.querySelector('#subtotal').textContent='$8.00';
 let tick=0;setInterval(()=>document.querySelector('#proof-label').textContent=++tick%2?'Manual $3':'Manual $3.00',20);
});
</script>"""

    def respond(_):
        time.sleep(0.8)
        return 200, {"id": "fixture-product"}

    rows, _ = await asyncio.wait_for(local_page(html, response=respond, bulk=True), 20)
    assert len(rows) == 1
    assert rows[0]["subtotal"] == "8.00"
    context = rows[0]["pricing_context"]["service_context"]
    assert context["groups"]["proofType"] == {"status": "selected", "values": ["100"]}
