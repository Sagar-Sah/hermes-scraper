"""Fail-closed review of one rendered Raised Print postcard supplier quote.

The controller and current form must describe the same customer selection.
This does not prove source-response freshness, checkout, or a whole price domain.
"""

from decimal import Decimal, InvalidOperation

from .price_contract import SOURCE_CONTRACT


SOURCE_URL = "https://www.printograph.com/products/postcards/order"
FIELD_BINDINGS = {
    "Size": "size",
    "Horizontal": "orientation",
    "Paper": "paper",
    "Color": "color",
    "Quantity": "quantity",
    "Rounded Corner": "roundedCorners",
    "Coating": "coating",
    "Spot High Gloss UV Coating": "spotCoating",
    "Raised Print": "raisedPrint",
    "Production Time": "productionTime",
}


def _field(fields, label):
    rows = [row for row in fields if row.get("label") == label]
    if len(rows) != 1 or rows[0].get("disabled") is True:
        raise ValueError("raised_quote_field_missing_or_disabled:" + label)
    return rows[0]


def _selected(field, source_value):
    selected = field.get("selected_values")
    if selected != [str(source_value)]:
        raise ValueError("raised_quote_rendered_selection_mismatch:" + field["label"])
    choices = [row for row in field.get("choices", [])
               if row.get("value") == str(source_value) and row.get("disabled") is False]
    if not choices:
        raise ValueError("raised_quote_choice_not_available:" + field["label"])
    labels = {row.get("label") for row in choices}
    if len(labels) != 1 or not next(iter(labels)):
        raise ValueError("raised_quote_choice_label_ambiguous:" + field["label"])
    return next(iter(labels))


def review_raised_scoped_quote(fields, scoped_price, *, source_url=SOURCE_URL):
    if source_url != SOURCE_URL or not isinstance(fields, list) or not isinstance(scoped_price, dict):
        raise ValueError("raised_quote_source_scope_invalid")
    if (scoped_price.get("status") != "observed"
            or scoped_price.get("source_bundle_verified") is not True
            or scoped_price.get("loaded_contract") != SOURCE_CONTRACT):
        raise ValueError("raised_quote_source_bundle_unverified")
    capture = scoped_price.get("capture") or {}
    reconciliation = scoped_price.get("reconciliation") or {}
    if (capture.get("observed") is not True or capture.get("settled") is not True
            or capture.get("capture_kind") != "controller"
            or capture.get("source_contract") != SOURCE_CONTRACT
            or capture.get("product_type") != "2"
            or capture.get("store_type") != "broker"
            or capture.get("currency") != "USD"
            or reconciliation.get("status") != "complete"
            or reconciliation.get("comparison") != "matched"):
        raise ValueError("raised_quote_price_not_reconciled")
    selected = capture.get("userSelectedOptions") or {}
    if (str(selected.get("paper")) != "10"
            or str(selected.get("raisedPrint")) not in {"135", "138"}
            or str(selected.get("coating")) != ""
            or str(selected.get("spotCoating")) != ""):
        raise ValueError("raised_quote_finish_scope_invalid")
    foil = str(selected.get("raisedPrint")) == "138"
    foil_color = str(selected.get("raisedFoilColor"))
    if (foil and foil_color not in {"141", "142", "143"}) or (not foil and foil_color != ""):
        raise ValueError("raised_quote_foil_color_scope_invalid")
    labels = {}
    for label, key in FIELD_BINDINGS.items():
        labels[key] = _selected(_field(fields, label), selected.get(key))
    if foil:
        labels["raisedFoilColor"] = _selected(_field(fields, "Foil Colors"), foil_color)
    if labels["productionTime"].lower().find("not available") >= 0:
        raise ValueError("raised_quote_production_unavailable")
    if str(selected.get("productId", "")).isdecimal() is False:
        raise ValueError("raised_quote_product_id_missing")
    try:
        quantity = int(selected["quantity"])
        amount = Decimal(str(reconciliation["subtotal"]))
    except (KeyError, ValueError, InvalidOperation, TypeError):
        raise ValueError("raised_quote_money_or_quantity_invalid") from None
    if quantity <= 0 or amount <= 0 or not amount.is_finite():
        raise ValueError("raised_quote_money_or_quantity_invalid")
    return {
        "schema": "hermes-printograph-raised-scoped-quote-review/v1",
        "storefront_product_id": "17413",
        "supplier_url": SOURCE_URL,
        "source_product_id": str(selected["productId"]),
        "source_selection": {key: str(selected[key]) for key in (
            "size", "orientation", "paper", "color", "quantity", "roundedCorners",
            "coating", "spotCoating", "raisedPrint", "raisedFoilColor", "productionTime")},
        "rendered_labels": labels,
        "supplier_total_excluding_shipping": format(amount, ".2f"),
        "proof_and_service_controller_context": {key: str(selected.get(key, ""))
                                                for key in ("proofType", "manualProofType",
                                                            "designServices", "fileDelivery")},
        "production_status": "rendered_current_choice",
        "source_bundle_verified": True,
        "source_response_freshness_verified": False,
        "service_policy_status": "controller_only_not_rendered_verified",
        "scope": "one_current_rendered_selection_only",
        "final_eligible": False,
    }
