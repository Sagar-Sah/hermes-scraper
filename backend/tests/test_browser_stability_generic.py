"""Unknown source calculators retain their original conservative timing."""

import pytest

from test_browser_traversal import coverage, local_page


@pytest.mark.asyncio
async def test_generic_delayed_quote_does_not_reuse_reviewed_source_certification(local_page):
    # This source exposes neither a busy marker nor an observed price request.
    # Reusing the intermediate restore() wait would publish $10 for quantity2
    # before the actual $20 result arrives. A reviewed-source shortcut must not
    # silently shorten generic timing merely because the DOM initially matches.
    html = '''<!doctype html><h1>Generic delayed source</h1><main>
    <label for="quantity">Quantity</label><select id="quantity"><option value="1">1</option><option value="2">2</option></select>
    </main><div id="subtotal">$10.00</div><script>
    document.querySelector('#quantity').addEventListener('change',()=>{
      setTimeout(()=>document.querySelector('#subtotal').textContent='$20.00',1800);
    });</script>'''
    rows, events = await local_page(html)
    assert [(row["quantity"], row["subtotal"]) for row in rows] == [
        (1, "10.00"), (2, "20.00"),
    ]
    assert coverage(events)["count_is_exact"]
