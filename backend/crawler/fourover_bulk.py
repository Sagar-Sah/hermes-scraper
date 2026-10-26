"""Source-backed child price table discovery for 4over configurable products."""

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


DEFAULT_CHILD_LIMIT = 2000
_REVIEWED_LARGE_CHILD_LIMIT = 3000
_REVIEWED_LARGE_CHILD_PATHS = {"/16pt-postcards", "/specialty-folds-brochures"}


def child_configuration_limit(source_url=None):
    """Keep the ordinary bound; only two reviewed exact source families use 3000.

    This is a declaration bound, not permission to request any child prices.
    The collector still validates source membership and every request budget.
    """
    if (not isinstance(source_url, str) or source_url != source_url.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in source_url)):
        return DEFAULT_CHILD_LIMIT
    try:
        parsed = urlsplit(source_url)
        reviewed = (parsed.scheme == "https"
                    and parsed.hostname in {"4over.com", "www.4over.com"}
                    and parsed.port in (None, 443)
                    and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment
                    and parsed.path.removesuffix("/") in _REVIEWED_LARGE_CHILD_PATHS)
    except ValueError:
        reviewed = False
    return _REVIEWED_LARGE_CHILD_LIMIT if reviewed else DEFAULT_CHILD_LIMIT


class ChildConfigurationError(ValueError):
    """A public schema category and counts, never source script/account data."""
    def __init__(self, code, message, diagnostic):
        super().__init__(message)
        self.code = code
        self.diagnostic = dict(diagnostic)


def inspect_child_configurations(html: str, *, source_url=None):
    """Read exact children and safe declaration counts; retain no script text."""
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script[type="text/x-magento-init"]')
    maps, invalid_types, invalid_json = [], 0, 0
    for script in scripts:
        try:
            body = json.loads(script.string or script.text)
        except (ValueError, TypeError):
            invalid_json += 1
            continue
        config = body
        for key in ('*', 'Magento_Ui/js/core/app', 'components', 'child-sku', 'childSkuConfig'):
            # Other Magento initializers can legitimately contain arrays or
            # unrelated JSON. They do not declare this product's child map.
            if not isinstance(config, dict) or key not in config:
                config = None
                break
            config = config[key]
        if isinstance(config, dict):
            maps.append(config)
        elif config is not None:
            invalid_types += 1
    maximum_children = child_configuration_limit(source_url)
    diagnostic = {'magento_init_scripts': len(scripts), 'invalid_json_scripts': invalid_json,
                  'child_map_count': len(maps), 'invalid_child_map_types': invalid_types,
                  'declared_child_count': len(maps[0]) if len(maps) == 1 else None,
                  'maximum_children': maximum_children}

    def reject(code, message):
        raise ChildConfigurationError(code, message, diagnostic)

    if invalid_types:
        reject('child_declaration_invalid_schema', 'Invalid child configuration map type')
    if not maps or len(maps) == 1 and not maps[0]:
        reject('child_declaration_missing', 'Unique child configuration map unavailable')
    if len(maps) != 1:
        reject('child_declaration_ambiguous', 'Unique child configuration map unavailable')
    if len(maps[0]) > maximum_children:
        reject('child_declaration_limit', 'Child configuration map exceeds safe bound')
    children = []
    for identifier, record in maps[0].items():
        if not re.fullmatch(r'[1-9][0-9]{0,12}', str(identifier)) or not isinstance(record, dict):
            reject('child_declaration_invalid_child', 'Invalid child configuration')
        sku = record.get('sku')
        if not isinstance(sku, str) or not 1 <= len(sku) <= 160:
            reject('child_declaration_invalid_sku', 'Invalid child SKU')
        children.append({'child_id': str(identifier), 'sku': sku})
    return children, diagnostic


def child_configurations(html: str):
    """Read the product's own child map; do not invent Cartesian combinations."""
    return inspect_child_configurations(html)[0]
