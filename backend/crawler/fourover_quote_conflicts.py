"""Persist a minimal, non-account diagnostic for rejected Gator quotes."""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def save_gator_quote_conflict(root, selection, pilot, reason):
    if not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9_]{1,100}', reason):
        raise ValueError('invalid_gator_quote_conflict_reason')
    if selection.get('product_id') != '6742':
        raise ValueError('invalid_gator_quote_conflict_product')

    # Never persist request bodies, shipping addresses, browser fields, cookies,
    # or full supplier responses. These amounts are enough to diagnose a race
    # between the project's last summary and the visible shipping-step total.
    summaries = []
    for item in pilot.get('project_response_shapes', [])[-3:]:
        totals = item.get('totals') if isinstance(item, dict) else None
        if not isinstance(totals, dict):
            continue
        summaries.append({'status': item.get('status'), 'totals': {
            key: str(totals[key])[:40] for key in ('subtotal', 'grand_total', 'small_qty_fee')
            if key in totals and isinstance(totals[key], (str, int, float))}})
    money = [value for value in pilot.get('shipping_view', {}).get('money', [])
             if isinstance(value, str) and re.fullmatch(
                 r'(?:Price Per Set:|Subtotal:?|Total:)\s*\$[0-9,]+\.[0-9]{2}', value)]
    record = {'id': uuid.uuid4().hex, 'captured_at': datetime.now(timezone.utc).isoformat(),
              'selection': {key: selection[key] for key in
                            ('product_id', 'source_url', 'child_id', 'width_in', 'height_in', 'sets')},
              'reason': reason, 'project_summaries': summaries,
              'visible_money': money[:30],
              'scope': 'rejected_quote_diagnostic_not_a_price'}
    directory = Path(root) / 'gator-quote-conflicts'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (record['id'] + '.json')
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return record
