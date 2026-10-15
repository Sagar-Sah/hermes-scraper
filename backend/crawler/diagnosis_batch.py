"""Prepare independent product policies together before queuing any work."""
from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid5

from .diagnosis_policy import normalize_selection
from .diagnosis_run import compile_browser_policy
from .product_finalization import same_product
from .store import Product, Run


def prepare_batch(manager, products):
    if not isinstance(products, list) or not 1 <= len(products) <= 100:
        raise ValueError('Choose between 1 and 100 diagnosed products')
    results, specs, seen, urls = [], [], set(), []
    for item in products:
        key = item['diagnosis_id']
        if key in seen:
            raise ValueError('A product diagnosis can appear only once in a batch')
        seen.add(key)
        result = {'diagnosis_id': key, 'title': 'Product', 'status': 'blocked', 'reasons': []}
        try:
            diagnosis = manager.get(key)
            result['title'] = diagnosis.get('title') or diagnosis['url']
            if any(same_product(diagnosis['url'], url) for url in urls):
                raise ValueError('The same product URL appears more than once in this batch')
            urls.append(diagnosis['url'])
            if diagnosis['status'] != 'ready':
                raise ValueError('Analyze this product in the current supplier session before running it')
            policy = normalize_selection(diagnosis, item['revision'], item['fields'])
            candidate = deepcopy(diagnosis)
            candidate['selection'] = policy
            compiled = compile_browser_policy(candidate, [diagnosis['url']], 'browser', {})
            # Generic numeric controls provide a current value, not a finite
            # domain. Never advertise an all-values run from that observation.
            for field in policy['fields']:
                if field['configurable'] and field['include'] and field['kind'] == 'number':
                    raise ValueError(f"{field['label']} needs an explicit quantity or numeric scope before a full run")
                if field['configurable'] and field['include'] and not (field.get('source_id') or field.get('source_name')):
                    raise ValueError(f"{field['label']} has no supported source control binding")
            spec = {'name': result['title'][:160] + ' · reviewed fields',
                    'urls': [diagnosis['url']], 'titles': [], 'instructions': '',
                    'adapter': 'browser', 'image_mode': 'urls', 'shipping_contexts': [],
                    'refresh': False, 'browser_bulk_quantities': False,
                    'diagnosis_id': key, 'diagnosis_revision': item['revision'], **compiled}
            specs.append(spec)
            result.update(status='ready', policy_fingerprint=policy['fingerprint'])
        except (LookupError, ValueError) as exc:
            result['reasons'] = [str(exc) if str(exc) else 'This product needs diagnosis before extraction']
        results.append(result)
    token = hashlib.sha256(json.dumps({'results': results, 'specs': specs}, sort_keys=True,
                                       separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {'ready': all(r['status'] == 'ready' for r in results), 'plan_token': token,
            'products': results, 'planned_products': len(results),
            'pricing_request_count': None, 'eta_seconds': None,
            'note': 'Browser request counts and ETA are unknown until conditional options are discovered. Each product keeps its own field policy.',
            '_specs': specs}


def request_fingerprint(products):
    return hashlib.sha256(json.dumps(products, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def existing_batch(store, products, token, idempotency_key):
    """Recover a successful launch even after its browser pages have closed."""
    try:
        namespace = UUID(idempotency_key)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('A valid launch identifier is required') from None
    identity = request_fingerprint(products)
    with store.session() as session:
        first = session.get(Run, uuid5(namespace, '0').hex)
        if first is None:
            return None
        if (first.spec.get('diagnosis_batch_plan') != token
                or first.spec.get('diagnosis_batch_request') != identity
                or first.spec.get('diagnosis_batch_size') != len(products)):
            raise ValueError('This launch identifier already belongs to a different product request')
        ids = [uuid5(namespace, str(i)).hex for i in range(len(products))]
        for run_id in ids:
            run = session.get(Run, run_id)
            if run is None or run.spec.get('diagnosis_batch_request') != identity:
                raise ValueError('The saved batch is incomplete; review its runs before retrying')
        return ids


def queue_batch(store, plan, token, idempotency_key, *, request_identity=None):
    """One transaction creates all runs; repeat requests return those same runs."""
    if not plan['ready']:
        raise ValueError('Resolve every product blocker before starting this batch')
    if token != plan['plan_token']:
        raise ValueError('The product plan changed; review the current preflight again')
    try:
        namespace = UUID(idempotency_key)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('A valid launch identifier is required') from None
    runs, created = [], []
    with store.session(immediate=True) as session:
        for index, original in enumerate(plan['_specs']):
            # Index-based identities make reusing a key for a changed product
            # set collide visibly rather than create a second campaign.
            run_id = uuid5(namespace, str(index)).hex
            existing = session.get(Run, run_id)
            if existing:
                if existing.spec.get('diagnosis_batch_plan') != token:
                    raise ValueError('This launch identifier already belongs to a different product plan')
                runs.append(run_id)
                continue
            # A prior batch with more entries must not accept a shorter retry.
            if index == 0 and session.get(Run, uuid5(namespace, str(len(plan['_specs']))).hex):
                raise ValueError('This launch identifier already belongs to a different product plan')
            spec = deepcopy(original)
            spec.update(diagnosis_batch_id=str(namespace), diagnosis_batch_plan=token,
                        diagnosis_batch_request=request_identity,
                        diagnosis_batch_size=len(plan['_specs']))
            run = Run(id=run_id, name=spec['name'], status='queued', spec=spec)
            session.add(run)
            session.add(Product(run_id=run_id, url=spec['urls'][0]))
            runs.append(run_id)
            created.append(run_id)
    for run_id in created:
        store.event(run_id, 'run_created', {'products': 1, 'adapter': 'browser',
                                         'diagnosis_batch_id': str(namespace)})
    return runs
