"""Built UI checks with every request intercepted; no supplier or server I/O."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@pytest.mark.asyncio
async def test_variation_plan_inventory_counts_scope_and_last_known_failure():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist first")
    run = {"id": "plan-run", "name": "Inventory fixture", "status": "paused", "created_at": "2026-09-26T00:00:00Z",
           "updated_at": "2026-09-26T00:00:00Z", "total_products": 4, "completed_products": 0,
           "row_count": 0, "request_count": 12, "cache_hits": 0, "error_count": 0,
           "coverage_status": "partial", "products": []}
    cards = {
        "product_id": "internal-cards-product", "product_type": "1", "title": "Business Cards",
        "product_url": "https://www.printograph.com/products/business-cards/order",
        "status": "inventory_partial", "counts": {"candidate_rows": None, "valid_rows": None, "priced_rows": None},
        "table_subtotal_rows": 43700,
        "notes": ["Quantity support is still being checked."],
        "inventory": {
            "sizes": [{"key": "supplier-size-101", "label": '3.5" × 2"'}, {"key": "supplier-size-260", "label": '2" × 2"'}],
            "stocks": [{"key": "supplier-stock-1", "label": "14 pt gloss"}],
            "colorspecs": [{"key": "supplier-color-1", "label": "Full color front"}],
            "corners": [{"key": "supplier-corner-0", "label": "Square corners"}, {"key": "supplier-corner-2", "label": "Rounded corners"}],
            "quantities": [100, 500, 1000],
            "other_dimensions": [{"label": "Coating", "choices": [{"key": "supplier-coating-40", "label": "UV coating"}], "notes": "Availability depends on stock."}],
        },
        "size_plans": [
            {"key": "supplier-size-101", "label": '3.5" × 2"', "candidate_rows": 624, "valid_rows": None, "priced_rows": 0},
            {"key": "supplier-size-260", "label": '2" × 2"', "candidate_rows": None, "valid_rows": 12, "priced_rows": None},
        ],
    }
    roll = {"product_id": "internal-roll-product", "product_type": "36", "title": "Roll Labels", "status": "inventory_partial",
            "product_url": "https://www.printograph.com/products/roll-labels/order",
            "counts": {"candidate_rows": 75, "valid_rows": None, "priced_rows": 0}, "table_subtotal_rows": 70,
            "inventory": {"other_dimensions": [
                {"label": "Shape", "choices": [{"key": "shape-5", "label": "Rectangle"}]},
                {"label": "Finish", "choices": [{"key": "finish-1", "label": "Gloss laminate"}]},
                {"label": "Wind position", "choices": [{"key": "wind-33", "label": "Off left"}]}]},
            "size_plans": [{"key": "internal-roll-size", "label": '0.5" × 1" Horizontal', "candidate_rows": 75,
                             "valid_rows": None, "priced_rows": 0, "notes": ["Rectangle; captured source geometry."]}],
            "non_cost_variants": [{"label": "Wind direction", "choices": [{"key": "supplier-wind-32", "label": "Off bottom"}],
                                   "notes": "No added price is established for this fixture variant."}]}
    club = {"product_id": "internal-club-product", "product_type": "27", "title": "Club Flyers", "status": "candidate_matrix_ready",
            "product_url": "https://www.printograph.com/products/club-flyers/order", "table_subtotal_rows": 2502,
            "counts": {"candidate_rows": 2502, "valid_rows": None, "priced_rows": None},
            "inventory": {"other_dimensions": [{"label": "Raised foil color", "choices": [{"key": "foil-141", "label": "Foil Colors: Gold"}]}]},
            "size_plans": [{"key": "club-size-1", "label": '4" × 6" Vertical', "candidate_rows": 2502, "valid_rows": None, "priced_rows": None}]}
    fallback = {"product_id": "other-supplier-product", "product_type": "36", "title": "Roll Labels", "status": "inventory_partial",
                "product_url": "https://printitprints.com/shop/standard-print/marketing-products/roll-labels-marketing-products/roll-labels/",
                "counts": {"candidate_rows": None, "valid_rows": None, "priced_rows": None}, "inventory": {}, "size_plans": [],
                "notes": ["Separate supplier evidence; pricing has not been assessed."]}
    state = {"fail": False, "requests": 0, "export_fail": False, "exports": [], "created": []}
    payload = {"products": [roll, cards, club, fallback], "notes": ["Listed options are not proof of valid combinations."]}
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        await page.add_init_script("sessionStorage.setItem('atlas_api_token','local-fixture-token')")

        async def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "variation.fixture":
                return await route.abort()
            path = parsed.path
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path == "/api/v1/runs":
                if route.request.method == "POST":
                    state["created"].append(json.loads(route.request.post_data))
                    return await route.fulfill(status=422, json={"detail": "Fixture captured request; no run created"})
                result = {"items": [run]}
            elif path == "/api/v1/providers":
                result = {"items": []}
            elif path == "/api/v1/runs/plan-run":
                result = run
            elif path.endswith("/variation-plan/export"):
                query = parse_qs(parsed.query)
                assert set(query) == {"product_id"}
                product_id = query["product_id"][0]
                assert product_id in {"internal-cards-product", "internal-club-product", "internal-roll-product"}
                assert route.request.headers["authorization"] == "Bearer local-fixture-token"
                state["exports"].append(product_id)
                if state["export_fail"]:
                    return await route.fulfill(status=503, json={"detail": "Fixture export unavailable"})
                return await route.fulfill(content_type="text/csv", body="Size,Quantity,Calculated subtotal\n3.5 x 2,100,20\n")
            elif path.endswith("/variation-plan"):
                state["requests"] += 1
                if state["fail"]:
                    return await route.fulfill(status=503, json={"detail": "Plan fixture unavailable"})
                result = payload
            elif path.endswith("/efficiency"):
                result = {"network_requests": 12, "new_rows": 0, "current_rows": 0}
            elif path.endswith(("/events", "/exports", "/source-domains")):
                result = {"items": []}
            elif path.startswith("/api/"):
                return await route.fulfill(status=404, json={"detail": "Unexpected local fixture request"})
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    content_type = "text/javascript" if target.suffix == ".js" else "text/css" if target.suffix == ".css" else "text/html"
                    return await route.fulfill(body=target.read_bytes(), content_type=content_type)
                return await route.fulfill(status=404, body="Not found")
            await route.fulfill(json=result)

        await page.route("**/*", intercept)
        await page.goto("http://variation.fixture/")
        await page.get_by_role("button", name="Harvest runs").click()
        await page.locator("button.table-title").filter(has_text="Inventory fixture").click()
        panel = page.get_by_role("region", name="Product variation plan")
        await playwright.expect(panel.get_by_role("heading", name="Business Cards", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_role("link", name="Printograph", exact=True)).to_have_attribute("href", cards["product_url"])
        await playwright.expect(panel.locator(".variation-totals dd")).to_have_text(["Unknown", "Unknown", "Unknown"])
        await playwright.expect(panel.get_by_text("43,700 candidates have calculated component subtotals; final price checks are pending.", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Pre-coupon calculated subtotals. These are not verified customer quotes.", exact=True)).to_be_visible()
        async with page.expect_download() as pending_download:
            await panel.get_by_role("button", name="Download candidate rows", exact=True).click()
        download = await pending_download.value
        assert download.suggested_filename == "business-cards-candidate-rows.csv"
        assert "Calculated subtotal" in Path(await download.path()).read_text()
        state["export_fail"] = True
        await panel.get_by_role("button", name="Download candidate rows", exact=True).click()
        await playwright.expect(panel.get_by_role("alert")).to_contain_text("Download failed (503)")
        assert state["exports"] == ["internal-cards-product", "internal-cards-product"]
        await playwright.expect(panel.get_by_text("Quantity support is still being checked.", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Listed options are not proof of valid combinations.", exact=True)).to_be_visible()
        coverage_y = await panel.locator(".variation-coverage").evaluate("e=>e.getBoundingClientRect().top")
        totals_y = await panel.locator(".variation-totals").evaluate("e=>e.getBoundingClientRect().top")
        assert coverage_y < totals_y
        table = panel.get_by_role("table", name="Business Cards row plan by size")
        await playwright.expect(table.locator("tbody tr").nth(0).locator("td")).to_have_text(['3.5" × 2"', "624", "Unknown", "0"])
        await playwright.expect(table.locator("tbody tr").nth(1).locator("td")).to_have_text(['2" × 2"', "Unknown", "12", "Unknown"])
        await playwright.expect(panel.get_by_text("Rounded corners", exact=True)).to_be_hidden()
        await panel.get_by_text("Option inventory", exact=True).click()
        for label in ("14 pt gloss", "Full color front", "Square corners", "Rounded corners", "UV coating", "Availability depends on stock."):
            await playwright.expect(panel.get_by_text(label, exact=True)).to_be_visible()
        inventory = panel.locator(".variation-inventory")
        for quantity in ("100", "500", "1,000"):
            await playwright.expect(inventory.get_by_text(quantity, exact=True)).to_be_visible()
        assert "supplier-" not in await panel.inner_text()
        assert "internal-cards-product" not in await panel.inner_text()
        await playwright.expect(panel.get_by_text("Non-cost variants", exact=True)).to_have_count(0)

        await panel.get_by_label("Variation plan product").select_option("internal-roll-product")
        await playwright.expect(panel.get_by_role("alert")).to_have_count(0)
        state["export_fail"] = False
        async with page.expect_download() as pending_download:
            await panel.get_by_role("button", name="Download candidate rows", exact=True).click()
        assert (await pending_download.value).suggested_filename == "roll-labels-candidate-rows.csv"
        await playwright.expect(panel.get_by_role("link", name="Printograph", exact=True)).to_have_attribute("href", roll["product_url"])
        await playwright.expect(panel.get_by_text("43,700 candidates", exact=False)).to_have_count(0)
        await playwright.expect(panel.get_by_text("70 candidates have calculated component subtotals; final price checks are pending.", exact=True)).to_be_visible()
        await playwright.expect(panel.locator(".variation-totals dd")).to_have_text(["75", "Unknown", "0"])
        roll_table = panel.get_by_role("table", name="Roll Labels row plan by size")
        await playwright.expect(roll_table.locator("tbody tr td")).to_have_text(['0.5" × 1" HorizontalRectangle; captured source geometry.', "75", "Unknown", "0"])
        for label in ("Rectangle", "Gloss laminate", "Off left"):
            await playwright.expect(panel.get_by_text(label, exact=True)).to_be_visible()
        await panel.get_by_text("Non-cost variants", exact=True).click()
        await playwright.expect(panel.get_by_text("Off bottom", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_text("Shown separately from the pricing plan.", exact=False)).to_be_visible()
        await panel.get_by_label("Variation plan product").select_option("internal-club-product")
        await playwright.expect(panel.locator(".variation-totals dd")).to_have_text(["2,502", "Unknown", "Unknown"])
        await playwright.expect(panel.get_by_text("Foil Colors: Gold", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_role("table", name="Club Flyers row plan by size").locator("tbody td")).to_have_text(['4" × 6" Vertical', "2,502", "Unknown", "Unknown"])
        async with page.expect_download() as pending_download:
            await panel.get_by_role("button", name="Download candidate rows", exact=True).click()
        assert (await pending_download.value).suggested_filename == "club-flyers-candidate-rows.csv"
        assert state["exports"][-2:] == ["internal-roll-product", "internal-club-product"]
        await panel.get_by_label("Variation plan product").select_option("other-supplier-product")
        await playwright.expect(panel.get_by_role("link", name="PrintItPrints", exact=True)).to_have_attribute("href", fallback["product_url"])
        await playwright.expect(panel.locator(".variation-totals dd")).to_have_text(["Unknown", "Unknown", "Unknown"])
        await playwright.expect(panel.get_by_text("Separate supplier evidence; pricing has not been assessed.", exact=True)).to_be_visible()
        await playwright.expect(panel.get_by_role("button", name="Download candidate rows", exact=True)).to_have_count(0)
        await playwright.expect(panel.get_by_text("The per-size row plan is not yet assessed.", exact=True)).to_be_visible()
        await panel.get_by_label("Variation plan product").select_option("internal-cards-product")
        state["fail"] = True
        await panel.get_by_label("Refresh variation plan").click()
        await playwright.expect(panel.get_by_text("Plan updates unavailable · showing last-known inventory.", exact=False)).to_be_visible()
        await playwright.expect(panel.locator(".variation-totals dd")).to_have_text(["Unknown", "Unknown", "Unknown"])
        await playwright.expect(table.locator("tbody tr").nth(0).locator("td").nth(1)).to_have_text("624")
        state["fail"] = False
        cards["counts"] = {"candidate_rows": 100, "valid_rows": 0, "priced_rows": None}
        await panel.get_by_label("Refresh variation plan").click()
        await playwright.expect(panel.locator(".variation-totals dd")).to_have_text(["100", "0", "Unknown"])
        await playwright.expect(panel.get_by_text("Plan updates unavailable", exact=False)).to_have_count(0)
        assert state["requests"] >= 3
        await page.set_viewport_size({"width": 390, "height": 844})
        await playwright.expect(panel.get_by_role("heading", name="Business Cards", exact=True)).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.set_viewport_size({"width": 1440, "height": 1100})
        await page.get_by_role("button", name="Overview", exact=True).click()
        await page.get_by_role("button", name="New harvest", exact=True).click()
        dialog = page.get_by_role("dialog", name="Start a product harvest")
        await dialog.get_by_role("textbox", name="Product URLs", exact=False).fill(fallback["product_url"])
        reader = dialog.get_by_label("PrintItPrints document reader", exact=False)
        await playwright.expect(reader).to_have_value("browser")
        await dialog.get_by_role("button", name="Start harvest", exact=True).click()
        await playwright.expect(dialog.get_by_text("Fixture captured request; no run created", exact=True)).to_be_visible()
        assert state["created"][-1]["adapter"] == "auto"
        assert state["created"][-1]["source_metadata"] == {"document_transport": "browser"}
        await dialog.get_by_label("Source adapter", exact=False).select_option("printitprints")
        await reader.select_option("http")
        await dialog.get_by_role("button", name="Start harvest", exact=True).click()
        await playwright.expect(dialog.get_by_text("Fixture captured request; no run created", exact=True)).to_be_visible()
        assert state["created"][-1]["adapter"] == "printitprints"
        assert state["created"][-1]["source_metadata"] == {"document_transport": "http"}
        await dialog.get_by_label("Source adapter", exact=False).select_option("auto")
        await dialog.get_by_role("textbox", name="Product URLs", exact=False).fill(cards["product_url"])
        await playwright.expect(reader).to_have_count(0)
        await dialog.get_by_role("button", name="Start harvest", exact=True).click()
        await playwright.expect(dialog.get_by_text("Fixture captured request; no run created", exact=True)).to_be_visible()
        assert "source_metadata" not in state["created"][-1]
        assert len(state["created"]) == 3
        await browser.close()
