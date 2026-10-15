"""A saved source quote must stay bound to the visible customer selection."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from crawler.printograph_raised_quote import review_raised_scoped_quote


ROOT = Path(__file__).resolve().parents[4]
SAMPLE = ROOT / "work/hermes-raised-print-q500-silver-vertical-4over4-rounded-bundle-verified-20260928.json"


def sample():
    data = json.loads(SAMPLE.read_text())
    return data["observed_fields"], data["scoped_price"]


def test_saved_quote_binds_rendered_size_color_quantity_foil_and_corner():
    fields, price = sample()
    review = review_raised_scoped_quote(fields, price)
    assert review["source_selection"]["size"] == "110"
    assert review["source_selection"]["orientation"] == "2"
    assert review["source_selection"]["color"] == "3"
    assert review["source_selection"]["quantity"] == "500"
    assert review["source_selection"]["raisedFoilColor"] == "143"
    assert review["source_selection"]["roundedCorners"] == "2"
    assert review["supplier_total_excluding_shipping"] == "89.69"
    assert review["final_eligible"] is False
    assert review["source_response_freshness_verified"] is False


@pytest.mark.parametrize("label,value", [
    ("Size", "111"), ("Horizontal", "1"), ("Color", "1"), ("Quantity", "250"),
    ("Foil Colors", "141"), ("Rounded Corner", ""),
])
def test_rejects_rendered_selection_that_differs_from_priced_controller(label, value):
    fields, price = sample()
    field = next(row for row in fields if row["label"] == label)
    field["selected_values"] = [value]
    with pytest.raises(ValueError, match="rendered_selection_mismatch"):
        review_raised_scoped_quote(fields, price)


def test_rejects_disabled_choice_and_unverified_bundle():
    fields, price = sample()
    next(row for row in fields if row["label"] == "Quantity")["disabled"] = True
    with pytest.raises(ValueError, match="missing_or_disabled"):
        review_raised_scoped_quote(fields, price)
    fields, price = sample()
    price["source_bundle_verified"] = False
    with pytest.raises(ValueError, match="source_bundle_unverified"):
        review_raised_scoped_quote(fields, price)


def test_rejects_price_from_unrelated_finish_or_nonreconciled_total():
    fields, price = sample()
    price["capture"]["userSelectedOptions"]["raisedPrint"] = 0
    with pytest.raises(ValueError, match="finish_scope_invalid"):
        review_raised_scoped_quote(fields, price)
    fields, price = sample()
    price["reconciliation"]["comparison"] = "mismatched"
    with pytest.raises(ValueError, match="price_not_reconciled"):
        review_raised_scoped_quote(fields, price)
