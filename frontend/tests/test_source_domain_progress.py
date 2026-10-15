"""Built frontend fixture: every request intercepted; no runtime or supplier calls."""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DIST = Path(__file__).resolve().parents[1] / "dist"


@pytest.mark.asyncio
async def test_source_table_progress_keeps_scope_stale_data_and_stops_unmounted():
    playwright = pytest.importorskip("playwright.async_api")
    run = dict(id="table-fixture", name="Direct table fixture", status="running",
               created_at="2026-09-26T00:00:00Z", updated_at="2026-09-26T01:00:00Z",
               total_products=3, completed_products=0, row_count=0, request_count=0,
               cache_hits=0, error_count=0, coverage_status="unknown", products=[],
               scoped_row_count=0, legacy_row_count=0, record_scope="assessed")
    stage = dict(job_id="domain-fixture", run_id=run["id"], scope="Roll Labels finite table stage",
                 status="running", planned_unique_requests=12, completed_requests=2,
                 remaining_unique_requests=10, attempts_used=3, attempt_budget=30,
                 task_counts=dict(pending=9, leased=1, succeeded=2, failed=0),
                 catalog_complete=False, final_configuration_count=None)
    state = dict(fail=False, requests=0, active=0, maximum_active=0, delay=0, items=[stage])
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium unavailable")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 1050})
        async def route(item):
            parsed = urlsplit(item.request.url)
            if parsed.hostname != "dashboard.fixture":
                return await item.abort()
            path = parsed.path
            if path.endswith("/source-domains"):
                assert item.request.method == "GET"
                state["requests"] += 1
                state["active"] += 1
                state["maximum_active"] = max(state["maximum_active"], state["active"])
                try:
                    await asyncio.sleep(state["delay"])
                    return await item.fulfill(status=503 if state["fail"] else 200,
                        json={"detail": "Fixture status unavailable"} if state["fail"] else {"items": state["items"]})
                finally:
                    state["active"] -= 1
            if path == "/api/v1/health": result = {"authentication_required": False}
            elif path == "/api/v1/runs": result = {"items": [run]}
            elif path == "/api/v1/runs/table-fixture": result = run
            elif path.endswith("/efficiency"):
                result = dict(network_requests=0, source_response_observations=0, distinct_response_keys=0,
                    distinct_semantic_responses=0, current_rows=0, new_rows=0, rows_per_request=None,
                    requests_per_unique_response=None, attribution_scope="fixture", started_at=None)
            elif path.endswith(("/events", "/exports", "/rows")): result = {"items": [], "total": 0}
            elif path.startswith("/api/"):
                return await item.fulfill(status=404, json={"detail": "Unexpected fixture route"})
            else:
                file = DIST / (path.lstrip("/") or "index.html")
                assert file.resolve().is_relative_to(DIST.resolve())
                if not file.is_file(): return await item.fulfill(status=404)
                mime = "text/javascript" if file.suffix == ".js" else "text/css" if file.suffix == ".css" else "text/html"
                return await item.fulfill(body=file.read_bytes(), content_type=mime)
            await item.fulfill(json=result)
        await page.route("**/*", route)
        await page.goto("http://dashboard.fixture/")
        await page.get_by_role("button", name="Harvest runs").click()
        await page.get_by_role("button", name="Direct table fixture").click()
        panel = page.get_by_role("region", name="Price table collection", exact=True)
        await playwright.expect(panel.get_by_text("2 / 12", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("3 / 30", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Not counted yet", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Unknown", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "2")
        stage.update(completed_requests=5, remaining_unique_requests=7, attempts_used=7)
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await playwright.expect(panel.get_by_text("5 / 12", exact=True)).to_be_visible()
        state["fail"] = True
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await playwright.expect(panel.get_by_text("Live updates unavailable.", exact=False)).to_be_visible()
        await playwright.expect(panel.get_by_text("5 / 12", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Last known: Running", exact=True)).to_be_visible()
        state["fail"] = False
        stage.update(status="paused")
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await playwright.expect(panel.get_by_text("Paused", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Live updates unavailable.", exact=False)).to_have_count(0)
        # Pausing the browser run must not relabel a completed HTTP stage paused.
        stage.update(status="requests_drained", run_paused=True, completed_requests=12, remaining_unique_requests=0)
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await playwright.expect(panel.get_by_text("Known request stage collected", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Unknown", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Not counted yet", exact=True)).to_be_visible()
        state["delay"] = .3
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_timeout(40)
        await page.evaluate("for(let i=0;i<5;i++)document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_timeout(400)
        assert state["maximum_active"] == 1
        await page.get_by_role("button", name="All harvests", exact=True).click()
        previous = state["requests"]
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_timeout(3200)
        assert state["requests"] == previous
        await browser.close()
