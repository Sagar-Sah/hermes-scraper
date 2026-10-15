"""Fail-closed completion review for a chosen, finite 4over one-set scope.

The caller must explicitly choose one set and both optional services off. This
module reads immutable supplier captures and writes a separate review artifact;
it never changes campaign state or a storefront product.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from decimal import Decimal, ROUND_HALF_UP

from .fourover_rows import package_rows


def _read_evidence(root: Path, record: dict, suffix: str) -> bytes:
    relative = record.get('object_path', '')
    if not isinstance(relative, str) or not re.fullmatch(
            rf'objects/[a-f0-9]{{2}}/[a-f0-9]{{64}}\.{re.escape(suffix)}', relative):
        raise ValueError('Saved 4over evidence path invalid')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
        raise ValueError('Saved 4over evidence unavailable')
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != record.get('sha256'):
        raise ValueError('Saved 4over evidence changed')
    return body


def review_finite_one_set(product: dict, root: Path, scope: dict) -> dict:
    """Write a complete review only for an explicitly fixed and proven scope.

    Price-route package rows establish availability for each mapped child and
    package quantity. Two distinct rendered source canaries check the route
    interpretation. The sole public turnaround applies to this product scope.
    """
    if scope != {'sets': 1, 'job_sample': False, 'digital_proof': False}:
        raise ValueError('Explicit one-set, no-service scope required')
    if product.get('supplier') != 'fourover' or product.get('stage') != 'extracted':
        raise ValueError('Extracted 4over product required')
    source_url = product.get('supplier_url')
    diagnosis_id = product.get('diagnosis_id')
    if not source_url or not re.fullmatch(r'[a-f0-9]{32}', diagnosis_id or ''):
        raise ValueError('Exact source and diagnosis required')
    root = Path(root)
    diagnosis_path = root / 'diagnoses' / f'{diagnosis_id}.json'
    diagnosis = json.loads(diagnosis_path.read_text())
    if diagnosis.get('url') != source_url or diagnosis.get('supplier_id') != 'fourover':
        raise ValueError('Diagnosis source mismatch')
    services = {f.get('source_name'): f for f in diagnosis.get('fields', [])
                if f.get('kind') == 'checkbox'}
    if (set(services) != {'job_sample', 'digital_proof'}
            or any(services[name].get('selected_values') != [] for name in services)):
        raise ValueError('Source service defaults are not proven off')
    option_record = product.get('fourover_option_map_evidence') or {}
    price_record = product.get('fourover_price_table_evidence') or {}
    draft_record = product.get('fourover_rows_evidence') or {}
    option_map = json.loads(_read_evidence(root, option_record, 'fourover-option-map.json'))
    price_tables = json.loads(_read_evidence(root, price_record, 'fourover-price-tables.json'))
    draft_body = _read_evidence(root, draft_record, 'fourover-draft.csv')
    if (option_record.get('source_url') != source_url or price_record.get('source_url') != source_url
            or draft_record.get('source_url') != source_url
            or draft_record.get('source_option_map_sha256') != option_record.get('sha256')
            or draft_record.get('source_price_table_sha256') != price_record.get('sha256')
            or option_map.get('price_table_sha256') != price_record.get('sha256')
            or option_map.get('source_url') != source_url or price_tables.get('source_url') != source_url
            or option_map.get('expected_children') != option_map.get('mapped_children')
            or option_map.get('mapped_children') != len(option_map.get('rows', []))
            or price_tables.get('child_count') != option_map.get('mapped_children')
            or price_tables.get('priced_count') != price_tables.get('child_count')):
        raise ValueError('4over option and price coverage incomplete')
    public_sha = draft_record.get('public_page_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', public_sha):
        raise ValueError('Public product page evidence missing')
    public_record = {'object_path':f'objects/{public_sha[:2]}/{public_sha}.public-source.json',
                     'sha256':public_sha}
    public = json.loads(_read_evidence(root, public_record, 'public-source.json'))['public_data']
    content = public.get('product_content') or {}
    specs = [item.get('values') for item in content.get('specifications', [])
             if item.get('label') == 'Turnaround Time']
    production = draft_record.get('product_level_production_reference')
    if (content.get('source_url') != source_url or len(specs) != 1 or specs[0] != [production]
            or not production or not draft_record.get('image_url')):
        raise ValueError('Single product-level production and image evidence required')
    expected, columns = package_rows(product['title'], source_url, option_map, price_tables,
                                      image_url=draft_record['image_url'], production_reference=production)
    saved = list(csv.DictReader(io.StringIO(draft_body.decode('utf-8-sig'))))
    canonical = lambda rows: [tuple(str(row.get(column, '')) for column in columns) for row in rows]
    if (len(saved) != draft_record.get('row_count') or canonical(saved) != canonical(expected)
            or any(row.get('Source URL') != source_url for row in saved)):
        raise ValueError('Saved draft differs from independently rebuilt rows')
    canaries = [item for item in product.get('fourover_rendered_canaries') or []
                if item.get('status') == 'matched'
                and item.get('source_price_table_sha256') == price_record['sha256']
                and item.get('scope') in {'one_rendered_configuration_price_no_cart_or_order',
                                           'one_rendered_shipping_step_subtotal_no_cart_or_order'}
                and item.get('matched_quantities')]
    children = {str(item['child_id']) for item in option_map['rows']}
    if len({item.get('child_id') for item in canaries if item.get('child_id') in children}) < 2:
        raise ValueError('Two distinct source price canaries required')
    review_id = uuid.uuid4().hex
    target_root = root / 'finalizations'
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.fourover-review-', dir=target_root))
    try:
        customer_rows = [{**row, 'Verification':'Source package price and availability checked; fixed one-set scope'}
                         for row in saved]
        with (staging / 'finalized.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(customer_rows)
        report = {'id':review_id,'product_id':product['id'],'diagnosis_id':diagnosis_id,
                  'source_url':source_url,'source':'fourover_finite_one_set',
                  'scope':scope,'examined_rows':len(saved),'eligible_rows':len(saved),
                  'held_rows':0,'invalid_rows_removed':0,'hidden_choice_rows_omitted':0,
                  'catalog_complete':True,'supplier_requests_made':0,
                  'coverage':{'mapped_children':len(children),'finite_package_rows':len(saved),
                              'production_reference':production,'matched_source_canaries':len(canaries)},
                  'evidence_sha256':{'option_map':option_record['sha256'],'price_table':price_record['sha256'],
                                     'draft':draft_record['sha256'],'public_page':public_sha},
                  'notes':['Only the explicitly chosen one-set, no-service catalog scope is certified.',
                           'Supplier shipping is reference data and is not a product completion gate.']}
        (staging / 'report.json').write_text(json.dumps(report, indent=2))
        (staging / 'scope.json').write_text(json.dumps(scope, indent=2))
        for path in staging.iterdir(): os.chmod(path, 0o600)
        staging.rename(target_root / review_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def digital_envelope_production_rows(option_map: dict, price_tables: dict,
                                     grids: list[dict]) -> list[str]:
    """Choose only the rendered fee-free production mode for each exact tier."""
    mappings=option_map['rows']
    prices={item['child_id']:item for item in price_tables['observations']}
    indexed={item['child_id']:item for item in grids}
    children={item['child_id'] for item in mappings}
    if (len(children)!=len(mappings) or len(indexed)!=len(grids)
            or children!=set(indexed) or children!=set(prices)):
        raise ValueError('Production grids do not cover every mapped child')
    result=[]
    for mapping in mappings:
        child=mapping['child_id']
        stock=next((field['choice'] for field in mapping['fields']
                    if field['label'].strip().rstrip('*').strip()=='Stock'),None)
        if not stock:
            raise ValueError('Mapped envelope stock unavailable')
        security='Security Tint' in stock
        selected='2' if security else '4'
        expected_values=['1','2'] if security else ['1','2','4']
        grid=indexed[child]
        tiers=grid.get('tiers')
        priced=prices[child].get('package_prices')
        if (grid.get('child_id')!=child or grid.get('source_url')!=option_map.get('source_url')
                or grid.get('scope')!='rendered_package_and_turnaround_choices_only_no_order'
                or not isinstance(tiers,list) or not isinstance(priced,list)
                or len(tiers)!=len(priced)
                or [tier.get('quantity') for tier in tiers]!=[tier.get('quantity') for tier in priced]):
            raise ValueError('Production tier and source price coverage differ')
        for tier in tiers:
            choices=tier.get('turnaround_choices')
            if (not isinstance(choices,list)
                    or [choice.get('value') for choice in choices]!=expected_values
                    or any(choice.get('disabled') is not False for choice in choices)):
                raise ValueError('Production availability changed for a mapped tier')
            chosen=next(choice for choice in choices if choice['value']==selected)
            label=chosen.get('label')
            if label!=f'{selected} Business Days' or '$' in label:
                raise ValueError('Fee-free production choice unavailable')
            checked=[choice['value'] for choice in choices if choice.get('checked') is True]
            if checked!=([] if security else ['4']):
                raise ValueError('Unexpected source production selection')
            result.append(label)
    return result


def review_digital_envelopes_one_set(product: dict, root: Path, scope: dict) -> dict:
    """Certify all finite envelope tiers at their exact fee-free production choice."""
    expected_scope={'sets':1,'job_sample':False,'digital_proof':False,
                    'production':'fee_free_rendered_choice_by_child'}
    if scope!=expected_scope or product.get('supplier')!='fourover' or product.get('stage')!='extracted':
        raise ValueError('Explicit extracted Digital Envelopes scope required')
    if product.get('supplier_url')!='https://4over.com/digital-envelopes':
        raise ValueError('Exact Digital Envelopes source required')
    root=Path(root)
    diagnosis_id=product.get('diagnosis_id','')
    if not re.fullmatch(r'[a-f0-9]{32}',diagnosis_id):
        raise ValueError('Exact diagnosis required')
    diagnosis=json.loads((root/'diagnoses'/f'{diagnosis_id}.json').read_text())
    services={field.get('source_name'):field for field in diagnosis.get('fields',[])
              if field.get('kind')=='checkbox'}
    if (diagnosis.get('url')!=product['supplier_url']
            or set(services)!={'job_sample','digital_proof'}
            or any(field.get('selected_values')!=[] for field in services.values())):
        raise ValueError('Supplier diagnosis or fixed service defaults differ')
    option_record=product.get('fourover_option_map_evidence') or {}
    price_record=product.get('fourover_price_table_evidence') or {}
    draft_record=product.get('fourover_rows_evidence') or {}
    option_map=json.loads(_read_evidence(root,option_record,'fourover-option-map.json'))
    price_tables=json.loads(_read_evidence(root,price_record,'fourover-price-tables.json'))
    draft_body=_read_evidence(root,draft_record,'fourover-draft.csv')
    if (option_record.get('source_url')!=product['supplier_url']
            or price_record.get('source_url')!=product['supplier_url']
            or draft_record.get('source_url')!=product['supplier_url']
            or option_map.get('price_table_sha256')!=price_record.get('sha256')
            or draft_record.get('source_option_map_sha256')!=option_record.get('sha256')
            or draft_record.get('source_price_table_sha256')!=price_record.get('sha256')
            or option_map.get('expected_children')!=option_map.get('mapped_children')
            or price_tables.get('priced_count')!=price_tables.get('child_count')
            or price_tables.get('child_count')!=option_map.get('mapped_children')):
        raise ValueError('Saved envelope option and price coverage incomplete')
    image_url=draft_record.get('image_url')
    public_sha=draft_record.get('public_page_sha256','')
    if not image_url or not re.fullmatch(r'[a-f0-9]{64}',public_sha):
        raise ValueError('Public source image evidence missing')
    public=json.loads(_read_evidence(root,{'object_path':f'objects/{public_sha[:2]}/{public_sha}.public-source.json',
                                           'sha256':public_sha},'public-source.json'))['public_data']
    if public.get('product_content',{}).get('source_url')!=product['supplier_url']:
        raise ValueError('Public source page mismatch')
    expected,columns=package_rows(product['title'],product['supplier_url'],option_map,price_tables,
                                  image_url=image_url,production_reference='')
    saved=list(csv.DictReader(io.StringIO(draft_body.decode('utf-8-sig'))))
    canonical=lambda rows:[tuple(str(row.get(column,'')) for column in columns) for row in rows]
    if len(saved)!=draft_record.get('row_count') or canonical(saved)!=canonical(expected):
        raise ValueError('Saved envelope draft differs from source tables')
    grids=[]
    grid_hashes={}
    for mapping in option_map['rows']:
        child=mapping['child_id']
        path=root.parent/f'digital-envelopes-production-grid-{child}-20260927.json'
        if path.is_symlink() or not path.is_file():
            raise ValueError('Saved exact-child production grid unavailable')
        body=path.read_bytes()
        grid=json.loads(body)
        if grid.get('option_map_sha256')!=option_record['sha256']:
            raise ValueError('Production grid option map changed')
        grids.append(grid)
        grid_hashes[child]=hashlib.sha256(body).hexdigest()
    production=digital_envelope_production_rows(option_map,price_tables,grids)
    if len(production)!=len(saved) or len(saved)!=236:
        raise ValueError('Complete finite envelope row coverage required')
    canaries=[item for item in product.get('fourover_rendered_canaries') or []
              if item.get('status')=='matched' and item.get('source_price_table_sha256')==price_record['sha256']]
    if len({item.get('child_id') for item in canaries})<2:
        raise ValueError('Two distinct rendered source price canaries required')
    observations={item['child_id']:item for item in price_tables['observations']}
    for child in ('8938','8939'):
        path=root.parent/f'digital-envelopes-tier25-turnaround2-pilot-{child}-20260927.json'
        if child=='8939':
            path=root.parent/'digital-envelopes-turnaround2-pilot-8939-20260927.json'
        pilot=json.loads(path.read_text())
        totals=[shape.get('totals') for shape in pilot.get('project_response_shapes',[]) if shape.get('totals')]
        base=Decimal(str(observations[child]['package_prices'][0]['price'])).quantize(
            Decimal('0.01'),rounding=ROUND_HALF_UP)
        if (pilot.get('selected_child_id')!=child or pilot.get('selected_turnaround_time')!=2
                or pilot.get('shipping_rate_binding',{}).get('status')!='matched'
                or not totals or Decimal(str(totals[-1].get('grand_total'))) != base
                or Decimal(str(totals[-1].get('subtotal'))) != base
                or totals[-1].get('turnaround_time') is not None):
            raise ValueError('Security Tint fee-free production quote mismatch')
    final_columns=[column for column in columns if column!='Production Time (product page reference)']
    index=final_columns.index('Currency')+1
    final_columns.insert(index,'Production Time')
    final_rows=[]
    for row,label in zip(saved,production,strict=True):
        clean={key:value for key,value in row.items()
               if key!='Production Time (product page reference)'}
        clean['Production Time']=label
        clean['Verification']='Exact source package tier and rendered production choice checked; one set, services off'
        final_rows.append(clean)
    review_id=uuid.uuid4().hex
    target_root=root/'finalizations'
    target_root.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.digital-envelope-review-',dir=target_root))
    try:
        with (staging/'finalized.csv').open('w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.DictWriter(stream,fieldnames=final_columns)
            writer.writeheader();writer.writerows(final_rows)
        (staging/'production-grids.json').write_text(json.dumps(grids,indent=2))
        report={'id':review_id,'product_id':product['id'],'diagnosis_id':diagnosis_id,
                'source_url':product['supplier_url'],'source':'fourover_digital_envelopes_fee_free_production',
                'scope':scope,'examined_rows':len(saved),'eligible_rows':len(saved),'held_rows':0,
                'invalid_rows_removed':0,'hidden_choice_rows_omitted':0,'catalog_complete':True,
                'supplier_requests_made':0,
                'coverage':{'mapped_children':len(grids),'finite_package_rows':len(saved),
                            'four_business_day_rows':production.count('4 Business Days'),
                            'two_business_day_rows':production.count('2 Business Days'),
                            'matched_source_canaries':len(canaries)},
                'evidence_sha256':{'option_map':option_record['sha256'],'price_table':price_record['sha256'],
                                   'draft':draft_record['sha256'],'public_page':public_sha,
                                   'production_grids':grid_hashes},
                'notes':['Only one set, services off, and the fee-free production choice for each child are certified.',
                         'Paid rush production choices are held: a displayed rush fee differed by one cent from the project total.',
                         'The public 4/7-day specification is broader than the rendered 2/4-day choices; exact calculator choices govern these rows.',
                         'Supplier shipping is reference data; PrintItPrints applies its own shipping algorithm.']}
        (staging/'report.json').write_text(json.dumps(report,indent=2))
        (staging/'scope.json').write_text(json.dumps(scope,indent=2))
        for path in staging.iterdir():os.chmod(path,0o600)
        staging.rename(target_root/review_id)
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True)
        raise
    return report
