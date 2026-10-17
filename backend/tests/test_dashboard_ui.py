"""Local built dashboard regression; every request is intercepted, no suppliers.

Run after `cd frontend && npm run build`. No application server is started.
"""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@pytest.mark.asyncio
async def test_dashboard_failure_history_scope_and_export_transparency():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist before dashboard integration tests")
    run = {
        "id": "fixture-run",
        "name": "Mixed fixture harvest",
        "status": "running",
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T01:00:00Z",
        "total_products": 3,
        "completed_products": 0,
        "finished_attempts": 1,
        "partial_products": 0,
        "active_products": 2,
        "attention_products": 1,
        "row_count": 20,
        "request_count": 30,
        "cache_hits": 0,
        "error_count": 1,
        "coverage_status": "partial",
        "record_scope": "assessed",
        "legacy_row_count": 17,
        "scoped_row_count": 3,
        "products": [
            {
                "id": "failed",
                "title": "Stopped product",
                "url": "https://fixture.invalid/failure",
                "status": "needs_attention",
                "error": "Source changed; refresh required",
                "row_count": 10,
                "request_count": 10,
                "coverage_status": "partial",
                "scoped_row_count": 0,
                "legacy_row_count": 10,
            },
            {
                "id": "retry",
                "title": "Retry product",
                "url": "https://fixture.invalid/retry",
                "status": "queued",
                "row_count": 5,
                "request_count": 10,
                "coverage_status": "partial",
                "retry_at": "2026-09-26T03:00:00Z",
                "retry": {"attempt": 2, "message": "Source connection reset"},
                "scoped_row_count": 0,
                "legacy_row_count": 5,
            },
            {
                "id": "active",
                "title": "Roll Labels",
                "url": "https://fixture.invalid/active",
                "status": "running",
                "row_count": 5,
                "request_count": 10,
                "coverage_status": "partial",
                "scoped_row_count": 3,
                "legacy_row_count": 2,
            },
        ],
    }
    state = {
        "fail_polls": False,
        "row_scopes": [],
        "export_scopes": [],
        "event_queries": [],
        "conflict": False,
        "product_actions": [],
    }
    context_key = {
        "product": "https://fixture.invalid/active",
        "configuration": {"paper": "1"},
        "currency": "USD",
    }
    rows = [
        {
            "id": str(i),
            "title": "Price fixture",
            "product_url": "https://fixture.invalid/active",
            "source_product_id": "fixture",
            "configuration": {"paper": {"value": "1", "label": label}},
            "quantity": quantity,
            "currency": "USD",
            "subtotal": str(quantity * price),
            "unit_price": str(price),
            "provenance": provenance,
            "verification_status": verification,
            "evidence_ids": [],
            "production": {},
            "images": [],
            "coverage_status": "partial",
            "record_scope": "current",
            "price_comparison_context": context_key,
        }
        for i, (quantity, price, label, provenance, verification) in enumerate(
            [
                (100, 0.2, "Paper", "direct_quote", "source_observed"),
                (500, 0.1, "Paper displayed price", "source_table", "pending"),
                (1000, 0.08, "Paper", "direct_quote", "source_observed"),
            ]
        )
    ]
    for row in rows:
        row["source_product_id"] = "supplier-id-998899"
        row["configuration"].update(color={"value": "1", "label": "Full color front"},
                                    coating={"value": "40", "label": "UV coated"})
        row["production"] = {"value": "1", "label": "Regular"}
    rows[0]["configuration"].update(size=3.5, shape={"value": "5", "label": "Rectangle"})
    rows[1]["configuration"].update(size={"width": 2, "height": 3, "unit": "in"})
    rows[2]["title"] = ""
    rows[2]["configuration"].update(size="2", shape={"value": "internal-shape-id"},
                                     source_product_id="internal-configuration-id")
    file = {
        "name": "Cards / combined.csv",
        "url": "/api/v1/runs/fixture-run/download/file.csv",
        "size": 100,
        "snapshot_row_count": 2,
        "current_row_count": 20,
        "stale": True,
        "created_at": "2026-09-26T00:00:00Z",
        "current_coverage_status": "partial",
        "record_scope": "service_context_v2",
        "verification_claim": "Per-row verification only",
    }
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})

        async def route(request):
            parsed = urlsplit(request.request.url)
            if parsed.hostname != "dashboard.fixture":
                return await request.abort()
            path, query = parsed.path, parse_qs(parsed.query)
            result = None
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path == "/api/v1/runs":
                result = {"items": [run]}
            elif path == "/api/v1/runs/fixture-run":
                result = run
            elif "/products/active/" in path:
                assert request.request.method == "POST"
                action = path.rsplit("/", 1)[-1]
                state["product_actions"].append(action)
                run["products"][2]["status"] = (
                    "paused" if action == "pause" else "queued"
                )
                result = run
            elif path.endswith("/source-domains"):
                result = {"items": [{
                    "job_id": "table-stage", "scope": "Printograph table fixture",
                    "status": "requests_drained", "run_paused": False,
                    "planned_unique_requests": 20, "completed_requests": 20,
                    "remaining_unique_requests": 0, "attempts_used": 21,
                    "attempt_budget": 25, "task_counts": {"failed": 0},
                    "known_stage_requests_complete": True,
                    "catalog_complete": False, "pricing_unlocked": False,
                    "final_configuration_count": None,
                }]}
            elif path.endswith("/efficiency"):
                if state["fail_polls"]:
                    return await request.fulfill(
                        status=503, json={"detail": "Efficiency fixture unavailable"}
                    )
                result = {
                    "network_requests": 10,
                    "source_response_observations": 8,
                    "distinct_response_keys": 3,
                    "distinct_semantic_responses": 4,
                    "current_rows": 3,
                    "new_rows": 2,
                    "rows_per_request": 0.2,
                    "requests_per_unique_response": 2,
                    "attribution_scope": "since_telemetry_start",
                    "started_at": "2026-09-26T01:00:00Z",
                    "limits": "Response reuse ratio covers successful captured source responses, not all traffic.",
                }
            elif path.endswith("/events"):
                state["event_queries"].append(query)
                if state["fail_polls"]:
                    return await request.fulfill(
                        status=503, json={"detail": "Activity fixture unavailable"}
                    )
                historical = "before" in query or query.get("category") == ["attention"]
                result = {
                    "items": [
                        {
                            "id": 1 if historical else 500,
                            "type": "product_error" if historical else "progress",
                            "created_at": "2026-09-26T01:00:00Z",
                            "payload": {
                                "message": (
                                    "Old retained source failure"
                                    if historical
                                    else "Newest progress"
                                )
                            },
                        }
                    ],
                    "has_older": not historical,
                }
            elif path.endswith("/exports"):
                state["export_scopes"].append(query.get("scope", ["current"])[0])
                if state["fail_polls"]:
                    return await request.fulfill(
                        status=503, json={"detail": "Export fixture unavailable"}
                    )
                result = {
                    "items": [file],
                    "withheld_historical_snapshots": (
                        1 if query.get("scope") != ["all"] else 0
                    ),
                }
            elif path.endswith("/rows"):
                scope = query.get("scope", ["current"])[0]
                state["row_scopes"].append(scope)
                items = (
                    rows
                    if scope == "current"
                    else [
                        {
                            **row,
                            "record_scope": "legacy_unscoped",
                            "price_comparison_context": None,
                        }
                        for row in rows
                    ]
                )
                if state["conflict"]:
                    items = [
                        *items,
                        {
                            **items[0],
                            "id": "conflict",
                            "subtotal": "99",
                            "unit_price": ".99",
                        },
                    ]
                result = {"items": items, "total": len(items)}
            elif "/download/" in path:
                return await request.fulfill(
                    status=503, json={"detail": "Download fixture unavailable"}
                )
            elif path.startswith("/api/"):
                return await request.fulfill(
                    status=404, json={"detail": "Unexpected fixture request"}
                )
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    content_type = (
                        "text/javascript"
                        if target.suffix == ".js"
                        else "text/css" if target.suffix == ".css" else "text/html"
                    )
                    return await request.fulfill(
                        body=target.read_bytes(), content_type=content_type
                    )
                return await request.fulfill(status=404, body="Not found")
            await request.fulfill(json=result)

        await page.route("**/*", route)
        await page.goto("http://dashboard.fixture/")
        await page.get_by_role("button", name="Harvest runs").click()
        await page.get_by_label("Filter run status").select_option("needs_attention")
        await page.locator("button.table-title").filter(has_text="Mixed fixture harvest").click()
        await playwright.expect(
            page.get_by_text("1 product needs attention · 2 active")
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("remaining · 0 fully complete", exact=False)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text(
                "The total configuration count and completion ETA are not established.",
                exact=False,
            )
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("Automatic retry 2 scheduled", exact=False)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("17 historical records retained for audit.")
        ).to_be_visible()
        await playwright.expect(
            page.get_by_role("region", name="Observed request efficiency")
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("New records / request", exact=True)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("Observations / unique response", exact=True)
        ).to_be_visible()
        await page.get_by_role(
            "button", name="Pause Roll Labels", exact=True
        ).click()
        await playwright.expect(
            page.get_by_role("button", name="Resume Roll Labels", exact=True)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("Products paused here remain paused", exact=False)
        ).to_be_visible()
        await page.get_by_role(
            "button", name="Resume Roll Labels", exact=True
        ).click()
        await playwright.expect(
            page.get_by_role("button", name="Pause Roll Labels", exact=True)
        ).to_be_visible()
        assert state["product_actions"] == ["pause", "resume"]
        table_stage = page.get_by_role("region", name="Price table collection")
        await playwright.expect(table_stage.get_by_text("Known request stage collected", exact=True)).to_be_visible()
        await playwright.expect(table_stage.get_by_text("See variation plan", exact=True)).to_be_visible()
        await playwright.expect(table_stage.get_by_text("Complete catalog", exact=True)).to_be_visible()
        await playwright.expect(table_stage.get_by_text("Unknown", exact=True)).to_be_visible()
        await playwright.expect(table_stage.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "20")
        assert "Not counted yet" not in await table_stage.inner_text()
        # A complete finite request stage cannot mark paused browser work as live
        # or change product completion, saved row counts, or price verification.
        previous_statuses = [product["status"] for product in run["products"]]
        for product in run["products"]:
            product["status"] = "paused"
        run["active_products"] = 0
        previous_finished = run.get("finished_attempts")
        run["finished_attempts"] = 0
        await playwright.expect(page.get_by_text("All product collectors are paused.", exact=True)).to_be_visible(timeout=6000)
        await playwright.expect(page.locator(".progress-header strong")).to_have_text("Product collection paused")
        await playwright.expect(page.locator(".progress-header .tiny-indicator.online")).to_have_count(0)
        await playwright.expect(page.get_by_role("progressbar", name="Products processed", exact=True)).to_have_attribute("aria-valuenow", "0")
        await playwright.expect(page.locator(".progress-header").get_by_text("20 retained records", exact=True)).to_be_visible()
        await playwright.expect(page.locator(".detail-heading").get_by_text("Running", exact=True)).to_be_visible()
        # A partial product response must not imply all collectors are paused.
        last_product = run["products"].pop()
        await playwright.expect(page.get_by_text("All product collectors are paused.", exact=True)).to_have_count(0, timeout=6000)
        run["products"].append(last_product)
        for product, status in zip(run["products"], previous_statuses):
            product["status"] = status
        run["active_products"] = 2
        run["finished_attempts"] = previous_finished
        roll_counts = page.get_by_role("group", name="Roll Labels row counts")
        await playwright.expect(roll_counts.get_by_text("Current extraction rows", exact=True)).to_be_visible()
        await playwright.expect(roll_counts.locator(".current-row-count dd")).to_have_text("3")
        await playwright.expect(roll_counts.get_by_text("Historical rows · legacy", exact=True)).to_be_visible()
        await playwright.expect(roll_counts.locator("dl > div").nth(1).locator("dd")).to_have_text("2")
        await playwright.expect(roll_counts.get_by_text("5 total retained records · final domain not confirmed", exact=True)).to_be_visible()
        await playwright.expect(roll_counts.locator(".configuration-total dd")).to_have_text("Unknown")
        await playwright.expect(page.get_by_text("Row counts are not verified configuration totals.", exact=True)).to_be_visible()
        # Neither completed status nor null count metadata establishes a domain total.
        run["products"][2].update(status="completed", coverage_status="complete", scoped_row_count=None, legacy_row_count=None)
        run.update(scoped_row_count=None, legacy_row_count=None, record_scope="not_assessed")
        await playwright.expect(roll_counts.locator(".current-row-count dd")).to_have_text("Not assessed", timeout=6000)
        await playwright.expect(roll_counts.locator("dl > div").nth(1).locator("dd")).to_have_text("Not assessed")
        await playwright.expect(roll_counts.locator(".configuration-total dd")).to_have_text("Unknown")
        run["products"][2].update(status="queued", coverage_status="partial", scoped_row_count=3, legacy_row_count=2)
        run.update(scoped_row_count=3, legacy_row_count=17, record_scope="assessed")
        await page.get_by_role("tab", name="Activity").click()
        await page.get_by_label("Activity category").select_option("attention")
        await playwright.expect(
            page.get_by_text('"Old retained source failure"', exact=False)
        ).to_be_visible()
        await page.get_by_label("Activity category").select_option("all")
        await page.get_by_role("button", name="Load earlier events").click()
        await playwright.expect(
            page.get_by_text('"Old retained source failure"', exact=False)
        ).to_be_visible()
        assert any("before" in query for query in state["event_queries"])
        await page.get_by_role("tab", name="Configuration rows").click()
        await playwright.expect(
            page.get_by_text("Matching configuration · 3 quantity tiers on this page")
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text(
                "Calculated quantity tiers · verification pending.", exact=False
            )
        ).to_be_visible()
        assert await page.locator(".price-chart circle[fill='#fff']").count() == 1
        assert await page.locator(".price-chart circle[fill='#207964']").count() == 2
        table = page.locator(".product-table")
        for heading in ("Size", "Shape", "Stock", "Colorspec", "Coating", "Quantity", "Production", "Price"):
            await playwright.expect(table.get_by_role("columnheader", name=heading, exact=True)).to_be_visible()
        await playwright.expect(table.get_by_text("3.5", exact=True)).to_be_visible()
        await playwright.expect(table.get_by_text("2 × 3 in", exact=True)).to_be_visible()
        await playwright.expect(table.get_by_text("2", exact=True)).to_be_visible()
        await playwright.expect(table.get_by_text("Rectangle", exact=True)).to_be_visible()
        await playwright.expect(table.get_by_text("Label unavailable", exact=True)).to_be_visible()
        assert "supplier-id-998899" not in await table.inner_text()
        assert "internal-shape-id" not in await table.inner_text()
        assert "internal-configuration-id" not in await table.inner_text()
        await table.get_by_role("button", name="Product", exact=True).click()
        inspector = page.get_by_role("dialog", name="Configuration details")
        await playwright.expect(inspector.get_by_text("supplier-id-998899", exact=True)).to_be_visible()
        await playwright.expect(inspector.get_by_text("internal-shape-id", exact=True)).to_be_visible()
        await page.keyboard.press("Escape")
        await page.get_by_label("Record scope").select_option("legacy")
        await playwright.expect(
            page.get_by_text(
                "Historical unscoped records preserve old quotes", exact=False
            )
        ).to_be_visible()
        await playwright.expect(page.locator(".price-chart")).to_have_count(0)
        assert "legacy" in state["row_scopes"]
        state["conflict"] = True
        await page.get_by_label("Record scope").select_option("current")
        await playwright.expect(
            page.get_by_text("Price curve withheld:", exact=False)
        ).to_be_visible()
        await page.get_by_role("tab", name="Exports").click()
        await playwright.expect(
            page.get_by_text("Older snapshot · 20 rows currently retained")
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("1 historical snapshot is excluded", exact=False)
        ).to_be_visible()
        await page.get_by_label("Export scope").select_option("all")
        await playwright.expect(
            page.get_by_text(
                "Historical snapshots may contain unscoped prices.", exact=False
            )
        ).to_be_visible()
        await page.get_by_role(
            "button", name="Cards / combined.csv", exact=False
        ).click()
        await playwright.expect(
            page.get_by_text("Download failed (503).", exact=False)
        ).to_be_visible()
        state["fail_polls"] = True
        await playwright.expect(
            page.get_by_text("Activity updates unavailable;", exact=False)
        ).to_be_visible(timeout=6000)
        await playwright.expect(
            page.get_by_text("Export updates unavailable;", exact=False)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text(
                "Last-known efficiency; live updates unavailable.", exact=False
            )
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("New records / request", exact=True)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_role("button", name="Cards / combined.csv", exact=False)
        ).to_be_visible()
        await playwright.expect(
            page.get_by_text("Download failed (503).", exact=False)
        ).to_be_visible()
        await page.get_by_role("tab", name="Activity").click()
        await playwright.expect(
            page.get_by_text('"Newest progress"', exact=False)
        ).to_be_visible()
        await browser.close()
