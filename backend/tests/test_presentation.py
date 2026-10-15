from copy import deepcopy

import pytest

from crawler.engine import row_identity
from crawler.presentation import present_row


def corner_row(provenance="source_table"):
    return {
        "id": "immutable-row-id",
        "product_url": "https://fixture.test/cards",
        "source_product_id": "100000021",
        "title": "Cards",
        "configuration": {
            "paper": {"value": "1", "label": "Gloss"},
            "roundedCorners": {"value": "2", "label": "Round Corners (+$3.00 )"},
        },
        "quantity": 250,
        "currency": "USD",
        "subtotal": "14.45",
        "unit_price": "0.05780000",
        "provenance": provenance,
        "verification_status": "pending",
        "evidence_ids": ["base", "corner", "anchor"],
        "production": {"value": "1", "status": "requires_configuration_quote"},
        "table_derivation": {
            "components": [
                {"option_id": None, "markup_price": "9.45", "evidence_id": "base"},
                {
                    "option_id": "100000104",
                    "markup_price": "5.00",
                    "evidence_id": "corner",
                },
            ],
            "sampled_quantities": [50, 100000],
        },
    }


def test_derived_label_projection_preserves_original_identity_money_and_evidence():
    original = corner_row()
    before = deepcopy(original)
    projected = present_row(original)
    assert original == before
    assert projected is not original
    assert projected["configuration"] is not original["configuration"]
    assert projected["configuration"]["roundedCorners"] == {
        "value": "2",
        "label": "Round Corners",
        "source_label": "Round Corners (+$3.00 )",
    }
    assert row_identity(projected) == row_identity(original)
    for key in (
        "id",
        "subtotal",
        "unit_price",
        "table_derivation",
        "evidence_ids",
        "verification_status",
        "production",
    ):
        assert projected[key] == original[key]
    assert present_row(projected) == projected


@pytest.mark.parametrize("provenance", ["direct_quote", "api", {"method": "table"}])
def test_direct_and_unrelated_provenance_labels_are_unchanged(provenance):
    original = corner_row(provenance)
    assert present_row(original) == original


@pytest.mark.parametrize(
    "label,value",
    [
        ("Round Corners", "2"),
        ("None", ""),
        ("Round Corners (+$3.00 )", "9"),
        ("A different finish (+$3.00 )", "2"),
        ("Round Corners (+$unknown)", "2"),
    ],
)
def test_projection_only_changes_known_quantity_annotation(label, value):
    original = corner_row()
    original["configuration"]["roundedCorners"] = {"label": label, "value": value}
    assert present_row(original) == original
