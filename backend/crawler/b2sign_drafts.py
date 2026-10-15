"""Materialize a completed B2Sign request scope as reviewable draft rows.

These are source quotes with shipping unselected. They do not assert catalog
completion, cart verification, or production availability beyond each quote.
"""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal, InvalidOperation


class DraftError(ValueError):
    pass


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DraftError('Invalid source quote money') from None
    if not amount.is_finite() or amount < 0:
        raise DraftError('Invalid source quote money')
    return format(amount, 'f')


def build_draft(manifest, report, result_documents):
    catalog = manifest.get('catalog') or {}
    candidates = manifest.get('candidates') or []
    if (report.get('status') != 'completed' or report.get('finite_requested_set_exhausted') is not True
            or report.get('warnings') or report.get('invalid_count') != 0
            or report.get('requested_count') != report.get('completed_count')
            or report.get('completed_count') != len(candidates)
            or report.get('url') != catalog.get('url')
            or not isinstance(candidates, list) or not candidates):
        raise DraftError('Complete B2Sign requested scope required')
    fields = catalog.get('dimensions') or []
    if not isinstance(fields, list) or len({field.get('key') for field in fields}) != len(fields):
        raise DraftError('Source option mapping unresolved')
    labels = {}
    choices = {}
    hidden = set()
    used_labels = set()
    for field in fields:
        key = field.get('key')
        if not isinstance(key, str) or not key:
            raise DraftError('Source option mapping unresolved')
        if field.get('component') == 'hidden':
            hidden.add(key)
            continue
        label = field.get('label') or key.replace('_', ' ').title()
        if label in used_labels:
            label = f'{label} ({key})'
        used_labels.add(label)
        labels[key] = label
        choices[key] = {str(choice['value']):str(choice['label']) for choice in field.get('choices') or []
                        if choice.get('visible') is not False and not choice.get('disabled')}
    by_id = {}
    for result in result_documents:
        identifier = result.get('candidate_id')
        if not isinstance(identifier, str) or identifier in by_id:
            raise DraftError('Duplicate or missing B2Sign quote')
        by_id[identifier] = result
    if len(by_id) != len(candidates):
        raise DraftError('B2Sign quote count mismatch')
    columns = ['Product', 'Supplier URL', 'Quantity', *labels.values(), 'Production Time',
               'Quoted Unit Price', 'Quoted Subtotal', 'Shipping', 'Verification']
    if len(set(columns)) != len(columns):
        raise DraftError('Source option labels are ambiguous')
    derived = {field.get('key') for field in catalog.get('contract',{}).get('derived_request_fields') or []}
    rows = []
    for candidate in candidates:
        identifier = candidate.get('candidate_id')
        result = by_id.get(identifier)
        if (result is None or result.get('status') != 'quoted' or result.get('candidate') != candidate
                or candidate.get('source_url') != catalog['url'] or result.get('quote',{}).get('quantity') != candidate.get('quantity')):
            raise DraftError('B2Sign quote does not match planned configuration')
        row = {'Product': catalog['title'], 'Supplier URL': catalog['url'],
               'Quantity': candidate['quantity'], 'Production Time': candidate.get('production_label') or '',
               'Quoted Unit Price': _money(result['quote']['unit_price']),
               'Quoted Subtotal': _money(result['quote']['subtotal']),
               'Shipping': 'Not quoted', 'Verification': 'Pending independent cart, shipping, and production checks'}
        for key, value in candidate.get('values',{}).items():
            if key in hidden or key in derived:
                continue
            if key not in labels:
                raise DraftError('B2Sign option lacks source display label')
            field = next(field for field in fields if field['key'] == key)
            if field.get('component') == 'size':
                row[labels[key]] = str(value)
            elif str(value) in choices[key]:
                row[labels[key]] = choices[key][str(value)]
            else:
                raise DraftError('B2Sign option lacks source display label')
        rows.append(row)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='raise')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig'), len(rows)
