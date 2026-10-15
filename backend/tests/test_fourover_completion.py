import csv
import io
import json
import copy
from pathlib import Path

import pytest

from crawler import fourover_completion as completion
from crawler.fourover_rows import package_rows


URL = 'https://4over.com/brown-kraft-bookmarks'
SHA_OPTION = 'c' * 64
SHA_PRICE = 'b' * 64
SHA_PUBLIC = 'a' * 64


def fixture(tmp_path, monkeypatch):
    diagnosis_id = 'd' * 32
    (tmp_path / 'diagnoses').mkdir(exist_ok=True)
    (tmp_path / 'diagnoses' / (diagnosis_id + '.json')).write_text(json.dumps({
        'url': URL, 'supplier_id': 'fourover', 'fields': [
            {'kind': 'checkbox', 'source_name': name, 'selected_values': []}
            for name in ('job_sample', 'digital_proof')]}))
    mappings = {'complete_child_mapping': True, 'source_url': URL,
        'price_table_sha256': SHA_PRICE, 'mapped_children': 2, 'expected_children': 2,
        'rows': [{'child_id': str(child), 'fields': [{'label': 'Size *', 'choice': size}]}
                 for child, size in [(1, '2 x 8'), (2, '3 x 8')]]}
    prices = {'coverage': 'all_source_child_routes', 'source_url': URL,
        'child_count': 2, 'priced_count': 2, 'observations': [
            {'child_id': str(child), 'status': 'priced', 'package_prices': [{'quantity': 100, 'price': cost}]}
            for child, cost in [(1, 10.25), (2, 11.5)]]}
    rows, columns = package_rows('Brown Kraft Bookmarks', URL, mappings, prices,
        image_url='https://4over.com/image.jpg', production_reference='4 Business Days')
    draft = io.StringIO()
    writer = csv.DictWriter(draft, fieldnames=columns)
    writer.writeheader(); writer.writerows(rows)
    public = {'public_data': {'product_content': {'source_url': URL,
        'specifications': [{'label': 'Turnaround Time', 'values': ['4 Business Days']}]}}}
    evidence = {'fourover-option-map.json': json.dumps(mappings).encode(),
                'fourover-price-tables.json': json.dumps(prices).encode(),
                'fourover-draft.csv': draft.getvalue().encode(),
                'public-source.json': json.dumps(public).encode()}
    monkeypatch.setattr(completion, '_read_evidence', lambda root, record, suffix: evidence[suffix])
    product = {'id': '9558', 'title': 'Brown Kraft Bookmarks', 'stage': 'extracted',
        'supplier': 'fourover', 'supplier_url': URL, 'diagnosis_id': diagnosis_id,
        'fourover_option_map_evidence': {'source_url': URL, 'object_path': 'unused', 'sha256': SHA_OPTION},
        'fourover_price_table_evidence': {'source_url': URL, 'object_path': 'unused', 'sha256': SHA_PRICE},
        'fourover_rows_evidence': {'source_url': URL, 'object_path': 'unused', 'sha256': 'e' * 64,
            'source_option_map_sha256': SHA_OPTION, 'source_price_table_sha256': SHA_PRICE,
            'public_page_sha256': SHA_PUBLIC, 'product_level_production_reference': '4 Business Days',
            'image_url': 'https://4over.com/image.jpg', 'row_count': 2},
        'fourover_rendered_canaries': [
            {'status': 'matched', 'child_id': str(child), 'scope': 'one_rendered_configuration_price_no_cart_or_order',
             'source_price_table_sha256': SHA_PRICE, 'matched_quantities': [100]}
            for child in (1, 2)]}
    return product, evidence


def test_one_set_review_requires_explicit_fixed_scope(tmp_path, monkeypatch):
    product, _ = fixture(tmp_path, monkeypatch)
    for scope in ({}, {'sets': 2, 'job_sample': False, 'digital_proof': False},
                  {'sets': 1, 'job_sample': True, 'digital_proof': False}):
        with pytest.raises(ValueError, match='Explicit one-set'):
            completion.review_finite_one_set(product, tmp_path, scope)
    assert not (tmp_path / 'finalizations').exists()


def test_one_set_review_writes_exact_rows_only_after_coverage(tmp_path, monkeypatch):
    product, _ = fixture(tmp_path, monkeypatch)
    scope = {'sets': 1, 'job_sample': False, 'digital_proof': False}
    report = completion.review_finite_one_set(product, tmp_path, scope)
    assert report['catalog_complete'] is True and report['eligible_rows'] == 2
    assert report['held_rows'] == 0 and report['supplier_requests_made'] == 0
    with (tmp_path / 'finalizations' / report['id'] / 'finalized.csv').open(encoding='utf-8-sig') as file:
        rows = list(csv.DictReader(file))
    assert {row['Size'] for row in rows} == {'2 x 8', '3 x 8'}
    assert {row['Supplier Table Price'] for row in rows} == {'10.25', '11.5'}
    assert all(row['Production Time (product page reference)'] == '4 Business Days' for row in rows)


def test_envelope_production_grid_requires_exact_fee_free_choice_per_tier():
    url='https://4over.com/digital-envelopes'
    options={'source_url':url,'rows':[
        {'child_id':'1','fields':[{'label':'Stock *','choice':'70LB Premium Opaque'}]},
        {'child_id':'2','fields':[{'label':'Stock *','choice':'60LB Uncoated Text - Security Tint'}]}]}
    prices={'observations':[
        {'child_id':child,'package_prices':[{'quantity':25,'price':10},{'quantity':50,'price':20}]}
        for child in ('1','2')]}
    def choice(value,label,checked=False):
        return {'value':value,'label':label,'checked':checked,'disabled':False}
    grids=[{'child_id':'1','source_url':url,
            'scope':'rendered_package_and_turnaround_choices_only_no_order','tiers':[
                {'quantity':quantity,'turnaround_choices':[choice('1','Next Business Day +$3.00'),
                    choice('2','2 Business Days +$1.00'),choice('4','4 Business Days',True)]}
                for quantity in (25,50)]},
           {'child_id':'2','source_url':url,
            'scope':'rendered_package_and_turnaround_choices_only_no_order','tiers':[
                {'quantity':quantity,'turnaround_choices':[choice('1','Next Business Day +$3.00'),
                    choice('2','2 Business Days')]}
                for quantity in (25,50)]}]
    assert completion.digital_envelope_production_rows(options,prices,grids)==[
        '4 Business Days','4 Business Days','2 Business Days','2 Business Days']
    changed=copy.deepcopy(grids)
    changed[1]['tiers'][1]['turnaround_choices'][1]['label']='2 Business Days +$1.00'
    with pytest.raises(ValueError,match='Fee-free'):
        completion.digital_envelope_production_rows(options,prices,changed)
    changed=copy.deepcopy(grids)
    changed[0]['tiers'][1]['quantity']=75
    with pytest.raises(ValueError,match='tier and source price'):
        completion.digital_envelope_production_rows(options,prices,changed)


def test_one_set_review_holds_missing_canary_and_multiple_production_modes(tmp_path, monkeypatch):
    product, evidence = fixture(tmp_path, monkeypatch)
    scope = {'sets': 1, 'job_sample': False, 'digital_proof': False}
    product['fourover_rendered_canaries'].pop()
    with pytest.raises(ValueError, match='Two distinct'):
        completion.review_finite_one_set(product, tmp_path, scope)
    product, evidence = fixture(tmp_path, monkeypatch)
    public = json.loads(evidence['public-source.json'])
    public['public_data']['product_content']['specifications'][0]['values'].append('7 Business Days')
    evidence['public-source.json'] = json.dumps(public).encode()
    with pytest.raises(ValueError, match='Single product-level production'):
        completion.review_finite_one_set(product, tmp_path, scope)
