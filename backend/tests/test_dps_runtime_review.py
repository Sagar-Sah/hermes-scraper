"""Campaign-specific source-evidence review; skip when its saved evidence is absent."""
import copy
import json
from pathlib import Path

import pytest

from crawler import dps_runtime_review as review


ROOT = Path(__file__).resolve().parents[4]
DATA = ROOT / 'work/atlas-data'
CAMPAIGN = DATA / 'campaigns/printit-89-20260927.json'
IDS = ('19919', '19918', '19882', '19881', '19880', '19879', '19574')


@pytest.fixture
def products():
    if not CAMPAIGN.is_file():
        pytest.skip('Saved Hermes campaign evidence unavailable')
    data = json.loads(CAMPAIGN.read_text())
    return {row['id']: row for row in data['products']}


def test_all_seven_reviews_recompute_without_static_price_claims(products):
    for pid in IDS:
        report = review.inspect_dps_runtime(products[pid], DATA, 'printit-89-20260927')
        assert report['product_id'] == pid
        assert report['pricing_model'] == 'source_live_quote'
        assert report['source_data_complete'] is True
        assert report['static_rows_claimed'] is False
        assert report['storefront_deployed'] is False
        assert report['quantity_bounds'] == [1, 25000]


@pytest.mark.parametrize('field,value', [
    ('availability', 'unverified'),
    ('selection_verified', False),
    ('status', 'attention'),
])
def test_tampered_rendered_quote_fails_closed(products, monkeypatch, field, value):
    original = review._saved

    def changed(path):
        item, sha = original(path)
        if path.name == 'hermes-group10-19574-8x8-q31-availability-20260930.json':
            item = copy.deepcopy(item)
            item[field] = value
        return item, sha

    monkeypatch.setattr(review, '_saved', changed)
    with pytest.raises(ValueError, match='dps_rendered_quote_incomplete'):
        review.inspect_dps_runtime(products['19574'], DATA, 'printit-89-20260927')


def test_missing_purchase_step_fails_closed(products, monkeypatch):
    original = review._saved

    def changed(path):
        item, sha = original(path)
        if path.name == 'hermes-group10-19574-8x8-q31-availability-20260930.json':
            item = copy.deepcopy(item)
            item['observations'][-1]['availability_signals']['purchase_actions'] = []
        return item, sha

    monkeypatch.setattr(review, '_saved', changed)
    with pytest.raises(ValueError, match='dps_rendered_purchase_step_unavailable'):
        review.inspect_dps_runtime(products['19574'], DATA, 'printit-89-20260927')
