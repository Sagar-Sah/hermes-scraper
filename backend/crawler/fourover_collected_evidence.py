"""Pinned cache-only 10359 evidence attachment. No supplier or CSV operations."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from .fourover_child_prices import FourOverChildPriceCollector, _encoded, _now
from .fourover_options import _bind, _unique_object

SOURCE = 'https://4over.com/z-fold-brochures'
PRODUCT = '10359'
CHILD = '160399'
RUN_SIZE = 500
SCOPE = {'sets': 1, 'job_sample': False, 'digital_proof': False}
VALUES = {'211':'438','197':'274','204':'18','199':'126','205':'466','216':'230'}
NEXT_CHILD = '160492'
ACTIVE_CHILD = '160495'
OBSERVATION_SELECTIONS = frozenset(((CHILD, RUN_SIZE), (NEXT_CHILD, RUN_SIZE), (ACTIVE_CHILD, RUN_SIZE), (ACTIVE_CHILD,1000), (ACTIVE_CHILD,2000)))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def mapped_product(manager, campaign_id):
    campaign = manager.require_active_group(campaign_id, PRODUCT)
    product = next((p for p in campaign['products'] if p['id'] == PRODUCT), None)
    if (campaign_id in manager.tasks or campaign.get('status') == 'running'
            or not product or product.get('supplier') != 'fourover' or product.get('supplier_url') != SOURCE
            or product.get('stage') != 'queued' or product.get('row_count', 0) != 0
            or product.get('verified_at') or product.get('finalized_at')):
        raise ValueError('Exact queued 10359 source required')
    return campaign, product


def load_cache(store, snapshot_id, checkpoint_sha256, option_rows_sha256, *, child_id=CHILD, package_quantity=RUN_SIZE):
    if type(package_quantity) is not int or (child_id, package_quantity) not in OBSERVATION_SELECTIONS:
        raise ValueError('Only exact reviewed historical pairs and160495/q2000 are supported')
    for value, length in ((snapshot_id,32),(checkpoint_sha256,64),(option_rows_sha256,64)):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}',value):
            raise ValueError('Exact bounded cache identifiers required')
    reader = FourOverChildPriceCollector(store, None)
    path = reader._path(snapshot_id, SOURCE)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8*1024*1024:
        raise ValueError('Pinned checkpoint unavailable')
    raw = path.read_bytes()
    if digest(raw) != checkpoint_sha256:
        raise ValueError('Pinned checkpoint changed')
    job = json.loads(raw,object_pairs_hook=_unique_object)
    if (job.get('schema') != 'hermes-fourover-child-price-collection/v1'
            or job.get('snapshot_id') != snapshot_id or job.get('source_url') != SOURCE
            or job.get('scope') != SCOPE or type(job['scope'].get('sets')) is not int
            or job['scope'].get('job_sample') is not False or job['scope'].get('digital_proof') is not False):
        raise ValueError('Pinned checkpoint context changed')
    children, contract = job.get('declared_children'), job.get('option_contract') or {}
    if (job.get('error') or job.get('status') != 'completed_requested_subset'
            or not isinstance(children,list) or len(children) != 705
            or contract.get('binding_complete') is not True
            or contract.get('source_data_complete') is not False
            or contract.get('catalog_complete') is not False or contract.get('final_eligible') is not False
            or not re.fullmatch(r'[a-f0-9]{64}',job.get('document_body_sha256',''))
            or job.get('document_body_sha256') != job.get('option_contract_document_sha256')):
        raise ValueError('Complete historical source cache required')
    ids = [row.get('child_id') for row in children if isinstance(row,dict)]
    if (len(ids) != 705 or len(set(ids)) != 705
            or any(not isinstance(v,str) or not re.fullmatch(r'[1-9][0-9]{0,12}',v) for v in ids)
            or any(not isinstance(row.get('sku'),str) or not 1 <= len(row['sku']) <= 160 for row in children)
            or set(job.get('observations',{})) != set(ids)):
        raise ValueError('Complete unique child membership required')
    rows = contract.get('rows')
    if not isinstance(rows,list) or digest(_encoded(rows)) != option_rows_sha256:
        raise ValueError('Pinned option binding changed')
    try:
        if len(contract['attributes']) != 6:raise ValueError()
        attrs = {a['id']:a for a in contract['attributes']}
        index = {row['child_id']:{f['attribute_id']:f['value'] for f in row['fields']} for row in rows}
        _, rebuilt = _bind({'attributes':attrs,'index':index},ids,2000)
    except (ValueError,KeyError,TypeError):
        raise ValueError('Saved option memberships invalid') from None
    if rebuilt != rows or len(rows) != 705 or set(attrs) != set(VALUES):
        raise ValueError('Complete exact saved bindings required')
    original = next((row for row in rows if row['child_id'] == CHILD),None)
    if not original or {f['attribute_id']:f['value'] for f in original['fields']} != VALUES:
        raise ValueError('Approved first child context changed')
    selected = next((row for row in rows if row['child_id'] == child_id),None)
    if not selected or {f['attribute_id'] for f in selected['fields']} != set(VALUES):
        raise ValueError('Reviewed selected child context missing')
    selection = {'child_id':child_id, 'run_size':package_quantity,
                 'attributes':{f['attribute_id']:f['value'] for f in selected['fields']}}
    observations, total_rows, total_bytes = [], 0, 0
    for child in ids:
        record = job['observations'][child]
        if record.get('status') != 'collected':
            raise ValueError('Uncollected child cannot attach')
        sha, relative = record.get('sha256'),record.get('object_path')
        if (not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{64}',sha)
                or relative != f'objects/{sha[:2]}/{sha}.fourover-child-price.json'):
            raise ValueError('Immutable evidence path invalid')
        unresolved = Path(store.root)/relative; source = unresolved.resolve()
        if unresolved.is_symlink() or not source.is_relative_to(Path(store.root).resolve()) or not source.is_file():
            raise ValueError('Immutable local evidence unavailable')
        size = source.stat().st_size
        total_bytes += size
        if size > 2*1024*1024 or total_bytes > 32*1024*1024:
            raise ValueError('Bounded immutable evidence required')
        raw = source.read_bytes()
        if len(raw) != size or digest(raw) != sha:
            raise ValueError('Immutable evidence changed')
        value = json.loads(raw,object_pairs_hook=_unique_object)
        if (value.get('source_url') != SOURCE or value.get('child_id') != child or value.get('scope') != SCOPE
                or type(value['scope'].get('sets')) is not int
                or value['scope'].get('job_sample') is not False or value['scope'].get('digital_proof') is not False):
            raise ValueError('Immutable evidence context changed')
        package, tiers = value.get('package_prices'), value.get('tier_prices')
        if len(raw) > 2*1024*1024 or total_bytes > 32*1024*1024 or not isinstance(package,list) or not 1 <= len(package) <= 1000 or tiers != []:
            raise ValueError('Bounded finite package evidence required')
        quantities = set()
        for row in package:
            try:
                q, text = row['quantity'],row['price']
                if not isinstance(text,str) or not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,18})?',text):
                    raise ValueError()
                amount = Decimal(text)
                if type(q) is not int or q < 1 or q in quantities or not amount.is_finite() or amount <= 0:
                    raise ValueError()
                quantities.add(q)
            except (KeyError,TypeError,ArithmeticError,ValueError):
                raise ValueError('Invalid exact package price') from None
        total_rows += len(package)
        observations.append({'child_id':child,'status':'priced','package_prices':package,'tier_prices':[],
                             'source_record_status':'collected','source_evidence':{k:record[k] for k in ('child_id','status','sha256','object_path')},
                             'price_basis':'historical_source_child_table_not_rendered_payable_total',
                             'payable_total_verified':False,'source_scope':SCOPE,
                             'source_captured_at':value.get('captured_at')})
    if total_rows != 12810:
        raise ValueError('Approved complete source row count changed')
    chosen = next(v for v in observations if v['child_id'] == child_id)
    tier = next((v for v in chosen['package_prices'] if v['quantity'] == package_quantity),None)
    if tier is None:
        raise ValueError('Approved run size missing')
    return {'job':job,'rows':rows,'observations':observations,'selected':selected,'selection':selection,
            'historical_price':tier['price'],'checkpoint_path':path,
            'provenance':{'snapshot_id':snapshot_id,'checkpoint_sha256':checkpoint_sha256,
                          'option_rows_sha256':option_rows_sha256,
                          'option_contract_sha256':digest(_encoded(contract)),
                          'source_document_sha256':job['option_contract_document_sha256']}}


def attach_cache(manager,campaign_id,snapshot_id,checkpoint_sha256,option_rows_sha256):
    """Validate everything, stage immutable objects, then write both pointers once."""
    with manager.mutation_lock:
        data, product = mapped_product(manager,campaign_id)
        loaded = load_cache(manager.store,snapshot_id,checkpoint_sha256,option_rows_sha256)
        provenance = loaded['provenance']
        old_price, old_map = product.get('fourover_price_table_evidence'), product.get('fourover_option_map_evidence')
        if old_price or old_map:
            for record,suffix in ((old_price,'fourover-price-tables.json'),(old_map,'fourover-option-map.json')):
                if not record or record.get('cache_provenance') != provenance:
                    raise ValueError('Existing evidence requires explicit review before replacement')
                relative = record.get('object_path','')
                if not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.'+re.escape(suffix),relative):
                    raise ValueError('Existing evidence path invalid')
                if digest(manager.store.get_object(relative)) != record.get('sha256'):
                    raise ValueError('Existing attached evidence changed')
            return {'product_id':PRODUCT,'status':'already_attached_historical_evidence',
                    'final_eligible':False,'rows_materialized':0,'supplier_requests':0}
        at = _now()
        flags = {'cart_verified':False,'shipping_verified':False,'production_verified':False,
                 'final_eligible':False,'cache_provenance':provenance,'observed_at':at}
        price = {'schema':'hermes-fourover-child-price-tables/v1','source_url':SOURCE,
                 'children':loaded['job']['declared_children'],'observations':loaded['observations'],
                 'child_count':705,'priced_count':705,'coverage':'all_source_child_routes',**flags}
        price_raw = _encoded(price); price_hash = digest(price_raw)
        price_path = manager.store.put_object(price_raw,'fourover-price-tables.json')
        mapping = {'schema':'hermes-fourover-option-map/v1','source_url':SOURCE,'rows':loaded['rows'],
                   'expected_children':705,'mapped_children':705,'complete_child_mapping':True,
                   'price_table_sha256':price_hash,**flags}
        map_raw = _encoded(mapping); map_hash = digest(map_raw)
        map_path = manager.store.put_object(map_raw,'fourover-option-map.json')
        # Both immutable objects exist before the sole atomic campaign update.
        product['fourover_price_table_evidence'] = {'source_url':SOURCE,'object_path':price_path,
            'sha256':price_hash,'child_count':705,'priced_count':705,'complete_child_routes':True,
            'tier_count':12810,'cache_provenance':provenance,'observed_at':at,'final_eligible':False}
        product['fourover_option_map_evidence'] = {'source_url':SOURCE,'object_path':map_path,
            'sha256':map_hash,'expected_children':705,'mapped_children':705,'complete_child_mapping':True,
            'cache_provenance':provenance,'observed_at':at,'final_eligible':False}
        product['updated_at'] = data['updated_at'] = at
        manager._write(data)
        return {'product_id':PRODUCT,'status':'attached_historical_evidence_only',
                'price_table_sha256':price_hash,'option_map_sha256':map_hash,
                'children':705,'exact_source_rows':12810,'product_stage':product['stage'],
                'rows_materialized':0,'supplier_requests':0,'final_eligible':False}
