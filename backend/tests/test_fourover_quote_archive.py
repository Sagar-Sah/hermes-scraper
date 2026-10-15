import pytest

from crawler.fourover_quote_archive import GatorQuoteArchive


def selection():
    return {'product_id': '6742', 'source_url': 'https://4over.com/gator-board-signs',
            'child_id': '49840', 'width_in': 18, 'height_in': 24, 'sets': 5}


def test_exact_quote_archive_is_durable_and_selection_bound(tmp_path):
    archive = GatorQuoteArchive(tmp_path)
    chosen = selection()
    quote = {**chosen, 'status': 'exact_scoped_quote',
             'supplier_total_excluding_shipping': '115.35'}
    assert archive.get('campaign-1', chosen) is None
    stored = archive.save('campaign-1', chosen, quote)
    assert stored['use'] == 'historical_evidence_only_require_fresh_for_checkout'
    assert GatorQuoteArchive(tmp_path).get('campaign-1', chosen)['quote'] == quote
    assert archive.get('campaign-1', {**chosen, 'sets': 6}) is None
    with pytest.raises(ValueError, match='selection_mismatch'):
        archive.save('campaign-1', {**chosen, 'sets': 6}, quote)
