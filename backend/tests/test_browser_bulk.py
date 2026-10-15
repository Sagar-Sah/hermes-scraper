"""Real local Chromium integration; no live suppliers or API replay.

Price/catalog and lookup bodies come from the public saved fixture. The page
and request-start provenance are deliberately synthetic local test scaffolding.
"""

import asyncio
import html as html_tools
import json
import re
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, urlunsplit
from unittest.mock import AsyncMock

import pytest

from crawler.adapters.browser import BrowserAdapter
from crawler.adapters import browser_tables
from test_browser_adapter import FixtureTransport


@pytest.fixture
async def chromium_runtime():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium runtime is not installed")


def fixture_html(
    *,
    initial_quantity=50,
    mismatch=False,
    disappearing_rush=False,
    lazy_quantity=False,
    source_branch=None,
    reset_plateau_boundary=False,
    missing_finishes=(),
    finish_appears=False,
    evict_initial_tables=False,
    early_specifications=False,
    service_state=None,
):
    if source_branch == "1_10_matte":
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures" / "printograph_standard_tables.json"
            ).read_text()
        )["branches"][source_branch]
        html = fixture_html(mismatch=mismatch).decode()
        controls = "".join(
            f'<label for="{control["key"]}_fixture">{html_tools.escape(control["label"])}</label><select id="{control["key"]}_fixture"><option value="{control["value"]}">{html_tools.escape(control["selected_label"])}</option></select>'
            for control in fixture["dom_quote"]["controls"]
            if control["key"] not in {"quantity", "orientation", "productionTime"}
        )
        html = re.sub(
            r'(<div id="order-form"[^>]+>).*?(<label for="quantity_fixture">)',
            lambda match: match[1] + controls + match[2],
            html,
            count=1,
            flags=re.S,
        )
        amounts = {
            row["quantity"]: row["markupPrice"]
            for row in fixture["captures"]["base_prices"]["data"]["items"]
            if row["quantity"] in (50, 100, 250)
        }
        html = html.replace(
            "let amounts={50:'4.69',100:'8.61',250:'9.45'}",
            "let amounts=" + json.dumps(amounts),
        )
        html = html.replace(
            "productType=1&size=101&orientation=1&paper=1&color=2",
            "&".join(
                f"{key}={value}" for key, value in fixture["base_selection"].items()
            ),
        )
        html = html.replace("/products/100000021/", "/products/100000009/")
        html = html.replace(
            "?'99.99':amounts[q]", "?(Number(amounts[q])+3).toFixed(2):amounts[q]"
        )
        return html.encode()
    if source_branch in {"36_1", "36_square"}:
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures" / "printograph_standard_tables.json"
            ).read_text()
        )["branches"][source_branch]
        html = fixture_html(initial_quantity=100, disappearing_rush=True).decode()
        selection = fixture["base_selection"]
        controls = "".join(
            f'<label for="{key}_fixture">{key}</label><select id="{key}_fixture"><option value="{value}">{value}</option></select>'
            for key, value in (
                *(
                    (key, selection[key])
                    for key in ("shape", "size", "paper", "finish", "color")
                ),
                ("windPosition", "32"),
            )
        )
        html = re.sub(
            r'(<div id="order-form"[^>]+>).*?(<label for="quantity_fixture">)',
            lambda match: match[1] + controls + match[2],
            html,
            count=1,
            flags=re.S,
        )
        html = re.sub(
            r'(<select id="quantity_fixture">).*?(</select>)',
            lambda match: match[1]
            + '<option value="100" selected>100</option><option value="250">250</option><option value="500">500</option>'
            + match[2],
            html,
            count=1,
            flags=re.S,
        )
        amounts = {
            row["quantity"]: row["markupPrice"]
            for row in fixture["captures"]["base_prices"]["data"]["items"]
            if row["quantity"] in (100, 250, 500)
        }
        html = html.replace(
            "let amounts={50:'4.69',100:'8.61',250:'9.45'}",
            "let amounts=" + json.dumps(amounts),
        )
        html = html.replace(
            'data-printproducttype="1"', 'data-printproducttype="36"'
        ).replace('data-ptype="business-cards"', 'data-ptype="roll-labels"')
        html = html.replace(
            "productType=1&size=101&orientation=1&paper=1&color=2",
            "&".join(f"{key}={value}" for key, value in selection.items()),
        )
        base_id = fixture["captures"]["product_lookup"]["data"]["items"][0]["id"]
        html = html.replace("/products/100000021/", f"/products/{base_id}/").replace(
            "optionIds=2,5", "optionIds=32,33,34,35,54"
        )
        html = html.replace("q===250?'none':'", "q===500?'none':'")
        # Rush is unavailable for this captured Roll branch, while Regular is
        # usable. The DOM adapter must keep the latter selected.
        html = html.replace("Rush 2 Business Days", "Rush Not Available")
        if source_branch == "36_square":
            html = re.sub(r"<fieldset>.*?</fieldset>", "", html, count=1, flags=re.S)
            # Ordinary local browser requests exercise the actual capture path.
            # The first response must survive eviction of the generic 256-item
            # table window, as during a long live traversal.
            filler = (
                "await Promise.all(Array.from({length:260},(_,i)=>fetch('/service/rest/v1/products/999999/options/'+i+'/prices').then(r=>r.json())));"
                if evict_initial_tables
                else ""
            )
            html = html.replace(
                "loadPromise=Promise.all(paths.map(path=>fetch(path).then(response=>response.json())))",
                "loadPromise=(async()=>{"
                + (
                    "const form=document.querySelector('#order-form');form.removeAttribute('data-printproducttype');form.removeAttribute('data-currentstoretype');"
                    if early_specifications
                    else ""
                )
                + "await fetch('/service/rest/v1/settings/product/specifications?requestValues=true&productType=36').then(r=>r.json());"
                + (
                    "form.setAttribute('data-printproducttype','36');form.setAttribute('data-currentstoretype','broker');"
                    if early_specifications
                    else ""
                )
                + filler
                + "await Promise.all(paths.map(path=>fetch(path).then(response=>response.json())));})()",
            )
        return html.encode()
    quantities = "".join(
        f'<option value="{quantity}" {"selected" if quantity == initial_quantity and not lazy_quantity else ""}>{quantity}</option>'
        for quantity in (50, 100, 250)
    )
    if lazy_quantity:
        quantities = (
            '<option value="" selected disabled>Please select quantity</option>'
            + quantities
        )
    controls = "".join(
        f'<label for="{key}_fixture">{label}</label><select id="{key}_fixture"><option value="{value}">{selected}</option></select>'
        for key, label, value, selected in (
            ("size", "Size", "101", "2 inch x 3.5 inch U.S. Standard"),
            ("paper", "Paper", "1", "14 pt. Gloss"),
            ("color", "Color", "2", "Full Color Front, Grayscale Back"),
            ("coating", "Coating", "5", "High Gloss UV Coating Front"),
            ("roundedCorners", "Rounded Corner", "", "None"),
        )
        if key not in missing_finishes
    )
    return f"""<!doctype html><html><head><title>Business Cards local fixture</title><meta itemprop="priceCurrency" content="USD"></head><body><h1>Business Cards</h1>
<div id="order-form" data-printproducttype="1" data-ptype="business-cards" data-currentstoretype="broker">{controls}
<label for="quantity_fixture">Quantity</label><select id="quantity_fixture">{quantities}</select>
<fieldset><legend>Orientation</legend><input type="radio" name="orientation" id="horizontal" value="1" checked><label for="horizontal">Horizontal</label></fieldset></div>
<div id="radioBtn" style="{'display:none' if lazy_quantity else ''}"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a><a id="rush" class="turnAround notActive" data-toggle="turnaround" data-title="3">Rush 2 Business Days</a></div>
<div class="totalSection"><div class="subTotal">Subtotal (excludes shipping):</div><div class="col-4">$0.00</div></div><div id="ajaxLoader" style="{'display:none' if lazy_quantity else ''}">Loading</div>
{'<div id="collapseOne" style="display:none"><input type="radio" id="proof-instant" name="proofType" value="31" checked><label for="proof-instant">Instantly Processed</label><input type="radio" id="proof-manual" name="proofType" value="100"><label for="proof-manual">Manually Processed</label></div>' if service_state else ''}
<script>
const quantity=document.querySelector('#quantity_fixture');
function price(){{let q=Number(quantity.value);let amounts={{50:'4.69',100:'8.61',250:'9.45'}};
 if({str(service_state == 'switch').lower()})document.querySelector(q===250?'#proof-manual':'#proof-instant').checked=true;
 if({str(service_state == 'unselected').lower()})document.querySelector('#proof-instant').checked=false;
 if({str(finish_appears).lower()} && q===250 && !document.querySelector('#coating_fixture')){{
  document.querySelector('#order-form').insertAdjacentHTML('beforeend','<label for="coating_fixture">Coating</label><select id="coating_fixture"><option value="5">High Gloss UV Coating Front</option></select>');
 }}
 if({str(reset_plateau_boundary).lower()})amounts[250]='4.69';
 document.querySelector('.col-4').textContent=q>0?'$'+(({str(mismatch).lower()} && q===250)?'99.99':amounts[q]):'$0.00';
 if({str(lazy_quantity).lower()})document.querySelector('#radioBtn').style.display=q>0?'':'none';
 if({str(disappearing_rush).lower()})document.querySelector('#rush').style.display=q===250?'none':'';
}}
price();
const query='productType=1&size=101&orientation=1&paper=1&color=2';
const paths=[
 '/service/rest/v1/products?'+query+'&turnaround=1',
 '/service/rest/v1/products/100000021/prices?itemId=null&cid=',
 '/service/rest/v1/products/100000021/options?requestValues=true&eddm=false',
 '/service/rest/v1/products/quantities?'+query+'&includeTurnaround=true',
 '/service/rest/v1/products/options/quantities?'+query+'&includeTurnaround=true&optionIds=2,5'
];
let loadPromise=null;
function loadTables(){{
 if(loadPromise)return loadPromise;
 document.querySelector('#ajaxLoader').style.display='';
 loadPromise=Promise.all(paths.map(path=>fetch(path).then(response=>response.json()))).then(()=>document.querySelector('#ajaxLoader').style.display='none');
 return loadPromise;
}}
quantity.addEventListener('change',()=>{{if({str(reset_plateau_boundary).lower()}&&quantity.value==='250')quantity.value='50';price();if(Number(quantity.value)>0)loadTables();}});
loadTables();
</script></body></html>""".encode()


@pytest.fixture
async def local_bulk_site(monkeypatch, chromium_runtime):
    captures = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "printograph_business_cards_tables.json"
        ).read_text()
    )["captures"]
    scenario = {
        "initial_quantity": 50,
        "mismatch": False,
        "disappearing_rush": False,
        "lazy_quantity": False,
        "source_branch": None,
        "reset_plateau_boundary": False,
        "missing_finishes": (),
        "finish_appears": False,
        "evict_initial_tables": False,
        "early_specifications": False,
        "service_state": None,
    }
    requests = []
    response_bodies = {
        "/service/rest/v1/products": captures["product_lookup"]["data"],
        "/service/rest/v1/products/100000021/prices": captures["base_prices"]["data"],
        "/service/rest/v1/products/100000021/options": captures["option_catalog"][
            "data"
        ],
        "/service/rest/v1/products/quantities": captures["quantity_support"]["data"],
        "/service/rest/v1/products/options/quantities": captures[
            "option_quantity_support"
        ]["data"],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            path = urlparse(self.path).path
            if path == "/product":
                body, content_type = fixture_html(**scenario), "text/html"
            else:
                bodies = response_bodies
                if scenario["source_branch"] is not None:
                    source = json.loads(
                        (
                            Path(__file__).parent
                            / "fixtures"
                            / "printograph_standard_tables.json"
                        ).read_text()
                    )["branches"][scenario["source_branch"]]["captures"]
                    bodies = {
                        urlparse(capture["url"]).path: capture["data"]
                        for capture in source.values()
                    }
                payload = bodies.get(path, {})
                if (
                    scenario["reset_plateau_boundary"]
                    and path == "/service/rest/v1/products/100000021/prices"
                ):
                    payload = {
                        "items": [
                            (
                                {**item, "markupPrice": "4.69"}
                                if item["quantity"] == 250
                                else item
                            )
                            for item in payload["items"]
                        ]
                    }
                body, content_type = (
                    json.dumps(payload).encode(),
                    "application/json",
                )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture_origin = f"127.0.0.1:{server.server_port}"
    real_bind = browser_tables.bind_business_card_tables

    def bind_local_fixture(snapshot, source_captures, **kwargs):
        normalized = []
        for capture in source_captures:
            parsed = urlsplit(capture["url"])
            if scenario["early_specifications"] and parsed.path.endswith(
                "/specifications"
            ):
                assert capture["request_product_type"] is None
                assert capture["request_store_type"] is None
            assert (
                parsed.netloc == fixture_origin
            ), "Fixture attempted an unexpected source origin"
            normalized.append(
                {
                    **capture,
                    "url": urlunsplit(
                        (
                            "https",
                            "www.printograph.com",
                            parsed.path,
                            parsed.query,
                            parsed.fragment,
                        )
                    ),
                }
            )
        return real_bind(snapshot, normalized, **kwargs)

    monkeypatch.setattr(browser_tables, "bind_business_card_tables", bind_local_fixture)
    monkeypatch.setattr("crawler.transport.validate_url", AsyncMock())
    monkeypatch.setenv("ATLAS_BROWSER_HEADLESS", "true")
    monkeypatch.setenv("ATLAS_BROWSER_PROFILE_DIR", "")
    monkeypatch.setenv("ATLAS_BROWSER_STORAGE_STATE", "")
    try:
        yield f"http://{fixture_origin}/product", scenario, requests
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


async def extract_site(site, *, maximum=None, quantity_filter=None):
    url, _, requests = site
    transport = FixtureTransport()
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    spec = {
        "url": url,
        "browser_bulk_quantities": True,
        "source_metadata": {"currency": "USD"},
        "selection_filters": {
            "size": "101",
            "paper": "1",
            "color": "2",
            "coating": "5",
            "roundedCorners": "",
            "orientation": "1",
            "productionTime": "1",
            "quantity": [50, 100, 250],
        },
    }
    if site[1].get("source_branch") in {"36_1", "36_square"}:
        spec["selection_filters"] = {
            "shape": "5",
            "size": "742",
            "paper": "13",
            "finish": "1",
            "color": "1",
            "windPosition": "32",
            "orientation": "1",
            "productionTime": "1",
            "quantity": [100, 250, 500],
        }
        if site[1]["source_branch"] == "36_square":
            spec["selection_filters"].update(shape="4", size="448")
            spec["selection_filters"].pop("orientation")
    if site[1].get("source_branch") == "1_10_matte":
        spec["selection_filters"].update(
            paper="10", color="1", coating="", spotcoating="", raisedprint=""
        )
    for key in site[1].get("missing_finishes", ()):
        spec["selection_filters"].pop(key, None)
    if quantity_filter is not None:
        spec["selection_filters"]["quantity"] = quantity_filter
    if maximum is not None:
        spec["max_configurations"] = maximum
    rows = [row async for row in BrowserAdapter().extract(spec, transport, emit)]
    return rows, events, transport


def dom_quantities(transport):
    return [
        int(
            next(
                control["value"]
                for control in observation["data"]["controls"]
                if control["key"] == "quantity"
            )
        )
        for observation in transport.observations
        if observation["context"]["lane"] == "browser_dom_quote"
    ]


@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.asyncio
async def test_matte_aqueous_requires_both_dom_subtotals(local_bulk_site, mismatch):
    from decimal import Decimal

    local_bulk_site[1].update(source_branch="1_10_matte", mismatch=mismatch)
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100, 250], events
    assert all(
        row["configuration"]["coating"]
        == {"value": "", "label": "Matte Aqueous Coating"}
        for row in rows
    )
    if mismatch:
        assert all(row["provenance"] == "direct_quote" for row in rows)
        source = json.loads(
            (
                Path(__file__).parent / "fixtures" / "printograph_standard_tables.json"
            ).read_text()
        )["branches"]["1_10_matte"]
        base = next(
            item["markupPrice"]
            for item in source["captures"]["base_prices"]["data"]["items"]
            if item["quantity"] == 250
        )
        assert Decimal(rows[-1]["subtotal"]) == Decimal(base) + Decimal("3.00")
        assert any(
            kind == "bulk_fallback" and "subtotal" in payload["reason"]
            for kind, payload in events
        )
        assert not any(kind == "bulk_quantity_expansion" for kind, _ in events)
    else:
        assert [row["provenance"] for row in rows] == [
            "direct_quote",
            "source_table",
            "direct_quote",
        ]
        assert dom_quantities(transport) == [50, 250]
        assert rows[1]["verification_status"] == "pending"
        assert rows[1]["table_derivation"]["formula"] == "base_only"
        assert not any(kind == "bulk_fallback" for kind, _ in events)


@pytest.mark.asyncio
async def test_bulk_captures_two_dom_samples_and_one_pending_table_row(local_bulk_site):
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100, 250], events
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert [row["subtotal"] for row in rows] == ["4.69", "8.61", "9.45"]
    assert rows[1]["verification_status"] == "pending"
    assert rows[1]["production"] == {
        "value": "1",
        "status": "requires_configuration_quote",
    }
    assert rows[1]["table_derivation"]["sampled_quantities"] == [50, 250]
    assert dom_quantities(transport) == [50, 250]
    assert (
        len([path for path in local_bulk_site[2] if path.startswith("/service/rest/")])
        == 5
    )
    assert any(
        kind == "bulk_quantity_expansion" and data["verification_complete"] is False
        for kind, data in events
    )


@pytest.mark.parametrize("service_state", ["stable", "switch", "unselected"])
@pytest.mark.asyncio
async def test_bulk_service_selection_must_be_known_and_stable_between_samples(
    local_bulk_site, service_state
):
    local_bulk_site[1]["service_state"] = service_state
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100, 250], events
    assert all(row["record_schema_version"] == 2 for row in rows)
    if service_state == "stable":
        assert [row["provenance"] for row in rows] == [
            "direct_quote",
            "source_table",
            "direct_quote",
        ], events
        assert rows[1]["pricing_context"]["service_context"]["groups"]["proofType"] == {
            "status": "selected",
            "values": ["31"],
        }
        assert rows[1]["verification_status"] == "pending"
        assert dom_quantities(transport) == [50, 250]
        binding = rows[1]["table_derivation"]["binding"]
        assert (
            binding["service_context"] == rows[1]["pricing_context"]["service_context"]
        )
        assert binding["request_service_contexts"]
        assert all(
            item["service_context"] == binding["service_context"]
            and item["evidence_id"] in rows[1]["evidence_ids"]
            for item in binding["request_service_contexts"]
        )
    else:
        assert all(row["provenance"] == "direct_quote" for row in rows), events
        assert any(kind == "bulk_fallback" for kind, _ in events)
        assert not any(kind == "bulk_quantity_expansion" for kind, _ in events)
    # Even identical numerical totals cannot prove identical selected services.
    assert [row["subtotal"] for row in rows] == ["4.69", "8.61", "9.45"]
    if service_state == "switch":
        assert rows[-1]["pricing_context"]["service_context"]["groups"]["proofType"][
            "values"
        ] == ["100"]


@pytest.mark.asyncio
async def test_bulk_boundary_mismatch_falls_back_to_direct_quotes(local_bulk_site):
    local_bulk_site[1]["mismatch"] = True
    rows, events, _ = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100, 250]
    assert all(row["provenance"] == "direct_quote" for row in rows), events
    assert rows[-1]["subtotal"] == "99.99"
    assert any(
        kind == "bulk_fallback" and "subtotal" in data["reason"]
        for kind, data in events
    )
    assert not any(kind == "bulk_quantity_expansion" for kind, _ in events)


@pytest.mark.asyncio
async def test_bulk_rejects_reset_boundary_even_when_its_price_matches(local_bulk_site):
    local_bulk_site[1]["reset_plateau_boundary"] = True
    rows, events, _ = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100], events
    assert all(row["provenance"] == "direct_quote" for row in rows)
    assert any(
        kind == "bulk_fallback"
        and "reset the requested boundary quantity" in data["reason"]
        for kind, data in events
    )
    assert not any(kind == "bulk_quantity_expansion" for kind, _ in events)
    coverage = next(data for kind, data in events if kind == "coverage")
    assert coverage["status"] == "partial"
    assert "source_auto_reset" in coverage["reasons"]


@pytest.mark.asyncio
async def test_configuration_limit_one_avoids_boundary_sampling(local_bulk_site):
    rows, events, transport = await extract_site(local_bulk_site, maximum=1)
    assert (
        len(rows) == 1
        and rows[0]["quantity"] == 50
        and rows[0]["provenance"] == "direct_quote"
    )
    assert dom_quantities(transport) == [50]
    assert not any(kind == "bulk_quantity_expansion" for kind, _ in events)


@pytest.mark.asyncio
async def test_bulk_maximum_initial_anchor_samples_opposite_minimum(local_bulk_site):
    local_bulk_site[1]["initial_quantity"] = 250
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert dom_quantities(transport) == [250, 50]
    assert rows[1]["table_derivation"]["sampled_quantities"] == [50, 250]


@pytest.mark.asyncio
async def test_regular_bulk_survives_disappearing_unselected_rush_choice(
    local_bulk_site,
):
    local_bulk_site[1]["disappearing_rush"] = True
    rows, events, _ = await extract_site(local_bulk_site)
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert all(row["production"]["value"] == "1" for row in rows)


@pytest.mark.asyncio
async def test_lazy_quantity_reveals_production_then_reenters_bulk_path(
    local_bulk_site,
):
    local_bulk_site[1]["lazy_quantity"] = True
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100, 250]
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert [row["subtotal"] for row in rows] == ["4.69", "8.61", "9.45"]
    assert dom_quantities(transport) == [50, 250]
    assert rows[1]["verification_status"] == "pending"
    assert rows[1]["production"] == {
        "value": "1",
        "status": "requires_configuration_quote",
    }
    assert any(kind == "bulk_quantity_expansion" for kind, _ in events)


@pytest.mark.asyncio
async def test_lazy_quantity_warmup_uses_first_requested_quantity(local_bulk_site):
    local_bulk_site[1]["lazy_quantity"] = True
    rows, events, transport = await extract_site(
        local_bulk_site, quantity_filter=[100, 250]
    )
    assert [row["quantity"] for row in rows] == [100, 250]
    assert all(row["provenance"] == "direct_quote" for row in rows)
    assert dom_quantities(transport) == [100, 250]
    assert any(kind == "bulk_quantity_expansion" for kind, _ in events)


@pytest.mark.asyncio
async def test_lazy_quantity_limit_one_respects_filter_without_boundary(
    local_bulk_site,
):
    local_bulk_site[1]["lazy_quantity"] = True
    rows, events, transport = await extract_site(
        local_bulk_site, maximum=1, quantity_filter=[100, 250]
    )
    assert len(rows) == 1 and rows[0]["quantity"] == 100
    assert rows[0]["provenance"] == "direct_quote"
    assert rows[0]["production"]["value"] == "1"
    assert dom_quantities(transport) == [100]
    assert not any(kind == "bulk_quantity_expansion" for kind, _ in events)


@pytest.mark.asyncio
async def test_roll_actual_tables_expand_two_dom_samples_and_one_pending_tier(
    local_bulk_site,
):
    local_bulk_site[1]["source_branch"] = "36_1"
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [100, 250, 500], events
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert dom_quantities(transport) == [100, 500]
    pending = rows[1]
    assert pending["verification_status"] == "pending"
    assert pending["production"] == {
        "value": "1",
        "status": "requires_configuration_quote",
    }
    assert pending["configuration"]["shape"]["value"] == "5"
    assert pending["configuration"]["windPosition"]["value"] == "32"
    assert pending["pricing_context"]["source_base_product_id"] == "100134417"
    assert pending["table_derivation"]["sampled_quantities"] == [100, 500]
    assert "roll_count" not in pending
    assert not any(kind == "bulk_fallback" for kind, _ in events), events


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.asyncio
async def test_square_roll_retains_specification_and_preserves_absent_orientation(
    local_bulk_site,
    early,
):
    local_bulk_site[1].update(
        source_branch="36_square", evict_initial_tables=True, early_specifications=early
    )
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [100, 250, 500], events
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert dom_quantities(transport) == [100, 500]
    assert all("orientation" not in row["configuration"] for row in rows)
    pending = rows[1]
    assert pending["verification_status"] == "pending"
    assert pending["pricing_context"]["source_base_product_id"] == "100134721"
    assert pending["configuration"]["shape"]["value"] == "4"
    assert pending["configuration"]["windPosition"]["value"] == "32"
    assert pending["table_derivation"]["sampled_quantities"] == [100, 500]
    assert pending["production"] == {
        "value": "1",
        "status": "requires_configuration_quote",
    }
    assert (
        len([path for path in local_bulk_site[2] if "/products/999999/" in path]) == 260
    )
    assert (
        sum(observation["data"] == {} for observation in transport.observations) == 260
    )
    assert not any(kind == "bulk_fallback" for kind, _ in events), events


@pytest.mark.asyncio
async def test_bulk_preserves_both_absent_finishing_controls(local_bulk_site):
    local_bulk_site[1]["missing_finishes"] = ("coating", "roundedCorners")
    rows, events, transport = await extract_site(local_bulk_site)
    assert [row["quantity"] for row in rows] == [50, 100, 250], events
    assert [row["provenance"] for row in rows] == [
        "direct_quote",
        "source_table",
        "direct_quote",
    ], events
    assert dom_quantities(transport) == [50, 250]
    assert all(
        not {"coating", "roundedCorners"} & set(row["configuration"]) for row in rows
    )
    assert rows[1]["verification_status"] == "pending"
    assert not any(kind == "bulk_fallback" for kind, _ in events), events


@pytest.mark.asyncio
async def test_bulk_control_appearing_at_boundary_forces_original_branch_fallback(
    local_bulk_site,
):
    local_bulk_site[1]["missing_finishes"] = ("coating", "roundedCorners")
    local_bulk_site[1]["finish_appears"] = True
    rows, events, _ = await extract_site(local_bulk_site, maximum=3)
    assert any(
        kind == "bulk_fallback" and "non-quantity options" in payload["reason"]
        for kind, payload in events
    ), events
    # No unobserved finishing-control absence may be copied into a candidate.
    # A later, separately validated branch with the now-present coating is safe.
    assert all(
        "coating" in row["configuration"]
        for row in rows
        if row["provenance"] == "source_table"
    )
