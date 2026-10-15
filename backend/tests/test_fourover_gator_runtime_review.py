import hashlib
import json

import pytest

from crawler.fourover_gator_runtime_review import inspect_gator_runtime


def _object(root, value, suffix):
    body = json.dumps(value).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = root / 'objects' / digest[:2] / (digest + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path.relative_to(root)), digest


def _fixture(tmp_path):
    root = tmp_path / 'atlas-data'
    root.mkdir()
    source = 'https://4over.com/gator-board-signs'
    children = ['49838', '49840', '58459', '58462']
    fields = [{'label': label, 'value': str(i), 'choice': label}
              for i, label in enumerate(('Stock *', 'Colorspec *', 'White Mask Front *'))]
    mapping = {'source_url': source, 'complete_child_mapping': True,
               'mapped_children': 4, 'expected_children': 4,
               'rows': [{'child_id': child, 'fields': fields} for child in children]}
    map_path, map_sha = _object(root, mapping, '.fourover-option-map.json')
    dimensions = {str(i): [str(j) for j in range(6, 37)] for i in range(6, 37)}
    grid = {'source_url': source, 'dimension_grid': [
        {'child_id': child, 'heights_by_width': dimensions} for child in children]}
    grid_path, grid_sha = _object(root, grid, '.fourover-dimension-grid.json')
    cache = tmp_path / 'fourover-image-cache'
    cache.mkdir()
    images = []
    for n in range(3):
        body = f'image-{n}'.encode()
        image_path = cache / f'gator-{n}.jpg'
        image_path.write_bytes(body)
        images.append({'url': f'https://4over.com/image-{n}.jpg',
                       'local_path': str(image_path), 'bytes': len(body),
                       'sha256': hashlib.sha256(body).hexdigest()})
    (tmp_path / 'hermes-gator-gallery-images-20260928.json').write_text(
        json.dumps({'source_url': source, 'images': images}))
    quote_dir = root / 'gator-exact-quotes' / 'campaign'
    quote_dir.mkdir(parents=True)
    for n, (child, sets) in enumerate(zip(children, (200, 3, 1, 4))):
        selections = [(child, sets)]
        if child == '49840':
            selections.append((child, 4))
        for suffix, (selected_child, count) in enumerate(selections):
            selection = {'product_id': '6742', 'source_url': source,
                         'child_id': selected_child, 'width_in': 8,
                         'height_in': 10, 'sets': count}
            quote = dict(selection, status='exact_scoped_quote',
                         source_fulfillment_available=True,
                         production_detail_status='observed',
                         set_control={'min': 1, 'max': 200, 'retained': True},
                         supplier_total_excluding_shipping='16.56',
                         small_quantity_fee='3.75' if count == 3 else '0.00')
            (quote_dir / f'{n}{suffix:01d}'.zfill(64)).with_suffix('.json').write_text(
                json.dumps({'selection': selection, 'quote': quote}))
    product = {'id': '6742', 'supplier': 'fourover', 'supplier_url': source,
               'diagnosis_id': 'diagnosis', 'source_collection_id': 'collection',
               'fourover_option_map_evidence': {'object_path': map_path, 'sha256': map_sha},
               'fourover_area_rows_evidence': {'dimension_grid_object_path': grid_path,
                                              'dimension_grid_sha256': grid_sha}}
    return root, product


def test_gator_runtime_review_requires_source_images_and_exact_canaries(tmp_path):
    root, product = _fixture(tmp_path)
    report = inspect_gator_runtime(product, root, 'campaign')
    assert report['source_data_complete'] is True
    assert report['pricing_model'] == 'source_live_quote'
    assert report['static_rows_claimed'] is False
    assert report['canary_count'] == 5
    manifest = tmp_path / 'hermes-gator-gallery-images-20260928.json'
    images = json.loads(manifest.read_text())
    images['images'][0]['sha256'] = '0' * 64
    manifest.write_text(json.dumps(images))
    with pytest.raises(ValueError, match='gator_gallery_image_not_verified'):
        inspect_gator_runtime(product, root, 'campaign')


def test_gator_runtime_review_rejects_missing_fee_boundary(tmp_path):
    root, product = _fixture(tmp_path)
    archive = root / 'gator-exact-quotes' / 'campaign'
    for path in archive.glob('*.json'):
        record = json.loads(path.read_text())
        if record['selection']['sets'] == 3:
            record['quote']['small_quantity_fee'] = '0.00'
            path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='gator_fee_transition_not_verified'):
        inspect_gator_runtime(product, root, 'campaign')
