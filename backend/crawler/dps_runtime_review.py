"""Recompute a DPS source-live quote review from pinned, local evidence.

No sampled unit price is generalized to another size, quantity or option path.
The storefront must request a fresh native-page quote for every configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

from .dps_pilot import SCRIPT_SHA256, money_matches, number
from .dps_quote_contract import scoped_request

DPS_IDS = {'19919', '19918', '19882', '19881', '19880', '19879', '19574'}


def _saved(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('dps_review_evidence_missing')
    body = path.read_bytes()
    return json.loads(body), hashlib.sha256(body).hexdigest()


def _image(product_id: str, work: Path):
    if product_id == '19574':
        row = json.loads((work / 'hermes-group6-19574-image-20260928.json').read_text())
    else:
        manifest = json.loads((work / 'dps-group5-images-20260928.json').read_text())
        images = [row for row in manifest['images'] if row['storefront_product_id'] == product_id]
        if len(images) != 1:
            raise ValueError('dps_supplier_image_missing_or_ambiguous')
        row = images[0]
    path = Path(row['local_path'])
    if not path.is_absolute():
        path = work.parent / path
    path = path.resolve()
    if not path.is_relative_to(work.resolve()) or path.is_symlink() or not path.is_file():
        raise ValueError('dps_supplier_image_missing')
    if path.stat().st_size != row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError('dps_supplier_image_hash_mismatch')
    return {'source_url': row['source_url'], 'path': str(path.relative_to(work.parent)),
            'sha256': row['sha256'], 'bytes': row['bytes']}


def inspect_dps_runtime(product: dict, root: Path, campaign_id: str):
    root = Path(root).resolve()
    work = root.parent
    pid = product.get('id')
    if (pid not in DPS_IDS or product.get('supplier') != 'digital_print_solutions'
            or not product.get('supplier_url') or not product.get('source_collection_id')
            or not product.get('diagnosis_id')):
        raise ValueError('active_mapped_dps_product_required')
    evidence = product.get('dps_route_price_evidence') or {}
    pilot_id = evidence.get('native_pilot_id')
    if not isinstance(pilot_id, str) or not re.fullmatch(r'[a-f0-9]{32}', pilot_id):
        raise ValueError('saved_dps_native_pilot_required')
    pilot, pilot_sha = _saved(root / 'dps-pricing-pilots' / (pilot_id + '.json'))
    if (pilot.get('id') != pilot_id or pilot.get('url') != product['supplier_url']
            or pilot.get('status') != 'completed' or pilot.get('source_contract_verified') is not True
            or pilot.get('source_script_sha256') != SCRIPT_SHA256
            or not pilot.get('observations') or pilot.get('final_eligible') is not False):
        raise ValueError('dps_pinned_source_pilot_incomplete')
    option_responses = [row for row in pilot.get('responses', []) if row.get('kind') == 'options']
    quantity_responses = [row for row in pilot.get('responses', []) if row.get('kind') == 'quantities']
    if len(option_responses) != 1 or len(quantity_responses) != 1:
        raise ValueError('dps_option_or_quantity_domain_incomplete')
    option_domain = option_responses[0].get('option_domain') or []
    controls = pilot['observations'][0]['controls']
    if (not option_domain or not controls.get('options')
            or any(controls[axis].get('kind') != 'select' or not controls[axis].get('choices')
                   for axis in ('width', 'height'))
            or controls['quantity'].get('kind') not in ('number', 'select')):
        raise ValueError('dps_source_controls_incomplete')
    quantity_control = controls['quantity']
    if quantity_control['kind'] == 'number':
        low = int(number(quantity_control.get('minimum'), integer=True, positive=True))
        high = int(number(quantity_control.get('maximum'), integer=True, positive=True))
        if not 1 <= low <= high <= 25000:
            raise ValueError('dps_quantity_bounds_unresolved')
    else:
        low = min(int(number(choice['value'], integer=True, positive=True))
                  for choice in quantity_control['choices'] if not choice.get('disabled'))
        high = max(int(number(choice['value'], integer=True, positive=True))
                   for choice in quantity_control['choices'] if not choice.get('disabled'))
    source_image = _image(pid, work)
    path = work / f'hermes-group10-{pid}-8x8-q31-availability-20260930.json'
    quote, quote_sha = _saved(path)
    if (quote.get('url') != product['supplier_url'] or quote.get('status') != 'completed'
            or quote.get('source_contract_verified') is not True
            or quote.get('source_script_sha256') != SCRIPT_SHA256
            or quote.get('selection_verified') is not True or quote.get('warnings')
            or quote.get('availability') != 'rendered_purchase_step_available_cart_unverified'
            or quote.get('production_availability') != 'selected_visible_choice_with_bound_quote'
            or quote.get('final_eligible') is not False):
        raise ValueError('dps_rendered_quote_incomplete')
    request = quote.get('expected_request') or {}
    observations = quote.get('observations') or []
    if len(observations) < 2 or not isinstance(request.get('options'), str):
        raise ValueError('dps_requested_quote_missing')
    selected = dict(pair.split('E') for pair in request['options'].split('C'))
    if (scoped_request(pilot, width=request.get('width'), height=request.get('height'),
                       quantity=request.get('quantity'), selected_options=selected) != request):
        raise ValueError('dps_source_selection_no_longer_supported')
    final = observations[-1]
    if (final.get('verification') != 'fresh_native_quote_matches_scoped_display'
            or any(final.get('context', {}).get(key) != value for key, value in request.items())
            or final.get('context', {}).get('product_id') != pilot['observations'][0]['context']['product_id']
            or not money_matches(final.get('dom_amount'), final.get('price', {}).get('price'))):
        raise ValueError('dps_quote_not_bound_to_rendered_selection')
    signals = final.get('availability_signals') or {}
    if signals.get('explicit_unavailable') is not False or not any(
            action.get('disabled') is False for action in signals.get('purchase_actions', [])):
        raise ValueError('dps_rendered_purchase_step_unavailable')
    production = [option for option in final['controls']['options']
                  if 'production' in option['label'].casefold()]
    if len(production) != 1:
        raise ValueError('dps_production_choice_unresolved')
    chosen = [choice for choice in production[0]['choices']
              if choice['value'] == production[0]['value'] and not choice['disabled']]
    if len(chosen) != 1 or not re.search(r'\b\d+\s+business\s+day', chosen[0]['label'], re.I):
        raise ValueError('dps_production_time_unresolved')
    if Decimal(final['price']['price']) <= 0:
        raise ValueError('dps_source_cost_invalid')
    return {'schema': 'hermes-dps-runtime-review/v1', 'campaign_id': campaign_id,
            'product_id': pid, 'source_url': product['supplier_url'],
            'source_collection_id': product['source_collection_id'],
            'diagnosis_id': product['diagnosis_id'], 'pilot_id': pilot_id,
            'pilot_sha256': pilot_sha, 'source_script_sha256': SCRIPT_SHA256,
            'option_domain_sha256': hashlib.sha256(json.dumps(option_domain, sort_keys=True).encode()).hexdigest(),
            'width_choice_count': len(controls['width']['choices']),
            'height_choice_count': len(controls['height']['choices']),
            'quantity_bounds': [low, high], 'supplier_image': source_image,
            'rendered_quote_ref': str(path.relative_to(work.parent)),
            'rendered_quote_sha256': quote_sha,
            'canary_selection': request, 'canary_unit_cost': final['price']['price'],
            'canary_production': chosen[0]['label'],
            'pricing_model': 'source_live_quote', 'source_data_complete': True,
            'static_rows_claimed': False, 'storefront_deployed': False,
            'price_policy': 'fresh_source_native_page_quote_per_complete_selection; fail_closed',
            'availability_policy': 'require_retained_controls_exact_display_price_and_enabled_purchase_step; cart_unverified',
            'production_policy': 'require_current_visible_selected_source_choice_with_bound_quote',
            'shipping_policy': 'supplier_reference_only_printitprints_calculates_shipping'}


def save_dps_runtime_review(product, root, campaign_id):
    root = Path(root).resolve()
    report = inspect_dps_runtime(product, root, campaign_id)
    report['id'] = uuid.uuid4().hex
    directory = root / 'finalizations' / report['id']
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = directory / 'report.json'
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(report, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return report
