"""Real local product pages share source admission without serializing all work."""

import asyncio

import pytest

from crawler.adapters import browser
from crawler.browser_capacity import BrowserWorkload
from test_browser_traversal import coverage, local_page


@pytest.mark.asyncio
async def test_four_real_product_tasks_run_two_source_pages_and_queue_two(local_page, monkeypatch):
    workload, semaphore = BrowserWorkload(), asyncio.Semaphore(4)
    monkeypatch.setattr(browser, "browser_workload", workload)
    monkeypatch.setattr(browser, "_BROWSER_LOCK", semaphore)
    two_pages_loaded, release_pages = asyncio.Event(), asyncio.Event()
    loaded_pages = []

    async def after_first_quote(page):
        # This callback runs inside the live BrowserAdapter after a source quote
        # was read. Holding both pages proves physical concurrent traversal,
        # rather than inferring it from configured semaphore capacity.
        assert not page.is_closed()
        assert await page.locator("#quantity").input_value() == "1"
        loaded_pages.append(page)
        if len(loaded_pages) == 2:
            two_pages_loaded.set()
        await release_pages.wait()

    html = '''<!doctype html><title>Local capacity product</title><h1>Local capacity product</h1>
    <main><label for="quantity">Quantity</label>
    <select id="quantity"><option value="1">1</option></select></main>
    <div id="subtotal">$12.00</div>'''
    tasks = [asyncio.create_task(local_page(html, after_first=after_first_quote)) for _ in range(4)]
    try:
        await asyncio.wait_for(two_pages_loaded.wait(), timeout=20)
        assert len({id(page) for page in loaded_pages}) == 2
        assert all(not page.is_closed() for page in loaded_pages)
        assert workload.snapshot() == {"active_browser_workers": 2, "waiting_for_browser_workers": 2}
        assert sum(workload._source_active.values()) == 2
    finally:
        release_pages.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    assert len(loaded_pages) == 4 and len({id(page) for page in loaded_pages}) == 4
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome
        rows, events = outcome
        assert [(row["quantity"], row["subtotal"]) for row in rows] == [(1, "12.00")]
        assert coverage(events)["status"] == "visible_domain_exhausted"
        assert coverage(events)["count_is_exact"]
    assert all(page.is_closed() for page in loaded_pages)
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert not workload._source_active and not workload._source_changed
    assert semaphore._value == 4
