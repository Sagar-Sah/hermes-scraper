"""Synthetic contracts: these fixtures are not claimed to be live supplier data."""

import unittest
from types import SimpleNamespace
from urllib.parse import urlparse

from crawler.adapters.printograph import (
    AdapterError,
    PrintographAdapter,
    normalize_specs,
    parse_frontend_contract,
    price_table,
)

HTML = """<title>Business cards</title><div id="order-form" data-printproducttype="1"></div>
<script src="https://static.printograph.com/orderForm.js?v=fixture"></script>"""
BUNDLE = """let fields={1:{specs:["size","orientation","paper","color"],options:["roundedCorners"]}};
let config={optionsHavingPrices:[2]};switch(x){case 2:o.roundedCorners.list.push(r.type);break;}"""
SPECS = {
    "sizes": [{"id": 101, "label": "3.5 x 2"}],
    "papers": [{"id": 7, "label": "Gloss"}],
    "colors": [{"id": 3, "label": "Both sides"}],
    "turnarounds": [{"id": 1, "label": "Standard"}],
}


class FixtureTransport:
    def __init__(self, denied=False, budget_fixture=False):
        self.calls = []
        self.denied = denied
        self.budget_fixture = budget_fixture

    async def fetch(self, url, method="GET", params=None, json=None, context=None):
        self.calls.append((url, method, params))
        if method != "GET":
            raise AssertionError("Adapter attempted a write")
        path = urlparse(url).path
        status = 200
        if path.endswith("/order"):
            data = HTML
        elif path.endswith("orderForm.js"):
            data = BUNDLE
        elif path.endswith("/specifications"):
            data = SPECS
            if self.denied:
                data, status = {"messageList": [{"resource": "API-Vendor-Token"}]}, 401
        elif path == "/service/rest/v1/products":
            data = {"items": [{"id": 123}]}
        elif path.endswith("/quantities"):
            data = {"items": [{"quantity": 100}, {"quantity": 250}]}
        elif path.endswith("/options/8/prices"):
            data = {
                "items": [
                    {"quantity": 100, "markupPrice": "5.50"},
                    {"quantity": 250, "markupPrice": "6.00"},
                ]
            }
        elif path.endswith("/prices"):
            data = {
                "items": [
                    {"quantity": 100, "markupPrice": "25.00"},
                    {"quantity": 250, "markupPrice": "40.00"},
                ]
            }
        elif path.endswith("/options"):
            data = {
                "items": [
                    {
                        "id": 8,
                        "type": {"id": 2, "label": "Rounded"},
                        "group": {"id": 1, "label": "Corners"},
                    }
                ]
            }
        else:
            raise AssertionError(f"Unexpected request: {url}")
        return SimpleNamespace(
            data=data, status=status, response_id=f"e{len(self.calls)}", cached=False
        )


class PrintographContracts(unittest.IsolatedAsyncioTestCase):
    def test_source_field_contract(self):
        data = parse_frontend_contract(BUNDLE, 1)
        self.assertEqual(data["option_fields"], {2: "roundedCorners"})
        self.assertEqual(data["priced_options"], {2})

    def test_unsupported_encoded_size_does_not_become_valid_domain(self):
        with self.assertRaises(AdapterError):
            normalize_specs({"sizes": {"list": ["size_123:encoded"]}})

    def test_plural_specifications_are_preserved(self):
        self.assertEqual(normalize_specs(SPECS)["size"][0]["id"], 101)
        self.assertEqual(normalize_specs(SPECS)["turnaround"][0]["id"], 1)

    def test_missing_and_conflicting_prices_rejected(self):
        for payload in (
            {"items": [{"quantity": 100}]},
            {
                "items": [
                    {"quantity": 100, "markupPrice": "1"},
                    {"quantity": 100, "markupPrice": "2"},
                ]
            },
            {"items": [{"quantity": 100, "markupPrice": "NaN"}]},
            {"items": [{"quantity": 100, "markupPrice": "1"}], "totalCount": 20},
        ):
            with self.assertRaises(AdapterError):
                price_table(payload)

    async def test_tiers_reused_with_pending_composition_and_atomic_evidence(self):
        transport = FixtureTransport()
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        rows = [
            r
            async for r in PrintographAdapter().extract(
                {"url": "https://www.printograph.com/products/business-cards/order"},
                transport,
                emit,
            )
        ]
        self.assertEqual([r["subtotal"] for r in rows], ["30.50", "46.00"])
        self.assertEqual([r["quantity"] for r in rows], [100, 250])
        self.assertTrue(
            all(
                r["provenance"] == "rule_derived"
                and r["verification_status"] == "pending"
                for r in rows
            )
        )
        self.assertTrue(all(r["coverage_status"] == "partial" for r in rows))
        self.assertIn("e8", rows[0]["evidence_ids"])
        self.assertEqual(
            len(transport.calls), 8
        )  # Two rows; one fetch per whole price table.
        self.assertEqual(events[-1][1]["status"], "partial")
        self.assertFalse(events[-1][1]["count_is_exact"])

    async def test_authorization_failure_stops_without_price_requests(self):
        transport = FixtureTransport(denied=True)

        async def emit(*args):
            pass

        with self.assertRaises(AdapterError) as caught:
            _ = [
                r
                async for r in PrintographAdapter().extract(
                    {
                        "url": "https://www.printograph.com/products/business-cards/order"
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(caught.exception.code, "access_required")
        self.assertEqual(len(transport.calls), 3)

    async def test_budget_exhaustion_never_reports_complete(self):
        transport = FixtureTransport()
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        with self.assertRaises(AdapterError) as caught:
            _ = [
                r
                async for r in PrintographAdapter().extract(
                    {
                        "url": "https://www.printograph.com/products/business-cards/order",
                        "max_requests": 4,
                    },
                    transport,
                    emit,
                )
            ]
        self.assertEqual(caught.exception.code, "request_limit")
        self.assertEqual(len(transport.calls), 4)
        self.assertFalse(any(k == "coverage" for k, _ in events))

    async def test_selection_filter_limits_rows(self):
        async def emit(*args):
            pass

        rows = [
            r
            async for r in PrintographAdapter().extract(
                {
                    "url": "https://www.printograph.com/products/business-cards/order",
                    "selection_filters": {"quantity": [250]},
                },
                FixtureTransport(),
                emit,
            )
        ]
        self.assertEqual([r["quantity"] for r in rows], [250])


if __name__ == "__main__":
    unittest.main()
