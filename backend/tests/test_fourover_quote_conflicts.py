import json

from crawler.fourover_quote_conflicts import save_gator_quote_conflict


def test_rejected_quote_diagnostic_keeps_only_price_comparison(tmp_path):
    selection = {'product_id': '6742', 'source_url': 'https://4over.com/gator-board-signs',
                 'child_id': '58462', 'width_in': 10, 'height_in': 20, 'sets': 4}
    pilot = {'project_response_shapes': [{'status': 200, 'totals': {
        'subtotal': 23.13, 'grand_total': 26.88, 'small_qty_fee': 3.75,
        'account_token': 'SECRET_TOKEN'}}],
        'shipping_view': {'money': ['Price Per Set: $7.71', 'Total: $26.88'],
                          'address': 'PRIVATE_ADDRESS'},
        'shipping_request_shapes': [{'account': 'PRIVATE_ACCOUNT'}]}
    result = save_gator_quote_conflict(tmp_path, selection, pilot,
                                       'supplier_displayed_total_disagrees')
    path = tmp_path / 'gator-quote-conflicts' / (result['id'] + '.json')
    body = path.read_text()
    assert json.loads(body)['reason'] == 'supplier_displayed_total_disagrees'
    assert 'SECRET_TOKEN' not in body
    assert 'PRIVATE_ADDRESS' not in body
    assert 'PRIVATE_ACCOUNT' not in body
