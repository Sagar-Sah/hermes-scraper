"""Fake-browser sampler checks; no live network, supplier, cart or WP calls."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.product_companion_area_render import (
    OBSERVE_CASE_JS, STOREFRONT_URL, _display_money, _request_allowed,
    default_cases, sample_coroplast_public_storefront,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pigf_area_6383"
SCRIPT = (FIXTURES / "configurator-loaded.js").read_bytes()
CONFIG = (FIXTURES / "config-inline.js").read_text()
SCRIPT_URL = "https://printitprints.com/wp-content/uploads/siteground-optimizer-assets/print-it-grand-format-configurator.min.js?ver=0.2.11"
PAIR = {"product_id": "6383", "storefront_url": STOREFRONT_URL}


def case(identifier="six", **overrides):
    return {"case_id": identifier, "expected": "price", "selection": {
        "product_id": "6383", "width": 24, "height": 18, "colorspec": "4/0",
        "quantity": 6, "flute_direction": "vertical", "stakes_per_sign": 0, **overrides}}


class Route:
    def __init__(self, method, url):
        self.request = SimpleNamespace(method=method, url=url)
        self.continued = self.aborted = False
    async def continue_(self): self.continued = True
    async def abort(self): self.aborted = True


class Locator:
    def __init__(self, page, selector): self.page, self.selector = page, selector
    @property
    def first(self): return self
    async def select_option(self, value, **kwargs): self.page.operations.append(("select", self.selector, value))
    async def fill(self, value, **kwargs): self.page.operations.append(("fill", self.selector, value))
    async def dispatch_event(self, name): self.page.operations.append(("event", self.selector, name))
    async def get_attribute(self, name):
        assert name == "src"
        return SCRIPT_URL
    async def text_content(self): return CONFIG


class Response:
    status = 200
    url = SCRIPT_URL
    request = SimpleNamespace(resource_type="script")
    def __init__(self, body=SCRIPT): self.raw = body
    async def body(self): return self.raw


class Page:
    def __init__(self, browser):
        self.browser, self.url, self.operations, self.navigations = browser, STOREFRONT_URL, [], 0
        self.response_handler = None
    def on(self, name, callback):
        assert name == "response"
        self.response_handler = callback
    async def goto(self, url, **kwargs):
        self.navigations += 1
        if self.browser.failure: raise RuntimeError("PRIVATE provider/account error must not escape")
        for route in self.browser.routes:
            await self.browser.route_handler(route)
        self.response_handler(Response(self.browser.script))
        return SimpleNamespace(status=200)
    async def wait_for_selector(self, selector, **kwargs): pass
    async def wait_for_function(self, script, *, arg, **kwargs): self.last = arg
    def locator(self, selector): return Locator(self, selector)
    async def evaluate(self, script):
        if script == OBSERVE_CASE_JS:
            return {"total_text": self.browser.price, "total_visible": True,
                    "public_price_text": self.browser.price, "public_price_visible": True,
                    "button_disabled": True,  # Artwork has not been uploaded; unrelated to price.
                    "warning_visible": self.browser.orientation_warning,
                    "warning_text": "H-stakes require landscape vertical flutes" if self.browser.orientation_warning else "",
                    "retained": {"width":str(self.last["width"]),"height":str(self.last["height"]),
                        "colorspec":self.last["colorspec"],"quantity":str(self.last["run_size"]),
                        "flute_direction":self.last["flute"],"stakes_per_sign":str(self.last["stakes_per_sign"])}}
        return {"schema":"hermes-companion-field-inventory/v1","observation_kind":"rendered_dom",
                "url":STOREFRONT_URL,"product_id":"6383","observed_at":"2026-10-29T00:00:00Z",
                "truncated":False,"controls":[],"headings":[],"viewport":{"width":1280,"height":720}}


class Browser:
    def __init__(self, *, price="$67.50", failure=False, script=SCRIPT, orientation_warning=False):
        self.price, self.failure, self.script, self.orientation_warning = price, failure, script, orientation_warning
        self.closed, self.contexts, self.route_handler = False, [], None
        self.routes = [Route("GET", STOREFRONT_URL), Route("POST", "https://printitprints.com/?add-to-cart=6383"),
                       Route("GET", "https://4over.com/4coro-coroplast-signs")]
        self.page = Page(self)
    async def new_context(self, **kwargs):
        self.contexts.append(kwargs)
        return self
    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.route_handler = handler
    async def new_page(self): return self.page
    async def close(self): self.closed = True


class Factory:
    def __init__(self, browser): self.browser, self.launches, self.entries = browser, [], 0
    def __call__(self): return self
    async def __aenter__(self):
        self.entries += 1
        return SimpleNamespace(chromium=SimpleNamespace(launch=self.launch))
    async def __aexit__(self, *args): pass
    async def launch(self, **kwargs):
        self.launches.append(kwargs)
        return self.browser


@pytest.mark.parametrize("method,url", [
    ("POST", STOREFRONT_URL), ("PUT", STOREFRONT_URL),
    ("GET", "https://4over.com/4coro-coroplast-signs"),
    ("GET", "https://printitprints.com/?add-to-cart=6383"),
    ("GET", "https://printitprints.com/?wc-ajax=add_to_cart"),
    ("GET", "https://printitprints.com/wp-admin/"),
    ("GET", "https://printitprints.com/checkout/"),
    ("GET", "https://printitprints.com/wp-json/wc/store/v1/cart"),
    ("GET", "https://user:pass@printitprints.com/"),
    ("GET", "http://printitprints.com/"),
])
def test_mutation_supplier_auth_and_checkout_routes_are_denied(method, url):
    assert not _request_allowed(method, url)
    assert _request_allowed("GET", STOREFRONT_URL)
    assert _request_allowed("GET", SCRIPT_URL)


@pytest.mark.asyncio
async def test_one_anonymous_page_native_controls_and_scoped_price_match_without_purchase_claim():
    browser, factory = Browser(), None
    factory = Factory(browser)
    result = await sample_coroplast_public_storefront(PAIR, cases=[case()], playwright_factory=factory)
    assert result["status"] == "match"
    assert result["samples"][0]["displayed_retail"] == "67.50"
    assert result["samples"][0]["observed"]["button_disabled"] is True
    assert result["backend_status"] == result["overall_product_status"] == result["current_supplier_prices"] == "unknown"
    assert result["samples"][0]["backend_acceptance"] == "unknown"
    assert result["coverage"]["status"] == "unknown"
    assert browser.page.navigations == 1 and browser.closed
    assert factory.launches == [{"headless":True}]
    assert browser.contexts == [{"service_workers":"block","ignore_https_errors":False}]
    assert all(op[0] in {"select","fill","event"} for op in browser.page.operations)
    assert not any("RunSize" in op[1] or "add_to_cart" in op[1] for op in browser.page.operations)
    assert browser.routes[0].continued
    assert all(route.aborted for route in browser.routes[1:])
    assert result["observations"]["blocked_requests"] == {"requests":2,"mutating_requests":1,"external_origin_requests":1}


@pytest.mark.asyncio
async def test_actual_wrong_price_is_mismatch_not_timeout_or_false_match():
    browser = Browser(price="$0.01")
    result = await sample_coroplast_public_storefront(PAIR, cases=[case()], playwright_factory=Factory(browser))
    assert result["status"] == "mismatch"
    assert result["samples"][0]["expected_retail"] == "67.50"
    assert result["backend_status"] == "unknown"


@pytest.mark.asyncio
async def test_changed_loaded_script_stops_before_touching_controls():
    browser = Browser(script=SCRIPT + b"\n")
    result = await sample_coroplast_public_storefront(PAIR, cases=[case()], playwright_factory=Factory(browser))
    assert result["status"] == "unknown"
    assert result["reason"] == "area_public_rule_not_verified"
    assert not browser.page.operations and browser.closed


@pytest.mark.asyncio
async def test_invalid_orientation_requires_both_disabled_button_and_visible_warning():
    blocked = case("portrait", width=18, height=24, stakes_per_sign=1)
    blocked["expected"] = "blocked_orientation"
    for warning, expected in [(True,"match"),(False,"mismatch")]:
        result = await sample_coroplast_public_storefront(PAIR, cases=[blocked],
            playwright_factory=Factory(Browser(orientation_warning=warning)))
        assert result["status"] == expected


@pytest.mark.asyncio
async def test_invalid_pair_or_large_plan_makes_no_browser_or_network_call():
    factory = Factory(Browser())
    assert (await sample_coroplast_public_storefront({**PAIR,"product_id":"6669"},playwright_factory=factory))["status"] == "unknown"
    assert (await sample_coroplast_public_storefront(PAIR,cases=[case()]*17,playwright_factory=factory))["status"] == "unknown"
    assert factory.entries == 0


@pytest.mark.asyncio
async def test_browser_failure_closes_and_never_exposes_error_bodies():
    browser = Browser(failure=True)
    result = await sample_coroplast_public_storefront(PAIR,cases=[case()],playwright_factory=Factory(browser))
    assert result["status"] == "unknown" and browser.closed
    assert "PRIVATE" not in json.dumps(result)


def test_declared_small_plan_covers_breaks_sides_stakes_and_height_conflict():
    cases = default_cases()
    assert len(cases) == 16 and len({c["case_id"] for c in cases}) == 16
    assert {1,5,6,25,26,50,51,75,76,499} <= {c["selection"]["quantity"] for c in cases}
    assert {36,46} <= {c["selection"]["height"] for c in cases}
    assert {0,1,2} <= {c["selection"]["stakes_per_sign"] for c in cases}
    assert len([c for c in cases if c["expected"] == "blocked_orientation"]) == 2
    assert _display_money("$1,234.50") == "1234.50"
    assert _display_money("$01,234.50") is None
