"""Customer-facing option inventory and cached finite row plans."""
from __future__ import annotations

import asyncio
import csv
import hashlib
from collections import defaultdict
import json
import time

from sqlalchemy import select, func

from .source_domains import DomainJob, DomainTask
from .store import Product, Run, digest
from .variation_inventory import load_inventories, missing_table_manifest
from .variation_matrix import VariationMatrix
from .commerce import unit_price

LABELS = {'coating':'Coating', 'spotCoating':'Spot coating', 'raisedPrint':'Raised printing',
          'raisedFoilColor':'Raised foil color', 'windPosition':'Wind position'}
OPTION_TYPES = {'coating': {'5','6','40'}, 'spotCoating':{'3','4','39'},
                'raisedPrint':{str(i) for i in range(135,141)},
                'raisedFoilColor':{'141','142','143'}, 'windPosition':{'32','33','34','35','54'}}


def _choices(values):
    return [{'key':str(item['id']), 'label':item.get('label') or 'Label unavailable'} for item in values]


def public_product_plan(inventory, matrix_report=None):
    spec = inventory['specification']; options = {}; quantities = set()
    for entry in inventory['bases']:
        for item in entry['catalog']['items']:
            options[str(item['type']['id'])] = item['type']
        domain = entry['base_support'] or entry['base_prices'] or {'items':[]}
        quantities.update(item['quantity'] for item in domain['items'])
    dimensions = []
    for key, types in OPTION_TYPES.items():
        values = [v for k,v in options.items() if k in types]
        if values:
            choices = ([] if key == 'windPosition' else [{'key':'','label':'None'}]) + _choices(values)
            dimensions.append({'label':LABELS[key], 'choices':choices})
    for key,label in [('shapes','Shape'),('finishes','Finish'),('turnarounds','Production')]:
        if spec.get(key): dimensions.append({'label':label,'choices':_choices(spec[key])})
    services = [item for key,item in options.items() if key in {'1','31','36','41','42','43'}]
    if services:
        dimensions.append({'label':'Processing and design services', 'choices':_choices(services),
                           'notes':'This row plan covers online and manual processing. Design-service rows need their separate pricing context.'})
    colors = _choices(spec.get('colors', []))
    for choice in colors:
        if choice['key'] in {'1','2','3'}:
            choice['label'] += ' (' + {'1':'4/0','2':'4/1','3':'4/4'}[choice['key']] + ')'
    report = matrix_report or {}
    sizes = []
    for item in spec.get('sizes', []):
        counts = report.get('count_by_size', {}).get(str(item['id']))
        sizes.append({'key':str(item['id']), 'label':item.get('label') or 'Label unavailable',
            'candidate_rows':sum(counts.values()) if counts is not None else None,
            'valid_rows':counts.get('supported',0) if counts and set(counts) <= {'supported'} else None,
            'priced_rows':None})
    missing = missing_table_manifest(inventory) if inventory.get('context') else {'requests':[]}
    notes = [
        'Counts use saved option tables and site rules, with separate rows for distinct choices. They are not a complete-catalog claim.',
        'Quantity presets are a combined inventory; availability depends on stock, color, finishing and production.',
    ]
    if inventory.get('product_type') == '1':
        notes.append('Business Cards exposes one Round Corners option for all four edges. Separate radius and corner-count choices were not found.')
        notes.append('Candidate rows include online processing and the two observed manual-processing services. Combined availability and price checks remain pending.')
    elif not report:
        notes.append('Option inventory is available. A complete finishing matrix has not yet been generated for this product.')
    if report and inventory.get('product_type') != '1':
        notes.append('Candidate rows enumerate captured selectors, finishing, quantities and processing services. Combined availability and price checks remain pending.')
    return {'product_id':inventory['product_id'], 'product_type':inventory.get('product_type'),
        'title':inventory['title'], 'product_url':inventory['product_url'],
        'status':'candidate_matrix_ready' if report else 'inventory_available' if spec else 'awaiting_inventory',
        'counts':{'candidate_rows':report.get('candidate_rows'), 'valid_rows':report.get('valid_rows'), 'priced_rows':report.get('priced_rows')},
        'inventory':{'sizes':_choices(spec.get('sizes', [])), 'stocks':_choices(spec.get('papers', [])),
            'colorspecs':colors, 'corners':([{'key':'','label':'None'}, *_choices([options['2']])] if '2' in options else []),
            'quantities':sorted(quantities), 'other_dimensions':dimensions},
        'size_plans':sizes, 'notes':notes,
        'table_subtotal_rows':report.get('table_subtotal_rows'),
        'non_cost_variants':([{'label':'Paper finish', 'choices':[{'key':'1','label':'Velvet'}],
            'notes':'A stock-specific description. The reviewed calculator does not add a separate charge for this field.'}]
            if inventory.get('product_type') in {'1','27'} and any(str(b['lookup']['request']['params'].get('paper')) in {'31','32','36','66'} for b in inventory['bases']) else []),
        'request_plan':{'missing_known_table_requests':len(missing['requests']), 'count_scope':'Known missing ordinary price and availability tables; excludes canaries and design-service contexts.'},
        'catalog_complete':False}


def build_plan(store, run_id):
    inventories = load_inventories(store, run_id)
    products = []
    for inventory in inventories:
        report = VariationMatrix(inventory).report() if inventory.get('product_type') in {'1','27','36'} and inventory['bases'] else None
        products.append(public_product_plan(inventory, report))
    return {'schema_version':1, 'generated_at':time.time(), 'products':products,
            'supplier_requests_made':0, 'notes':[]}


def export_candidate_rows(store, run_id, product_id):
    """Export explicitly provisional rows separately from customer quote exports."""
    inventory = next((i for i in load_inventories(store, run_id) if i['product_id'] == product_id), None)
    if inventory is None: raise LookupError('Product not found in this run')
    if inventory.get('product_type') not in {'1','27','36'} or not inventory['bases']:
        raise ValueError('A candidate matrix is not available for this product yet')
    target = store.root / 'variation-plans' / (digest([run_id,product_id])+'.csv')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.csv.tmp')
    fields = ['Product','Source URL','Shape','Size','Orientation','Stock','Finish','Colorspec','Rounded corners','Coating','Spot coating',
        'Raised printing','Raised foil color','Paper finish','Wind position','Quantity','Production mode',
        'Processing service','Calculated component subtotal (USD, before coupons)',
        'Calculated component unit price (USD, before coupons)',
        'Availability','Price verification','Amount scope']
    dimension_columns = {'Shape':'shape','Size':'size','Orientation':'orientation','Stock':'paper','Finish':'finish','Colorspec':'color','Rounded corners':'roundedCorners',
        'Coating':'coating','Spot coating':'spotCoating','Raised printing':'raisedPrint',
        'Raised foil color':'raisedFoilColor','Paper finish':'paperFinish','Wind position':'windPosition'}
    def safe(value):
        if isinstance(value, str) and (value.startswith(('\t','\r','\n')) or value.lstrip().startswith(('=','+','-','@'))):
            return "'" + value
        return value
    count = 0
    try:
        with temporary.open('w',newline='',encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
            for row in VariationMatrix(inventory).iter_rows():
                selected = row['selection']; config = row['configuration']
                values = {'Product':inventory['title'], 'Source URL':inventory['product_url'], 'Quantity':row['quantity'],
                    'Production mode':next((v['label'] for v in inventory['specification']['turnarounds']
                                           if str(v['id']) == selected['productionTime']), 'Label unavailable'),
                    'Processing service':(config.get('manualProofType',{}).get('label') if selected['proofType'] == '100'
                                          else config.get('proofType',{}).get('label')) or 'Label unavailable',
                    'Calculated component subtotal (USD, before coupons)':row.get('table_subtotal') if row.get('table_subtotal') is not None else '',
                    'Calculated component unit price (USD, before coupons)':unit_price(row['table_subtotal'], row['quantity']) if row.get('table_subtotal') is not None else '',
                    'Availability':'Candidate; combined availability pending',
                    'Price verification':('Calculated; verification pending' if row.get('table_subtotal') is not None
                                          else 'Unavailable; required components or context unresolved'),
                    'Amount scope':'Saved base and selected option tables; excludes coupons, shipping and tax'}
                for column,key in dimension_columns.items():
                    choice = config.get(key)
                    values[column] = (choice.get('label') or ('None' if choice.get('value') == '' else 'Label unavailable')) if choice else ''
                if selected['color'] in {'1','2','3'}:
                    values['Colorspec'] += ' (' + {'1':'4/0','2':'4/1','3':'4/4'}[selected['color']] + ')'
                writer.writerow({k:safe(v) for k,v in values.items()}); count += 1
        # Each download keeps an immutable content-addressed file, so a later
        # refresh cannot replace bytes after FileResponse chose its row count.
        with temporary.open('rb') as stream:
            content_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        target = target.with_name(target.stem + '-' + content_hash + '.csv')
        if not target.exists(): temporary.replace(target)
    finally:
        if temporary.exists(): temporary.unlink()
    return target, count


class VariationPlanCache:
    """One computation per evidence revision; poll reads never contact suppliers."""
    def __init__(self, store):
        self.store = store
        self.locks = defaultdict(asyncio.Lock)
        self.values = {}

    def _revision(self, run_id):
        with self.store.session() as session:
            if session.get(Run, run_id) is None: raise LookupError('Run not found')
            return tuple(session.execute(select(func.count(DomainTask.id),func.max(DomainJob.updated_at))
                .join(DomainJob, DomainJob.id == DomainTask.job_id)
                .where(DomainJob.run_id == run_id,DomainTask.status == 'succeeded')).one())

    async def get(self, run_id):
        async with self.locks[run_id]:
            revision = await asyncio.to_thread(self._revision,run_id)
            prior = self.values.get(run_id)
            if prior and prior[0] == revision: return prior[1]
            value = await asyncio.to_thread(build_plan,self.store,run_id)
            self.values[run_id] = (revision,value)
            # Retain only compact UI data, never source authentication/context.
            target = self.store.root / 'variation-plans' / (digest(run_id)+'.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.tmp'); temp.write_text(json.dumps(value)); temp.replace(target)
            return value

    async def export(self, run_id, product_id):
        async with self.locks[run_id]:
            await asyncio.to_thread(self._revision,run_id)
            return await asyncio.to_thread(export_candidate_rows,self.store,run_id,product_id)
