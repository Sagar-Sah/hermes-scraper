"""Offline publication gate for an explicitly reviewed product field policy.

Original records are immutable. A report distinguishes invalid source rows,
rows outside the selected fixed choices, and rows still needing evidence.
Only eligible rows enter the separate customer CSV. This module never makes
supplier requests, edits a cart, or changes a live product.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from urllib.parse import urlsplit

from sqlalchemy import select

from .diagnosis_policy import apply_selection, classify_finalization
from .service_context import canonical_service_context
from .store import Product, Run
from .variation_inventory import load_inventories
from .variation_matrix import VariationMatrix


def same_product(left, right):
    """Only observed public host aliases; a different query is a different page."""
    def identity(url):
        try:
            parts = urlsplit(str(url))
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
                return None
            return (parts.scheme, parts.hostname.removeprefix('www.'),
                    parts.port, parts.path.rstrip('/') or '/', parts.query)
        except ValueError:
            return None
    first, second = identity(left), identity(right)
    return first is not None and first == second


def saved_products(store, diagnosis):
    with store.session() as session:
        pairs = session.execute(select(Product, Run).join(Run, Run.id == Product.run_id)
                                .order_by(Run.created_at.desc())).all()
        items = [{'run_id': run.id, 'run_name': run.name, 'product_id': product.id,
                 'title': product.title or diagnosis.get('title') or 'Product',
                 'row_count': product.row_count, 'source': 'quotes'}
                for product, run in pairs if same_product(product.url, diagnosis['url'])]
    inventories = {}
    result = []
    for item in items:
        result.append(item)
        run_id = item['run_id']
        if run_id not in inventories:
            inventories[run_id] = load_inventories(store, run_id)
        if any(inv['product_id'] == item['product_id'] and inv.get('bases')
               and inv.get('product_type') in {'1','27','36'} for inv in inventories[run_id]):
            result.append({**item, 'source': 'matrix', 'row_count': None})
    return result


def _values(value):
    if isinstance(value, dict):
        if 'value' not in value:
            return None
        return _values(value['value'])
    if isinstance(value, (tuple, list)):
        result = [_values(item) for item in value]
        return None if any(item is None for item in result) else sorted(v for part in result for v in part)
    if value is None or isinstance(value, bool):
        return None
    return [str(value)]


def bind_configuration(policy, row):
    """Bind source names to stored controls, without matching labels or guessing.

Printograph's existing browser adapter canonicalizes a finite alias dictionary.
Other adapters require an exact stored key. Missing or conflicting keys remain
unresolved; a diagnosis cannot supply a row's unobserved service context.
"""
    from .adapters.browser import ALIASES
    entries = {}
    def add(key, value):
        entries.setdefault(key, []).append(_values(value))
    for name in ('selection', 'configuration'):
        for key, value in (row.get(name) or {}).items():
            add(key, value)
    if row.get('quantity') is not None:
        add('quantity', row['quantity'])
    if isinstance(row.get('production'), dict) and 'value' in row['production']:
        add('productionTime', row['production'])
    services = canonical_service_context(row)
    for key, group in services['groups'].items():
        if group['status'] == 'selected':
            add(key, group['values'])
    printograph = urlsplit(policy['source_url']).hostname in {'printograph.com', 'www.printograph.com'}
    result, issues = {}, []
    claimed = {}
    for field in policy['fields']:
        if not field.get('configurable'):
            continue
        candidates = {field['key']}
        for key in ('source_name', 'source_id', 'source_key'):
            raw = field.get(key)
            if not raw:
                continue
            candidates.add(raw)
            if printograph:
                normalized = re.sub(r'[^a-z0-9]', '', raw.casefold())
                if normalized in ALIASES:
                    candidates.add(ALIASES[normalized])
                elif raw in set(ALIASES.values()):
                    candidates.add(raw)
        matches = candidates & entries.keys()
        values = [value for key in matches for value in entries[key]]
        if not values or any(value is None or value != values[0] for value in values):
            issues.append('field_binding_missing_or_conflicting:' + field['label'])
            continue
        if any(key in claimed for key in matches):
            issues.append('field_binding_ambiguous:' + field['label'])
            continue
        for key in matches:
            claimed[key] = field['key']
        result[field['key']] = values[0]
    for key in entries.keys() - claimed.keys():
        issues.append('configuration_field_not_diagnosed:' + key)
    return result, issues


def _safe(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


def _public_reason(reason, policy):
    prefix, separator, key = reason.partition(':')
    if separator:
        if prefix in {'configuration_field_not_diagnosed','undiagnosed_configuration_field'}:
            return 'Additional source fields need diagnosis'
        labels = {field['key']: field['label'] for field in policy['fields']}
        if key in labels:
            return prefix + ': ' + labels[key]
    return reason


def _customer_row(diagnosis, policy, row, projected):
    result = {'Source URL': policy['source_url']}
    content = {f['key'] for f in policy['fields'] if not f.get('configurable') and f['include']}
    for key, column in (('title', 'Product title'), ('description', 'Description'), ('images', 'Images')):
        if 'content_' + key in content:
            result[column] = row.get(key, diagnosis.get('content', {}).get(key, ''))
    reserved = {'Source URL', 'Product title', 'Description', 'Images', 'Subtotal', 'Currency', 'Verification'}
    used = {_safe(key) for key in reserved}
    for label, value in projected['public_configuration'].items():
        if isinstance(value, list):
            value = '; '.join(str(item) for item in value)
        base = 'Option: ' + label if label in reserved else label
        key, index = base, 1
        while _safe(key) in used:
            index += 1
            key = f'{base} ({index})'
        used.add(_safe(key))
        result[key] = value
    price = row['finalization_evidence']['price']
    result.update(Subtotal=price['subtotal'], Currency=price['currency'], Verification='Full configuration checked')
    return {_safe(key): _safe(value) for key, value in result.items()}


def review_rows(rows, diagnosis, output_root, *, run_id, product_id, matrix=None):
    """Stream a separate publication generation; never rewrite source records."""
    policy = diagnosis['selection']
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    review_id = uuid.uuid4().hex
    staging = Path(tempfile.mkdtemp(prefix='.review-', dir=root))
    target = root / review_id
    counts, reasons = Counter(), Counter()
    columns = set()
    try:
        with (staging / 'rows.jsonl').open('w', encoding='utf-8') as audit, \
             (staging / 'eligible.jsonl').open('w+', encoding='utf-8') as spool:
            for incoming in rows:
                row = deepcopy(incoming)
                counts['examined_rows'] += 1
                checked = classify_finalization(row)
                bound, binding_issues = bind_configuration(policy, row)
                source_url = row.get('product_url') or row.get('source_url')
                source_issues = []
                if not source_url:
                    source_issues.append('row_source_url_missing')
                elif not same_product(source_url, policy['source_url']):
                    source_issues.append('row_source_url_mismatch')
                if row.get('product_url') and row.get('source_url') and not same_product(row['product_url'], row['source_url']):
                    source_issues.append('conflicting_row_source_urls')
                projected = apply_selection(policy, bound)
                why = []
                if source_issues:
                    status = 'held'
                    counts['held_rows'] += 1
                    why = source_issues
                elif checked['status'] == 'invalid':
                    status = 'invalid'
                    counts['invalid_rows_removed'] += 1
                    why = checked['reasons']
                elif projected['status'] == 'excluded':
                    status = 'outside_fixed_choices'
                    counts['hidden_choice_rows_omitted'] += 1
                    why = projected['reasons']
                elif checked['status'] != 'eligible' or projected['status'] != 'included' or binding_issues:
                    status = 'held'
                    counts['held_rows'] += 1
                    why = sorted(set(checked['reasons'] + projected['reasons'] + binding_issues))
                else:
                    status = 'eligible'
                    counts['eligible_rows'] += 1
                    customer = _customer_row(diagnosis, policy, row, projected)
                    columns.update(customer)
                    spool.write(json.dumps(customer, ensure_ascii=False) + '\n')
                reasons.update({_public_reason(reason, policy) for reason in why})
                audit.write(json.dumps({'row': counts['examined_rows'],
                    'record_key': row.get('variation_key') or row.get('id'), 'status': status,
                    'reasons': why}, ensure_ascii=False) + '\n')
            spool.seek(0)
            if counts['eligible_rows']:
                with (staging / 'finalized.csv').open('w', newline='', encoding='utf-8-sig') as output:
                    writer = csv.DictWriter(output, fieldnames=sorted(columns))
                    writer.writeheader()
                    for line in spool:
                        writer.writerow(json.loads(line))
        (staging / 'eligible.jsonl').unlink()
        known_invalid = (getattr(matrix, 'last_stats', {}) or {}).get('known_invalid_combinations', 0)
        report = {'id': review_id, 'run_id': run_id, 'product_id': product_id,
            'diagnosis_id': diagnosis['id'], 'revision': diagnosis['revision'],
            'source': 'matrix' if matrix is not None else 'quotes',
            'policy_fingerprint': policy.get('fingerprint'), 'source_url': policy['source_url'],
            **{key: counts[key] for key in ('examined_rows','eligible_rows','held_rows','invalid_rows_removed','hidden_choice_rows_omitted')},
            'source_rule_rows_excluded_before_review': known_invalid,
            'reasons': dict(reasons), 'catalog_complete': False, 'supplier_requests_made': 0,
            'notes': ['Unresolved rows are held separately; they are not assumed invalid.',
                      'The original captures and candidate rows are unchanged.',
                      'Current-page field choices do not establish complete conditional inventory.'],
            'download_url': f'/api/v1/finalizations/{review_id}/download' if counts['eligible_rows'] else None,
            'report_url': f'/api/v1/finalizations/{review_id}/report',
            'policy_url': f'/api/v1/finalizations/{review_id}/policy',
            'row_report_url': f'/api/v1/finalizations/{review_id}/rows'}
        (staging / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
        (staging / 'policy.json').write_text(json.dumps(policy, indent=2, ensure_ascii=False))
        for path in staging.iterdir():
            os.chmod(path, 0o600)
        staging.rename(target)
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def review_product(store, diagnosis, run_id, product_id, source='quotes'):
    if not diagnosis.get('selection') or diagnosis['selection']['revision'] != diagnosis['revision']:
        raise ValueError('Save the current field choices before reviewing rows')
    with store.session() as session:
        product = session.get(Product, product_id)
        if product is None or product.run_id != run_id:
            raise LookupError('Product not found in this run')
        if not same_product(product.url, diagnosis['url']):
            raise ValueError('The diagnosis belongs to a different product URL')
    if source not in {'quotes','matrix'}:
        raise ValueError('Choose saved quotes or a saved matrix')
    inventory = next((item for item in load_inventories(store, run_id)
                      if item['product_id'] == product_id and item.get('bases')
                      and item.get('product_type') in {'1','27','36'}), None) if source == 'matrix' else None
    if source == 'matrix' and inventory is None:
        raise ValueError('No saved matrix is available for this product')
    matrix = VariationMatrix(inventory) if inventory else None
    rows = (dict(row, product_url=row.get('product_url') or inventory['product_url'])
            for row in matrix.iter_rows()) if matrix else store.current_rows(product_id=product_id)
    return review_rows(rows, diagnosis, store.root / 'finalizations',
                       run_id=run_id, product_id=product_id, matrix=matrix)


def review_file(store, review_id, kind):
    if not re.fullmatch('[a-f0-9]{32}', review_id):
        raise LookupError('Review not found')
    filename = {'download': 'finalized.csv', 'report': 'report.json', 'rows': 'rows.jsonl', 'policy': 'policy.json'}[kind]
    root = store.root / 'finalizations'
    path = root / review_id / filename
    if (not path.is_file() or path.is_symlink() or path.parent.is_symlink()
            or root.is_symlink() or not path.resolve().is_relative_to(root.resolve())):
        raise LookupError('No finalized rows' if kind == 'download' else 'Review not found')
    return path
