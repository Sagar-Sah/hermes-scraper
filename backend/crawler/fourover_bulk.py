"""Source-backed child price table discovery for 4over configurable products."""

import json
import re

from bs4 import BeautifulSoup


def child_configurations(html: str):
    """Read the product's own child map; do not invent Cartesian combinations."""
    soup = BeautifulSoup(html, 'html.parser')
    maps = []
    for script in soup.select('script[type="text/x-magento-init"]'):
        try:
            body = json.loads(script.string or script.text)
        except (ValueError, TypeError):
            continue
        config = (body.get('*', {}).get('Magento_Ui/js/core/app', {})
                  .get('components', {}).get('child-sku', {}).get('childSkuConfig'))
        if isinstance(config, dict):
            maps.append(config)
    if len(maps) != 1 or not 1 <= len(maps[0]) <= 2000:
        raise ValueError('Unique child configuration map unavailable')
    children = []
    for identifier, record in maps[0].items():
        if not re.fullmatch(r'[1-9][0-9]{0,12}', str(identifier)) or not isinstance(record, dict):
            raise ValueError('Invalid child configuration')
        sku = record.get('sku')
        if not isinstance(sku, str) or not 1 <= len(sku) <= 160:
            raise ValueError('Invalid child SKU')
        children.append({'child_id': str(identifier), 'sku': sku})
    return children
