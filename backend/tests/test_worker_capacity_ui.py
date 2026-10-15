"""Read loaded worker limits from a built dashboard; no live server or suppliers."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@pytest.mark.asyncio
async def test_sources_shows_active_worker_limit_and_restart_guidance():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist before dashboard integration tests")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")
        browser = await runtime.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})

            async def route(request):
                path = urlsplit(request.request.url).path
                if path == "/api/v1/health":
                    payload = {"authentication_required": False, "execution_capacity": {
                        "product_task_limit": 100, "browser_page_limit": 99,
                        "effective_browser_page_limit": 99, "maximum_configured_limit": 256,
                        "browser_setting": "ATLAS_BROWSER_PRODUCT_CONCURRENCY",
                        "restart_required": True, "scope": "per_instance",
                        "active_browser_workers": 4, "waiting_for_browser_workers": 95}}
                    await request.fulfill(content_type="application/json", body=json.dumps(payload))
                elif path.startswith("/api/v1/"):
                    await request.fulfill(content_type="application/json", body='{"items":[]}')
                else:
                    file = DIST / ("index.html" if path == "/" else path.lstrip("/"))
                    if file.is_file():
                        await request.fulfill(path=str(file))
                    else:
                        await request.abort()

            await page.route("**/*", route)
            await page.goto("http://capacity-fixture.invalid/")
            await page.get_by_role("button", name="Sources & AI", exact=True).click()
            panel = page.get_by_role("region", name="Worker capacity")
            await panel.get_by_text("Up to 99 browser products at once", exact=True).wait_for()
            assert "100 total product tasks per instance" in await panel.inner_text()
            assert "4 browser workers active · 95 waiting for a browser worker" in await panel.inner_text()
            await panel.get_by_text("Change the browser worker limit", exact=True).click()
            assert "ATLAS_BROWSER_PRODUCT_CONCURRENCY=99" in await panel.inner_text()
            assert "restart Hermes" in await panel.inner_text()
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await panel.evaluate("e=>e.scrollWidth <= e.clientWidth")
        finally:
            await browser.close()
