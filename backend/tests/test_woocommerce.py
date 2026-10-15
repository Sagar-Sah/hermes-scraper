import html
import json
from types import SimpleNamespace

import pytest

from crawler.adapters.printograph import AdapterError
from crawler.adapters.woocommerce import WooCommerceAdapter


def product_page(variations, *, endpoint="/?wc-ajax=%%endpoint%%"):
    encoded = html.escape(json.dumps(variations), quote=True)
    return f"""<html><head><title>Variable &amp; Precise</title><meta itemprop="priceCurrency" content="USD"></head>
    <body><form class="cart variations_form" data-product_id="42" data-product_variations="{encoded}">
    <select name="attribute_pa_size"><option value="">Choose</option><option value="small">Small</option><option value="large">Large</option></select>
    <select name="attribute_pa_color"><option value="">Choose</option><option value="blue&amp;silver">Blue &amp; Silver</option><option value="red">Red</option><option value="hidden" disabled>Hidden</option></select>
    </form><script>var wc_add_to_cart_variation_params = {json.dumps({"wc_ajax_url": endpoint})};</script></body></html>"""


def variant(identifier=1, size="small", color="blue&silver", price="10.25", **extra):
    return {
        "variation_id": identifier,
        "attributes": {"attribute_pa_size": size, "attribute_pa_color": color},
        "display_price": price,
        "min_qty": 1,
        "max_qty": "",
        "variation_is_visible": True,
        "variation_is_active": True,
        "is_purchasable": True,
        "is_in_stock": True,
        "image": {"full_src": "/image.png"},
        **extra,
    }


class FixtureTransport:
    def __init__(self, page, resolve=None):
        self.page = page
        self.resolve = resolve
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        data = self.page if len(self.calls) == 1 else self.resolve(kwargs["data"])
        return SimpleNamespace(
            data=data, status=200, response_id=f"e{len(self.calls)}", cached=False
        )


async def collect(page, resolve=None, **spec_extra):
    transport = FixtureTransport(page, resolve)
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    records = [
        row
        async for row in WooCommerceAdapter().extract(
            {"url": "https://store.test/product/cards", **spec_extra}, transport, emit
        )
    ]
    return records, transport, events


@pytest.mark.asyncio
async def test_embedded_variations_do_not_generate_nonexistent_cartesian_combinations():
    rows, transport, events = await collect(
        product_page([variant(), variant(2, "large", "red", "20.50")])
    )
    assert len(rows) == 2
    assert len(transport.calls) == 1
    assert rows[0]["configuration"]["attribute_pa_color"] == {
        "value": "blue&silver",
        "label": "Blue & Silver",
    }
    assert rows[0]["images"] == ["https://store.test/image.png"]
    assert rows[0]["verification_status"] == "pending"
    assert events[-1][1]["status"] == "partial"


@pytest.mark.asyncio
async def test_wildcard_precedence_matches_source_order_and_keeps_resolved_ids():
    wildcard = variant(1, "", "", "9.00")
    explicit = variant(2, "large", "red", "15.00")
    rows, _, _ = await collect(product_page([explicit, wildcard]))
    assert len(rows) == 4
    selected = [
        r
        for r in rows
        if r["configuration"]["attribute_pa_size"]["value"] == "large"
        and r["configuration"]["attribute_pa_color"]["value"] == "red"
    ]
    assert (
        selected[0]["subtotal"] == "15.00" and selected[0]["source_product_id"] == "2"
    )
    reversed_rows, _, _ = await collect(product_page([wildcard, explicit]))
    assert {r["source_product_id"] for r in reversed_rows} == {"1"}
    assert len(reversed_rows) == 4


@pytest.mark.asyncio
async def test_unavailable_first_match_is_not_replaced_by_later_variant():
    rows, _, events = await collect(
        product_page(
            [variant(1, "", "", variation_is_active=False), variant(2, "large", "red")]
        )
    )
    assert rows == []
    assert events[-1][1]["unavailable_or_nonunit_configurations"] == 4


@pytest.mark.asyncio
async def test_minimum_quantity_does_not_fabricate_quantity_one_subtotal():
    rows, _, _ = await collect(product_page([variant(min_qty=100)]))
    assert rows == []
    with pytest.raises(AdapterError, match="one-unit pricing"):
        await collect(product_page([variant()]), selection_filters={"quantity": 100})


@pytest.mark.asyncio
async def test_dynamic_read_posts_exact_raw_values_and_only_emits_resolved_candidates():
    def resolve(payload):
        assert payload["product_id"] == "42"
        if (
            payload["attribute_pa_size"] == "small"
            and payload["attribute_pa_color"] == "blue&silver"
        ):
            return variant()
        return False

    rows, transport, events = await collect(product_page(False), resolve)
    assert len(transport.calls) == 5
    assert len(rows) == 1
    assert all(
        kwargs["method"] == "POST" and "data" in kwargs
        for _, kwargs in transport.calls[1:]
    )
    assert all("wc-ajax=get_variation" in url for url, _ in transport.calls[1:])
    assert rows[0]["evidence_ids"] == ["e1", "e2"]
    assert rows[0]["provenance"] == "source_variation_response"
    assert events[-1][1]["unavailable_candidates"] == 3
    assert not events[-1][1]["verification_complete"]


@pytest.mark.asyncio
async def test_dynamic_mismatched_selection_and_malformed_response_fail_visibly():
    with pytest.raises(AdapterError) as mismatch:
        await collect(product_page(False), lambda payload: variant(size="large"))
    assert mismatch.value.code == "configuration_mismatch"
    with pytest.raises(AdapterError) as malformed:
        await collect(product_page(False), lambda payload: "<html>broken</html>")
    assert malformed.value.code == "schema_changed"


@pytest.mark.asyncio
async def test_foreign_or_mutating_endpoints_are_not_contacted():
    for endpoint in (
        "https://elsewhere.test/?wc-ajax=get_variation",
        "/?wc-ajax=checkout",
        "/?wc-ajax=get_variation&add-to-cart=42",
    ):
        with pytest.raises(AdapterError) as error:
            await collect(
                product_page(False, endpoint=endpoint), lambda payload: variant()
            )
        assert error.value.code == "unsupported_endpoint"


@pytest.mark.asyncio
async def test_filters_match_labels_but_post_raw_identifiers():
    rows, transport, _ = await collect(
        product_page(False),
        lambda payload: variant(),
        selection_filters={"pa_color": "Blue & Silver", "pa_size": "Small"},
    )
    assert len(rows) == 1 and len(transport.calls) == 2
    assert transport.calls[1][1]["data"]["attribute_pa_color"] == "blue&silver"
