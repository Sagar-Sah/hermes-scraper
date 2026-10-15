"""Deterministic commerce calculations. Observations and estimates stay distinct."""

from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .service_context import (
    canonical_service_context,
    requires_service_context,
    service_context_is_bound,
)
from .record_scope import is_legacy_unscoped

ZERO_DECIMAL = frozenset(
    "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split()
)
THREE_DECIMAL = frozenset("BHD IQD JOD KWD LYD OMR TND".split())
FOUR_DECIMAL = frozenset("CLF UYW".split())
TWO_DECIMAL = frozenset(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHF CNY COP COU CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS UAH USD USN UYU UZS VED VES WST XCD YER ZAR ZMW ZWG".split()
)


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Expected a finite decimal value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Expected a finite decimal value") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite decimal value")
    return result


def currency_places(currency: str) -> int:
    code = currency.upper()
    if code in ZERO_DECIMAL:
        return 0
    if code in THREE_DECIMAL:
        return 3
    if code in FOUR_DECIMAL:
        return 4
    if code in TWO_DECIMAL:
        return 2
    raise ValueError(
        f"Unsupported currency {currency!r}; supply a supported ISO currency"
    )


def money(value: Any, currency: str = "USD") -> str:
    """Format decimal money using explicit currency precision and half-up rounding."""
    places = currency_places(currency)
    return format(
        decimal_value(value).quantize(
            Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
        ),
        f".{places}f",
    )


def unit_price(subtotal: Any, quantity: Any, *, precision: int = 8) -> str:
    count = decimal_value(quantity)
    if count <= 0:
        raise ValueError("Quantity must be positive")
    if not 0 <= precision <= 18:
        raise ValueError("Precision must be between 0 and 18")
    return format(
        (decimal_value(subtotal) / count).quantize(
            Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP
        ),
        "f",
    )


def configuration(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("configuration", row.get("config", row.get("options", {})))
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        raise ValueError("Configuration must be an object")
    return dict(raw)


def semantic_configuration(row: Mapping[str, Any]) -> dict[str, Any]:
    """Compare selected raw values, retaining the difference between absent/empty."""
    return {
        key: (
            value["value"] if isinstance(value, Mapping) and "value" in value else value
        )
        for key, value in configuration(row).items()
    }


def semantic_production(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in ("value", "mode", "id", "production_mode"):
        if key in value:
            return {"selection": str(value[key])}
    return {key: item for key, item in value.items() if key not in {"label", "status"}}


def semantic_pricing_context(row: Mapping[str, Any]) -> Any:
    context = row.get("pricing_context")
    if context is None and not requires_service_context(row):
        return None
    result = {
        key: value
        for key, value in (context or {}).items()
        if key != "source_base_product_id"
    }
    if requires_service_context(row):
        result["service_context"] = canonical_service_context(row)
    return result


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def price_comparison_context(
    row: Mapping[str, Any],
    *,
    quantity_attributes: Iterable[str] = ("quantity", "runsize", "run_size"),
) -> dict[str, Any] | None:
    """Shared API/chart/analysis grouping; unresolved services never imply a curve."""
    if is_legacy_unscoped(row) or (
        requires_service_context(row)
        and not service_context_is_bound(canonical_service_context(row))
    ):
        return None
    excluded = {str(key).casefold() for key in quantity_attributes}
    return {
        "product": row.get("product_url")
        or row.get("source_product_id")
        or row.get("product_id"),
        "configuration": {
            k: v
            for k, v in semantic_configuration(row).items()
            if k.casefold() not in excluded
        },
        "currency": str(row.get("currency", "USD")).upper(),
        "production": semantic_production(row.get("production")),
        "pricing_context": semantic_pricing_context(row),
        "account_context": row.get("account_context"),
        "tax_basis": row.get("tax_basis"),
        "quantity_unit": row.get("quantity_unit", "physical_unit"),
        "units_per_pack": row.get("units_per_pack"),
        "discount_context": row.get("discount_context"),
        "services": row.get("services"),
        "snapshot_id": row.get("snapshot_id"),
    }


def analyze_price_breaks(
    rows: Iterable[Mapping[str, Any]],
    *,
    quantity_attributes: Iterable[str] = ("quantity", "runsize", "run_size"),
) -> list[dict[str, Any]]:
    """Compare observed total/unit prices only within equivalent contexts.

    Callers must name any configuration attributes that encode the physical-unit
    quantity. No other option is removed. Missing subtotals produce no estimate.
    """
    quantity_attributes = tuple(quantity_attributes)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incoming in rows:
        row = dict(incoming)
        context = price_comparison_context(row, quantity_attributes=quantity_attributes)
        if context is None:
            continue
        subtotal = row.get("subtotal", row.get("price"))
        if subtotal is None or row.get("quantity") is None:
            continue
        quantity = decimal_value(row.get("physical_units", row["quantity"]))
        total = decimal_value(subtotal)
        if quantity <= 0 or total < 0:
            raise ValueError(
                "Price analysis requires positive quantities and nonnegative subtotals"
            )
        groups[_json(context)].append(
            {"row": row, "quantity": quantity, "total": total, "unit": total / quantity}
        )
    results = []
    for group_key in sorted(groups):
        observations = sorted(
            groups[group_key],
            key=lambda r: (r["quantity"], r["total"], _json(r["row"])),
        )
        base = observations[0]["unit"]
        previous = None
        for observation in observations:
            q, total, unit = (
                observation["quantity"],
                observation["total"],
                observation["unit"],
            )
            same_quantity_conflict = (
                previous is not None
                and q == previous["quantity"]
                and total != previous["total"]
            )
            change = (
                "reference"
                if previous is None
                else (
                    "same_quantity_conflict"
                    if same_quantity_conflict
                    else (
                        "discount"
                        if unit < previous["unit"]
                        else "increase" if unit > previous["unit"] else "unchanged"
                    )
                )
            )
            results.append(
                {
                    "context": json.loads(group_key),
                    "quantity": format(q, "f"),
                    "subtotal": money(total, json.loads(group_key)["currency"]),
                    "unit_price": unit_price(total, q),
                    "discount_fraction": (
                        None
                        if base == 0
                        else format(
                            (1 - unit / base).quantize(Decimal("0.00000001")), "f"
                        )
                    ),
                    "change": change,
                    "price_break": change == "discount"
                    and previous is not None
                    and q != previous["quantity"],
                    "verification_status": observation["row"].get(
                        "verification_status", "provisional"
                    ),
                    "evidence_ids": observation["row"].get("evidence_ids", []),
                }
            )
            previous = observation
    return results


def quote_shipping(
    tariff: Mapping[str, Any], inputs: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate a supplied tariff, never invent its rates or package dimensions.

    Tariff: currency, weight_unit, rates [{zone, service, base,
    per_weight, per_package, handling, residential_surcharge, fuel_percent}],
    optional dimensional_divisor + dimension_unit, weight_increment and version.
    Inputs: zone, service, residential (bool), packages [{weight, length, width,
    height}], weight_unit; dimension_unit is required for dimensional weight.
    Every rate component is explicit; absent optional components mean zero.
    This computes one declared shipment/cart, never a sum of unrelated quotes.
    """
    result: dict[str, Any] = {
        "status": "missing_inputs",
        "quote_type": "calculated_estimate",
        "amount": None,
        "currency": tariff.get("currency"),
        "tariff_version": tariff.get("version"),
        "missing_inputs": [],
    }
    for key in ("currency", "weight_unit", "rates", "weight_increment"):
        if tariff.get(key) is None:
            result["missing_inputs"].append(f"tariff.{key}")
    for key in ("zone", "service", "residential", "packages", "weight_unit"):
        if inputs.get(key) is None:
            result["missing_inputs"].append(key)
    if tariff.get("dimensional_divisor") is not None:
        for label, mapping in (
            ("tariff.dimension_unit", tariff),
            ("dimension_unit", inputs),
        ):
            if not mapping.get("dimension_unit"):
                result["missing_inputs"].append(label)
    if result["missing_inputs"]:
        return result
    if inputs["weight_unit"] != tariff["weight_unit"] or (
        tariff.get("dimensional_divisor") is not None
        and inputs.get("dimension_unit") != tariff.get("dimension_unit")
    ):
        return {
            **result,
            "status": "unsupported_units",
            "reason": "Tariff and shipment units must match; conversion must be explicit",
        }
    if not isinstance(tariff["rates"], (list, tuple)) or any(
        not isinstance(rate, Mapping) for rate in tariff["rates"]
    ):
        return {
            **result,
            "status": "invalid_inputs",
            "reason": "Tariff rates must be objects in a list",
        }
    rates = [
        r
        for r in tariff["rates"]
        if r.get("zone") == inputs["zone"] and r.get("service") == inputs["service"]
    ]
    if len(rates) != 1:
        return {
            **result,
            "status": "unsupported_tariff",
            "reason": "Expected exactly one matching zone/service rate",
        }
    packages = inputs["packages"]
    if (
        not isinstance(packages, (list, tuple))
        or not packages
        or any(not isinstance(package, Mapping) for package in packages)
        or not isinstance(inputs["residential"], bool)
    ):
        return {
            **result,
            "status": "invalid_inputs",
            "reason": "Provide packages and a boolean residential flag",
        }
    billable = Decimal(0)
    package_weights = []
    try:
        divisor = (
            decimal_value(tariff["dimensional_divisor"])
            if tariff.get("dimensional_divisor") is not None
            else None
        )
        increment = decimal_value(tariff["weight_increment"])
        if increment <= 0 or (divisor is not None and divisor <= 0):
            raise ValueError(
                "Weight increment and dimensional divisor must be positive"
            )
        for index, package in enumerate(packages):
            if package.get("weight") is None:
                result["missing_inputs"].append(f"packages[{index}].weight")
                continue
            weight = decimal_value(package["weight"])
            if weight <= 0:
                raise ValueError("Package weight must be positive")
            dimensional = Decimal(0)
            if divisor is not None:
                missing_dimensions = [
                    d for d in ("length", "width", "height") if package.get(d) is None
                ]
                if missing_dimensions:
                    result["missing_inputs"].extend(
                        f"packages[{index}].{d}" for d in missing_dimensions
                    )
                    continue
                dimensions = [
                    decimal_value(package[d]) for d in ("length", "width", "height")
                ]
                if any(d <= 0 for d in dimensions):
                    raise ValueError("Package dimensions must be positive")
                dimensional = dimensions[0] * dimensions[1] * dimensions[2] / divisor
            from decimal import ROUND_CEILING

            charged = (max(weight, dimensional) / increment).to_integral_value(
                rounding=ROUND_CEILING
            ) * increment
            package_weights.append(format(charged, "f"))
            billable += charged
        if result["missing_inputs"]:
            return result
        rate = rates[0]
        if rate.get("base") is None:
            return {**result, "missing_inputs": ["tariff.rate.base"]}
        components = {
            k: decimal_value(rate.get(k, 0))
            for k in (
                "base",
                "per_weight",
                "per_package",
                "handling",
                "residential_surcharge",
                "fuel_percent",
            )
        }
        if any(value < 0 for value in components.values()):
            raise ValueError("Tariff components must be nonnegative")
        if rate.get("max_weight") is not None and billable > decimal_value(
            rate["max_weight"]
        ):
            return {
                **result,
                "status": "unsupported_tariff",
                "reason": "Shipment exceeds the rate's supported weight",
            }
        transport = (
            components["base"]
            + components["per_weight"] * billable
            + components["per_package"] * len(packages)
        )
        # Fuel applies to transport only; handling and residential remain separate.
        fuel = transport * components["fuel_percent"] / 100
        surcharge = (
            components["residential_surcharge"] if inputs["residential"] else Decimal(0)
        )
        amount = transport + fuel + components["handling"] + surcharge
        return {
            **result,
            "status": "estimated",
            "amount": money(amount, tariff["currency"]),
            "billable_weight": format(billable, "f"),
            "package_billable_weights": package_weights,
            "components": {
                "transport": money(transport, tariff["currency"]),
                "fuel": money(fuel, tariff["currency"]),
                "handling": money(components["handling"], tariff["currency"]),
                "residential": money(surcharge, tariff["currency"]),
            },
            "context": dict(inputs),
            "calculation_basis": "Per-package max(actual, dimensional), rounded upward by weight_increment; fuel on transport only",
        }
    except (ValueError, TypeError, KeyError) as exc:
        return {**result, "status": "invalid_inputs", "reason": str(exc)}


PACIFIC = ZoneInfo("America/Los_Angeles")


def _timestamp(value: datetime | str) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("Timestamps must include an explicit time zone")
    return parsed.astimezone(PACIFIC)


def _holidays(year: int) -> set[date]:
    # Source-listed holidays, on actual dates. Facility-observed substitutes must
    # be supplied through extra holidays instead of being silently assumed.
    may_last = date(year, 5, calendar.monthrange(year, 5)[1])
    memorial = may_last - timedelta(days=may_last.weekday())
    september = date(year, 9, 1)
    labor = september + timedelta(days=(-september.weekday()) % 7)
    november = date(year, 11, 1)
    thanksgiving = november + timedelta(days=(3 - november.weekday()) % 7 + 21)
    return {
        date(year, 1, 1),
        memorial,
        date(year, 7, 4),
        labor,
        thanksgiving,
        date(year, 12, 25),
    }


def printograph_delivery_window(
    processed_at: datetime | str,
    production_days: int | tuple[int, int] | list[int],
    *,
    approved: bool,
    paid: bool,
    rush: bool = False,
    ordered_at: datetime | str | None = None,
    transit_days: int | tuple[int, int] | list[int] | None = None,
    pickup: bool = False,
    holidays: Iterable[date | str] = (),
    indeterminate_services: Iterable[str] = (),
    calendar_version: str = "printograph-published-v1",
) -> dict[str, Any]:
    """Return a conditional range, not a delivery guarantee or carrier quote.

    The first eligible production date is counted as production day one. Source
    seasonal extensions are applied once, based on the processing date or start
    date; weekends and published holiday dates are excluded. Additional facility
    closure dates can be supplied explicitly. Transit day one is after shipment.
    """
    processed = _timestamp(processed_at)
    extra_holidays = {
        date.fromisoformat(h) if isinstance(h, str) else h for h in holidays
    }
    result: dict[str, Any] = {
        "status": "estimate",
        "timezone": "America/Los_Angeles",
        "calendar_version": calendar_version,
        "production_start": None,
        "production_complete": None,
        "arrival": None,
        "guaranteed": False,
        "source_url": "https://www.printograph.com/resources/production-time.html",
    }
    if not approved or not paid:
        return {
            **result,
            "status": "prerequisites_pending",
            "missing_prerequisites": [
                name
                for name, satisfied in (("approved", approved), ("paid", paid))
                if not satisfied
            ],
        }
    services = list(indeterminate_services)
    if services:
        return {
            **result,
            "status": "indeterminate",
            "reason": "Design or mailing services prevent a supported arrival estimate",
            "services": services,
        }

    def business_day(day: date) -> bool:
        return (
            day.weekday() < 5
            and day not in _holidays(day.year)
            and day not in extra_holidays
        )

    def next_business(day: date) -> date:
        while not business_day(day):
            day += timedelta(days=1)
        return day

    def advance(day: date, count: int) -> date:
        for _ in range(count):
            day = next_business(day + timedelta(days=1))
        return day

    def span(value: Any, minimum: int) -> tuple[int, int]:
        pair = (
            (value, value)
            if isinstance(value, int) and not isinstance(value, bool)
            else tuple(value)
        )
        if (
            len(pair) != 2
            or any(not isinstance(v, int) or isinstance(v, bool) for v in pair)
            or not minimum <= pair[0] <= pair[1]
        ):
            raise ValueError("Day ranges must be ordered integer bounds")
        return pair

    production_range = span(production_days, 1)
    if rush:
        if ordered_at is None:
            return {
                **result,
                "status": "missing_inputs",
                "missing_inputs": ["ordered_at"],
            }
        ordered = _timestamp(ordered_at)
        if ordered > processed:
            raise ValueError("Order placement cannot be after processing")
        eligible = (
            business_day(ordered.date())
            and ordered.timetz().replace(tzinfo=None) <= time(5)
            and processed.date() == ordered.date()
            and processed.timetz().replace(tzinfo=None) <= time(9)
        )
        if not eligible:
            return {
                **result,
                "status": "rush_cutoff_missed",
                "reason": "Rush requires placement by 05:00 PT and approval/payment/processing by 09:00 PT on the same business day",
            }
    start = processed.date()
    if not business_day(start) or processed.timetz().replace(tzinfo=None) > time(9):
        start = next_business(start + timedelta(days=1))
    seasonal = (
        lambda day: (day.month == 11 and day.day >= 20)
        or day.month == 12
        or (day.month == 1 and day.day <= 10)
    )
    extension = (
        2
        if seasonal(processed.date())
        or seasonal(start)
        or processed.date() in _holidays(processed.year)
        or processed.date() in extra_holidays
        else 0
    )
    complete = (
        advance(start, production_range[0] + extension - 1),
        advance(start, production_range[1] + extension - 1),
    )
    result.update(
        {
            "production_start": start.isoformat(),
            "production_complete": {
                "earliest": complete[0].isoformat(),
                "latest": complete[1].isoformat(),
            },
            "seasonal_or_holiday_extra_days": extension,
            "production_days": list(production_range),
            "calculation_basis": "Production start counts as day one; PT cutoffs inclusive; business days exclude weekends and supplied/source-listed holidays",
            "limitations": [
                "Observed substitute holiday dates and unannounced facility closures require a calendar update",
                "Production and carrier delays are not predicted",
                "Processing and artwork approval must already be complete",
            ],
        }
    )
    if pickup:
        result["arrival"] = {
            "earliest": advance(complete[0], 1).isoformat(),
            "latest": advance(complete[1], 1).isoformat(),
            "type": "pickup_ready_estimate",
        }
    elif transit_days is not None:
        transit = span(transit_days, 0)
        result["arrival"] = {
            "earliest": advance(complete[0], transit[0]).isoformat(),
            "latest": advance(complete[1], transit[1]).isoformat(),
            "type": "delivery_estimate",
        }
        result["transit_days"] = list(transit)
    return result


ROLL_LABEL_RULES = (
    '0.5" x 0.5" - 3" x 3" - Up to 1000 - 1 Roll',
    '3.5" x 3.5" - 6" x 6.5" - Up to 500 - 1 Roll',
    '0.5" x 0.5" - 3" x 3" - 1000 and plus - 1 Roll every 1000 qty + additional roll for every 500 qty',
    '3.5" x 3.5" - 6" x 6.5" - 500 and plus - 1 Roll every 500 qty',
)


def roll_label_packaging(
    size: Any, quantity: Any, rules: Iterable[str] | None = None
) -> dict[str, Any]:
    if decimal_value(quantity) <= 0:
        raise ValueError("Label quantity must be positive")
    return {
        "status": "unresolved",
        "size": size,
        "quantity": str(quantity),
        "quantity_unit": "labels",
        "roll_count": None,
        "rules": list(ROLL_LABEL_RULES if rules is None else rules),
        "core_diameter_inches": "3",
        "applies_to": "all shapes and materials",
        "source_url": "https://www.printograph.com/products/roll-labels/order",
        "ambiguities": [
            "Size-band classification for nonsquare, rotated, and intermediate dimensions",
            "Overlapping inclusive quantity boundaries",
            "Remainders and the interaction of 1000-label and 500-label roll wording",
        ],
        "shipping_dimensions_inferred": False,
    }
