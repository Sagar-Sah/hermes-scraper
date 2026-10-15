import pytest

from crawler.fourover_exact_quote import summarize_gator_quote


def pilot(sets=3):
    subtotal = 23.13 if sets == 3 else 30.84
    fee = 3.75 if sets == 3 else 0
    total = subtotal + fee
    per_set = 7.71 if sets == 3 else 30.84
    return {
        "product_id": "6742", "selected_child_id": "58462", "width": 12,
        "height": 18, "sets": sets,
        "dimension_control": {
            "width": {"present": True, "value": "12", "valid": True, "enabled": True},
            "height": {"present": True, "value": "18", "valid": True, "enabled": True}},
        "set_control": {"value": str(sets), "min": "1", "max": "200", "valid": True},
        "selected_services": {"job_sample": False, "digital_proof": False},
        "project_response_shapes": [{"status": 200, "totals": {
            "subtotal": subtotal, "grand_total": total, "small_qty_fee": fee}}],
        "shipping_view": {"money": [f"Price Per Set: ${per_set:.2f}",
            f"Subtotal ${subtotal:.2f} Total: ${total:.2f} Taxes may apply"]},
        "shipping_response_shapes": [{"status": 200, "rates": [
            {"available": True, "production_estimate": "2026-10-01"}]}],
    }


def quote(value):
    return summarize_gator_quote(value, product_id="6742", child_id="58462",
                                 width=12, height=18, sets=3)


def test_exact_scoped_gator_quote_preserves_fee_and_production():
    result = quote(pilot())
    assert result["supplier_total_excluding_shipping"] == "26.88"
    assert result["small_quantity_fee"] == "3.75"
    assert result["production_estimates"] == ["2026-10-01"]
    assert result["production_detail_status"] == "observed"
    assert result["source_fulfillment_available"] is True


def test_exact_scoped_gator_quote_requires_available_fulfillment_rate():
    value = pilot()
    value["shipping_response_shapes"][0]["rates"] = [{"available": False}]
    with pytest.raises(ValueError, match="supplier_fulfillment_unavailable"):
        quote(value)


def test_exact_scoped_gator_quote_flags_missing_production_without_estimating():
    value = pilot()
    value["shipping_response_shapes"][0]["rates"] = [{"available": True}]
    result = quote(value)
    assert result["production_estimates"] == []
    assert result["production_detail_status"] == "missing_at_supplier"


@pytest.mark.parametrize("change", [
    lambda p: p["set_control"].update(value="200"),
    lambda p: p["dimension_control"]["width"].update(value="13"),
    lambda p: p["dimension_control"]["height"].update(enabled=False),
    lambda p: p["project_response_shapes"][0]["totals"].update(grand_total=23.13),
    lambda p: p["shipping_view"].update(money=["Price Per Set: $7.71", "Total: $23.13"]),
    lambda p: p.update(selected_services={"job_sample": True, "digital_proof": False}),
])
def test_exact_scoped_gator_quote_rejects_mismatched_source_state(change):
    value = pilot()
    change(value)
    with pytest.raises(ValueError):
        quote(value)
