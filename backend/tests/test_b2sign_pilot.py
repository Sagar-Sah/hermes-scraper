"""Pure source binding and intercepted Chromium; no supplier requests."""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from crawler.b2sign_pilot import (
    B2SignPilotManager, PilotError, bind_diagnosis, contract_projection,
    expected_turnaround, load_contract, quote_schema, sanitize_price_breaks,
    sanitize_quote, sanitize_request, safe_dom_diagnostics,
)
from test_b2sign_diagnosis import B2_HTML


def diagnosis(contract=None):
    contract = contract or load_contract()
    fields = []
    for attr in contract["attrs"]["attrs"]:
        key = attr["key"]
        if key in ("hem", "pole_width"):
            continue
        if key == "_size":
            fields.append({"label": "Size", "source_id": "", "selected_values": ['18" x 24"']})
        else:
            option = next(item for item in attr["options"] if item.get("default"))
            fields.append({"label": attr["label"], "source_id": "attr-" + key, "selected_values": [option["label"]]})
    fields.extend([{"label": "Turnaround", "selected_values": ["v1"]},
                   {"label": "Shipping", "selected_values": []},
                   {"label": "Email Proof", "selected_values": []}])
    for index, field in enumerate(fields):
        field.update(key=str(index), configurable=True)
    policy = [dict(field, include=field["label"] != "Email Proof", fixed_values=[]) for field in fields]
    return {"id": "d" * 32, "revision": 1, "supplier_id": "b2sign", "url": contract["source_url"],
            "status": "closed", "fields": fields,
            "selection": {"revision": 1, "source_url": contract["source_url"], "fields": policy}}


def payload(quantity=1, contract=None):
    contract = contract or load_contract()
    values = bind_diagnosis(diagnosis(contract), contract)
    return {"id": 31, "values": dict(values, _qty=str(quantity)), "jobs": json.dumps([{
        "subjobs": [{"name": "", "quantity": str(quantity)}], "shipping": "", "shipping_service": "03",
        "shipping_service_label": "Ground", "turnaround": expected_turnaround(quantity, contract)["value"]}])}


def response(quantity=1):
    return {"props": {"customer": {"email": "PRIVATE_EMAIL"}, "debug": "PRIVATE_DEBUG",
            "quoteResult": {"quote": [{"unit_price": 56.26, "subtotal": round(56.26 * quantity, 2),
                "price": [round(56.26 * quantity, 2)], "qty": [quantity],
                "shipping": [{"address": "PRIVATE_ADDRESS", "price": 5}], "tax": 11}],
                "price_break": [{"qty": [1, 9], "price": 56.26, "private": "PRIVATE_TIER"},
                                {"qty": 10, "price": 50, "full_sheet_price": False}]}}}


def test_contract_binds_labels_to_source_keys_and_proven_production_boundaries():
    contract = load_contract()
    values = bind_diagnosis(diagnosis(), contract)
    assert values == {"hardware": "p", "sides": "2", "material": "18oz_blockout_vinyl_banner",
                      "polepocket": "2/TB", "grommet": "C", "windslit": "0", "_size": "18x24", "pole_width": "18", "hem": "0"}
    assert [expected_turnaround(q, contract)["value"] for q in [1, 10, 11, 100, 101, 200, 201]] == ["v1", "v1", "v1", "v1", "v3", "v3", "v5"]
    assert "price" not in contract and "customer" not in contract


@pytest.mark.parametrize("change,reason", [
    (lambda d: d["selection"].update(revision=2), "saved_selection_required"),
    (lambda d: d["fields"][-1].update(selected_values=[""]), "unchecked_email_proof_required"),
    (lambda d: d["fields"][-2].update(selected_values=["dropship"]), "shipping_must_remain_unselected"),
    (lambda d: d["selection"]["fields"][0].update(include=False), "additional_hidden_controls_unsupported"),
    (lambda d: d["fields"][0].update(selected_values=["Unknown"]), "source_option_binding_ambiguous"),
])
def test_saved_policy_does_not_silently_override_current_baseline(change, reason):
    row = diagnosis()
    change(row)
    with pytest.raises(PilotError, match=reason):
        bind_diagnosis(row, load_contract())


@pytest.mark.parametrize("mutation,reason", [
    (lambda p: p["values"].update(windslit="1"), "quote_values_changed"),
    (lambda p: p["values"].update(unknown="PRIVATE"), "unexpected_quote_values"),
    (lambda p: p.update(id=32), "unexpected_quote_request"),
    (lambda p: p["jobs"][0].update(shipping="dropship"), "shipping_or_address_context_present"),
    (lambda p: p["jobs"][0].update(shipping_to_id=123), "shipping_or_address_context_present"),
    (lambda p: p["jobs"][0].update(turnaround="v0"), "turnaround_changed"),
    (lambda p: p["jobs"][0]["subjobs"][0].update(name="PRIVATE_NAME"), "quantity_or_job_context_changed"),
    (lambda p: p["jobs"][0]["subjobs"][0].update(quantity="10"), "quantity_or_job_context_changed"),
])
def test_request_guard_rejects_unapproved_context_before_transmission(mutation, reason):
    contract = load_contract()
    values = bind_diagnosis(diagnosis(), contract)
    p = payload()
    p["jobs"] = json.loads(p["jobs"])
    mutation(p)
    with pytest.raises(PilotError, match=reason):
        sanitize_request(p, contract, 1, values)


def test_transient_request_counted_but_not_bound_and_unknown_response_is_safe():
    contract = load_contract()
    p = payload()
    p["jobs"] = json.loads(p["jobs"])
    p["jobs"][0].pop("turnaround")
    safe = sanitize_request(p, contract, 1, bind_diagnosis(diagnosis(), contract))
    assert not safe["configuration_bound"]
    out = sanitize_quote(response(), 1)
    assert out["price_break_tiers"] == [{"qty": [1, 9], "price": "56.26"}, {"qty": 10, "price": "50", "full_sheet_price": False}]
    assert "PRIVATE" not in json.dumps(out)
    assert sanitize_price_breaks([{"qty": "PRIVATE", "price": 1}])["price_break_status"] == "unresolved_schema"
    assert sanitize_price_breaks(None)["price_break_status"] == "not_exposed_in_response"
    assert "PRIVATE" not in json.dumps(quote_schema({"props": {"quoteResult": {"PRIVATE_KEY": "PRIVATE"}}}))


@pytest.mark.parametrize("money", ["-1", "(1.00)", float("nan"), True, {"private": "x"}])
def test_response_money_fails_closed(money):
    r = response()
    r["props"]["quoteResult"]["quote"][0]["subtotal"] = money
    with pytest.raises(PilotError, match="invalid_quote_money"):
        sanitize_quote(r, 1)


@pytest.mark.parametrize("value", [1, "1", [1], ["1"]])
def test_live_single_subjob_quantity_shape(value):
    r = response()
    r["props"]["quoteResult"]["quote"][0]["qty"] = value
    assert sanitize_quote(r, 1)["quantity"] == 1


@pytest.mark.parametrize("value", [[], [1, 1], [10], ["10"], [[1]], True, [True], 1.0])
def test_response_quantity_rejects_other_or_ambiguous_jobs(value):
    r = response()
    r["props"]["quoteResult"]["quote"][0]["qty"] = value
    with pytest.raises(PilotError, match="response_quantity_mismatch"):
        sanitize_quote(r, 1)


def test_dom_diagnostics_keep_only_scoped_amount_text_and_counts():
    assert safe_dom_diagnostics({"quantity": "1", "subtotal": "$56.26", "quantity_count": 1, "subtotal_count": 1}, 1) == {
        "requested_quantity": 1, "quantity": "1", "quantity_count": 1, "subtotal_count": 1,
        "subtotal_numeric_currency_text": "$56.26", "subtotal_text_status": "amount_only"}
    unknown = safe_dom_diagnostics({"quantity": "PRIVATE_JOB", "subtotal": "PRIVATE_USER $56.26"}, 1)
    assert unknown["subtotal_text_status"] == "unsupported_text"
    assert "PRIVATE" not in json.dumps(unknown)
    assert safe_dom_diagnostics({}, 1)["subtotal_text_status"] == "missing"


class PilotPage:
    def __init__(self, real, pool):
        self.real, self.pool = real, pool

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def route(self, pattern, handler):
        async def intercepted(route):
            async def local_continue():
                request = route.request
                self.pool.requests.append((request.method, request.url))
                path = urlsplit(request.url).path
                if path == "/pole-banner-set":
                    await route.fulfill(status=200, content_type="text/html", body=self.pool.html())
                elif path == "/build/assets/app-fixture.js":
                    await route.fulfill(status=200, content_type="text/javascript", body=self.pool.bundle)
                elif path == "/item/quote":
                    p = request.post_data_json
                    self.pool.posted.append(p)
                    q = int(json.loads(p["jobs"])[0]["subjobs"][0]["quantity"])
                    body = response(q)
                    if self.pool.bad_quantity:
                        body["props"]["quoteResult"]["quote"][0]["qty"] = [q + 1]
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
                else:
                    await route.fulfill(status=404, body="")
            await handler(SimpleNamespace(request=route.request, abort=route.abort, continue_=local_continue))
        await self.real.route(pattern, intercepted)


class PilotPool:
    def __init__(self, context, contract, *, mismatch=False):
        self.context, self.contract, self.mismatch = context, contract, mismatch
        self.requests, self.posted, self.pages = [], [], []
        self.bad_quantity = False
        self.remove_bootstrap = False
        self.partial_bootstrap = False
        self.conflicting_bootstrap = False
        self.always_busy = False
        self.calls = []
        self.bundle = "/* public source fixture */"

    @asynccontextmanager
    async def page(self, origin, *, headless=True):
        self.calls.append((origin, headless))
        page = await self.context.new_page()
        self.pages.append(page)
        try:
            yield PilotPage(page, self)
        finally:
            if not page.is_closed():
                await page.close()

    def html(self):
        # Source-equivalent URL initializer and quote readback, deliberately
        # includes private sibling props which must never reach evidence.
        item = {"id": 31, "url": "/pole-banner-set", "title": "Pole Banner Set", "attrs": self.contract["attrs"]}
        html = B2_HTML.replace('value="1" checked', 'value="v1" checked').replace('value="0"><span', 'value="v0"><span')
        html = html.replace('</body>', '<div id="app"></div><script src="/build/assets/app-fixture.js"></script><script>' + r'''
document.querySelector('#app').setAttribute('data-page',btoa(encodeURIComponent(JSON.stringify({props:{item:ITEM,customer:{secret:'PRIVATE_ACCOUNT'}}}))));
const selected=Object.fromEntries(new URLSearchParams(location.search));
const q=Number(selected._qty), turn=q>100?'v3':'v1';
const quantity=document.querySelector('div[class*="_quantity_"] input');quantity.value=String(q);
const radio=document.querySelector('input[name=":r8:"]:checked');radio.value=turn;
radio.closest('label').querySelector('span[class*="_option_"]').textContent=q>100?'3 Business Days':'Next Day';
const jobs=[{subjobs:[{name:'',quantity:String(q)}],shipping:'',shipping_service:'03',shipping_service_label:'Ground',turnaround:turn}];
fetch('/item/quote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:31,values:selected,jobs:JSON.stringify(jobs)})}).then(r=>r.json()).then(p=>{
 document.querySelector('div[class*="_subtotal_"]').textContent='$'+(p.props.quoteResult.quote[0].subtotal+MISMATCH).toFixed(2);
});
fetch('/cart/add',{method:'POST',body:'PRIVATE_CART'}).catch(()=>{});
fetch('/account/addresses').catch(()=>{});
fetch('https://third-party.invalid/collect').catch(()=>{});
EXTRA_SCRIPT
''' .replace('ITEM', json.dumps(item)).replace('MISMATCH', '3' if self.mismatch else '0').replace('EXTRA_SCRIPT',
            ('setTimeout(()=>document.querySelector("#app").removeAttribute("data-page"),0);' if self.remove_bootstrap else '')
            + ('setTimeout(()=>document.querySelector("#app").dataset.page=JSON.stringify({props:{quoteResult:{quote:[]}}}),0);' if self.partial_bootstrap else '')
            + ('setTimeout(()=>{const e=document.querySelector("#app");const data=JSON.parse(decodeURIComponent(atob(e.dataset.page)));data.props.item.attrs.designproof=false;e.dataset.page=btoa(encodeURIComponent(JSON.stringify(data)));},100);' if self.conflicting_bootstrap else '')
            + ('document.querySelector("div[class*=\\"_instant-quote_\\"]").insertAdjacentHTML("beforeend", "<div class=MuiCircularProgress-root style=width:12px;height:12px>Loading</div>");' if self.always_busy else '')) + '</script></body>')
        return html


@pytest_asyncio.fixture
async def pilot_setup(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Local Chromium unavailable")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        contract = load_contract()
        contract["bundle_url"] = "https://www.b2sign.com/build/assets/app-fixture.js"
        contract["bundle_sha256"] = hashlib.sha256(b"/* public source fixture */").hexdigest()
        directory = tmp_path / "contract"
        directory.mkdir()
        (directory / "b2sign-pole-banner.json").write_text(json.dumps(contract))
        monkeypatch.setattr("crawler.b2sign_pilot.validate_url", AsyncMock())
        pool = PilotPool(context, contract)
        manager = B2SignPilotManager(SimpleNamespace(root=tmp_path), pool, directory, timeout_seconds=4)
        try:
            yield manager, pool, contract
        finally:
            await manager.close()
            await browser.close()


@pytest.mark.asyncio
async def test_real_chromium_four_fresh_bound_quotes_tiers_and_private_persistence(pilot_setup):
    manager, pool, contract = pilot_setup
    # Represents the user's diagnosis tab; pilot must not change or close it.
    owned = await pool.context.new_page()
    await owned.set_content('<h1>User diagnosis unchanged</h1>')
    opened = await manager.start(diagnosis(contract), [1, 10, 100, 101])
    assert opened["status"] == "opening"
    await manager._tasks[opened["id"]]
    result = manager.get(opened["id"])
    assert result["status"] == "completed", result
    assert [item["quantity"] for item in result["observations"]] == [1, 10, 100, 101]
    assert [item["dom"]["turnaround"]["value"] for item in result["observations"]] == ["v1", "v1", "v1", "v3"]
    assert result["quote_requests"] == 4 and len(pool.posted) == 4
    assert result["shipping"]["status"] == "held" and result["final_eligible"] is False
    assert result["catalog_complete"] is False
    assert all(item["quote"]["price_break_status"] == "source_numeric_tiers" for item in result["observations"])
    assert not owned.is_closed() and await owned.locator('h1').inner_text() == 'User diagnosis unchanged'
    assert all(page.is_closed() for page in pool.pages)
    assert pool.calls == [("https://www.b2sign.com", False)] * 4
    assert all('/cart/' not in url and '/account/' not in url and 'third-party' not in url for _, url in pool.requests)
    saved = manager.root / (opened["id"] + '.json')
    assert saved.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE" not in saved.read_text()
    assert manager.list()[0]["id"] == opened["id"]
    assert result["sample_comparisons"][-1]["production_changed"] is True
    assert result["sample_comparisons"][-1]["exact_price_break"] == "not_established"
    assert result["last_readiness_diagnostic"]["outstanding_quote_requests"] == 0
    assert result["last_readiness_diagnostic"]["pending_response_tasks"] == 0
    assert result["last_readiness_diagnostic"]["waiting_for"] is None


@pytest.mark.asyncio
async def test_chromium_mismatch_preserved_without_continuing_other_quantities(pilot_setup):
    manager, pool, contract = pilot_setup
    pool.mismatch = True
    opened = await manager.start(diagnosis(contract), [1, 10])
    await manager._tasks[opened["id"]]
    result = manager.get(opened["id"])
    assert result["status"] == "attention"
    assert result["warnings"] == ["quote_dom_math_mismatch"]
    assert result["observations"][0]["status"] == "mismatch"
    assert len(pool.posted) == 1 and all(page.is_closed() for page in pool.pages)


@pytest.mark.asyncio
async def test_chromium_specific_response_rejection_survives_deadline(pilot_setup):
    manager, pool, contract = pilot_setup
    pool.bad_quantity = True
    manager.timeout_seconds = 1
    opened = await manager.start(diagnosis(contract), [1])
    await manager._tasks[opened["id"]]
    result = manager.get(opened["id"])
    assert result["status"] == "attention"
    assert result["warnings"] == ["response_quantity_mismatch"]
    assert result["observations"] == []
    assert result["quotes"][0]["schema"]["row_fields"]["qty"] == "list"
    assert result["quotes"][0]["reason"] == "response_quantity_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["remove_bootstrap", "partial_bootstrap"])
async def test_chromium_public_contract_latched_before_hydration_changes_attribute(pilot_setup, mode):
    manager, pool, contract = pilot_setup
    setattr(pool, mode, True)
    opened = await manager.start(diagnosis(contract), [1])
    await manager._tasks[opened["id"]]
    result = manager.get(opened["id"])
    assert result["status"] == "completed", result
    assert result["last_readiness_diagnostic"]["item_current_present"] is False
    assert result["last_readiness_diagnostic"]["item_bootstrap_captured"] is True
    assert result["last_readiness_diagnostic"]["item_contract_validated"] is True
    assert result["last_readiness_diagnostic"]["outstanding_quote_requests"] == 0
    assert result["last_readiness_diagnostic"]["waiting_for"] is None


@pytest.mark.asyncio
async def test_chromium_latched_public_contract_does_not_hide_later_conflict(pilot_setup):
    manager, pool, contract = pilot_setup
    pool.conflicting_bootstrap = True
    opened = await manager.start(diagnosis(contract), [1])
    await manager._tasks[opened["id"]]
    result = manager.get(opened["id"])
    assert result["status"] == "attention"
    assert result["warnings"] == ["live_product_contract_changed"]
    assert result["observations"] == []


@pytest.mark.asyncio
async def test_chromium_busy_gate_reports_current_reason_with_valid_price(pilot_setup):
    manager, pool, contract = pilot_setup
    pool.always_busy = True
    manager.timeout_seconds = 1
    opened = await manager.start(diagnosis(contract), [1])
    await manager._tasks[opened["id"]]
    result = manager.get(opened["id"])
    assert result["status"] == "attention", result
    assert result["warnings"] == ["source_quote_panel_busy"]
    assert result["last_readiness_diagnostic"]["busy"] is True
    assert result["last_readiness_diagnostic"]["dom_bound"] is True
    assert result["last_readiness_diagnostic"]["latest_bound_quote_accepted"] is True
    assert result["last_readiness_diagnostic"]["outstanding_quote_requests"] == 0


@pytest.mark.asyncio
async def test_pilot_budget_validation_restart_state_and_cancellation(pilot_setup):
    manager, pool, contract = pilot_setup
    for values in [[1, 1], [2], [True], [1000], [[1]]]:
        with pytest.raises(PilotError, match="pilot_quantities_outside_approved_scope"):
            await manager.start(diagnosis(contract), values)
    opened = await manager.start(diagnosis(contract), [1, 10])
    with pytest.raises(PilotError, match="pilot_already_running"):
        await manager.start(diagnosis(contract), [1])
    await asyncio.sleep(0.1)
    await manager.close()
    assert manager.get(opened["id"])["status"] == "closed"
    assert all(page.is_closed() for page in pool.pages)
    stale = copy.deepcopy(opened)
    manager._write(stale)
    assert manager.get(opened["id"])["warnings"] == ["pilot_interrupted"]
    with pytest.raises(KeyError):
        manager.get('../elsewhere')
