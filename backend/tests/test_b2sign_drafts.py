import csv
import io

import pytest

from crawler.b2sign_drafts import DraftError, build_draft


def fixture():
    candidate = {'candidate_id': 'a', 'source_url': 'https://www.b2sign.com/fixture',
                 'values': {'_size': '12x18', 'material': 'vinyl', '_hidden': 'x', 'derived': 'y'},
                 'quantity': 10, 'production_label': '2 Business Days'}
    manifest = {'catalog': {'url': candidate['source_url'], 'title': 'Banner',
                'dimensions': [
                    {'key': '_size', 'label': 'Size', 'component': 'size', 'choices': []},
                    {'key': 'material', 'label': 'Material', 'component': 'select',
                     'choices': [{'value': 'vinyl', 'label': '13oz Vinyl', 'visible': True}]},
                    {'key': '_hidden', 'label': 'Hidden', 'component': 'hidden', 'choices': []}],
                'contract': {'derived_request_fields': [{'key': 'derived'}]}},
                'candidates': [candidate]}
    report = {'status': 'completed', 'finite_requested_set_exhausted': True,
              'warnings': [], 'invalid_count': 0, 'requested_count': 1,
              'completed_count': 1, 'url': candidate['source_url']}
    result = {'candidate_id': 'a', 'candidate': candidate, 'status': 'quoted',
              'quote': {'quantity': 10, 'unit_price': '1.20', 'subtotal': '12.00'}}
    return manifest, report, [result]


def test_draft_has_display_labels_and_pending_status_without_source_ids():
    body, count = build_draft(*fixture())
    rows = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
    assert count == 1
    assert rows[0]['Size'] == '12x18'
    assert rows[0]['Material'] == '13oz Vinyl'
    assert rows[0]['Quoted Subtotal'] == '12.00'
    assert rows[0]['Shipping'] == 'Not quoted'
    assert '_hidden' not in rows[0] and 'derived' not in rows[0]
    assert 'source_product_id' not in rows[0]


def test_draft_rejects_partial_or_mismatched_quote():
    manifest, report, results = fixture()
    report['completed_count'] = 0
    with pytest.raises(DraftError):
        build_draft(manifest, report, results)
    report['completed_count'] = 1
    results[0]['quote']['quantity'] = 11
    with pytest.raises(DraftError):
        build_draft(manifest, report, results)
