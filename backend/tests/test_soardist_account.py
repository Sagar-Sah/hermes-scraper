from crawler.soardist_account import exact_product_page, observed_price


def test_account_price_requires_one_unambiguous_visible_money_value():
    assert observed_price({'prices': ['Regular price $359.00'], 'login_prompt': False}) == '359.00'
    assert observed_price({'prices': ['$1,359.50'], 'login_prompt': False}) == '1359.50'
    assert observed_price({'prices': ['$359.00'], 'login_prompt': True}) is None
    assert observed_price({'prices': ['$359.00', '$399.00'], 'login_prompt': False}) is None
    assert observed_price({'prices': ['$359.00 - $399.00'], 'login_prompt': False}) is None


def test_account_price_requires_exact_product_path():
    expected = 'https://soardistusa.com/products/tent'
    assert exact_product_page('https://www.soardistusa.com/products/tent/', expected)
    assert not exact_product_page('https://soardistusa.com/products/other', expected)
    assert not exact_product_page('https://soardistusa.com/products/tent?variant=1', expected)
