"""Saved public table/calculator fixtures; no supplier HTTP or JS execution."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.adapters.printograph import AdapterError
from crawler.adapters.printitprints import (CALCULATOR_SHA256, PrintItPrintsAdapter,
    parse_product_html, records_from_html, retail_total)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/printitprints_calculator.json").read_text())
URL = FIXTURE["source_url"]


def page(rows=None, *, calculator=None, product_id=17515, currency="USD"):
    metadata = f'<script type="application/ld+json">{json.dumps({"priceCurrency": currency})}</script>' if currency else ""
    return (f'<html><head><title>Club Flyers</title>{metadata}<meta property="og:image" content="/club.png"></head>'
        '<body><h1 class="product_title">Club Flyers</h1><div id="product_calculator_display"></div>'
        f'<script>const csvdata = {json.dumps(FIXTURE["rows"] if rows is None else rows)};</script>'
        f'<script>const current_product_id_is_this = {json.dumps(product_id)};</script>'
        f'<script>{FIXTURE["calculator"] if calculator is None else calculator}</script></body></html>')


class FixtureTransport:
    def __init__(self, body, status=200):
        self.body, self.status, self.calls = body, status, []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(data=self.body, status=self.status, response_id="fixture-response")


async def collect(body=None, status=200, **spec):
    transport = FixtureTransport(page() if body is None else body, status)
    events = []
    async def emit(kind, payload):
        events.append((kind, payload))
    rows = [row async for row in PrintItPrintsAdapter().extract({"url": URL, **spec}, transport, emit)]
    return rows, transport, events


@pytest.mark.asyncio
async def test_saved_450_rows_use_one_document_and_preserve_supplier_scope():
    rows, transport, events = await collect()
    assert len(rows) == 450 and len(transport.calls) == 1
    assert transport.calls[0][1] == {"context": {"adapter": "printitprints", "lane": "discovery"}}
    assert rows[0]["subtotal"] == "9.90" and rows[0]["pricing_components"]["embedded_base_total"] == "3.3"
    assert rows[0]["unit_price"] == "0.198"
    assert rows[0]["pricing_context"]["calculator_sha256"] == CALCULATOR_SHA256
    assert rows[0]["configuration"]["proof_processing"]["label"] == "Instantly Processed"
    assert rows[0]["production"]["label"] == "Regular 2-4 Business Days"
    assert rows[0]["images"] == ["https://printitprints.com/club.png"]
    assert {row["supplier"] for row in rows} == {"PrintItPrints"}
    assert all(row["product_url"] == URL and row["verification_status"] == "pending" and not row["cart_verified"] for row in rows)
    assert all(row["evidence_ids"] == ["fixture-response"] for row in rows)
    assert events[-1][1]["status"] == "partial" and not events[-1][1]["count_is_exact"]
    assert events[-1][1]["embedded_table_exhausted"] is True


def test_captured_canary_configuration_calculates_5130_from_base_1710():
    result = list(records_from_html(page(), {"url": URL, "selection_filters": {
        "size": '2" x 5.5"', "orientation": "Horizontal", "paper": "100 lb. Gloss Cover",
        "colorspec": "Full Color Both Sides", "coating": "Gloss Aqueous Coating", "quantity": 250,
        "proof_processing": "Instantly Processed", "artwork_delivery": "Upload Files Now"}}, "saved-canary-html"))
    assert len(result) == 1
    assert result[0]["pricing_components"]["embedded_base_total"] == "17.1"
    assert result[0]["subtotal"] == "51.30" and result[0]["unit_price"] == "0.2052"
    assert result[0]["verification_status"] == "pending"  # Matching math is not cart verification.


@pytest.mark.parametrize("base,expected", [(0,"0.00"),(25,"75.00"),(25.01,"62.53"),(50,"125.00"),
    (50.01,"100.02"),(100,"200.00"),(100.01,"185.02"),(250,"462.50"),(250.01,"425.02"),
    (600,"1020.00"),(600.01,"990.02"),(1000,"1650.00"),(1000.01,"1500.01"),(1500,"2250.00"),
    (1500.01,"2175.01"),(2000,"2900.00"),(2000.01,"2800.01"),(3000.99,"4201.39"),(3001,"4051.35")])
def test_exact_source_markup_boundaries_and_binary_display_rounding(base, expected):
    assert retail_total(base, True)[0] == expected


@pytest.mark.parametrize("product_id,expected", [(17515,"9.90"),(7426,"9.90"),(999999,"3.30")])
def test_markup_requires_source_membership_or_explicit_special_id(product_id, expected):
    result = list(records_from_html(page(FIXTURE["rows"][:1], product_id=product_id), {"url": URL}, "saved"))
    assert result[0]["subtotal"] == expected


@pytest.mark.parametrize("change", ["threshold", "whitespace", "missing", "duplicate"])
def test_unrecognized_calculator_never_publishes_embedded_base_as_retail(change):
    script = FIXTURE["calculator"]
    if change == "threshold": script = script.replace("(200/100)", "(201/100)")
    elif change == "whitespace": script += "\n"
    elif change == "missing": script = ""
    body = page(FIXTURE["rows"][:1], calculator=script)
    if change == "duplicate": body += f'<script>{script}</script>'
    row = list(records_from_html(body, {"url": URL}, "saved"))[0]
    assert row["subtotal"] is None and row["unit_price"] is None
    assert row["pricing_components"]["embedded_base_total"] == "3.3"
    assert "retail_calculator_contract_unrecognized" in row["coverage_reasons"]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "1,000", "NaN", "Infinity", True, None, "9007199254740992"])
def test_invalid_quantity_is_rejected_before_any_rows(value):
    row = deepcopy(FIXTURE["rows"][0]); row["RUNSIZE"] = value
    with pytest.raises(AdapterError):
        parse_product_html(page([row]), URL)


@pytest.mark.parametrize("value", ["-3", "NaN", "Infinity", True, None, "1e1000"])
def test_nonfinite_or_invalid_base_prices_cannot_produce_retail(value):
    row = deepcopy(FIXTURE["rows"][0]); row["TotalPrice"] = value
    with pytest.raises(AdapterError):
        parse_product_html(page([row]), URL)


def test_duplicate_or_executable_json_declaration_is_rejected():
    body = page(FIXTURE["rows"][:1])
    with pytest.raises(AdapterError):
        parse_product_html(body + '<script>const csvdata = [];</script>', URL)
    with pytest.raises(AdapterError):
        parse_product_html(body.replace('const csvdata = [', 'const csvdata = (() => ['), URL)
    with pytest.raises(AdapterError):
        parse_product_html(body.replace('const current_product_id_is_this = 17515;', 'const current_product_id_is_this = 17515; stealCookies();'), URL)
    with pytest.raises(AdapterError):
        parse_product_html(page([FIXTURE["rows"][0], FIXTURE["rows"][0]]), URL)


def test_sparse_row_with_multiple_possible_matches_keeps_retail_unresolved():
    first = deepcopy(FIXTURE["rows"][0]); second = deepcopy(first)
    first["COATING"] = ""
    result = list(records_from_html(page([first, second]), {"url": URL}, "saved"))
    assert result[0]["subtotal"] is None
    assert "sparse_configuration_not_unique_in_source_calculator" in result[0]["coverage_reasons"]
    assert result[1]["subtotal"] == "9.90"


def test_all_union_dimensions_are_preserved_but_nonquantity_last_control_cannot_verify_price():
    row = deepcopy(FIXTURE["rows"][0]); row["CUSTOM FINISH"] = "New finish"
    result = list(records_from_html(page([row]), {"url": URL}, "saved"))[0]
    assert result["configuration"]["custom_finish"]["label"] == "New finish"
    assert result["subtotal"] is None


def test_missing_currency_requires_explicit_metadata_and_unknown_filters_fail():
    with pytest.raises(AdapterError, match="currency"):
        list(records_from_html(page(currency=None), {"url": URL}, "saved"))
    result = list(records_from_html(page(FIXTURE["rows"][:1], currency=None), {"url": URL, "source_metadata": {"currency": "USD"}}, "saved"))
    assert result[0]["currency"] == "USD"
    with pytest.raises(AdapterError, match="No embedded choice"):
        list(records_from_html(page(), {"url": URL, "selection_filters": {"imaginary": "choice"}}, "saved"))
    with pytest.raises(AdapterError, match="evidence"):
        list(records_from_html(page(), {"url": URL}, ""))


def test_refetched_identical_document_does_not_create_new_configuration_identity():
    from crawler.engine import price_signature, row_identity
    first = list(records_from_html(page(FIXTURE["rows"][:1]), {"url": URL}, "old-response"))[0]
    second = list(records_from_html(page(FIXTURE["rows"][:1]), {"url": URL}, "new-response"))[0]
    assert first["evidence_ids"] != second["evidence_ids"]
    assert row_identity(first) == row_identity(second)
    changed = deepcopy(FIXTURE["rows"][0]); changed["TotalPrice"] = "4.0"
    third = list(records_from_html(page([changed]), {"url": URL}, "changed-response"))[0]
    assert row_identity(first) == row_identity(third)
    assert price_signature(first) != price_signature(third)


@pytest.mark.asyncio
async def test_explicit_configuration_limit_is_honored_and_not_called_exhaustive():
    rows, _, events = await collect(max_configurations=2)
    assert len(rows) == 2
    assert "configured_configuration_limit" in rows[0]["coverage_reasons"]
    assert events[-1][1]["embedded_table_exhausted"] is False


@pytest.mark.asyncio
async def test_http_access_denied_does_not_trigger_browser_fallback(monkeypatch):
    async def forbidden(*args):
        raise AssertionError("Browser fallback was not explicitly selected")
    monkeypatch.setattr("crawler.browser_document.capture_product_document", forbidden)
    with pytest.raises(AdapterError) as error:
        await collect(status=403)
    assert error.value.code == "access_required"


@pytest.mark.asyncio
async def test_browser_document_mode_uses_explicit_reader_without_http(monkeypatch):
    calls = []
    async def capture(transport, url):
        calls.append(url)
        return SimpleNamespace(data=page(FIXTURE["rows"][:1]), status=200, response_id="browser-document")
    monkeypatch.setattr("crawler.browser_document.capture_product_document", capture)
    rows, transport, _ = await collect(source_metadata={"document_transport": "browser"})
    assert not transport.calls and calls == [URL]
    assert rows[0]["evidence_ids"] == ["browser-document"]
    with pytest.raises(AdapterError, match="document_transport"):
        await collect(source_metadata={"document_transport": "unsupported"})
