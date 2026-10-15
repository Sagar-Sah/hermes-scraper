import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.campaign_progress import CampaignManager


def test_alternate_offer_completes_without_replacing_primary_product(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Fulfillment',products=[{
        'id':'20969','title':'Five-foot tent',
        'storefront_url':'https://printitprints.com/shop/five-foot-tent/',
        'supplier_url':'https://soardistusa.com/products/china-offer',
        'supplier':'soardist'}])
    campaign_id=campaign['id']
    usa_url='https://soardistusa.com/products/usa-offer'
    manager.queue_alternate_supplier(campaign_id,'20969','soardist_usa','US tent',usa_url)
    review_id='a'*32
    root=Path(tmp_path)/'finalizations'/review_id
    root.mkdir(parents=True)
    (root/'report.json').write_text(json.dumps({'id':review_id,'product_id':'20969',
        'source_url':usa_url,'catalog_complete':True,'held_rows':0,'eligible_rows':2}))
    with (root/'finalized.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['Supplier SKU','Price'])
        writer.writerow(['USA206112','459.00']);writer.writerow(['USA206112','609.00'])
    with pytest.raises(ValueError,match='unavailable'):
        manager.complete_alternate_supplier(campaign_id,'20969-soardist_usa','b'*32)
    completed=manager.complete_alternate_supplier(campaign_id,'20969-soardist_usa',review_id)
    job=completed['alternate_supplier_jobs'][0]
    product=completed['products'][0]
    assert job['stage']=='finalized' and job['row_count']==2
    assert product['supplier_url']=='https://soardistusa.com/products/china-offer'
    assert product['stage']!='finalized'
    assert manager.complete_alternate_supplier(campaign_id,'20969-soardist_usa',review_id)['alternate_supplier_jobs'][0]['finalization_id']==review_id
    with pytest.raises(ValueError,match='already finalized'):
        manager.complete_alternate_supplier(campaign_id,'20969-soardist_usa','c'*32)
