import json

import pytest

from crawler.shopify_variants import product_json_url, project_product_json


def test_soardist_public_variant_projection_is_explicitly_unverified():
    source = 'https://soardistusa.com/products/tent-wall'
    body = json.dumps({'id': 101, 'title': 'Tent Wall',
        'options': [{'name': 'Graphic', 'values': ['Single', 'Double']}],
        'variants': [{'id': 201, 'title': 'Single', 'price': 20000, 'available': True,
                      'options': ['Single'], 'requires_shipping': True,
                      'quantity_price_breaks': [{'minimum_quantity': 10, 'price': 18000}]}],
        'images': ['//cdn.shopify.com/image.jpg']}).encode()
    result = project_product_json(body, source)
    assert product_json_url(source) == source + '.js'
    assert result['variants'][0]['public_unit_price'] == '200'
    assert result['variants'][0]['quantity_price_breaks'] == [
        {'minimum_quantity': 10, 'public_unit_price': '180'}]
    assert result['price_basis'] == 'public_shopify_product_json_unverified_account_price'
    assert result['images'] == ['https://cdn.shopify.com/image.jpg']
    assert result['final_eligible'] is False


def test_public_variant_route_and_malformed_prices_are_rejected():
    with pytest.raises(ValueError):
        product_json_url('https://example.com/products/tent-wall')
    with pytest.raises(ValueError):
        product_json_url('https://soardistusa.com/products/tent-wall?token=secret')
    body = json.dumps({'id': 101, 'title': 'Tent Wall', 'options': [],
                       'variants': [{'id': 201, 'price': -1, 'options': []}]}).encode()
    with pytest.raises(ValueError):
        project_product_json(body, 'https://soardistusa.com/products/tent-wall')
