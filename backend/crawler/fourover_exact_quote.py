"""Reduce one rendered 4over Gator Board pilot to an exact scoped quote.

This module does not infer prices for uncaptured dimensions, sets, or finishes.
"""

from decimal import Decimal, InvalidOperation
import re


def _money(value):
    try:
        amount = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError("supplier_money_invalid") from None
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise ValueError("supplier_money_invalid")
    return amount


def summarize_gator_quote(pilot, *, product_id, child_id, width, height, sets):
    if (pilot.get("product_id") != product_id or str(pilot.get("selected_child_id")) != str(child_id)
            or pilot.get("width") != width or pilot.get("height") != height
            or pilot.get("sets") != sets):
        raise ValueError("supplier_selection_mismatch")
    dimensions = pilot.get("dimension_control") or {}
    for name, requested in (("width", width), ("height", height)):
        if dimensions.get(name) != {"present": True, "value": str(requested),
                                    "valid": True, "enabled": True}:
            raise ValueError("supplier_dimensions_not_retained")
    control = pilot.get("set_control") or {}
    if (control.get("value") != str(sets) or not control.get("valid")
            or control.get("min") != "1" or control.get("max") != "200"):
        raise ValueError("supplier_set_count_not_retained")
    if pilot.get("selected_services") != {"job_sample": False, "digital_proof": False}:
        raise ValueError("supplier_service_scope_changed")
    summaries = [item.get("totals") for item in pilot.get("project_response_shapes", [])
                 if item.get("status") == 200 and isinstance(item.get("totals"), dict)]
    if not summaries:
        raise ValueError("supplier_project_total_missing")
    totals = summaries[-1]
    subtotal = _money(totals.get("subtotal"))
    grand = _money(totals.get("grand_total"))
    fee = _money(totals.get("small_qty_fee", 0))
    if subtotal + fee != grand:
        raise ValueError("supplier_total_components_disagree")
    money = pilot.get("shipping_view", {}).get("money", [])
    per_set_values = {match.group(1) for item in money
                      if (match := re.fullmatch(r"Price Per Set:\s*\$([0-9,]+\.[0-9]{2})", item))}
    displayed_totals = {match.group(1) for item in money
                        if (match := re.search(r"\bTotal:\s*\$([0-9,]+\.[0-9]{2})", item))}
    if len(per_set_values) != 1 or len(displayed_totals) != 1:
        raise ValueError("supplier_displayed_total_ambiguous")
    per_set = _money(next(iter(per_set_values)))
    displayed = _money(next(iter(displayed_totals)))
    # The UI rounds its per-set label to cents; source subtotals may multiply
    # a more precise underlying rate before rounding the final amount.
    rounding_bound = Decimal("0.005") * sets + Decimal("0.01")
    if abs(per_set * sets - subtotal) > rounding_bound or displayed != grand:
        raise ValueError("supplier_displayed_total_disagrees")
    latest_rates = next((item.get("rates") for item in reversed(pilot.get("shipping_response_shapes", []))
                         if item.get("status") == 200 and isinstance(item.get("rates"), list)), [])
    available_rates = [rate for rate in latest_rates if isinstance(rate, dict) and rate.get("available") is True]
    if not available_rates:
        raise ValueError("supplier_fulfillment_unavailable")
    production = sorted({rate.get("production_estimate") for rate in available_rates
                         if rate.get("production_estimate")})
    return {"status": "exact_scoped_quote", "product_id": product_id,
            "source_url": "https://4over.com/gator-board-signs", "child_id": str(child_id),
            "width_in": width, "height_in": height, "sets": sets,
            "set_control": {"min": 1, "max": 200, "retained": True},
            "price_per_set": f"{per_set:.2f}", "print_subtotal": f"{subtotal:.2f}",
            "small_quantity_fee": f"{fee:.2f}", "supplier_total_excluding_shipping": f"{grand:.2f}",
            "production_estimates": production,
            "production_detail_status": "observed" if production else "missing_at_supplier",
            "source_fulfillment_available": True,
            "shipping": "supplier_reference_only",
            "scope": "Only this rendered child, size, service-off selection and set count; no other price is implied."}
