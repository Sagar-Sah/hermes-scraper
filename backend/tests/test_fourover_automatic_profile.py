"""Portable automatic-4over regressions: synthetic data, no live supplier access.

Default imports use this checkout's crawler files. To review an uninstalled
revision, HERMES_FOUROVER_TEST_OVERLAY may name a crawler directory; unchanged
pure dependencies still come from this checkout. No task artifacts are needed.
Browser/transport entry points are replaced in an isolated module namespace.
Optional Chromium cases intercept every request and never use a saved profile.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
from html import escape
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import urlencode
import uuid

import pytest

CRAWLER = Path(__file__).resolve().parents[1] / "crawler"
SOURCE = "https://4over.com/synthetic-package"
PRIVATE = "SYNTHETIC-PRIVATE-ADDRESS-NEVER-RETAIN"
SELECTION = {"child_id": "700", "run_size": 250, "attributes": {"111": "10", "999": "20"}}


def load_driver():
    """Load the actual modules with I/O entry points disabled, without app/DB."""
    overlay = os.environ.get("HERMES_FOUROVER_TEST_OVERLAY")
    paths = [str(Path(overlay).resolve()), str(CRAWLER)] if overlay else [str(CRAWLER)]
    name = "_portable_fourover_" + uuid.uuid4().hex
    package = ModuleType(name); package.__path__ = paths; sys.modules[name] = package
    adapters = ModuleType(name + ".adapters"); adapters.__path__ = []
    browser = ModuleType(name + ".adapters.browser")
    @asynccontextmanager
    async def browser_slot(*args, **kwargs):
        raise AssertionError("Real browser entry is forbidden in synthetic tests")
        yield  # pragma: no cover
    browser.browser_slot = browser_slot
    transport = ModuleType(name + ".transport")
    class ExtractionError(Exception):
        def __init__(self, message, code=None): super().__init__(message); self.code = code
    class RateLimiter:
        def __init__(self, *args, **kwargs): raise AssertionError("Real limiter/store is forbidden")
    async def validate_url(*args): raise AssertionError("Real URL validation/network is forbidden")
    transport.ExtractionError, transport.RateLimiter, transport.validate_url = ExtractionError, RateLimiter, validate_url
    for suffix, module in ((".adapters", adapters), (".adapters.browser", browser), (".transport", transport)):
        sys.modules[name + suffix] = module
    target = next(Path(p) / "fourover_automatic_profile.py" for p in paths if (Path(p) / "fourover_automatic_profile.py").is_file())
    spec = importlib.util.spec_from_file_location(name + ".fourover_automatic_profile", target)
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver(): return load_driver()


def ui_ready_state():
    """An explicit completed prerequisite for tests focused on other components.

    The registry/DOM probe and delayed registration gate are independently tested
    in test_fourover_native_ui_readiness. These values are synthetic test state,
    never runtime fallbacks or source/customer values.
    """
    booleans = {key: False for key in (
        "modules_proved", "registry_defined", "tabs_module_defined", "project_module_defined", "registry_available",
        "tabs_registered", "project_registered", "tab_domain_observed", "selected_tab_in_domain", "next_tab_available",
        "current_is_product_options", "current_is_shipping", "current_is_addresses", "next_is_shipping", "next_is_addresses",
        "guest_restriction_observed", "guest_restricted", "tabs_loading_observed", "tabs_loading",
        "project_loading_observed", "project_loading", "shipping_active_observed", "shipping_active",
        "project_name_invalid_observed", "project_name_invalid", "current_project_name_present")}
    for key in ("modules_proved", "registry_defined", "tabs_module_defined", "project_module_defined", "registry_available",
                "tabs_registered", "project_registered", "tab_domain_observed", "selected_tab_in_domain", "next_tab_available",
                "current_is_product_options", "next_is_shipping", "guest_restriction_observed", "tabs_loading_observed",
                "project_loading_observed", "shipping_active_observed", "project_name_invalid_observed"):
        booleans[key] = True
    counts = ("count", "visible_count", "native_invalid_visible", "value_missing_visible", "aria_invalid_visible", "mage_error_visible")
    return {"schema": "native-project-ui-readiness/v1", **booleans, "form_count": 1, "visible_error_count": 0,
            "fields": {key: dict.fromkeys(counts, 0) for key in ("attributes", "quantity", "production", "project_name", "services", "other")}}


def state():
    # These pricing-only fixtures model a completed independent native-controller
    # prerequisite. The real getter/module gate is exercised separately by
    # test_fourover_native_controller_readiness and its intercepted source fixture.
    controller = {"schema": "native-controller-readiness/v1", **{k: True for k in (
        "modules_proved", "available", "registration_defined", "child_matches_dom", "quantity_matches_dom",
        "quantity_strict_row", "unit_price_positive", "item_total_positive", "tat_matches_dom",
        "tat_input_visible", "tat_label_visible", "tat_ready", "ready")}}
    return {"source_ui": ui_ready_state(), "source_controller": controller, "attributes": {k: {"count": 1, "value": v, "enabled": True, "visible": True, "choice_enabled": True}
                           for k, v in SELECTION["attributes"].items()},
            "services": {k: {"count": 1, "checked": False, "enabled": True} for k in ("job_sample", "digital_proof")},
            "child_id": "700", "run_size": "250", "run_size_values": ["250"], "available_run_sizes": ["250"],
            "sets": None, "set_control_count": 0, "parent_ids": ["1234"], "blocked_popup_attempts": 0,
            "production": {"value": "3", "label": "3 Business Days", "visible": True}, "production_control_count": 1,
            "money": [{"kind": "subtotal", "value": "10.01"}, {"kind": "total", "value": "11.26"}]}


def quote_form():
    return {"uuid": PRIVATE, "name": "", "product_id": "1234", "run_size": "250", "custom_run_size": "0",
            "product_options[turnaround_time]": "3", "product_options[job_sample]": "false", "product_options[digital_proof]": "false",
            **{f"product_options[super_attribute][{k}]": v for k, v in SELECTION["attributes"].items()},
            "shipments[0][uuid]": PRIVATE, "shipments[0][jobs][0][uuid]": PRIVATE,
            "shipments[0][jobs][0][name]": "", "shipments[0][jobs][0][artwork_status]": "",
            "shipments[0][shipping_method]": "", "shipments[0][group_shipping_applied]": "false",
            "shipments[0][shipping_time_from_carrier]": "", "shipments[0][shipping_address][firstname]": PRIVATE,
            "shipments[0][shipping_address][street][]": PRIVATE}


def test_semantic_once_identity_ignores_journal_id_but_binds_source_child_quantity(driver, tmp_path):
    key = driver.semantic_key(SOURCE, "700", 250)
    assert key != driver.semantic_key(SOURCE, "701", 250)
    assert key != driver.semantic_key(SOURCE, "700", 500)
    assert key != driver.semantic_key("https://4over.com/other-package", "700", 250)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda n: driver.reserve_once(tmp_path, SOURCE, SELECTION, str(n) * 64), range(1, 5)))
    assert sum(results) == 1
    assert driver.spent(tmp_path, SOURCE, "700", 250)
    assert not driver.reserve_once(tmp_path, SOURCE, SELECTION, "f" * 64)
    path = tmp_path / "fourover-native-observation-selections" / key[:2] / (key + ".json")
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_bytes())["selection"] == SELECTION


@pytest.mark.parametrize("patch", [{"run_size": True}, {"run_size": 0}, {"child_id": "0700"}, {"attributes": {"bad": "10"}}])
def test_invalid_selection_never_reserves(driver, tmp_path, patch):
    with pytest.raises(driver.Held): driver.reserve_once(tmp_path, SOURCE, {**SELECTION, **patch}, "a" * 64)
    assert not (tmp_path / "fourover-native-observation-selections").exists()


def test_reservation_rejects_symlink_directory_without_external_write(driver, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (tmp_path / "fourover-native-observation-selections").symlink_to(outside, target_is_directory=True)
    with pytest.raises((driver.Held, OSError)): driver.reserve_once(tmp_path, SOURCE, SELECTION, "a" * 64)
    assert list(outside.iterdir()) == []


def test_native_quote_projection_retains_product_context_and_discards_private_data(driver):
    projected = driver.automatic_request_context(urlencode(quote_form()), state(), selection=SELECTION)
    assert projected["set_binding"] == "one_native_job" and projected["sets"] == 1
    assert projected["attributes"] == SELECTION["attributes"] and projected["run_size"] == 250
    assert PRIVATE not in json.dumps(projected) and "uuid" not in json.dumps(projected)


@pytest.mark.parametrize("mutation", ["action", "shipping", "service", "quantity", "attribute", "second_job", "duplicate", "prototype"])
def test_quote_guard_rejects_mutations_before_admission(driver, mutation):
    form = quote_form()
    if mutation == "action": form["action"] = "cart"
    elif mutation == "shipping": form["shipments[0][shipping_method]"] = "ground"
    elif mutation == "service": form["product_options[job_sample]"] = "true"
    elif mutation == "quantity": form["run_size"] = "500"
    elif mutation == "attribute": form["product_options[super_attribute][111]"] = "11"
    elif mutation == "second_job": form["shipments[0][jobs][1][uuid]"] = PRIVATE
    elif mutation == "prototype": form["shipments[0][shipping_address][__proto__][x]"] = PRIVATE
    encoded = urlencode(form) + ("&run_size=250" if mutation == "duplicate" else "")
    with pytest.raises(driver.Held): driver.automatic_request_context(encoded, state(), selection=SELECTION)


def test_scope_and_url_do_not_silently_enable_services_or_all_inclusive(driver):
    for scope in ({**driver.SCOPE, "sets": True}, {**driver.SCOPE, "sets": 2}, {**driver.SCOPE, "digital_proof": True}):
        with pytest.raises(driver.Held): driver.validate_scope(scope)
    association = {"campaign_id": "repair", "product_id": "42", "supplier": "4over", "source_url": SOURCE}
    assert driver.supports(association)
    assert not driver.supports({**association, "source_url": "https://4over.com/all-inclusive-postcards"})
    assert not driver.supports({**association, "source_url": SOURCE + "?private=1"})


class Store:
    def __init__(self, root): self.root = root; self.config = SimpleNamespace(requests_per_second=1, source_concurrency=1); self.objects = {}
    def profiles(self, kind): return []
    def put_object(self, raw, suffix):
        sha = hashlib.sha256(raw).hexdigest(); relative = f"objects/{sha[:2]}/{sha}.{suffix}"
        target = self.root / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        self.objects[relative] = raw; return relative


def session_fixture(driver, tmp_path, mode="good"):
    store = Store(tmp_path); checkpoint = tmp_path / "catalog.json"; checkpoint.write_text("synthetic catalog")
    entry = {"child_id": "700", "attributes": dict(SELECTION["attributes"]), "historical_prices": {250: "8.00"}, "fields": []}
    catalog = {"association": {"campaign_id": "repair", "product_id": "42", "supplier": "4over", "source_url": SOURCE},
               "source_url": SOURCE, "job": {}, "rows": [], "entries": [entry], "excluded_children": [], "declared_domain_count": 1,
               "reference": {"snapshot_id": "a" * 32, "checkpoint_sha256": "b" * 64, "option_rows_sha256": "c" * 64},
               "checkpoint_path": checkpoint, "checkpoint_identity": driver.file_identity(checkpoint)}
    path, cursor = driver.load_cursor(store, catalog)
    observer = SimpleNamespace(store=store, pool=None)
    async def scope(): pass
    session = driver.AutomaticFourOverSession(observer, {"catalog": catalog, "cursor_path": path, "cursor": cursor},
                                             {"profile_id": driver.PROFILE_ID, "max_quotes_per_session": 8}, scope)
    # Pricing-only fixture: the separately tested native address read is complete.
    session.address_ready = True
    session.first_target_table_ready = True; session.deadline = session.session_deadline
    session.document_sha256 = "d" * 64; session.prefix = "https://4over.com/static/version1/frontend/FourOver/default/en_US/"
    session.tables["700"] = {"rows": [{"quantity": 250, "price": "10.006"}], "sha256": "e" * 64,
                             "evidence": {"object_path": "synthetic-table", "sha256": "e" * 64}, "price_epoch": 0}
    current = state(); current["run_size"] = None; current["run_size_values"] = []
    if mode == "unready_services": current["services"]["job_sample"] = {"count": 0, "enabled": False}
    if mode == "missing_quantity": current["available_run_sizes"] = []
    class Control:
        async def count(self): return 1
    class Page:
        url = SOURCE
        main_frame = object()
        quantity_clicks = 0
        def locator(self, name): return Control()
        async def evaluate(self, script, arg=None):
            if script == driver.ATTRIBUTE_DIAGNOSTIC_JS: return {"selected_matches": True, "expected_choice_enabled": True}
            if script.startswith("() =>"): return deepcopy(current)
            if script.startswith("({quantity,source})"):
                self.quantity_clicks += 1; current["run_size"] = str(arg["quantity"]); current["run_size_values"] = [str(arg["quantity"])]
                totals = {"subtotal": "10.01", "grand_total": "11.26", "small_qty_fee": "1.25"}
                if mode == "wrong_subtotal": totals.update(subtotal="10.02", grand_total="11.27")
                if mode == "wrong_rendered": current["money"][1]["value"] = "99.00"
                projection = driver.automatic_request_context(urlencode(quote_form()), current, selection=session.selection)
                session.summary = {"epoch": session.epoch, "totals": driver.parse_summary(json.dumps({"totals": totals}).encode()),
                                   "request_context": projection, "response_sha256": "f" * 64, "observed_at": "synthetic"}
                return 1
            raise AssertionError("Unexpected synthetic browser operation")
    page = Page(); session.page = page
    async def wait(predicate, seconds=35):
        value = await session.state()
        if not predicate(value): raise driver.Held("source_context_timeout")
        return value
    session.wait = wait
    return session, store, page


@pytest.mark.asyncio
async def test_current_table_rounding_summary_fee_and_rendered_cost_reconcile(driver, tmp_path):
    session, store, page = session_fixture(driver, tmp_path)
    assert await session.collect() is True and page.quantity_clicks == 1
    receipts = [json.loads(raw) for name, raw in store.objects.items() if name.endswith("fourover-automatic-quote.json")]
    assert len(receipts) == 1
    quote = receipts[0]
    assert quote["totals"]["subtotal"] == "10.01"
    assert quote["supplier_product_total_excluding_shipping"] == "11.26"
    assert quote["historical_source_table_price"] == "8.00" and quote["source_table_price_changed"]
    assert quote["scope"] == driver.SCOPE and quote["shipping_verified"] is False
    assert not quote["source_data_finalized"] and not quote["woocommerce_import_ready"]
    assert PRIVATE not in json.dumps(quote)
    # A fresh coordinator/cursor still sees the durable semantic reservation.
    assert driver.spent(tmp_path, SOURCE, "700", 250)
    assert await session.collect() is True and page.quantity_clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("wrong_subtotal", "source_table_total_disagree"), ("wrong_rendered", "source_rendered_total_disagree")])
async def test_disagreement_is_held_and_attempt_is_not_replayed(driver, tmp_path, mode, reason):
    session, store, page = session_fixture(driver, tmp_path, mode)
    with pytest.raises(driver.Held) as error: await session.collect()
    assert error.value.code == reason
    assert session.cursor["processed_count"] == 1 and session.cursor["verified_count"] == 0
    assert next(iter(session.cursor["receipts"].values()))["status"] == "held"
    assert driver.spent(tmp_path, SOURCE, "700", 250)
    assert not any(name.endswith("fourover-automatic-quote.json") for name in store.objects)
    assert await session.collect() is True and page.quantity_clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unready_services", "missing_quantity"])
async def test_controls_are_validated_before_once_reservation(driver, tmp_path, mode):
    session, store, page = session_fixture(driver, tmp_path, mode)
    with pytest.raises(driver.Held): await session.collect()
    assert page.quantity_clicks == 0
    assert not (tmp_path / "fourover-native-observation-selections").exists()
    assert session.cursor["inflight"] is None


@pytest.mark.asyncio
async def test_static_handlers_do_not_block_quote_drain(driver):
    session = driver.AutomaticFourOverSession.__new__(driver.AutomaticFourOverSession)
    static_gate, quote_gate = asyncio.Event(), asyncio.Event()
    static_task = asyncio.create_task(static_gate.wait()); quote_task = asyncio.create_task(quote_gate.wait())
    session.handlers = {static_task, quote_task}; session.public_static_handlers = {static_task}
    session.price_metadata_handlers = set(); session.customer_metadata_handlers = set(); session.address_metadata_handlers = set()
    asyncio.get_running_loop().call_soon(quote_gate.set)
    try:
        await asyncio.wait_for(session.drain(), .2)
        assert quote_task.done() and not static_task.done()
    finally:
        static_task.cancel(); await asyncio.gather(static_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/rest/default/V1/project/mine/addresses", "/print/shipment/estimate", "/print/shipment/rates"])
async def test_background_shipping_aborts_before_private_body_or_headers(driver, tmp_path, path):
    session, _, page = session_fixture(driver, tmp_path)
    session.stage = "bootstrap"; reads = []
    class Request:
        frame = page.main_frame; method = "POST"; resource_type = "xhr"; url = "https://4over.com" + path
        @property
        def post_data(self): reads.append("body"); raise AssertionError("Private body must not be read")
        async def header_value(self, name): reads.append("header"); raise AssertionError("Private header must not be read")
    class Route:
        request = Request(); aborted = False
        async def abort(self): self.aborted = True
    route = Route(); await session.guarded(route)
    assert route.aborted and reads == []
    assert session.counts == {"document": 0, "price": 0, "summary": 0, "static": 0}
    if path == "/rest/default/V1/project/mine/addresses":
        assert session.failure == "source_address_dependency_unproved"
        assert session.address_reads == 0
    else:
        assert session.failure is None and session.blocked.get("background_shipping", 0) == 1


FIXTURE_URL = "https://4over.com/offline-quote-fixture"


@asynccontextmanager
async def intercepted_quote_page(label="CALCULATE PRICE", button_type="button"):
    """An isolated actual DOM. Every request is fulfilled locally or aborted."""
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file(): pytest.skip("Local Chromium is not installed")
        browser = await runtime.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block"); page = await context.new_page()
        body = urlencode(quote_form())
        document = ('<!doctype html><main class="column main"><form action="/checkout/cart/add" method="post">'
                    '<select name="super_attribute[111]"><option value="10">A</option><option value="11">Other A</option></select>'
                    '<select name="super_attribute[999]"><option value="20">B</option></select>'
                    '<input type="hidden" name="product" value="1234">'
                    '<input type="hidden" name="selected_configurable_option" value="700">'
                    '<input type="hidden" name="run_size" value="250">'
                    '<ul><li class="runsizes_row selected-qty"><span class="runsize">250</span></li></ul>'
                    '<input type="checkbox" name="job_sample"><input type="checkbox" name="digital_proof">'
                    '<div id="production" hidden><input id="regular" type="radio" name="turnaround_time" value="3" checked>'
                    '<label for="regular">3 Business Days</label></div>'
                    '<div>Subtotal: $10.01</div><div>Total: $11.26</div>'
                    '<button id="quote" type="' + escape(button_type, quote=True) + '">' + escape(label) + '</button>'
                    '</form></main><script>window.fixtureClicks=0;window.fixtureQuotes=0;'
                    'document.querySelector("form").addEventListener("submit",e=>e.preventDefault());'
                    'document.querySelector("#quote").addEventListener("click",async()=>{'
                    'window.fixtureClicks++;document.querySelector("#production").hidden=false;'
                    'const r=await fetch("/offline-quote-fixture/quote",{method:"POST",'
                    'headers:{"Content-Type":"application/x-www-form-urlencoded"},body:' + json.dumps(body) + '});'
                    'window.fixtureSummary=await r.json();window.fixtureQuotes++;});</script>')
        requests, external, quotes = [], [], []
        async def intercept(route):
            request = route.request; requests.append((request.method, request.url))
            if request.url == FIXTURE_URL and request.method == "GET":
                await route.fulfill(status=200, content_type="text/html", body=document)
            elif request.url == FIXTURE_URL + "/quote" and request.method == "POST":
                quotes.append(request.post_data)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"totals": {
                    "subtotal": "10.01", "grand_total": "11.26", "small_qty_fee": "1.25"}}))
            else:
                external.append((request.method, request.url)); await route.abort()
        await context.route("**/*", intercept)
        try:
            await page.goto(FIXTURE_URL)
            yield page, quotes, requests, external
        finally:
            await context.close(); await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("label,button_type,clicks", [
    ("CALCULATE", "button", 1), ("calculate price", "button", 1), ("PROCEED TO SHIPPING", "button", 1),
    ("Add to Cart", "button", 0), ("Calculate Price and Order", "button", 0),
    ("CALCULATE", "submit", 0), ("constructor", "button", 0), ("__proto__", "button", 0)])
async def test_atomic_quote_button_in_intercepted_chromium(driver, label, button_type, clicks):
    async with intercepted_quote_page(label, button_type) as (page, quotes, requests, external):
        before = await page.evaluate(driver.automatic_state_js(SELECTION["attributes"]))
        assert before["run_size"] == "250" and before["run_size_values"] == ["250"]
        assert before["production"]["visible"] is False
        result = await page.evaluate(driver.automatic_quote_button_js(SELECTION["attributes"]), {"source": FIXTURE_URL, "selection": SELECTION, "click": True})
        if clicks: await page.wait_for_function("window.fixtureQuotes === 1")
        assert await page.evaluate("window.fixtureClicks") == clicks
        assert result["status"] == ("clicked" if clicks else "unavailable")
        after = await page.evaluate(driver.automatic_state_js(SELECTION["attributes"]))
        assert after["production"]["visible"] is bool(clicks)
        assert len(quotes) == clicks and external == []
        assert requests == [("GET", FIXTURE_URL)] + ([("POST", FIXTURE_URL + "/quote")] if clicks else [])
        if clicks:
            projection = driver.automatic_request_context(quotes[0], after, selection=SELECTION)
            assert projection["run_size"] == 250 and PRIVATE not in json.dumps(projection)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["source", "quantity", "attribute", "service", "child", "duplicate_attribute", "formaction"])
async def test_atomic_quote_button_rechecks_retained_dom_in_intercepted_chromium(driver, drift):
    async with intercepted_quote_page() as (page, quotes, requests, external):
        expected_source = FIXTURE_URL
        mutations = {
            "source": "history.replaceState(null,'','/offline-quote-fixture-changed')",
            "quantity": "document.querySelector('input[name=run_size]').value='500';document.querySelector('li.selected-qty .runsize').textContent='500'",
            "attribute": "document.querySelector('select[name=\"super_attribute[111]\"]').value='11'",
            "service": "document.querySelector('input[name=job_sample]').checked=true",
            "child": "document.querySelector('input[name=selected_configurable_option]').value='701'",
            "duplicate_attribute": "document.querySelector('select').after(document.querySelector('select').cloneNode(true))",
            "formaction": "document.querySelector('#quote').setAttribute('formaction','/checkout/cart/add')",
        }
        await page.evaluate(mutations[drift])
        result = await page.evaluate(driver.automatic_quote_button_js(SELECTION["attributes"]), {
            "source": expected_source, "selection": SELECTION, "click": True})
        assert result["status"] == ("unavailable" if drift == "formaction" else "context_changed")
        assert await page.evaluate("window.fixtureClicks") == 0
        assert quotes == [] and requests == [("GET", FIXTURE_URL)] and external == []


@pytest.mark.asyncio
async def test_wait_quote_clicks_native_control_once_in_intercepted_chromium(driver, tmp_path):
    async with intercepted_quote_page() as (page, quotes, requests, external):
        session, _, _ = session_fixture(driver, tmp_path)
        session.page = page; session.source = FIXTURE_URL; session.target = SELECTION
        session.stage = "quantity"; session.selection = deepcopy(SELECTION)
        session.summary = None; session.summary_request_observed = False
        session.summary_seen = False; session.summary_pending = False; session.quote_failure = None
        native_state = session.state
        async def pricing_state():
            current = await native_state()
            # Independent calculator loading is covered by the full source-backed
            # browser fixture; retain this test's narrow button/once responsibility.
            current["source_controller"] = state()["source_controller"]
            current["source_ui"] = ui_ready_state()
            return current
        session.state = pricing_state
        async def focused_button_action(selection):
            # This fixture isolates button/once behavior. The complete native
            # model/step/TAT handoff is tested in test_fourover_native_proceed_transition.
            return await page.evaluate(driver.automatic_quote_button_js(selection["attributes"]),
                                       {"source": session.source, "selection": selection, "click": True})
        session.ordinary_quote_action = focused_button_action
        async def receive_fixture_quote():
            await page.wait_for_function("window.fixtureQuotes === 1")
            current = await session.state()
            session.summary = {"request_context": driver.automatic_request_context(quotes[0], current, selection=session.selection)}
        receiver = asyncio.create_task(receive_fixture_quote())
        try:
            current = await asyncio.wait_for(session.wait_quote(session.selection), 4)
            await receiver
            assert current["production"] == {"value": "3", "label": "3 Business Days", "visible": True}
            assert session.quote_button_clicked and await page.evaluate("window.fixtureClicks") == 1
            # Once the native response is retained, another wait never clicks again.
            await asyncio.wait_for(session.wait_quote(session.selection), 1)
            assert await page.evaluate("window.fixtureClicks") == 1 and len(quotes) == 1
            assert requests == [("GET", FIXTURE_URL), ("POST", FIXTURE_URL + "/quote")] and external == []
        finally:
            if not receiver.done(): receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
