"""Real policy-to-publication integration; entirely offline source fixtures."""
from copy import deepcopy
import csv
import json

import pytest

from crawler.diagnosis_policy import normalize_selection, selection_fingerprint
from crawler.product_finalization import bind_configuration, review_file, review_rows


def diagnosis():
    def field(key, label, choices, **extra):
        return {'key': 'page-' + key, 'source_name': key, 'label': label,
                'configurable': True, 'kind': 'select', 'disabled': False,
                'selected_values': [choices[0][0]],
                'choices': [{'value': value, 'label': text, 'disabled': False} for value, text in choices],
                **extra}
    result = {'id': 'a' * 32, 'revision': 1, 'status': 'ready',
        'url': 'https://www.printograph.com/products/business-cards/order',
        'content': {'title': 'Cards'},
        'fields': [field('size', 'Size', [('S1','3.5 x 2')]),
                   field('proofType', 'Proof Processing', [('31','Instantly Processed'),('36','Manual Proof')]),
                   field('quantity', 'Quantity', [('100','100')]),
                   field('productionTime', 'Production time', [('1','Regular'),('3','Rush')]),
                   {'key':'content_title','label':'Product title','kind':'content','configurable':False,'choices':[]}]}
    result['selection'] = normalize_selection(result, 1, [
        {'key': f['key'], 'include': f['key'] != 'page-proofType',
         'fixed_values': ['31'] if f['key'] == 'page-proofType' else []} for f in result['fields']])
    return result


def row(proof='31', **changes):
    value = {'id': 'record-' + proof, 'product_url': diagnosis()['url'], 'title':'Cards',
             'quantity':100, 'currency':'USD', 'subtotal':'10.00' if proof == '31' else '13.00',
             'configuration': {'size': {'value':'S1','label':'3.5 x 2'},
                               'proofType':{'value':proof,'label':'Instantly Processed' if proof == '31' else 'Manual Proof'}},
             'production': {'value':'1','label':'Regular'}, 'status':'supported',
             'provenance':'direct_quote', 'evidence_ids':['availability','turnaround','quote'], **changes}
    fingerprint = selection_fingerprint(value)
    def bound(status, evidence, **extra):
        return {'selection_fingerprint':fingerprint,'full_selection':True,'status':status,'evidence_ids':[evidence],**extra}
    value['finalization_evidence'] = {
        'availability':bound('supported','availability'),
        'production':bound('supported','turnaround',range={'min':2,'max':4}),
        'price':bound('direct_quote','quote',subtotal=value['subtotal'],currency='USD')}
    return value


def test_policy_consumed_and_final_csv_separates_invalid_unknown_and_fixed_choices(tmp_path):
    valid, outside, invalid, unknown = row(), row('36'), row(), row()
    invalid['finalization_evidence']['production']['status'] = 'invalid'
    unknown.pop('finalization_evidence')
    source = [valid, outside, invalid, unknown]
    before = deepcopy(source)
    report = review_rows(iter(source), diagnosis(), tmp_path, run_id='run', product_id='product')
    assert source == before
    assert {k: report[k] for k in ('examined_rows','eligible_rows','invalid_rows_removed','held_rows','hidden_choice_rows_omitted')} == {
        'examined_rows':4,'eligible_rows':1,'invalid_rows_removed':1,'held_rows':1,'hidden_choice_rows_omitted':1}
    assert report['supplier_requests_made'] == 0 and report['catalog_complete'] is False
    with (tmp_path / report['id'] / 'finalized.csv').open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]['Subtotal'] == '10.00'
    assert rows[0]['Size'] == '3.5 x 2' and rows[0]['Quantity'] == '100'
    assert 'Proof Processing' not in rows[0]
    assert 'S1' not in json.dumps(rows) and 'page-proofType' not in json.dumps(rows)
    audit = [json.loads(line) for line in (tmp_path / report['id'] / 'rows.jsonl').read_text().splitlines()]
    assert [r['status'] for r in audit] == ['eligible','outside_fixed_choices','invalid','held']


def test_undiagnosed_or_conflicting_internal_fields_hold_instead_of_silently_dropping(tmp_path):
    extra = row(configuration={'size': {'value':'S1'}, 'proofType':{'value':'31'}, 'stock':{'value':'private-stock'}})
    conflicting = row(selection={'size':'different','proofType':'31','quantity':'100','productionTime':'1'})
    report = review_rows([extra, conflicting], diagnosis(), tmp_path, run_id='r', product_id='p')
    assert report['held_rows'] == 2 and report['eligible_rows'] == 0
    assert report['download_url'] is None
    assert not (tmp_path / report['id'] / 'finalized.csv').exists()
    assert 'Additional source fields need diagnosis' in report['reasons']
    assert any('field_binding_missing_or_conflicting' in key for key in report['reasons'])


def test_binding_does_not_guess_from_display_label():
    policy = diagnosis()['selection']
    changed = deepcopy(policy)
    changed['fields'][0].pop('source_name')
    bound, issues = bind_configuration(changed, row())
    assert 'page-size' not in bound
    assert 'field_binding_missing_or_conflicting:Size' in issues


def test_other_source_product_cannot_be_published_as_diagnosed_product(tmp_path):
    other = row(product_url='https://www.printograph.com/products/postcards/order')
    missing = row(product_url=None)
    other['finalization_evidence']['availability']['status'] = 'invalid'
    conflicting = row('36', source_url='https://www.printograph.com/products/postcards/order')
    report = review_rows([other, missing, conflicting], diagnosis(), tmp_path, run_id='r', product_id='p')
    assert report['eligible_rows'] == 0 and report['held_rows'] == 3
    assert report['invalid_rows_removed'] == report['hidden_choice_rows_omitted'] == 0
    assert report['reasons']['row_source_url_mismatch'] == 1
    assert report['reasons']['row_source_url_missing'] == 1
    assert report['reasons']['conflicting_row_source_urls'] == 1


def test_output_label_collisions_do_not_overwrite_and_policy_is_immutable(tmp_path):
    d = diagnosis()
    d['fields'][0]['label'] = 'Option: Subtotal'
    d['fields'][2]['label'] = 'Subtotal'
    submitted = [{k: f[k] for k in ('key','include','fixed_values')} for f in d['selection']['fields']]
    d['selection'] = normalize_selection(d, 1, submitted)
    report = review_rows([row()], d, tmp_path, run_id='r', product_id='p')
    with (tmp_path / report['id'] / 'finalized.csv').open(encoding='utf-8-sig') as stream:
        result = next(csv.DictReader(stream))
    assert result['Option: Subtotal'] == '3.5 x 2'
    assert result['Option: Subtotal (2)'] == '100'
    assert result['Subtotal'] == '10.00'
    saved = json.loads((tmp_path / report['id'] / 'policy.json').read_text())
    d['selection'] = None
    assert saved['hidden_fixed'] == {'page-proofType':['31']}


def test_review_download_paths_are_owned_and_bounded(tmp_path):
    class Store: root = tmp_path
    with pytest.raises(LookupError):
        review_file(Store(), '../outside', 'report')
    folder = tmp_path / 'finalizations' / ('b' * 32)
    folder.mkdir(parents=True)
    target = tmp_path / 'private.txt'
    target.write_text('private')
    (folder / 'report.json').symlink_to(target)
    with pytest.raises(LookupError):
        review_file(Store(), 'b' * 32, 'report')
    (tmp_path / 'finalizations' / ('c' * 32)).symlink_to(folder, target_is_directory=True)
    with pytest.raises(LookupError):
        review_file(Store(), 'c' * 32, 'report')
