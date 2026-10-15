from datetime import datetime
from decimal import Decimal

import pytest

from crawler.commerce import (
    analyze_price_breaks,
    money,
    printograph_delivery_window,
    quote_shipping,
    roll_label_packaging,
    unit_price,
)


def test_currency_rounding_and_fractional_unit_prices():
    assert money("1.005", "USD") == "1.01"
    assert money("12.5", "JPY") == "13"
    assert money("1.2345", "KWD") == "1.235"
    assert unit_price("19.99", 1000) == "0.01999000"
    for invalid in (None, True, "NaN", "Infinity"):
        with pytest.raises(ValueError):
            money(invalid)
    with pytest.raises(ValueError):
        unit_price("5", 0)
    with pytest.raises(ValueError):
        money("5", "ZZZ")


def test_volume_analysis_never_mixes_production_currency_or_tax_context():
    base = {
        "product_url": "https://fixture/cards",
        "configuration": {"stock": "matte", "coating": ""},
        "currency": "USD",
        "tax_basis": "exclusive",
        "production": "regular",
    }
    rows = [
        {**base, "quantity": 100, "subtotal": "20.00"},
        {**base, "quantity": 500, "subtotal": "50.00"},
        {**base, "quantity": 1000, "subtotal": "120.00"},
        {**base, "quantity": 100, "subtotal": "40.00", "production": "rush"},
        {**base, "quantity": 100, "subtotal": "25.00", "currency": "CAD"},
        {**base, "quantity": 100, "subtotal": "21.60", "tax_basis": "inclusive"},
    ]
    analyzed = analyze_price_breaks(rows)
    regular = [
        r
        for r in analyzed
        if r["context"]["production"] == "regular"
        and r["context"]["currency"] == "USD"
        and r["context"]["tax_basis"] == "exclusive"
    ]
    assert [r["change"] for r in regular] == ["reference", "discount", "increase"]
    assert regular[1]["discount_fraction"] == "0.50000000"
    assert regular[2]["subtotal"] == "120.00"  # anomalies are not "corrected"
    assert all(r["change"] == "reference" for r in analyzed if r not in regular)


def test_duplicate_quantity_conflicts_and_unknown_prices_are_not_discounts():
    rows = [
        {
            "product_id": "a",
            "quantity": 100,
            "subtotal": p,
            "configuration": {"quantity": 100},
        }
        for p in ("20", "22", None)
    ]
    result = analyze_price_breaks(rows)
    assert len(result) == 2
    assert result[1]["change"] == "same_quantity_conflict"
    assert not result[1]["price_break"]


def tariff_fixture():
    return {
        "currency": "USD",
        "weight_unit": "lb",
        "dimension_unit": "in",
        "dimensional_divisor": "100",
        "weight_increment": "1",
        "version": "fixture-1",
        "rates": [
            {
                "zone": "west",
                "service": "ground",
                "base": "5",
                "per_weight": "2",
                "handling": "1",
                "residential_surcharge": "3",
                "fuel_percent": "10",
            }
        ],
    }


def shipment_fixture():
    return {
        "zone": "west",
        "service": "ground",
        "residential": True,
        "weight_unit": "lb",
        "dimension_unit": "in",
        "packages": [{"weight": "8", "length": "10", "width": "10", "height": "10"}],
    }


def test_shipping_uses_dimensional_weight_and_keeps_estimate_status():
    result = quote_shipping(tariff_fixture(), shipment_fixture())
    assert result["status"] == "estimated"
    assert result["quote_type"] == "calculated_estimate"
    assert Decimal(result["billable_weight"]) == 10
    assert result["amount"] == "31.50"
    assert result["components"]["fuel"] == "2.50"


def test_shipping_missing_dimensions_or_unsupported_units_never_yields_price():
    inputs = shipment_fixture()
    del inputs["packages"][0]["width"]
    result = quote_shipping(tariff_fixture(), inputs)
    assert result["status"] == "missing_inputs"
    assert "packages[0].width" in result["missing_inputs"]
    assert result["amount"] is None
    result = quote_shipping(
        tariff_fixture(), {**shipment_fixture(), "weight_unit": "kg"}
    )
    assert result["status"] == "unsupported_units"
    assert result["amount"] is None


def test_shipping_cannot_choose_between_overlapping_rates():
    tariff = tariff_fixture()
    tariff["rates"] *= 2
    assert quote_shipping(tariff, shipment_fixture())["status"] == "unsupported_tariff"


def test_shipping_requires_rounding_rule_and_rejects_malformed_packages():
    tariff = tariff_fixture()
    del tariff["weight_increment"]
    result = quote_shipping(tariff, shipment_fixture())
    assert (
        result["status"] == "missing_inputs"
        and "tariff.weight_increment" in result["missing_inputs"]
    )
    assert (
        quote_shipping(
            tariff_fixture(), {**shipment_fixture(), "packages": ["not a package"]}
        )["status"]
        == "invalid_inputs"
    )


def test_friday_processing_cutoff_and_transit_are_separate():
    result = printograph_delivery_window(
        "2026-09-25T09:00:00-07:00", 2, approved=True, paid=True, transit_days=2
    )
    assert result["production_start"] == "2026-09-25"
    assert result["production_complete"] == {
        "earliest": "2026-09-28",
        "latest": "2026-09-28",
    }
    assert result["arrival"]["earliest"] == "2026-09-30"
    late = printograph_delivery_window(
        "2026-09-25T09:00:01-07:00", 2, approved=True, paid=True, transit_days=2
    )
    assert late["production_start"] == "2026-09-28"
    assert late["arrival"]["earliest"] == "2026-10-01"
    assert late["guaranteed"] is False


def test_production_prerequisites_and_rush_have_distinct_cutoffs():
    pending = printograph_delivery_window(
        "2026-09-25T08:00:00-07:00", 1, approved=False, paid=True
    )
    assert pending["status"] == "prerequisites_pending"
    assert pending["production_start"] is None
    assert (
        printograph_delivery_window(
            "2026-09-25T09:00:00-07:00",
            1,
            approved=True,
            paid=True,
            rush=True,
            ordered_at="2026-09-25T05:00:00-07:00",
        )["status"]
        == "estimate"
    )
    assert (
        printograph_delivery_window(
            "2026-09-25T09:00:00-07:00",
            1,
            approved=True,
            paid=True,
            rush=True,
            ordered_at="2026-09-25T05:00:01-07:00",
        )["status"]
        == "rush_cutoff_missed"
    )
    assert (
        printograph_delivery_window(
            "2026-09-25T09:00:01-07:00",
            1,
            approved=True,
            paid=True,
            rush=True,
            ordered_at="2026-09-25T04:00:00-07:00",
        )["status"]
        == "rush_cutoff_missed"
    )


def test_holidays_seasonal_extension_and_pickup():
    labor = printograph_delivery_window(
        "2026-09-04T08:00:00-07:00", 2, approved=True, paid=True
    )
    assert labor["production_complete"]["earliest"] == "2026-09-08"
    holiday_season = printograph_delivery_window(
        "2026-11-20T08:00:00-08:00", 2, approved=True, paid=True, pickup=True
    )
    assert holiday_season["seasonal_or_holiday_extra_days"] == 2
    assert holiday_season["production_complete"]["earliest"] == "2026-11-25"
    assert (
        holiday_season["arrival"]["earliest"] == "2026-11-27"
    )  # Thanksgiving excluded


def test_pacific_dst_and_indeterminate_design_service():
    winter = printograph_delivery_window(
        "2026-11-02T16:59:00Z", 1, approved=True, paid=True
    )
    assert winter["production_start"] == "2026-11-02"
    summer = printograph_delivery_window(
        "2026-09-25T16:01:00Z", 1, approved=True, paid=True
    )
    assert summer["production_start"] == "2026-09-28"
    uncertain = printograph_delivery_window(
        "2026-09-25T08:00:00-07:00",
        1,
        approved=True,
        paid=True,
        indeterminate_services=["design"],
    )
    assert uncertain["status"] == "indeterminate" and uncertain["arrival"] is None
    with pytest.raises(ValueError):
        printograph_delivery_window(
            datetime(2026, 9, 25, 8), 1, approved=True, paid=True
        )


@pytest.mark.parametrize(
    "size,qty",
    [("2 x 4", 1000), ("3.25 x 3.25", 1500), ("6.5 x 6", 501), ("3 x 3", 2000)],
)
def test_roll_packaging_ambiguity_is_not_turned_into_shipping_math(size, qty):
    result = roll_label_packaging(size, qty)
    assert result["status"] == "unresolved"
    assert result["roll_count"] is None
    assert len(result["rules"]) == 4
    assert not result["shipping_dimensions_inferred"]


def test_discount_grouping_uses_raw_values_and_keeps_known_service_context_separate():
    from crawler.commerce import analyze_price_breaks
    from test_service_context import service_observation

    base = {
        "product_url": "https://fixture/cards",
        "currency": "USD",
        "configuration": {"corners": {"value": "2", "label": "Round Corners (+$3.00)"}},
        "production": {"value": "1", "status": "source_displayed"},
        **service_observation(),
    }
    rows = [
        {**base, "quantity": 100, "subtotal": "11.54"},
        {
            **base,
            "quantity": 250,
            "subtotal": "14.38",
            "configuration": {
                "corners": {"value": "2", "label": "Round Corners (+$5.00)"}
            },
            "production": {"value": "1", "status": "requires_configuration_quote"},
        },
        {
            **base,
            **service_observation("100", delivery=False),
            "quantity": 500,
            "subtotal": "15.00",
        },
    ]
    result = analyze_price_breaks(rows)
    assert (
        next(row for row in result if row["quantity"] == "250")["change"] == "discount"
    )
    assert (
        next(row for row in result if row["quantity"] == "500")["change"] == "reference"
    )


def test_unknown_browser_service_context_never_implies_price_breaks():
    from crawler.commerce import analyze_price_breaks

    base = {
        "product_url": "https://fixture/cards",
        "currency": "USD",
        "configuration": {},
        "pricing_context": {"store_type": "broker", "source_product_type": "1"},
    }
    rows = [
        {**base, "quantity": 50, "subtotal": "5.60"},
        {**base, "quantity": 100, "subtotal": "6.60"},
    ]
    assert analyze_price_breaks(rows) == []


def test_shared_chart_context_is_semantic_and_returns_none_for_unresolved_services():
    from copy import deepcopy
    from crawler.commerce import price_comparison_context
    from test_service_context import service_observation

    row = {
        "configuration": {"corners": {"value": "2", "label": "+$3"}, "quantity": 50},
        "currency": "USD",
        "production": {"value": "1", "status": "source_displayed"},
        "pricing_context": {"source_base_product_id": "123"},
        **service_observation(),
    }
    changed = deepcopy(row)
    changed["configuration"]["corners"]["label"] = "+$5"
    changed["configuration"]["quantity"] = 250
    changed["production"]["status"] = "requires_configuration_quote"
    changed["pricing_context"].pop("source_base_product_id")
    changed["provenance"] = "source_table"
    assert price_comparison_context(row) == price_comparison_context(changed)
    changed["service_context_observed"] = False
    assert price_comparison_context(changed) is None


def test_legacy_printograph_quotes_are_excluded_from_current_price_breaks():
    row = {
        "product_url": "https://www.printograph.com/products/business-cards/order",
        "source_product_id": "1",
        "provenance": "direct_quote",
        "configuration": {},
        "quantity": 50,
        "subtotal": "5.60",
        "currency": "USD",
    }
    assert analyze_price_breaks([row]) == []
