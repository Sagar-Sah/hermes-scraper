from crawler.fourover_probe import price_tiers, area_tiers


def test_price_tiers_requires_finite_unique_quantities():
    response = [{'packagePrices': [{'qty': 100, 'price': 23.5}, {'qty': 250, 'price': 42.0}]}]
    assert price_tiers(response) == [{'quantity': 100, 'price': 23.5}, {'quantity': 250, 'price': 42.0}]
    assert price_tiers([{'packagePrices': [{'qty': 100, 'price': 2}, {'qty': 100, 'price': 3}]}]) is None
    assert price_tiers([{'packagePrices': [{'qty': 0, 'price': 2}]}]) is None


def test_area_tiers_are_separate_from_fixed_package_prices():
    assert area_tiers([{'tierPrices': [{'qty': 100, 'price': 0.5}]}]) == [{'quantity': 100, 'price': 0.5}]
    assert area_tiers([{'tierPrices': [{'qty': 100, 'price': -1}]}]) is None
