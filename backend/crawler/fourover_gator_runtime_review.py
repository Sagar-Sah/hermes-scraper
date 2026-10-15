"""Review Gator Board as a guarded, source-live quote product.

The saved one-set area table is reference data, never a price table for other
set counts. Every customer selection still needs a fresh exact source quote.
"""

import hashlib
import json
import os
import re
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


SOURCE_URL = 'https://4over.com/gator-board-signs'
CHILD_IDS = {'49838', '49840', '58459', '58462'}
DIMENSIONS = {str(value) for value in range(6, 37)}


def _object(root, evidence, path_key='object_path', hash_key='sha256'):
    rel = evidence.get(path_key)
    expected = evidence.get(hash_key)
    if not isinstance(rel, str) or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.[a-z0-9.-]+', rel):
        raise ValueError('invalid_gator_source_object_path')
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError('gator_source_object_missing')
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('gator_source_object_hash_mismatch')
    return json.loads(body)


def _images(root):
    path = root.parent / 'hermes-gator-gallery-images-20260928.json'
    manifest = json.loads(path.read_text())
    if manifest.get('source_url') != SOURCE_URL:
        raise ValueError('gator_image_source_mismatch')
    images = manifest.get('images') or []
    if len(images) != 3 or len({row.get('url') for row in images}) != 3:
        raise ValueError('gator_gallery_incomplete')
    cache = (root.parent / 'fourover-image-cache').resolve()
    reviewed = []
    for row in images:
        path = Path(row.get('local_path', '')).resolve()
        if (not path.is_relative_to(cache) or not path.is_file() or path.is_symlink()
                or path.stat().st_size != row.get('bytes')
                or hashlib.sha256(path.read_bytes()).hexdigest() != row.get('sha256')):
            raise ValueError('gator_gallery_image_not_verified')
        reviewed.append({'url': row['url'], 'sha256': row['sha256'], 'bytes': row['bytes']})
    return sorted(reviewed, key=lambda row: row['url'])


def _quotes(root, campaign_id):
    directory = root / 'gator-exact-quotes' / campaign_id
    rows = []
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}\.json', path.name):
            raise ValueError('invalid_gator_quote_archive_path')
        record = json.loads(path.read_text())
        selection, quote = record.get('selection') or {}, record.get('quote') or {}
        if (selection.get('source_url') != SOURCE_URL or selection.get('product_id') != '6742'
                or quote.get('status') != 'exact_scoped_quote'
                or any(quote.get(k) != selection.get(k) for k in
                       ('product_id', 'source_url', 'child_id', 'width_in', 'height_in', 'sets'))):
            raise ValueError('gator_archived_quote_scope_mismatch')
        if quote.get('source_fulfillment_available') is not True:
            continue  # Older observations remain reference data, not review canaries.
        if (quote.get('set_control') != {'min': 1, 'max': 200, 'retained': True}
                or quote.get('production_detail_status') not in ('observed', 'missing_at_supplier')
                or not 1 <= selection['sets'] <= 200):
            raise ValueError('gator_runtime_quote_controls_incomplete')
        try:
            total = Decimal(quote['supplier_total_excluding_shipping'])
            fee = Decimal(quote['small_quantity_fee'])
        except (InvalidOperation, KeyError, TypeError):
            raise ValueError('gator_runtime_quote_money_invalid') from None
        if total <= 0 or fee < 0:
            raise ValueError('gator_runtime_quote_money_invalid')
        rows.append({'ref': str(path.relative_to(root)), 'child_id': selection['child_id'],
                     'width_in': selection['width_in'], 'height_in': selection['height_in'],
                     'sets': selection['sets'], 'total': str(total), 'fee': str(fee),
                     'production_detail_status': quote['production_detail_status']})
    return rows


def inspect_gator_runtime(product, root, campaign_id):
    root = Path(root).resolve()
    if (product.get('id') != '6742' or product.get('supplier') != 'fourover'
            or product.get('supplier_url') != SOURCE_URL or not product.get('diagnosis_id')
            or not product.get('source_collection_id')):
        raise ValueError('mapped_gator_campaign_product_required')
    map_evidence = product.get('fourover_option_map_evidence') or {}
    area_evidence = product.get('fourover_area_rows_evidence') or {}
    mapping = _object(root, map_evidence)
    grid = _object(root, area_evidence, 'dimension_grid_object_path', 'dimension_grid_sha256')
    rows = mapping.get('rows') or []
    if (mapping.get('source_url') != SOURCE_URL or mapping.get('complete_child_mapping') is not True
            or mapping.get('mapped_children') != 4 or mapping.get('expected_children') != 4
            or {row.get('child_id') for row in rows} != CHILD_IDS or len(rows) != 4):
        raise ValueError('gator_option_mapping_incomplete')
    for row in rows:
        if {field.get('label') for field in row.get('fields', [])} != {
                'Stock *', 'Colorspec *', 'White Mask Front *'}:
            raise ValueError('gator_option_mapping_fields_incomplete')
    grids = grid.get('dimension_grid') or []
    if grid.get('source_url') != SOURCE_URL or len(grids) != 4 or {
            row.get('child_id') for row in grids} != CHILD_IDS:
        raise ValueError('gator_dimension_grid_incomplete')
    for row in grids:
        widths = row.get('heights_by_width') or {}
        if set(widths) != DIMENSIONS or any(set(heights) != DIMENSIONS
                                            for heights in widths.values()):
            raise ValueError('gator_dimension_grid_incomplete')
    images = _images(root)
    quotes = _quotes(root, campaign_id)
    if ({row['child_id'] for row in quotes} != CHILD_IDS
            or not {1, 3, 4, 200}.issubset({row['sets'] for row in quotes})):
        raise ValueError('gator_runtime_canary_coverage_incomplete')
    by_selection = defaultdict(dict)
    for row in quotes:
        by_selection[(row['child_id'], row['width_in'], row['height_in'])][row['sets']] = row
    if not any(3 in qty and 4 in qty and Decimal(qty[3]['fee']) > 0
               and Decimal(qty[4]['fee']) == 0 for qty in by_selection.values()):
        raise ValueError('gator_fee_transition_not_verified')
    return {'schema': 'hermes-fourover-gator-runtime-review/v1',
            'product_id': '6742', 'diagnosis_id': product['diagnosis_id'],
            'source_collection_id': product['source_collection_id'],
            'source_url': SOURCE_URL, 'pricing_model': 'source_live_quote',
            'source_data_complete': True, 'static_rows_claimed': False,
            'storefront_deployed': False,
            'option_map_sha256': map_evidence['sha256'],
            'dimension_grid_sha256': area_evidence['dimension_grid_sha256'],
            'source_dimensions': {'width_in': [6, 36], 'height_in': [6, 36],
                                  'sets': [1, 200]},
            'child_ids': sorted(CHILD_IDS), 'images': images,
            'canary_result_refs': sorted(row['ref'] for row in quotes),
            'canary_count': len(quotes),
            'price_policy': 'fresh_exact_source_quote_per_complete_selection; fail_closed',
            'production_policy': 'read_current_available_supplier_rate; flag_missing',
            'shipping_policy': 'supplier_reference_only_printitprints_site_calculates_shipping',
            'known_unverified_selections': [
                {'child_id': '58462', 'width_in': 10, 'height_in': 20, 'sets': 4,
                 'reason': 'pre_hydration_total_mismatch', 'usable_price': False},
                {'child_id': '49840', 'width_in': 9, 'height_in': 9, 'sets': 3,
                 'reason': 'visible_zero_placeholder_before_hydration_guard',
                 'usable_price': False}],
            'runtime_guard': 'The exact quote must reconcile current rendered and source totals; rejected selections remain unpriced until a fresh valid quote.'}


def save_gator_runtime_review(product, root, campaign_id):
    root = Path(root).resolve()
    report = inspect_gator_runtime(product, root, campaign_id)
    report['id'] = uuid.uuid4().hex
    directory = root / 'finalizations' / report['id']
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = directory / 'report.json'
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return report
