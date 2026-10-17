"""Local Chromium checks for traversal reuse without reusing a quoted price."""

import pytest

from crawler.browser_stability import SettledSnapshot
from test_browser_traversal import local_page, coverage


@pytest.mark.asyncio
async def test_reviewed_traversal_reuses_only_unchanged_stable_reads(local_page, monkeypatch):
    reused = []
    original = SettledSnapshot.matches

    def track(self, signature, token, *, idle):
        result = original(self, signature, token, idle=idle)
        reused.append(result)
        return result

    monkeypatch.setattr(SettledSnapshot, "matches", track)
    # This local form is an explicit synchronous readiness fixture. Unknown
    # source hosts keep the original generic settling path in production.
    monkeypatch.setattr("crawler.adapters.browser.supports_stable_reuse", lambda url, state: True)
    html = """<!doctype html><h1>Local variants</h1><main>
    <label for="paper">Paper</label><select id="paper"><option value="a">A</option><option value="b">B</option></select>
    <label for="quantity">Quantity</label><select id="quantity"><option value="1">1</option><option value="2">2</option></select>
    </main><div id="subtotal">$10.00</div><script>
    document.querySelectorAll('select').forEach(e=>e.addEventListener('change',()=>{
      document.querySelector('#subtotal').textContent='$'+((document.querySelector('#paper').value==='a'?10:20)*Number(document.querySelector('#quantity').value)).toFixed(2);
    }));</script>"""
    rows, events = await local_page(html)
    assert {(r["configuration"]["paper"]["value"], r["quantity"], r["subtotal"]) for r in rows} == {
        ("a", 1, "10.00"), ("a", 2, "20.00"), ("b", 1, "20.00"), ("b", 2, "40.00")
    }
    assert any(reused)
    assert coverage(events)["count_is_exact"]


@pytest.mark.asyncio
async def test_final_quote_has_fresh_window_after_action_even_without_busy_marker(local_page, monkeypatch):
    # A source can schedule a price update without a loader or tracked request.
    # The final price wait must not reuse choose()'s earlier stable certificate.
    monkeypatch.setattr("crawler.adapters.browser.supports_stable_reuse", lambda url, state: True)
    html = """<!doctype html><h1>Delayed price</h1><main>
    <label for="quantity">Quantity</label><select id="quantity"><option value="1">1</option><option value="2">2</option></select>
    </main><div id="subtotal">$10.00</div><script>
    document.querySelector('#quantity').addEventListener('change',()=>{
      setTimeout(()=>document.querySelector('#subtotal').textContent='$20.00',950);
    });</script>"""
    rows, events = await local_page(html)
    assert [(r["quantity"], r["subtotal"]) for r in rows] == [(1, "10.00"), (2, "20.00")]
    assert coverage(events)["count_is_exact"]
