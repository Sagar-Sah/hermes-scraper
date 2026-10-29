"""Independent repair rechecks with injected browser/fixture IO; zero network."""
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest

from crawler.companion_environments import digest
from crawler.companion_storefront_verifier import (
    CompanionStorefrontVerifier, TrustedStorefrontFixture, allowed_read_request,
    dimensions, expected_data, row_signature, sample_rows,
)


PRODUCTION = "https://printitprints.com"
STAGING = "https://staging3.tonyl36.sg-host.com"
PATH = "/shop/cards/"
DOC = "https://4over.com/media/asset/templates/card.pdf"
BEFORE = {"selection": {"size": "2 x 3.5", "shape": "Rectangle", "stock": "18pt Kraft",
    "colorspec": "4/4", "coating": "None", "quantity": "100"}, "cost": "20.00", "production": "4 Business Days"}


class Environments:
    def __init__(self, snapshot=None):
        self.snapshot_value = snapshot

    def status(self, environment):
        return {"base_url": STAGING if environment == "staging" else PRODUCTION, "connected": True}

    def _browser_options(self, environment):
        return {"http_credentials": {"username": "normal-preview-user", "password": "private-preview-password"}} if environment == "staging" else {}

    async def snapshot(self, environment, product_id):
        if callable(self.snapshot_value):
            return self.snapshot_value()
        return deepcopy(self.snapshot_value)


def setup(change=None, *, rollback=False):
    manifest = {"schema": "hermes-staging-repair-manifest/v1", "product_id": "9532",
        "production_origin": PRODUCTION, "staging_origin": STAGING,
        "before_data": {"rows": [deepcopy(BEFORE)], "documents": [], "headers": [], "columns": {}, "pricing_basis": "supplier_cost", "currency": "USD"},
        "before_retail_rule": {"fingerprint": "a" * 64}, "before_retail_script_sha256": "b" * 64,
        "before_artwork": {"status": "verified", "canonical_unit": "in"},
        "changes": [change or {"kind": "set_cost", "selection": deepcopy(BEFORE["selection"]), "cost": "30.00"}]}
    expected = expected_data(manifest, rollback=rollback)
    snapshot = {"product_id": "9532", "storefront_url": STAGING + PATH, "revision": "c" * 64,
        "calculator": {"family": "custom_csv"}, "retail_rule": {"fingerprint": "a" * 64}, **deepcopy(expected)}
    observed = {"status": "observed", "url": snapshot["storefront_url"], "product_id": "9532",
        "html_sha256": "d" * 64, "observed_at": time.time(), "rows": deepcopy(expected["rows"]),
        "document_links": [row["url"] for row in expected["documents"]],
        "connector_documents": deepcopy(expected["documents"]),
        "retail_rule": {"status": "verified", "script_sha256": "b" * 64, "member": True},
        "samples": [{"retained_selection": deepcopy(row["selection"]),
            "retained_production": row["production"],
            "displayed_retail": "60.00" if row["cost"] == "20.00" else "75.00",
            "artwork_dimensions": [{"value": row["selection"]["size"], "unit": "in"}]}
            for row in sample_rows(manifest, expected, rollback=rollback)]}
    calls = []

    async def browser(environment, product_id, supplied_manifest, supplied_snapshot, **kwargs):
        calls.append((environment, product_id, kwargs["rollback"]))
        assert kwargs["expected"] == expected
        return deepcopy(observed)

    return manifest, snapshot, observed, browser, calls


def fixture(kind, *, environment=("staging",), mode="staging_sandbox", mutate=None, calls=None):
    async def run(context):
        if calls is not None:
            calls.append(context)
        value = {"status": "match", "manifest_sha256": context["manifest_sha256"],
            "product_id": context["product_id"], "environment": context["environment"],
            "fixture_sha256": "e" * 64, "evidence_ids": [kind + "-observed-evidence"],
            "snapshot_revision": context["snapshot_revision"], "observed_at": time.time(),
            "orders_placed": 0, "payments_submitted": 0, "supplier_orders_placed": 0,
            "cart_actions": 0, "uploads": 0, "wordpress_writes": 0, "cart_restored": True}
        if mutate:
            mutate(value)
        return value
    return TrustedStorefrontFixture(kind + "-fixture", "e" * 64, run, environments=environment, mode=mode)


def verify(verifier, manifest, snapshot, **kwargs):
    return asyncio.run(verifier("staging", "9532", manifest, snapshot, **kwargs))


def test_scope_complete_browser_checks_do_not_invent_shipping_or_checkout():
    manifest, snapshot, _, browser, calls = setup()
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot)
    assert result["checks"] == {"calculator_data": "match", "rendered_prices": "match", "artwork_dimensions": "match",
        "shipping": "unknown", "customer_flow": "unknown", "documents": "match", "retail_rule_unchanged": "match"}
    assert result["status"] == "unknown" and not result["release_ready"]
    assert result["release_hold_checks"] == ["shipping", "customer_flow"]
    assert result["manifest_sha256"] == digest(manifest)
    assert calls == [("staging", "9532", False)]


def test_registered_bound_staging_checks_can_complete_the_declared_recheck():
    manifest, snapshot, _, browser, _ = setup()
    fixtures = {"9532": {kind: fixture(kind) for kind in ("shipping", "customer_flow")}}
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser, fixtures=fixtures), manifest, snapshot)
    assert result["status"] == "match" and result["release_ready"]
    assert result["release_hold_checks"] == []


@pytest.mark.parametrize("failure,check", [
    ("table", "calculator_data"), ("price", "rendered_prices"), ("retained", "rendered_prices"), ("production", "rendered_prices"),
    ("artwork", "artwork_dimensions"), ("retail_script", "retail_rule_unchanged"), ("server_rule", "retail_rule_unchanged"),
])
def test_actual_differences_are_mismatches(failure, check):
    manifest, snapshot, observed, browser, _ = setup()
    if failure == "table":
        observed["rows"][0]["cost"] = "31"
    elif failure == "price":
        observed["samples"][0]["displayed_retail"] = "90.00"
    elif failure == "retained":
        observed["samples"][0]["retained_selection"]["quantity"] = "200"
    elif failure == "production":
        observed["samples"][0]["retained_production"] = "2 Business Days"
    elif failure == "artwork":
        observed["samples"][0]["artwork_dimensions"][0]["value"] = "3.5 x 2"
    elif failure == "retail_script":
        observed["retail_rule"]["script_sha256"] = "f" * 64
    else:
        snapshot["retail_rule"]["fingerprint"] = "f" * 64
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot)
    assert result["checks"][check] == "mismatch" and result["status"] == "mismatch"
    assert not result["release_ready"]


@pytest.mark.parametrize("missing", ["script_baseline", "artwork_units", "artwork_bridge", "sample", "effective_rule"])
def test_missing_evidence_stays_unknown(missing):
    manifest, snapshot, observed, browser, _ = setup()
    check = "rendered_prices"
    if missing == "script_baseline":
        del manifest["before_retail_script_sha256"]
    elif missing == "artwork_units":
        manifest["before_artwork"] = {"status": "unknown"}
        check = "artwork_dimensions"
    elif missing == "artwork_bridge":
        observed["samples"][0]["artwork_dimensions"] = []
        check = "artwork_dimensions"
    elif missing == "sample":
        observed["samples"] = []
    else:
        observed["retail_rule"]["status"] = "unknown"
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot)
    assert result["checks"][check] == "unknown" and result["status"] != "match"


def test_document_addition_and_rollback_observe_the_expected_page_links():
    operation = {"kind": "add_document", "url": DOC, "label": "card.pdf"}
    manifest, snapshot, observed, browser, _ = setup(operation)
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot)
    assert result["checks"]["documents"] == "match"
    observed["document_links"] = []
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot)
    assert result["checks"]["documents"] == "mismatch"
    manifest, snapshot, _, browser, calls = setup(operation, rollback=True)
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot, rollback=True)
    assert result["checks"]["calculator_data"] == result["checks"]["documents"] == "match"
    assert calls == [("staging", "9532", True)]


def test_rollback_stale_visible_added_document_is_a_mismatch():
    operation = {"kind": "add_document", "url": DOC, "label": "card.pdf"}
    manifest, snapshot, observed, browser, _ = setup(operation, rollback=True)
    observed["document_links"] = [DOC]
    observed["connector_documents"] = [{"url": DOC, "label": "card.pdf"}]
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot, rollback=True)
    assert result["checks"]["documents"] == "mismatch" and not result["release_ready"]


def test_cached_browser_result_is_not_relabelled_fresh():
    manifest, snapshot, observed, browser, _ = setup()
    observed["observed_at"] = time.time() - 3600
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot)
    assert result["checks"]["rendered_prices"] == "unknown"
    assert result["reasons"]["verifier"] == "storefront_browser_observation_not_fresh"


@pytest.mark.parametrize("changed", ["revision", "rows", "documents", "retail_rule", "artwork"])
def test_closing_snapshot_detects_changes_during_the_independent_recheck(changed):
    manifest, snapshot, _, browser, _ = setup()
    latest = deepcopy(snapshot)
    if changed == "revision":
        latest["revision"] = "f" * 64
    elif changed == "rows":
        latest["rows"][0]["cost"] = "31.00"
    elif changed == "documents":
        latest["documents"] = [{"url": DOC, "label": "unexpected.pdf"}]
    elif changed == "retail_rule":
        latest["retail_rule"]["fingerprint"] = "f" * 64
    else:
        latest["artwork"] = {"canonical_unit": "ft"}
    result = verify(CompanionStorefrontVerifier(Environments(latest), browser_runner=browser), manifest, snapshot)
    assert result["checks"]["calculator_data"] == "mismatch"
    assert result["reasons"]["calculator_data"] == "storefront_snapshot_changed_during_recheck"
    assert result["release_ready"] is False


def test_a_late_connection_error_cannot_leave_scoped_checks_verified():
    manifest, snapshot, _, browser, _ = setup()
    def failed():
        raise RuntimeError("private-preview-password")
    fixtures = {"9532": {kind: fixture(kind) for kind in ("shipping", "customer_flow")}}
    result = verify(CompanionStorefrontVerifier(Environments(failed), browser_runner=browser, fixtures=fixtures), manifest, snapshot)
    assert result["status"] == "unknown" and result["checks"]["calculator_data"] == "unknown"
    assert result["release_ready"] is False and "private-preview-password" not in json.dumps(result)


def test_restored_cost_uses_before_data_not_the_repair_cost():
    manifest, snapshot, observed, browser, _ = setup(rollback=True)
    assert snapshot["rows"][0]["cost"] == "20.00" and observed["samples"][0]["displayed_retail"] == "60.00"
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser), manifest, snapshot, rollback=True)
    assert result["checks"]["rendered_prices"] == result["checks"]["artwork_dimensions"] == "match"


def test_production_cannot_run_a_staging_sandbox_fixture():
    manifest, snapshot, observed, browser, _ = setup()
    snapshot["storefront_url"] = observed["url"] = PRODUCTION + PATH
    calls = []
    verifier = CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser,
        fixtures={"9532": {"shipping": fixture("shipping", calls=calls), "customer_flow": fixture("customer_flow", calls=calls)}})
    result = asyncio.run(verifier("production", "9532", manifest, snapshot))
    assert result["checks"]["shipping"] == result["checks"]["customer_flow"] == "unknown"
    assert not calls and not result["release_ready"]


@pytest.mark.parametrize("mutation,expected", [
    (lambda value: value.update(manifest_sha256="f" * 64), "unknown"),
    (lambda value: value.update(product_id="6669"), "unknown"),
    (lambda value: value.update(fixture_sha256="f" * 64), "unknown"),
    (lambda value: value.update(evidence_ids=[]), "unknown"),
    (lambda value: value.update(snapshot_revision="f" * 64), "unknown"),
    (lambda value: value.update(observed_at=time.time() - 3600), "unknown"),
    (lambda value: value.update(orders_placed=1), "mismatch"),
    (lambda value: value.update(payments_submitted=1), "mismatch"),
    (lambda value: value.update(cart_restored=False), "unknown"),
])
def test_fixture_receipts_need_binding_evidence_and_no_order_or_payment(mutation, expected):
    manifest, snapshot, _, browser, _ = setup()
    verifier = CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser,
        fixtures={"9532": {"customer_flow": fixture("customer_flow", mutate=mutation)}})
    result = verify(verifier, manifest, snapshot)
    assert result["checks"]["customer_flow"] == expected and not result["release_ready"]


def test_read_only_production_fixture_cannot_claim_a_cart_mutation():
    manifest, snapshot, observed, browser, _ = setup()
    snapshot["storefront_url"] = observed["url"] = PRODUCTION + PATH
    verifier = CompanionStorefrontVerifier(Environments(snapshot), browser_runner=browser,
        fixtures={"9532": {"shipping": fixture("shipping", environment=("production",), mode="read_only", mutate=lambda value: value.update(cart_actions=1))}})
    result = asyncio.run(verifier("production", "9532", manifest, snapshot))
    assert result["checks"]["shipping"] == "mismatch"


@pytest.mark.parametrize("url", ["http://printitprints.com/shop/cards/", "https://4over.com/cards", PRODUCTION + "/cart/", PRODUCTION + PATH + "?add-to-cart=9532", PRODUCTION + "/wp-admin/admin-ajax.php", PRODUCTION + "/wp-json/wc/store/v1/cart", "https://user:secret@printitprints.com/shop/cards/"])
def test_readonly_guard_rejects_supplier_cart_admin_and_mutating_urls(url):
    assert not allowed_read_request("GET", url, PRODUCTION)


def test_readonly_guard_allows_only_normal_same_origin_reads():
    assert allowed_read_request("GET", PRODUCTION + PATH, PRODUCTION)
    assert allowed_read_request("HEAD", PRODUCTION + "/wp-content/plugins/test.js?ver=1", PRODUCTION)
    assert not allowed_read_request("POST", PRODUCTION + PATH, PRODUCTION)
    assert not allowed_read_request("PUT", STAGING + PATH, STAGING)
    assert not allowed_read_request("GET", STAGING + PATH, PRODUCTION)


def test_exact_dimensions_require_units_and_preserve_orientation():
    assert dimensions("2 x 3.5") is None
    assert dimensions("2 x 3.5", assumed_unit="in") == ["2", "3.5"]
    assert dimensions('2" x 3.5"') == ["2", "3.5"]
    assert dimensions("10 ft x 3 ft") == ["120", "36"]
    assert dimensions("2 ft x 8 in") == ["24", "8"]
    assert dimensions("10 x 3", assumed_unit="ft") == ["120", "36"]
    assert dimensions("A4") is None and dimensions("0 x 2", assumed_unit="in") is None


def test_expected_data_is_immutable_and_rejects_ambiguous_or_unbounded_changes():
    manifest, _, _, _, _ = setup()
    before = json.dumps(manifest, sort_keys=True)
    assert expected_data(manifest)["rows"][0]["cost"] == "30.00"
    assert json.dumps(manifest, sort_keys=True) == before
    duplicate = [deepcopy(BEFORE), deepcopy(BEFORE)]
    with pytest.raises(ValueError, match="duplicate_configuration"):
        row_signature(duplicate)
    manifest["changes"] = [{"kind": "remove_row", "selection": BEFORE["selection"]}]
    with pytest.raises(ValueError, match="unsupported"):
        expected_data(manifest)


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
    @property
    def first(self):
        return self
    async def is_visible(self):
        return True
    async def select_option(self, value, **_):
        self.page.selected.append((self.selector, value))


class Page:
    def __init__(self, html, observed):
        self.html, self.observed, self.selected = html, observed, []
        self.url = STAGING + PATH
    async def goto(self, url, **_):
        self.url = url
        return SimpleNamespace(status=200)
    async def content(self):
        return self.html
    async def evaluate(self, script):
        if "ids:Array.from" in script:
            return {"ids": ["product-9532"]}
        if ".hermes-artwork-documents" in script:
            return {"document_links": deepcopy(self.observed["document_links"]), "connector_documents": deepcopy(self.observed["connector_documents"])}
        return deepcopy(self.observed["samples"][0])
    async def wait_for_selector(self, *_, **__):
        pass
    async def wait_for_function(self, *_, **__):
        pass
    def locator(self, selector):
        return Locator(self, selector)


def test_actual_browser_lane_uses_normal_preview_auth_and_never_submits(monkeypatch):
    manifest, snapshot, observed, _, _ = setup()
    row = {"SIZE": "2 x 3.5", "SHAPE": "Rectangle", "STOCK": "18pt Kraft", "COLORSPEC": "4/4", "COATING": "None", "RunSize": "100", "Turnaround": "4 Business Days", "TotalPrice": "30.00"}
    html = '<body class="postid-9532"><div id="product-9532"></div><script>const current_product_id_is_this = 9532;</script><script>const csvdata = ' + json.dumps([row]) + ';</script></body>'
    page = Page(html, observed)
    browser_options, routed, closed = [], [], []
    class Context:
        async def route(self, pattern, callback):
            assert pattern == "**/*"
            routed.append(callback)
        async def new_page(self):
            return page
    class Browser:
        async def new_context(self, **kwargs):
            browser_options.append(kwargs)
            return Context()
        async def close(self):
            closed.append(True)
    class Chromium:
        async def launch(self, **kwargs):
            assert kwargs == {"headless": True}
            return Browser()
    class Runtime:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())
        async def __aexit__(self, *_):
            pass
    monkeypatch.setattr("crawler.companion_storefront_verifier._retail_rule", lambda *_: deepcopy(observed["retail_rule"]))
    verifier = CompanionStorefrontVerifier(Environments(snapshot), playwright_factory=Runtime)
    result = verify(verifier, manifest, snapshot)
    assert result["checks"]["calculator_data"] == result["checks"]["rendered_prices"] == result["checks"]["artwork_dimensions"] == "match"
    assert browser_options[0]["ignore_https_errors"] is False and browser_options[0]["service_workers"] == "block"
    assert browser_options[0]["http_credentials"]["password"] == "private-preview-password"
    assert "private-preview-password" not in json.dumps(result) and closed == [True]
    assert len(page.selected) == 7
    assert 'print_option_turnaround' in page.selected[-2][0] and 'print_option_quantity' in page.selected[-1][0]
    class Route:
        def __init__(self, method, url):
            self.request = SimpleNamespace(method=method, url=url)
            self.action = None
        async def continue_(self):
            self.action = "continued"
        async def abort(self):
            self.action = "blocked"
    async def check_guards():
        good, write, supplier = Route("GET", STAGING + PATH), Route("POST", STAGING + PATH), Route("GET", "https://4over.com/product")
        for item in (good, write, supplier):
            await routed[0](item)
        assert good.action == "continued" and write.action == supplier.action == "blocked"
    asyncio.run(check_guards())


def test_private_errors_do_not_reach_evidence():
    manifest, snapshot, _, _, _ = setup()
    async def bad(*_, **__):
        raise ValueError("private_preview_password")
    result = verify(CompanionStorefrontVerifier(Environments(snapshot), browser_runner=bad), manifest, snapshot)
    assert "private_preview_password" not in json.dumps(result)
    assert result["status"] == "unknown"
