"""Durable exact-selection evidence for the 4over Gator Board calculator.

Archived quotes are historical observations. A checkout integration must ask
for a fresh source quote and must never treat this archive as live availability.
"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class GatorQuoteArchive:
    def __init__(self, root):
        self.root = Path(root) / 'gator-exact-quotes'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, campaign_id, selection):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', campaign_id):
            raise ValueError('invalid_campaign_id')
        required = {'product_id', 'source_url', 'child_id', 'width_in', 'height_in', 'sets'}
        if set(selection) != required or selection['product_id'] != '6742' or selection['source_url'] != 'https://4over.com/gator-board-signs':
            raise ValueError('invalid_gator_selection')
        if not re.fullmatch(r'[0-9]{1,16}', str(selection['child_id'])):
            raise ValueError('invalid_gator_child')
        if any(type(selection[key]) is not int or not 1 <= selection[key] <= 200
               for key in ('width_in', 'height_in', 'sets')):
            raise ValueError('invalid_gator_dimensions_or_sets')
        digest = hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return self.root / campaign_id / f'{digest}.json'

    def get(self, campaign_id, selection):
        path = self._path(campaign_id, selection)
        if not path.is_file() or path.is_symlink():
            return None
        record = json.loads(path.read_text())
        if record.get('selection') != selection or record.get('quote', {}).get('status') != 'exact_scoped_quote':
            raise ValueError('gator_quote_archive_mismatch')
        return record

    def save(self, campaign_id, selection, quote, evidence_file=None, captured_at=None):
        if quote.get('status') != 'exact_scoped_quote' or any(quote.get(k) != v for k, v in selection.items()):
            raise ValueError('gator_quote_selection_mismatch')
        path = self._path(campaign_id, selection)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if captured_at is not None:
            parsed = datetime.fromisoformat(captured_at)
            if parsed.tzinfo is None:
                raise ValueError('captured_at_must_have_timezone')
        record = {'selection': selection, 'quote': quote,
                  'captured_at': captured_at or datetime.now(timezone.utc).isoformat(),
                  'evidence_file': evidence_file,
                  'use': 'historical_evidence_only_require_fresh_for_checkout'}
        temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x') as stream:
                os.chmod(temp, 0o600)
                json.dump(record, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return record
