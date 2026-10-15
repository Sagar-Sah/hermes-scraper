from decimal import Decimal

import pytest

from crawler.adapters.browser import observed_amount
from crawler.adapters.printograph import AdapterError


@pytest.mark.parametrize(
    "text",
    [
        "-$5.60",
        "$-5.60",
        "− USD 5.60",
        "-usd5.60",
        "($5.60)",
        "(5.60 USD)",
        "Subtotal: (1,250.50)",
    ],
)
def test_negative_and_accounting_totals_cannot_become_positive_prices(text):
    with pytest.raises(AdapterError, match="Negative or accounting-style"):
        observed_amount({"price": text})


@pytest.mark.parametrize(
    "text,amount",
    [
        ("$5.60", "5.60"),
        ("Subtotal (excludes shipping): $1,250.50", "1250.50"),
        ("USD 5.60", "5.60"),
    ],
)
def test_positive_subtotal_and_descriptive_parentheses_still_parse(text, amount):
    assert observed_amount({"price": text}) == Decimal(amount)
