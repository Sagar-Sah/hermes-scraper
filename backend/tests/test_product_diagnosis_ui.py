"""Built Product diagnosis UI; every browser request is intercepted locally."""
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from crawler.diagnosis_policy import normalize_selection

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@pytest.mark.asyncio
async def test_diagnosis_login_policy_revision_and_offline_finalization():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist first")
    supplier = {"id": "printograph", "name": "Printograph", "origin": "https://www.printograph.com",
                "entry_url": "https://www.printograph.com/login", "status": "unknown"}
    url = "https://www.printograph.com/products/business-cards/order"

    def field(key, label, choices=(), selected=(), *, configurable=True, disabled=False, kind="select"):
        return {"key": key, "label": label, "kind": kind, "configurable": configurable, "disabled": disabled,
                "required": configurable, "choices": [{"value": value, "label": name, "disabled": unavailable}
                                                       for value, name, unavailable in choices], "selected_values": list(selected)}

    ready = {"id": "diagnosis-one", "url": url, "title": "Business Cards", "status": "ready", "revision": "v1",
             "supplier_id": supplier["id"], "session_status": "signed_in", "coverage": "current_page_only",
             "warnings": ["Collapsed finishing choices were not inspected.", "initial_snapshot_not_settled", "page_not_open",
                          "no_visible_configurable_controls", "selection_cleared_after_page_change", "diagnosis_limits_reached",
                          "page_timeout", "page_unavailable", "unexpected_source_origin", "product_page_required", "additional_snapshot_limit"], "selection": None,
             "fields": [field("internal-size-id", "Size", [("101", '3.5" × 2"', False), ("102", '2" × 2"', False)], ["101"]),
                        field("internal-proof-mode", "Proof review", [("31", "Instant processing", False), ("100", "Manual proof", False), ("", "None", False)], ["31"], kind="radio"),
                        field("unavailable-coating", "Coating", [("5", "UV coating", True)], ["5"]),
                        field("number-quantity", "Quantity", selected=["100"], kind="number"),
                        field("content-title", "Title", configurable=False, kind="content"),
                        field("content-description", "Description", configurable=False, kind="content"),
                        field("content-images", "Images", configurable=False, kind="content"),
                        field("optional-finishes", "Optional finishes", [("matte", "Matte finish", False)], [], kind="checkbox")]}
    ready["fields"][3].update(min="100", max="10000", step="100")
    state = {"record": None, "get_count": 0, "create_fail": True, "poll_fail": False,
             "actions": [], "policies": [], "finalizations": [], "downloads": [], "eligible": False}
    saved_products = [
        {"run_id": "run-a", "run_name": "Earlier capture", "product_id": "product-a", "title": "Business Cards", "row_count": 200, "source": "quotes"},
        {"run_id": "run-b", "run_name": "Current matrix", "product_id": "product-b", "title": "Business Cards", "row_count": None, "source": "matrix"},
    ]
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        await page.add_init_script("sessionStorage.setItem('atlas_api_token','fixture-token')")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "diagnosis.fixture":
                return await route.abort()
            path, method = parsed.path, route.request.method
            result = None
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path == "/api/v1/runs":
                assert method == "GET", "Diagnosis must never start a harvest"
                result = {"items": []}
            elif path == "/api/v1/supplier-sessions":
                result = {"items": [supplier]}
            elif path.startswith("/api/v1/supplier-sessions/"):
                assert method == "POST"
                action = path.rsplit("/", 1)[-1]
                state["actions"].append(action)
                supplier["status"] = "signed_in" if action == "check" else "login_required"
                result = supplier
            elif path == "/api/v1/product-diagnoses":
                if method == "GET":
                    result = {"items": []}
                else:
                    assert json.loads(route.request.post_data) == {"url": url}
                    if state["create_fail"]:
                        state["create_fail"] = False
                        return await route.fulfill(status=503, json={"detail": "Supplier page could not be opened; retry available."})
                    state["record"] = copy.deepcopy(ready)
                    result = {**ready, "status": "opening", "fields": [], "revision": None}
            elif path == "/api/v1/product-diagnoses/diagnosis-one":
                state["get_count"] += 1
                if state["poll_fail"]:
                    return await route.fulfill(status=503, json={"detail": "Local status unavailable"})
                result = state["record"]
            elif path.endswith("/selection"):
                assert method == "POST"
                policy = json.loads(route.request.post_data)
                assert policy["revision"] == state["record"]["revision"]
                state["policies"].append(policy)
                state["record"]["selection"] = normalize_selection(state["record"], policy["revision"], policy["fields"])
                result = state["record"]
            elif path.endswith("/refresh"):
                assert method == "POST"
                assert json.loads(route.request.post_data) == {}, "Refresh must preserve browser choices"
                state["record"]["revision"] = "v2"
                state["record"]["selection"] = None
                state["record"]["fields"][0]["selected_values"] = ["102"]
                state["record"]["fields"][0]["disabled"] = True
                result = state["record"]
            elif path.endswith("/saved-products"):
                result = {"items": saved_products}
            elif path.endswith("/finalization"):
                payload = json.loads(route.request.post_data)
                assert payload == {"revision": "v1", "run_id": "run-b", "product_id": "product-b", "source": "matrix"}
                state["finalizations"].append(payload)
                result = {"id": "check-one", "examined_rows": 100, "eligible_rows": 60 if state["eligible"] else 0,
                          "held_rows": 30, "invalid_rows_removed": 3, "hidden_choice_rows_omitted": 10,
                          "reasons": {"verification_pending": 30}, "catalog_complete": False,
                          "download_url": "/api/v1/product-diagnoses/diagnosis-one/finalized.csv" if state["eligible"] else None,
                          "report_url": "/api/v1/product-diagnoses/diagnosis-one/check.json",
                          "row_report_url": "/api/v1/product-diagnoses/diagnosis-one/rows.json", "source": "matrix"}
            elif path.endswith(("/finalized.csv", "/check.json", "/rows.json")):
                assert route.request.headers["authorization"] == "Bearer fixture-token"
                state["downloads"].append(path)
                return await route.fulfill(body='{"catalog_complete":false}' if path.endswith(".json") else "Size,Quantity\n3.5 x 2,100\n",
                                           content_type="application/json" if path.endswith(".json") else "text/csv")
            elif path.startswith("/api/"):
                pytest.fail(f"Unexpected local UI request: {method} {path}")
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    return await route.fulfill(body=target.read_bytes(), content_type="text/javascript" if target.suffix == ".js" else "text/css" if target.suffix == ".css" else "text/html")
                return await route.fulfill(status=404, body="Not found")
            await route.fulfill(json=result)

        await page.route("**/*", intercept)
        await page.goto("http://diagnosis.fixture/")
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        await playwright.expect(page.get_by_role("heading", name="Product diagnosis", exact=True)).to_be_visible()
        await page.get_by_label("Product URL", exact=True).fill("javascript:alert(1)")
        analyze = page.get_by_role("button", name="Analyze product", exact=True)
        await playwright.expect(analyze).to_be_disabled()
        await page.get_by_label("Product URL", exact=True).fill("https://independent-supplier.example/product")
        await playwright.expect(analyze).to_be_enabled()
        await page.get_by_label("Product URL", exact=True).fill(url)
        await playwright.expect(analyze).to_be_enabled()  # Unknown session can diagnose public pages.
        await playwright.expect(page.get_by_text("Printograph sign-in is not confirmed.", exact=False)).to_be_visible()
        await page.get_by_role("button", name="Open Printograph browser", exact=True).click()
        await playwright.expect(page.get_by_text("Sign-in needed", exact=True)).to_be_visible()
        await page.get_by_role("button", name="Check sign-in", exact=True).click()
        await playwright.expect(page.get_by_text("Signed in", exact=True)).to_be_visible()
        assert state["actions"] == ["open", "check"]
        assert await page.locator('input[type="password"]').count() == 0
        await analyze.click()
        await playwright.expect(page.get_by_text("Supplier page could not be opened; retry available.", exact=True)).to_be_visible()
        await analyze.click()
        review = page.get_by_role("region", name="Choose output fields", exact=True)
        await playwright.expect(review.get_by_text("Ready for field review", exact=True)).to_be_visible()
        await playwright.expect(review.get_by_text("Collapsed finishing choices were not inspected.", exact=True)).to_be_visible()
        warnings = review.locator(".diagnosis-warnings")
        for code in ready["warnings"][1:]:
            assert code not in await warnings.inner_text()
        for instruction in ("The product options are still loading.", "Analyze its URL again to reopen it", "No configurable product controls were visible.",
                            "Review your fields and fixed choices", "Some fields could not be included", "The supplier page took too long",
                            "The supplier page could not be read", "The browser moved to a different site.", "This page is not a product page.", "Additional Snapshot Limit"):
            await playwright.expect(warnings.get_by_text(instruction, exact=False)).to_be_visible()
        await playwright.expect(review.get_by_role("link", name="Open source URL in a separate browser tab", exact=True)).to_have_attribute("href", url)
        await playwright.expect(review.get_by_text("Source link opens separately; use the Hermes browser for sign-in and option changes.", exact=True)).to_be_visible()
        await playwright.expect(review.get_by_text("This is the current visible page, not every conditional option.", exact=False)).to_be_visible()
        gets = state["get_count"]
        await page.wait_for_timeout(2200)
        assert state["get_count"] == gets, "Ready diagnoses must not keep polling"
        assert "internal-proof-mode" not in await review.inner_text()
        assert "internal-size-id" not in await review.inner_text()
        assert await page.get_by_role("button", name="Start campaign", exact=False).count() == 0
        save = review.get_by_role("button", name="Save field policy", exact=True)
        await review.get_by_label("Include Coating", exact=True).uncheck()
        await playwright.expect(review.get_by_label("Fixed Coating", exact=True)).to_have_value("__hermes_choose__")
        await playwright.expect(save).to_be_disabled()  # No fallback from a disabled selected value.
        await review.get_by_label("Include Coating", exact=True).check()
        await review.get_by_label("Include Quantity", exact=True).uncheck()
        await playwright.expect(review.get_by_label("Fixed Quantity", exact=True)).to_have_value("100")
        await review.get_by_label("Fixed Quantity", exact=True).fill("99")
        await playwright.expect(save).to_be_disabled()
        await review.get_by_label("Fixed Quantity", exact=True).fill("150")
        await playwright.expect(save).to_be_disabled()  # Source step is 100.
        await review.get_by_label("Include Quantity", exact=True).check()
        await review.get_by_label("Include Optional finishes", exact=True).uncheck()
        await playwright.expect(save).to_be_enabled()
        await playwright.expect(review.get_by_text("Keep no choices selected, as observed on the page.", exact=True)).to_be_visible()
        await review.get_by_label("Include Optional finishes", exact=True).check()
        await review.get_by_label("Include Proof review", exact=True).uncheck()
        await playwright.expect(review.get_by_label("Fixed Proof review", exact=True)).to_have_value("31")
        await review.get_by_label("Fixed Proof review", exact=True).select_option("")
        await playwright.expect(save).to_be_enabled()
        await review.get_by_label("Include Description", exact=True).uncheck()
        await review.get_by_label("Include Images", exact=True).uncheck()
        await playwright.expect(review.get_by_text("3 fields excluded from output", exact=True)).to_be_visible()
        await save.click()
        await playwright.expect(review.get_by_text("Field policy saved · 3 excluded from output", exact=True)).to_be_visible()
        policy = {item["key"]: item for item in state["policies"][-1]["fields"]}
        assert policy["internal-size-id"] == {"key": "internal-size-id", "include": True, "fixed_values": []}
        assert policy["internal-proof-mode"] == {"key": "internal-proof-mode", "include": False, "fixed_values": [""]}
        assert policy["content-description"] == {"key": "content-description", "include": False, "fixed_values": []}
        assert policy["content-images"]["include"] is False
        await playwright.expect(review.get_by_text("No harvest has started; prices remain unverified.", exact=False)).to_be_visible()
        check = page.get_by_role("region", name="Check saved product rows", exact=True)
        await playwright.expect(check.get_by_role("button", name="Check saved rows", exact=True)).to_be_disabled()
        await check.get_by_label("Saved product and run", exact=True).select_option("1")
        await check.get_by_role("button", name="Check saved rows", exact=True).click()
        await playwright.expect(check.get_by_text("No finalized row download is available for this report.", exact=True)).to_be_visible()
        await playwright.expect(check.get_by_role("button", name="Download finalized rows", exact=True)).to_have_count(0)
        async with page.expect_download() as report_download:
            await check.get_by_role("button", name="Download check report", exact=True).click()
        assert (await report_download.value).suggested_filename == "product-row-check-report.json"
        async with page.expect_download() as row_report:
            await check.get_by_role("button", name="Download row-by-row report", exact=True).click()
        assert (await row_report.value).suggested_filename == "product-row-by-row-report.ndjson"
        state["eligible"] = True
        await check.get_by_role("button", name="Check saved rows", exact=True).click()
        async with page.expect_download() as row_download:
            await check.get_by_role("button", name="Download finalized rows", exact=True).click()
        assert (await row_download.value).suggested_filename == "finalized-product-rows.csv"
        assert len(state["finalizations"]) == 2
        await review.get_by_role("button", name="Refresh current page", exact=True).click()
        await playwright.expect(review.get_by_text("The page snapshot changed.", exact=False)).to_be_visible()
        await playwright.expect(review.get_by_text("Field policy saved", exact=False)).to_have_count(0)
        await playwright.expect(review.get_by_label("Include Description", exact=True)).to_be_checked()
        await playwright.expect(save).to_be_disabled()  # Disabled controls cannot be included as variable fields.
        await review.get_by_label("Include Size", exact=True).uncheck()
        await playwright.expect(review.get_by_label("Fixed Size", exact=True)).to_have_value("102")
        await playwright.expect(save).to_be_enabled()
        await playwright.expect(check).to_have_count(0)
        state["record"]["status"] = "login_required"
        await review.get_by_role("button", name="Refresh current page", exact=True).click()
        await playwright.expect(review.get_by_text("Sign in in the Hermes browser and check sign-in above.", exact=False)).to_be_visible()
        await playwright.expect(review.get_by_text("return to the product page in that Hermes browser", exact=False)).to_be_visible()
        await playwright.expect(save).to_be_disabled()
        await page.set_viewport_size({"width": 390, "height": 844})
        await playwright.expect(review.get_by_role("button", name="Save field policy", exact=True)).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors
        await browser.close()


@pytest.mark.asyncio
async def test_b2_custom_choices_static_field_and_unchecked_proof_keep_binding_notice():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist first")
    sizes = [{"value": f'{n}" × {n + 1}"', "label": f'{n}" × {n + 1}"', "disabled": False} for n in range(1, 220)]
    metadata = {"binding_kind": "display_label_only", "value_origin": "display_label_surrogate"}
    record = {"id": "b2-one", "url": "https://www.b2sign.com/fixture-product", "title": "Banner fixture", "status": "ready",
              "revision": 1, "supplier_id": "b2sign", "session_status": "unknown", "coverage": "current_page_only", "selection": None,
              "warnings": ["field_label_from_choices", "custom_dropdown_inspection_incomplete", "custom_dropdown_choices_unavailable",
                           "custom_dropdown_labels_ambiguous", "custom_dropdown_inspection_failed", "custom_dropdown_close_failed",
                           "custom_dropdown_selection_changed", "unsupported_visible_attribute"],
              "fields": [
                  {"key": "internal-b2-size", "label": "Size", "kind": "custom_select", "configurable": True,
                   "choices": sizes, "selected_values": [sizes[54]["value"]], "disabled": False, "required": False, **metadata},
                  {"key": "internal-b2-sides", "label": "Printing sides", "kind": "static", "configurable": True,
                   "choices": [{"value": "Single sided", "label": "Single sided", "disabled": False}],
                   "selected_values": ["Single sided"], "disabled": False, "required": False, **metadata},
                  {"key": "internal-b2-proof", "label": "Email proof", "kind": "checkbox", "configurable": True,
                   "choices": [{"value": "on", "label": "Email proof", "disabled": False}], "selected_values": [],
                   "disabled": False, "required": False, "multiple": True, "binding_kind": "native_control", "value_origin": "native_value"},
              ]}
    state = {"policies": []}
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})

        async def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "b2diagnosis.fixture":
                return await route.abort()
            path = parsed.path
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path == "/api/v1/runs":
                assert route.request.method == "GET", "No harvest is authorized by this diagnosis"
                result = {"items": []}
            elif path == "/api/v1/supplier-sessions":
                result = {"items": [{"id": "b2sign", "name": "B2Sign", "origin": "https://www.b2sign.com", "entry_url": "https://www.b2sign.com/", "status": "unknown"}]}
            elif path == "/api/v1/product-diagnoses":
                result = {"items": [record]}
            elif path == "/api/v1/product-diagnoses/b2-one":
                result = record
            elif path.endswith("/selection"):
                submitted = json.loads(route.request.post_data)
                state["policies"].append(submitted)
                record["selection"] = normalize_selection(record, submitted["revision"], submitted["fields"])
                result = record
            elif path.endswith("/saved-products"):
                result = {"items": []}
            elif path.startswith("/api/"):
                pytest.fail(f"Unexpected local B2 fixture request: {route.request.method} {path}")
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    return await route.fulfill(body=target.read_bytes(), content_type="text/javascript" if target.suffix == ".js" else "text/css" if target.suffix == ".css" else "text/html")
                return await route.fulfill(status=404, body="Not found")
            await route.fulfill(json=result)

        await page.route("**/*", intercept)
        await page.goto("http://b2diagnosis.fixture/")
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        await page.get_by_label("Previous diagnoses", exact=True).select_option("b2-one")
        review = page.get_by_role("region", name="Choose output fields", exact=True)
        await playwright.expect(review.get_by_text("Supplier adapter support needed.", exact=True)).to_be_visible()
        await playwright.expect(review.get_by_text("You can save this diagnosis, but automatic harvesting of those fields still needs supplier adapter support.", exact=False)).to_be_visible()
        warnings = review.locator(".diagnosis-warnings")
        for code in record["warnings"]:
            assert code not in await warnings.inner_text()
        for explanation in ("A field name was taken from its visible choices.", "Some dropdowns were not fully inspected.",
                            "A dropdown’s choices could not be read.", "A dropdown has repeated choice labels",
                            "A dropdown could not be inspected.", "A dropdown stayed open after inspection.", "A dropdown selection changed during inspection.",
                            "Some visible product options need supplier support before this diagnosis can be used for a complete run."):
            await playwright.expect(warnings.get_by_text(explanation, exact=False)).to_be_visible()
        size = review.locator(".diagnosis-field").filter(has=page.get_by_label("Include Size", exact=True))
        await playwright.expect(size.get_by_text("Observed choices (219)", exact=True)).to_be_visible()
        await playwright.expect(size.locator("ul")).to_be_hidden()
        await size.get_by_text("Observed choices (219)", exact=True).click()
        assert await size.locator("li").count() == 219
        assert await size.locator("ul").evaluate("e => e.scrollHeight > e.clientHeight && e.clientHeight <= 210")
        await review.get_by_label("Include Size", exact=True).uncheck()
        await playwright.expect(review.get_by_label("Fixed Size", exact=True)).to_have_value(sizes[54]["value"])
        assert await review.get_by_label("Fixed Size", exact=True).locator("option").count() == 220
        await review.get_by_label("Fixed Size", exact=True).select_option(sizes[-1]["value"])
        await playwright.expect(review.get_by_label("Include Printing sides", exact=True)).to_be_checked()
        await review.get_by_label("Include Printing sides", exact=True).uncheck()
        await playwright.expect(review.get_by_label("Fixed Printing sides", exact=True)).to_have_value("Single sided")
        await review.get_by_label("Include Email proof", exact=True).uncheck()
        await playwright.expect(review.get_by_text("Keep no choices selected, as observed on the page.", exact=True)).to_be_visible()
        await playwright.expect(review.get_by_role("button", name="Save field policy", exact=True)).to_be_enabled()
        await review.get_by_role("button", name="Save field policy", exact=True).click()
        await playwright.expect(review.get_by_text("Field policy saved · 3 excluded from output", exact=True)).to_be_visible()
        submitted = {item["key"]: item for item in state["policies"][-1]["fields"]}
        assert submitted["internal-b2-size"]["fixed_values"] == [sizes[-1]["value"]]
        assert submitted["internal-b2-sides"]["fixed_values"] == ["Single sided"]
        assert submitted["internal-b2-proof"]["fixed_values"] == []
        await playwright.expect(review.get_by_text("Supplier adapter support needed.", exact=True)).to_be_visible()
        body = await review.inner_text()
        for technical in ("internal-b2-size", "display_label_only", "display_label_surrogate", "custom_select", "native_control"):
            assert technical not in body
        record["status"] = "attention"
        await page.get_by_role("tab", name="Banner fixture", exact=False).click()
        await playwright.expect(review.get_by_text("Resolve the diagnosis warnings before saving.", exact=False)).to_be_visible()
        await playwright.expect(review.get_by_text("You can save this diagnosis", exact=False)).to_have_count(0)
        await playwright.expect(review.get_by_role("button", name="Save field policy", exact=True)).to_be_disabled()
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await browser.close()


@pytest.mark.asyncio
async def test_add_supplier_validation_persistence_and_browser_controls_preserve_policy_draft():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist first")
    record = {"id": "supplier-policy-draft", "url": "https://www.printograph.com/products/business-cards/order", "title": "Existing review",
              "status": "ready", "revision": 1, "supplier_id": "printograph", "warnings": [], "selection": None,
              "fields": [{"key": "content_description", "label": "Description", "kind": "content", "configurable": False,
                          "choices": [], "selected_values": [], "disabled": False, "required": False}]}
    sessions = [{"id": "printograph", "name": "Printograph", "origin": "https://www.printograph.com",
                 "entry_url": "https://www.printograph.com/user/login", "status": "unknown"}]
    state = {"submitted": [], "duplicate": True, "actions": []}
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})

        async def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "addsupplier.fixture":
                return await route.abort()
            path, method = parsed.path, route.request.method
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path == "/api/v1/runs":
                assert method == "GET"
                result = {"items": []}
            elif path == "/api/v1/supplier-sessions":
                if method == "GET":
                    result = {"items": sessions}
                else:
                    payload = json.loads(route.request.post_data)
                    state["submitted"].append(payload)
                    if state["duplicate"]:
                        state["duplicate"] = False
                        return await route.fulfill(status=422, json={"detail": "A supplier with this domain already exists."})
                    site = urlsplit(payload["website_url"])
                    result = {"id": f"custom-{len(sessions)}", "name": payload["name"], "origin": f"{site.scheme}://{site.netloc}",
                              "entry_url": payload.get("login_url") or payload["website_url"], "status": "not_open", "custom": True}
                    sessions.append(result)
            elif path.startswith("/api/v1/supplier-sessions/custom-"):
                assert method == "POST" and json.loads(route.request.post_data) == {}
                action = path.rsplit("/", 1)[-1]
                state["actions"].append(action)
                sessions[-1]["status"] = "signed_in" if action == "check" else "unknown"
                result = sessions[-1]
            elif path == "/api/v1/product-diagnoses":
                assert method == "GET"
                result = {"items": [record]}
            elif path == "/api/v1/product-diagnoses/supplier-policy-draft":
                assert method == "GET"
                result = record
            elif path.startswith("/api/"):
                pytest.fail(f"Unexpected fixture request: {method} {path}")
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    return await route.fulfill(body=target.read_bytes(), content_type="text/javascript" if target.suffix == ".js" else "text/css" if target.suffix == ".css" else "text/html")
                return await route.fulfill(status=404, body="Not found")
            await route.fulfill(json=result)

        await page.route("**/*", intercept)
        await page.goto("http://addsupplier.fixture/")
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        await page.get_by_label("Previous diagnoses", exact=True).select_option(record["id"])
        await page.get_by_label("Include Description", exact=True).uncheck()
        add = page.get_by_role("button", name="Add Product Supplier", exact=True)
        await add.click()
        form = page.get_by_role("form", name="Add Product Supplier", exact=True)
        await form.get_by_role("button", name="Add supplier", exact=True).click()
        await playwright.expect(form.get_by_text("Enter a supplier name.", exact=True)).to_be_visible()
        await form.get_by_label("Supplier name", exact=True).fill("Custom printer")
        await form.get_by_label("Website URL", exact=True).fill("http://printer.example")
        await form.get_by_role("button", name="Add supplier", exact=True).click()
        await playwright.expect(form.get_by_text("Use complete public HTTPS URLs without embedded credentials.", exact=True)).to_be_visible()
        await form.get_by_label("Website URL", exact=True).fill("https://printer.example")
        await form.get_by_label("Login page URL", exact=False).fill("https://another.example/login")
        await form.get_by_role("button", name="Add supplier", exact=True).click()
        await playwright.expect(form.get_by_text("The login page must use the same supplier domain", exact=False)).to_be_visible()
        assert state["submitted"] == []
        await form.get_by_label("Login page URL", exact=False).fill("")
        await form.get_by_role("button", name="Add supplier", exact=True).click()
        await playwright.expect(form.get_by_text("A supplier with this domain already exists.", exact=True)).to_be_visible()
        await playwright.expect(form.get_by_label("Supplier name", exact=True)).to_have_value("Custom printer")
        await form.get_by_role("button", name="Cancel", exact=True).click()
        await playwright.expect(form).to_have_count(0)
        await playwright.expect(page.get_by_label("Include Description", exact=True)).not_to_be_checked()
        await add.click()
        await playwright.expect(form.get_by_role("alert")).to_have_count(0)
        await playwright.expect(form.get_by_label("Supplier name", exact=True)).to_have_value("")
        await form.get_by_label("Supplier name", exact=True).fill("Custom printer")
        await form.get_by_label("Website URL", exact=True).fill("https://printer.example")
        await form.get_by_role("button", name="Add supplier", exact=True).click()
        card = page.get_by_role("article", name="Custom printer session", exact=True)
        await playwright.expect(card.get_by_text("Browser not opened", exact=True)).to_be_visible()
        await playwright.expect(page.get_by_text("Custom printer added.", exact=False)).to_be_visible()
        assert state["submitted"][-1] == {"name": "Custom printer", "website_url": "https://printer.example/"}
        assert sessions[-1]["entry_url"] == "https://printer.example/"
        assert state["actions"] == []  # Save does not open the supplier automatically.
        await card.get_by_role("button", name="Open Custom printer browser", exact=True).click()
        await card.get_by_role("button", name="Check sign-in", exact=True).click()
        await playwright.expect(card.get_by_text("Signed in", exact=True)).to_be_visible()
        assert state["actions"] == ["open", "check"]
        await page.get_by_role("button", name="Refresh status", exact=True).click()
        await playwright.expect(card).to_be_visible()
        await playwright.expect(page.get_by_label("Include Description", exact=True)).not_to_be_checked()
        await add.click()
        await form.get_by_label("Supplier name", exact=True).fill("Other printer")
        await form.get_by_label("Website URL", exact=True).fill("https://otherprinter.example")
        await form.get_by_label("Login page URL", exact=False).fill("https://www.otherprinter.example/account/login")
        await form.get_by_role("button", name="Add supplier", exact=True).click()
        await playwright.expect(page.get_by_role("article", name="Other printer session", exact=True)).to_be_visible()
        assert state["submitted"][-1]["login_url"] == "https://www.otherprinter.example/account/login"
        await page.set_viewport_size({"width": 390, "height": 844})
        await add.click()
        await playwright.expect(form.get_by_label("Website URL", exact=True)).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert await form.locator('input[type="password"]').count() == 0
        await browser.close()


@pytest.mark.asyncio
async def test_product_tabs_preserve_unsaved_drafts_and_batch_preflight_retry_is_idempotent():
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist first")

    def diagnosis(identifier, title):
        return {"id": identifier, "url": f"https://printer.example/{identifier}", "title": title,
                "status": "ready", "revision": 1, "warnings": [], "selection": None, "coverage": "current_page_only",
                "fields": [{"key": "description", "label": "Description", "kind": "content", "configurable": False,
                            "choices": [], "selected_values": [], "disabled": False, "required": False},
                           {"key": "size", "label": "Size", "kind": "select", "configurable": True, "required": False, "disabled": False,
                            "choices": [{"value": "small", "label": "Small", "disabled": False}, {"value": "large", "label": "Large", "disabled": False}],
                            "selected_values": ["small"]}]}

    first, second = diagnosis("product-one", "First product"), diagnosis("product-two", "Second product")
    old = {**first, "id": "old-closed-copy", "status": "closed"}
    records = {item["id"]: item for item in [first, second, old]}
    state = {"created": [], "preflights": [], "launches": [], "ready": False, "launch_fail": True, "runs": []}
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})

        async def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "workspace.fixture":
                return await route.abort()
            path, method = parsed.path, route.request.method
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path == "/api/v1/runs":
                assert method == "GET", "Only the atomic diagnosis launch may create runs"
                result = {"items": state["runs"]}
            elif path == "/api/v1/supplier-sessions":
                result = {"items": []}
            elif path == "/api/v1/product-diagnoses":
                if method == "GET":
                    result = {"items": list(records.values())}
                else:
                    submitted = json.loads(route.request.post_data)
                    state["created"].append(submitted)
                    identifier = urlsplit(submitted["url"]).path.strip("/")
                    result = diagnosis(identifier, f"Added {identifier}")
                    records[identifier] = result
            elif path.endswith("/selection"):
                identifier = path.split("/")[-2]
                submitted = json.loads(route.request.post_data)
                records[identifier]["selection"] = normalize_selection(records[identifier], submitted["revision"], submitted["fields"])
                result = records[identifier]
            elif path.endswith("/saved-products"):
                result = {"items": []}
            elif path.endswith("/preflight"):
                submitted = json.loads(route.request.post_data)
                state["preflights"].append(submitted)
                for product in submitted["products"]:
                    normalize_selection(records[product["diagnosis_id"]], product["revision"], product["fields"])
                result = {"ready": state["ready"], "plan_token": "signed-fixture-plan" if state["ready"] else None,
                          "products": [{"diagnosis_id": product["diagnosis_id"], "title": records[product["diagnosis_id"]]["title"],
                                        "status": "ready" if state["ready"] else "blocked",
                                        "reasons": [] if state["ready"] else ["Supplier adapter support is required for this control."]}
                                       for product in submitted["products"]], "planned_products": len(submitted["products"]),
                          "pricing_request_count": None, "eta_seconds": None, "note": "No source traffic during preflight."}
            elif path.endswith("/launch"):
                submitted = json.loads(route.request.post_data)
                state["launches"].append(submitted)
                assert submitted["plan_token"] == "signed-fixture-plan"
                if state["launch_fail"]:
                    state["launch_fail"] = False
                    return await route.fulfill(status=503, json={"detail": "Launch response unavailable"})
                runs = [{"id": "launched-" + product["diagnosis_id"], "name": "Configured " + records[product["diagnosis_id"]]["title"],
                         "status": "queued", "created_at": "2026-09-27T01:00:00Z", "updated_at": "2026-09-27T01:00:00Z",
                         "total_products": 1, "completed_products": 0, "row_count": 0, "request_count": 0,
                         "cache_hits": 0, "error_count": 0, "coverage_status": "unknown"} for product in submitted["products"]]
                state["runs"] = runs
                result = {"batch_id": "atomic-fixture", "runs": runs}
            elif path.startswith("/api/v1/product-diagnoses/"):
                identifier = path.rsplit("/", 1)[-1]
                assert method == "GET" and identifier in records
                result = records[identifier]
            elif path.startswith("/api/"):
                pytest.fail(f"Unexpected workspace request: {method} {path}")
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    return await route.fulfill(body=target.read_bytes(), content_type="text/javascript" if target.suffix == ".js" else "text/css" if target.suffix == ".css" else "text/html")
                return await route.fulfill(status=404, body="Not found")
            await route.fulfill(json=result)

        await page.route("**/*", intercept)
        await page.goto("https://workspace.fixture/")
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        tabs = page.get_by_role("tablist", name="Product reviews", exact=True)
        await playwright.expect(tabs.get_by_role("tab")).to_have_count(2)
        first_tab = tabs.get_by_role("tab", name="First product", exact=False)
        second_tab = tabs.get_by_role("tab", name="Second product", exact=False)
        await first_tab.click()
        await page.get_by_label("Include Description", exact=True).uncheck()
        await first_tab.press("ArrowRight")
        await playwright.expect(second_tab).to_have_attribute("aria-selected", "true")
        await page.get_by_label("Include Size", exact=True).uncheck()
        await page.get_by_label("Fixed Size", exact=True).select_option("large")
        await second_tab.press("ArrowLeft")
        await playwright.expect(first_tab).to_have_attribute("aria-selected", "true")
        await playwright.expect(page.get_by_label("Include Description", exact=True)).not_to_be_checked()
        await second_tab.click()
        await playwright.expect(page.get_by_label("Fixed Size", exact=True)).to_have_value("large")
        launch = page.get_by_role("region", name="Run configured products", exact=True)
        await launch.get_by_role("button", name="Run all as configured (2)", exact=True).click()
        await playwright.expect(launch.get_by_text("No products started · preflight needs attention", exact=True)).to_be_visible()
        assert state["launches"] == []
        policies = {product["diagnosis_id"]: {field["key"]: field for field in product["fields"]} for product in state["preflights"][-1]["products"]}
        assert policies["product-one"]["description"]["include"] is False
        assert policies["product-two"]["size"]["fixed_values"] == ["large"]
        await playwright.expect(launch.get_by_text("Pricing request count and completion time are unknown", exact=False)).to_be_visible()
        # Save the same reviewed settings before testing a full reload; launch
        # must not save policies implicitly, and unsaved-tab retention was checked above.
        await first_tab.click()
        await page.get_by_role("button", name="Save field policy", exact=True).click()
        await playwright.expect(page.get_by_text("Field policy saved · 1 excluded from output", exact=True)).to_be_visible()
        await second_tab.click()
        await page.get_by_role("button", name="Save field policy", exact=True).click()
        await playwright.expect(page.get_by_text("Field policy saved · 1 excluded from output", exact=True)).to_be_visible()
        state["ready"] = True
        await launch.get_by_role("button", name="Run all as configured (2)", exact=True).click()
        await playwright.expect(launch.get_by_text("Launch response unavailable", exact=False)).to_be_visible()
        second["status"] = "closed"
        await page.reload()
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        await second_tab.click()
        await playwright.expect(page.get_by_text("The owned product page is closed.", exact=False)).to_be_visible()
        await launch.get_by_role("button", name="Run all as configured (2)", exact=True).click()
        await playwright.expect(launch.get_by_text("2 product runs created", exact=True)).to_be_visible()
        assert len(state["preflights"]) == 2
        assert len(state["launches"]) == 2 and state["launches"][0] == state["launches"][1]
        import uuid
        uuid.UUID(state["launches"][0]["idempotency_key"])
        await playwright.expect(launch.get_by_role("button", name="Configured First product · Queued", exact=False)).to_be_enabled()
        await page.reload()
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        await second_tab.click()
        await launch.get_by_role("button", name="Run all as configured (2)", exact=True).click()
        assert len(state["launches"]) == 2  # A repeated successful click does not create another batch.
        second["status"] = "ready"
        await first_tab.click()
        await second_tab.click()
        await launch.get_by_role("button", name="Run this product", exact=True).click()
        await playwright.expect(launch.get_by_text("1 product run created", exact=True)).to_be_visible()
        assert [product["diagnosis_id"] for product in state["launches"][-1]["products"]] == ["product-two"]
        await page.get_by_label("Product URLs to add", exact=True).fill("https://printer.example/product-one\nhttps://printer.example/third\nhttps://www.printer.example/third")
        await page.get_by_role("button", name="Add products", exact=True).click()
        await playwright.expect(tabs.get_by_role("tab")).to_have_count(3)
        assert state["created"] == [{"url": "https://printer.example/third"}]
        await page.get_by_label("Product URLs to add", exact=True).fill("\n".join(f"https://printer.example/extra-{index}" for index in range(21)))
        await page.get_by_role("button", name="Add products", exact=True).click()
        await playwright.expect(tabs.get_by_role("tab")).to_have_count(24)
        assert await tabs.evaluate("e => e.scrollWidth > e.clientWidth")
        assert len(state["launches"]) == 3  # Adding diagnoses never launches a harvest.
        await first_tab.click()
        await playwright.expect(page.get_by_label("Include Description", exact=True)).not_to_be_checked()
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["closed_saved_policy", "live_snapshot", "created_at", "stale_policy"])
async def test_same_url_diagnosis_prefers_live_then_valid_policy_then_creation_time(scenario):
    playwright = pytest.importorskip("playwright.async_api")
    if not (DIST / "index.html").exists():
        pytest.skip("Build frontend/dist first")
    url = "https://www.b2sign.com/pole-banner-set"
    old = {"id": "older-title-only", "url": url, "title": "Unreviewed snapshot", "status": "closed",
           "revision": 1, "coverage": "current_page_only", "warnings": [], "selection": None,
           "fields": [{"key": "title", "label": "Title", "kind": "content", "choices": [],
                       "selected_values": [], "required": False, "disabled": False, "configurable": False}]}
    reviewed = {"id": "saved-email-proof", "url": url, "title": "Reviewed product", "status": "closed", "supplier_id": "b2sign",
                "revision": 1, "coverage": "current_page_only", "warnings": [],
                "fields": [{"key": "EmailProof[]", "label": "Email Proof", "kind": "checkbox",
                            "choices": [{"value": "proof", "label": "Email proof", "disabled": False}],
                            "selected_values": [], "required": False, "disabled": False, "configurable": True}],
                "selection": {"revision": 1, "fields": [{"key": "EmailProof[]", "include": False, "fixed_values": []}]}}
    expected = reviewed
    if scenario == "live_snapshot":
        old["status"] = "ready"
        expected = old
    elif scenario == "created_at":
        # Both have equally valid policies: creation time, not close-time order,
        # chooses the newer record without moving this URL's tab position.
        old = copy.deepcopy(reviewed)
        old.update(id="older-reviewed", title="Older reviewed", created_at="2026-09-26T10:00:00Z")
        reviewed["created_at"] = "2026-09-26T11:00:00Z"
    elif scenario == "stale_policy":
        reviewed["selection"]["revision"] = 0
        expected = old
    records = [old, reviewed]  # Exactly two closed same-URL records in the reported case.
    fetched = []
    quote = {"unit_price": "56.26", "subtotal": "56.26", "price_break_status": "not_exposed_in_response", "price_break_tiers": []}
    if scenario == "closed_saved_policy":
        quote.update(price_break_status="source_numeric_tiers", price_break_tiers=[
            {"qty": [1, 9], "price": "56.26"},
            {"qty": 10, "price": "50", "full_sheet_price": False},
            {"qty": 100, "price": "45", "full_sheet_price": True},
        ])
    observations = [{"quantity": 1, "status": "matched", "quote": quote, "dom": {"turnaround": {"label": "Next business day"}}}]
    if scenario == "closed_saved_policy":
        unresolved = copy.deepcopy(observations[0])
        unresolved.update(quantity=10)
        unresolved["quote"].update(price_break_status="unresolved_schema", price_break_tiers=[])
        observations.append(unresolved)
    pilot = {"id": "saved-pilot", "diagnosis_id": reviewed["id"], "diagnosis_revision": 1,
             "status": "completed", "quote_requests": len(observations), "quantities": [1, 10, 100, 101],
             "observations": observations, "warnings": [], "catalog_complete": False}
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Chromium not installed")
        browser = await runtime.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})

        async def intercept(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname != "grouping.fixture":
                return await route.abort()
            path = parsed.path
            assert route.request.method == "GET", "Reviewing closed history must not mutate policies or start runs"
            if path == "/api/v1/health":
                result = {"authentication_required": False}
            elif path in {"/api/v1/runs", "/api/v1/supplier-sessions"}:
                result = {"items": []}
            elif path == "/api/v1/pricing-pilots":
                result = {"items": [pilot]}
            elif path == "/api/v1/product-diagnoses":
                result = {"items": records}
            elif path.startswith("/api/v1/product-diagnoses/"):
                identifier = path.rsplit("/", 1)[-1]
                fetched.append(identifier)
                result = next(record for record in records if record["id"] == identifier)
            elif path.startswith("/api/"):
                pytest.fail(f"Unexpected grouping request: {path}")
            else:
                target = DIST / (path.lstrip("/") or "index.html")
                if target.is_file() and target.resolve().is_relative_to(DIST.resolve()):
                    return await route.fulfill(body=target.read_bytes(), content_type="text/javascript" if target.suffix == ".js" else "text/css" if target.suffix == ".css" else "text/html")
                return await route.fulfill(status=404, body="Not found")
            await route.fulfill(json=result)

        await page.route("**/*", intercept)
        await page.goto("https://grouping.fixture/")
        await page.get_by_role("button", name="Product diagnosis", exact=True).click()
        tabs = page.get_by_role("tablist", name="Product reviews", exact=True)
        await playwright.expect(tabs.get_by_role("tab")).to_have_count(1)
        tab = tabs.get_by_role("tab", name=expected["title"], exact=False)
        await tab.click()
        await playwright.expect(tab).to_have_attribute("aria-selected", "true")
        assert fetched == [expected["id"]]
        if expected is reviewed:
            proof = page.get_by_label("Include Email Proof", exact=True)
            await playwright.expect(proof).not_to_be_checked()
            await playwright.expect(proof).to_be_disabled()
            assert await page.get_by_label("Fixed Email Proof", exact=True).evaluate("e => Array.from(e.selectedOptions, o => o.value)") == []
            await playwright.expect(page.get_by_text("Keep no choices selected, as observed on the page.", exact=True)).to_be_visible()
            check = page.get_by_role("region", name="Supplier pricing check", exact=True)
            await playwright.expect(check).to_be_visible()
            if scenario == "closed_saved_policy":
                await check.get_by_text("Supplier-provided quantity tiers", exact=True).click()
                tier_table = check.get_by_role("table", name="Supplier quantity tiers for response 1", exact=True)
                await playwright.expect(tier_table.get_by_role("columnheader")).to_have_text(["Quantity", "Supplier price", "Price basis"])
                await playwright.expect(tier_table.get_by_role("row").nth(1)).to_have_text("1–9$56.26Not specified")
                await playwright.expect(tier_table.get_by_role("row").nth(2)).to_have_text("10$50.00Not a full-sheet price")
                await playwright.expect(tier_table.get_by_role("row").nth(3)).to_have_text("100$45.00Full sheet")
                await playwright.expect(check.get_by_text("Some quantity-tier responses have an unresolved format and need review.", exact=True)).to_be_visible()
                await playwright.expect(check.get_by_text("No quantity tiers were returned in these responses.", exact=True)).to_have_count(0)
                await playwright.expect(check.locator("pre")).to_have_count(0)
            else:
                await playwright.expect(check.get_by_text("No quantity tiers were returned in these responses.", exact=True)).to_be_visible()
        await browser.close()
