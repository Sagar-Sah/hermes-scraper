"""Build a finite variation inventory from saved network evidence, without I/O.

Selectors, options and prices stay attached to their exact request context. A
matrix candidate is not automatically an available, priced or verified product.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select

from .route_dependencies import validate_unique_lookup, DependencyError
from .source_domains import DomainJob, DomainTask
from .store import Product, Response, ResponseObservation, canonical, digest

API = '/service/rest/v1/'
TABLE = re.compile(API + r'products/(\d+)/(prices|options)(?:/(\d+)/prices)?$')
CHOICE_FIELDS = {'size': 'sizes', 'paper': 'papers', 'color': 'colors',
                 'turnaround': 'turnarounds', 'shape': 'shapes', 'finish': 'finishes'}


@lru_cache(maxsize=1)
def rule_contract():
    path = Path(__file__).with_name('contracts') / 'printograph-constraints.json'
    return json.loads(path.read_text())


def selector_key(params):
    return canonical({k: str(v) for k, v in params.items()
                      if k in {'productType', 'size', 'orientation', 'paper', 'color', 'shape', 'finish'}})


def _body(store, row):
    raw = store.get_object(row['object_path'])
    if raw.startswith(b'\x1f\x8b'):
        raw = gzip.decompress(raw)
    if digest(raw.hex()) != row['raw_hash']:
        raise ValueError('Response content hash differs from saved evidence')
    return json.loads(raw)


def _exact_url(row):
    request = row['request']; parsed = urlsplit(row['url'])
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    params = {k: str(v).lower() if isinstance(v, bool) else str(v)
              for k, v in request['params'].items()}
    return (request['method'] == 'GET' and request.get('body') is None and
            f'{parsed.scheme}://{parsed.netloc}' == request['context']['origin'] and
            parsed.path == request['path'] and not parsed.fragment and
            len(pairs) == len(dict(pairs)) and dict(pairs) == params)


def load_inventories(store, run_id):
    """Return one inventory per product from its latest specification context.

    Only successful durable DOMAIN tasks with a matching observation qualify.
    The current browser selection is never substituted for old response context.
    """
    with store.session() as session:
        products = [dict(id=p.id, title=p.title, url=p.url) for p in session.scalars(
            select(Product).where(Product.run_id == run_id))]
        rows = [dict(r._mapping) for r in session.execute(select(
            DomainTask.request, DomainTask.request_key, DomainTask.products,
            Response.id.label('response_id'), Response.url, Response.object_path,
            Response.raw_hash, Response.created_at
        ).join(DomainJob, DomainJob.id == DomainTask.job_id)
         .join(Response, Response.id == DomainTask.response_id)
         .where(DomainJob.run_id == run_id, DomainTask.status == 'succeeded', Response.status == 200)
         .order_by(Response.created_at))]
        bindings = {(r.product_id, r.response_id, r.request_signature) for r in session.execute(
            select(ResponseObservation.product_id, ResponseObservation.response_id, ResponseObservation.request_signature)
            .where(ResponseObservation.run_id == run_id, ResponseObservation.kind == 'harvest'))}
    grouped = defaultdict(list)
    for row in rows:
        if not _exact_url(row):
            continue
        for product in row['products']:
            if (product, row['response_id'], row['request_key']) in bindings:
                grouped[product].append(row)
    bodies = {}
    def body(row):
        if row['object_path'] not in bodies:
            bodies[row['object_path']] = _body(store, row)
        return bodies[row['object_path']]
    results = []
    for product in products:
        observed = grouped[product['id']]
        specs = [r for r in observed if r['request']['path'] == API + 'settings/product/specifications']
        if not specs:
            results.append({'product_id': product['id'], 'title': product['title'], 'product_url': product['url'],
                            'product_type': None, 'bases': [], 'specification': {}, 'diagnostics': ['No saved specification response.']})
            continue
        spec = specs[-1]; context = spec['request']['context']; kind = str(spec['request']['params']['productType'])
        observed = [r for r in observed if r['request']['context'] == context]
        catalogs = {}; prices = {}; option_prices = defaultdict(dict)
        support = {}; option_support = {}; lookups = {}; diagnostics = []
        for row in observed:
            req = row['request']; path = req['path']; params = req['params']; match = TABLE.fullmatch(path)
            if match:
                base, family, option = match.groups()
                # Filtered prices, discount overrides and design mode never stand
                # in for a full ordinary table. Their context needs another plan.
                if family == 'prices' or option:
                    if any(k not in {'itemId', 'cid'} for k in params) or params.get('itemId', 'null') != 'null' or params.get('cid', '') != '':
                        continue
                    if option: option_prices[base][option] = row
                    else: prices[base] = row
                elif set(params) <= {'requestValues', 'eddm'} and str(params.get('eddm', 'false')).lower() == 'false':
                    catalogs[base] = row
            elif path in {API + 'products/quantities', API + 'products/options/quantities'}:
                if str(params.get('includeTurnaround', '')).lower() != 'true': continue
                expected = {'productType','size','orientation','paper','color','includeTurnaround'}
                if str(params.get('productType')) == '36': expected |= {'shape','finish'}
                if path.endswith('/options/quantities'): expected.add('optionIds')
                # A selected-quantity, mode or price-group filter is a different
                # domain and must not overwrite the full unfiltered support.
                if set(params) != expected: continue
                skey = selector_key(params)
                if path.endswith('/products/quantities'):
                    support[skey] = row
                else:
                    option_support.setdefault(skey, []).append(row)
            elif path == API + 'products' and str(params.get('productType')) == kind and 'turnaround' in params and 'size' in params and not {'offset', 'limit'} & params.keys():
                expected = {'productType','size','orientation','paper','color','turnaround'}
                if kind == '36': expected |= {'shape','finish'}
                if set(params) != expected: continue
                try:
                    lookup = validate_unique_lookup({'request': req, 'data': body(row),
                        'response_id': row['response_id'], 'status': 200, 'complete': True})
                    lookups[canonical(req)] = lookup
                except DependencyError as exc:
                    diagnostics.append(str(exc))
        bases = []
        for lookup in lookups.values():
            base = lookup['base_product_id']; params = lookup['request']['params']; skey = selector_key(params)
            cat = catalogs.get(base); price = prices.get(base); sup = support.get(skey); optsup = option_support.get(skey, [])
            refs = [lookup['response_id']] + [r['response_id'] for r in (cat, price, sup) if r] + [r['response_id'] for r in optsup]
            merged_support = {}; requested_types = set(); conflicted = set()
            for row in optsup:
                requested = set(str(row['request']['params']['optionIds']).split(','))
                requested_types.update(requested)
                for type_id, values in body(row).items():
                    if type_id not in requested: continue
                    if type_id in merged_support and merged_support[type_id] != values:
                        conflicted.add(type_id)
                    merged_support[type_id] = values
            for type_id in conflicted:
                merged_support.pop(type_id, None)
            if conflicted:
                diagnostics.append('Conflicting option quantity support for an exact selector; affected types remain unresolved.')
            labels = {}
            for key, plural in CHOICE_FIELDS.items():
                labels[key] = next((item.get('label', '') for item in body(spec).get(plural, [])
                                    if str(item['id']) == str(params.get(key))), '')
            bases.append({'lookup': lookup, 'source_labels': labels,
                'catalog': body(cat) if cat else {'items': []},
                'base_prices': body(price) if price else None,
                'base_support': body(sup) if sup else None,
                'option_support': merged_support,
                'option_support_requested_types': sorted(requested_types, key=int),
                'option_support_conflicted_types': sorted(conflicted, key=int),
                'option_prices': {oid: body(r) for oid, r in option_prices[base].items()},
                'evidence_ids': refs + [r['response_id'] for r in option_prices[base].values()],
                'table_evidence': {'catalog': cat['response_id'] if cat else None,
                    'base_prices': price['response_id'] if price else None,
                    'base_support': sup['response_id'] if sup else None,
                    'option_support': [r['response_id'] for r in optsup],
                    'option_prices': {oid:r['response_id'] for oid,r in option_prices[base].items()}}})
        results.append({'schema_version': 1, 'product_id': product['id'], 'title': product['title'],
            'product_url': product['url'], 'product_type': kind, 'context': context,
            'contract': rule_contract(), 'specification': body(spec), 'specification_evidence_id': spec['response_id'],
            'service_domains': [{'proofType': '31', 'fileDelivery': '5', 'designServices': ''},
                                {'proofType': '100', 'manualProofType': '1', 'fileDelivery': '5', 'designServices': ''},
                                {'proofType': '100', 'manualProofType': '36', 'fileDelivery': '5', 'designServices': ''}],
            'bases': bases, 'diagnostics': diagnostics,
            'source_context_verified_for_pricing': False,
            'catalog_complete': False})
    return results


def missing_table_manifest(inventory):
    """One full tier table per base/option and support request per exact selector.

    Includes online/manual processing choices. Design service is a separate
    query context and deliberately not silently priced with ordinary tables.
    """
    requests = {}; context = inventory['context']; product = inventory['product_id']
    priced = set(inventory['contract']['price_bearing_option_types'])
    def add(purpose, path, params, refs):
        request = {'method':'GET', 'path':API+path, 'params':params, 'context':context}
        requests[canonical(request)] = {'product_id':product, 'purpose':purpose,
                                      'evidence_ids':refs, 'request':request}
    for entry in inventory['bases']:
        base = entry['lookup']['base_product_id']; params = entry['lookup']['request']['params']
        selectors = json.loads(selector_key(params)); refs = [entry['lookup']['response_id']]
        if not entry['table_evidence']['catalog']:
            add('option_catalog', f'products/{base}/options', {'requestValues':'true','eddm':'false'}, refs)
        if entry['base_support'] is None:
            add('base_quantity_support', 'products/quantities', {**selectors,'includeTurnaround':'true'}, refs)
        relevant = [item for item in entry['catalog']['items'] if str(item['type']['id']) not in {'41','42','43'}]
        type_ids = sorted({str(item['type']['id']) for item in relevant}, key=int)
        if type_ids and not all(t in entry.get('option_support_requested_types', []) for t in type_ids):
            add('option_quantity_support', 'products/options/quantities',
                {**selectors,'optionIds':','.join(type_ids),'includeTurnaround':'true'}, refs)
        if entry['base_prices'] is None:
            add('tier_domain', f'products/{base}/prices', {'itemId':'null','cid':''}, refs)
        for item in relevant:
            if str(item['type']['id']) in priced and str(item['id']) not in entry['option_prices']:
                add('tier_domain', f'products/{base}/options/{item["id"]}/prices', {},
                    [entry['table_evidence']['catalog']])
    return {'phase':'DOMAIN','requests':list(requests.values()), 'unresolved':[
        'Saved tables support finite row planning; combined availability and canary verification remain separate.',
        'Design-service pricing and shipping are outside this ordinary online/manual processing table stage.']}
