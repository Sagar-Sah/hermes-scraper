"""Public Shopify variant evidence, separate from account quotes and shipping."""
from __future__ import annotations

from decimal import Decimal
import json
from urllib.parse import urlsplit


def product_json_url(source_url: str) -> str:
    if not isinstance(source_url, str):
        raise ValueError('Expected an exact public Soardist product URL')
    parts = urlsplit(source_url)
    path = parts.path.rstrip('/')
    if (parts.scheme != 'https' or parts.hostname not in {'soardistusa.com', 'www.soardistusa.com'}
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.query or parts.fragment or not path.startswith('/products/')
            or path.count('/') != 2 or not path.removeprefix('/products/')):
        raise ValueError('Expected an exact public Soardist product URL')
    return source_url.rstrip('/') + '.js'


def _image_url(value):
    if not isinstance(value, str):
        return None
    url = 'https:' + value if value.startswith('//') else value
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or any(ord(c) < 33 for c in url):
        return None
    return url


def project_product_json(body: bytes, source_url: str) -> dict:
    """Retain bounded public fields; prices are never labeled account prices."""
    if not isinstance(body, bytes) or len(body) > 2_000_000:
        raise ValueError('Public product JSON exceeds the size limit')
    item = json.loads(body)
    if not isinstance(item, dict) or type(item.get('id')) is not int or not isinstance(item.get('title'), str):
        raise ValueError('Public product JSON lacks identity')
    variants = item.get('variants')
    options = item.get('options')
    if not isinstance(variants, list) or not 1 <= len(variants) <= 1000 or not isinstance(options, list) or len(options) > 10:
        raise ValueError('Public variant domain is missing or too large')
    clean_options = []
    for option in options:
        if (not isinstance(option, dict) or not isinstance(option.get('name'), str)
                or not isinstance(option.get('values'), list) or len(option['values']) > 1000
                or any(not isinstance(value, str) for value in option['values'])):
            raise ValueError('Public variant option is malformed')
        clean_options.append({'name': option['name'][:160], 'values': [v[:300] for v in option['values']]})
    clean_variants = []
    for variant in variants:
        if not isinstance(variant, dict) or type(variant.get('id')) is not int or type(variant.get('price')) is not int or variant['price'] < 0:
            raise ValueError('Public variant price or ID is malformed')
        labels = variant.get('options')
        if not isinstance(labels, list) or len(labels) != len(clean_options) or any(not isinstance(value, str) for value in labels):
            raise ValueError('Public variant choices are malformed')
        breaks = variant.get('quantity_price_breaks') or []
        if not isinstance(breaks, list) or len(breaks) > 1000:
            raise ValueError('Public quantity breaks are malformed')
        clean_breaks = []
        for entry in breaks:
            if not isinstance(entry, dict) or type(entry.get('minimum_quantity')) is not int or entry['minimum_quantity'] < 1 or type(entry.get('price')) is not int or entry['price'] < 0:
                raise ValueError('Public quantity break is malformed')
            clean_breaks.append({'minimum_quantity': entry['minimum_quantity'],
                                 'public_unit_price': str(Decimal(entry['price']) / 100)})
        image = variant.get('featured_image')
        if isinstance(image, dict):
            image = image.get('src')
        clean_variants.append({'id': variant['id'], 'title': str(variant.get('title') or '')[:300],
                               'choices': [label[:300] for label in labels],
                               'public_unit_price': str(Decimal(variant['price']) / 100),
                               'available': variant.get('available') if type(variant.get('available')) is bool else None,
                               'quantity_price_breaks': clean_breaks,
                               'quantity_rule': variant.get('quantity_rule') if isinstance(variant.get('quantity_rule'), dict) else None,
                               'requires_shipping': variant.get('requires_shipping') if type(variant.get('requires_shipping')) is bool else None,
                               'image_url': _image_url(image)})
    images = [url for image in item.get('images', []) if (url := _image_url(image))][:100]
    return {'schema': 'hermes-soardist-public-variants/v1', 'source_url': source_url,
            'source_product_id': item['id'], 'source_title': item['title'][:300],
            'options': clean_options, 'variants': clean_variants, 'images': images,
            'price_basis': 'public_shopify_product_json_unverified_account_price',
            'shipping_status': 'not_quoted', 'production_status': 'not_verified',
            'final_eligible': False}
