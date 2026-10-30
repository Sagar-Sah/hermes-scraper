"""Reviewed, add-only mapping seed import. No Store, browser or network imports.

Historical source files establish mapping provenance, never current prices or
completion. This narrow migration imports the thirteen reviewed Standard Print
mappings; it does not infer other products from names or change a runtime root.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import uuid


SEED_ID = 'companion-reviewed-standard-print-mappings-20261029'
EXPECTED_SOURCES = {
    '6862': 'https://4over.com/standard-business-cards',
    '9533': 'https://4over.com/brown-kraft-postcards',
    '9534': 'https://4over.com/brown-kraft-announcement-cards',
    '9535': 'https://4over.com/brown-kraft-trading-cards',
    '9554': 'https://4over.com/brown-kraft-sell-sheets',
    '9555': 'https://4over.com/brown-kraft-menus',
    '9556': 'https://4over.com/brown-kraft-hang-tags',
    '9557': 'https://4over.com/brown-kraft-greeting-cards',
    '9558': 'https://4over.com/brown-kraft-bookmarks',
    '9866': 'https://4over.com/16pt-postcards',
    '10359': 'https://4over.com/z-fold-brochures',
    '10364': 'https://4over.com/specialty-folds-brochures',
    '17430': 'https://4over.com/digital-envelopes',
}
FAMILY_IDS = {'9866', '10359', '10364'}
TABLE_IDS = set(EXPECTED_SOURCES) - FAMILY_IDS - {'6862'}
HELD_IDS = {'8434', '8445', '8446', '8447', '17394', '17493', '17792', '17983', '18105'}
OBJECT_SUFFIXES = {'fourover_rows_evidence': 'fourover-draft.csv',
                   'fourover_option_map_evidence': 'fourover-option-map.json',
                   'fourover_price_table_evidence': 'fourover-price-tables.json'}
MAX_BYTES = 32 * 1024 * 1024
SHA = re.compile(r'[a-f0-9]{64}')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _json(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('mapping_import_duplicate_json_key')
            out[key] = value
        return out
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError):
        raise ValueError('mapping_import_invalid_json') from None
    if not isinstance(value, dict):
        raise ValueError('mapping_import_invalid_json')
    return value


def _root(path):
    path = Path(path).absolute()
    if not path.is_dir() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('mapping_import_root_invalid')
    return path


def _relative(path, root):
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError('mapping_import_path_outside_root') from None
    if not relative.parts or any(part in ('', '.', '..') for part in relative.parts):
        raise ValueError('mapping_import_path_invalid')
    return relative


def _directory(root, parts, *, create=False):
    """Walk held directory descriptors; never follow a replaced parent symlink."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if part in ('', '.', '..') or '/' in part:
                raise ValueError('mapping_import_path_invalid')
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except FileNotFoundError:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise ValueError('mapping_import_unsafe_path') from None
    except BaseException:
        os.close(fd)
        raise


def _read(root, path, *, missing=False):
    relative = _relative(path, root)
    parent = None
    try:
        parent = _directory(root, relative.parts[:-1])
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError('mapping_import_file_invalid')
            raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('mapping_import_file_invalid')
            return raw
    except FileNotFoundError:
        if missing:
            return None
        raise ValueError('mapping_import_evidence_missing') from None
    except OSError:
        raise ValueError('mapping_import_unsafe_path') from None
    finally:
        if parent is not None:
            os.close(parent)


def _add(root, path, raw):
    """Atomic no-overwrite publication. Equal existing bytes are an idempotent hit."""
    relative = _relative(path, root)
    fd = _directory(root, relative.parts[:-1], create=True)
    temporary = '.' + relative.name + '.' + uuid.uuid4().hex + '.tmp'
    try:
        before = _read(root, relative, missing=True)
        if before is not None:
            if before != raw:
                raise ValueError('mapping_import_target_collision')
            return False
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(handle, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, relative.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            if _read(root, relative) != raw:
                raise ValueError('mapping_import_target_collision') from None
            return False
        os.fsync(fd)
        return True
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


class ReviewedMappingImporter:
    def __init__(self, *, source_data, target_data, evidence_root):
        self.source = _root(source_data)
        self.target = _root(target_data)
        self.evidence = _root(evidence_root)
        if (self.source == self.target or self.source.is_relative_to(self.target) or self.target.is_relative_to(self.source)
                or not self.source.is_relative_to(self.evidence) or not self.target.is_relative_to(self.evidence)):
            raise ValueError('mapping_import_distinct_data_roots_required')
        self.inputs = {}

    def _input(self, root, path, expected_sha=None):
        raw = _read(root, path)
        sha = hashlib.sha256(raw).hexdigest()
        if expected_sha is not None and (not _sha(expected_sha) or sha != expected_sha):
            raise ValueError('mapping_import_evidence_hash_changed')
        key = ('source' if root == self.source else 'target' if root == self.target else 'evidence', str(_relative(path, root)))
        previous = self.inputs.get(key)
        if previous is not None and previous['sha256'] != sha:
            raise ValueError('mapping_import_evidence_changed_during_plan')
        self.inputs[key] = {'root': key[0], 'path': key[1], 'sha256': sha, 'bytes': len(raw)}
        return raw

    def _proof(self, reference):
        if (not isinstance(reference, dict) or not isinstance(reference.get('path'), str)
                or not _sha(reference.get('sha256')) or type(reference.get('bytes')) is not int):
            raise ValueError('mapping_import_artifact_proof_required')
        raw = self._input(self.evidence, reference['path'], reference['sha256'])
        if len(raw) != reference['bytes']:
            raise ValueError('mapping_import_artifact_size_changed')
        return _json(raw)

    @staticmethod
    def _excluded(product):
        return product.get('stage') in {'excluded', 'cancelled'} or bool(product.get('excluded_at')) or product.get('excluded') is True

    def _target_preconditions(self):
        directory = self.target / 'campaigns'
        _relative(directory, self.target)
        fd = _directory(self.target, ('campaigns',))
        os.close(fd)
        records = []
        for path in sorted(directory.glob('*.json')):
            if path.stem == SEED_ID:
                continue
            raw = _read(self.target, path)
            campaign = _json(raw)
            if campaign.get('id') != path.stem or not isinstance(campaign.get('products'), list):
                raise ValueError('mapping_import_target_campaign_invalid')
            for product in campaign['products']:
                pid = str(product.get('id'))
                if pid not in EXPECTED_SOURCES:
                    continue
                if self._excluded(product):
                    raise ValueError('mapping_import_target_exclusion_blocks_seed')
                if product.get('supplier_url') not in (None, EXPECTED_SOURCES[pid]):
                    raise ValueError('mapping_import_existing_mapping_conflict')
            records.append({'path': str(path.relative_to(self.target)), 'sha256': hashlib.sha256(raw).hexdigest()})
        pairs_path = self.target / 'product-companion/pairs.json'
        pairs_raw = _read(self.target, pairs_path, missing=True)
        if pairs_raw is not None:
            pairs = _json(pairs_raw)
            for pair in pairs.values():
                pid = str(pair.get('product_id'))
                if pid in EXPECTED_SOURCES and pair.get('source_url') != EXPECTED_SOURCES[pid]:
                    raise ValueError('mapping_import_existing_mapping_conflict')
        records.append({'path': str(pairs_path.relative_to(self.target)),
                        'sha256': hashlib.sha256(pairs_raw).hexdigest() if pairs_raw is not None else None})
        return records

    def _table_objects(self, product, reviewed, source_url):
        objects, saved = [], {}
        refs = reviewed.get('evidence_objects')
        if not isinstance(refs, list) or {r.get('field') for r in refs} != set(OBJECT_SUFFIXES) or len(refs) != 3:
            raise ValueError('mapping_import_exact_object_allowlist_required')
        for field, suffix in OBJECT_SUFFIXES.items():
            info = product.get(field)
            approved = next(r for r in refs if r.get('field') == field)
            if (not isinstance(info, dict) or info.get('source_url') != source_url or info.get('sha256') != approved.get('sha256')
                    or info.get('object_path') != approved.get('object_path') or not _sha(info.get('sha256'))
                    or info.get('object_path') != f'objects/{info["sha256"][:2]}/{info["sha256"]}.{suffix}'):
                raise ValueError('mapping_import_object_identity_changed')
            raw = self._input(self.source, info['object_path'], info['sha256'])
            present = _read(self.target, info['object_path'], missing=True)
            if present is not None and present != raw:
                raise ValueError('mapping_import_target_collision')
            saved[field] = {'source_url': source_url, 'object_path': info['object_path'], 'sha256': info['sha256'],
                            'historical': True, 'use': 'historical_source_only_require_fresh_cycle',
                            **({'observed_at': info['observed_at']} if 'observed_at' in info else {})}
            objects.append({'source_path': info['object_path'], 'object_path': info['object_path'], 'sha256': info['sha256'], 'bytes': len(raw)})
        mapping = _json(_read(self.source, saved['fourover_option_map_evidence']['object_path']))
        prices = _json(_read(self.source, saved['fourover_price_table_evidence']['object_path']))
        map_keys = {'schema', 'source_url', 'observed_at', 'price_table_sha256', 'rows', 'mapped_children',
                    'expected_children', 'complete_child_mapping', 'final_eligible', 'cart_verified', 'production_verified', 'shipping_verified'}
        price_keys = {'schema', 'source_url', 'observed_at', 'children', 'observations', 'child_count', 'priced_count',
                      'coverage', 'final_eligible', 'cart_verified', 'production_verified', 'shipping_verified'}
        if (mapping.get('schema') != 'hermes-fourover-option-map/v1' or mapping.get('complete_child_mapping') is not True
                or mapping.get('source_url') != source_url or prices.get('source_url') != source_url
                or mapping.get('price_table_sha256') != saved['fourover_price_table_evidence']['sha256']
                or prices.get('schema') != 'hermes-fourover-child-price-tables/v1'
                or not set(mapping).issubset(map_keys) or not set(prices).issubset(price_keys)
                or mapping.get('final_eligible') is not False or prices.get('final_eligible') is not False
                or not isinstance(mapping.get('rows'), list) or not mapping['rows']):
            raise ValueError('mapping_import_historical_binding_invalid')
        for row in mapping['rows']:
            if (not isinstance(row, dict) or set(row) != {'child_id', 'fields'} or not isinstance(row['fields'], list)
                    or any(not isinstance(f, dict) or set(f) != {'label', 'value', 'choice'}
                           or not isinstance(f['label'], str) or not isinstance(f['choice'], str)
                           or not re.fullmatch(r'[1-9][0-9]{0,12}', str(f['value'])) for f in row['fields'])):
                raise ValueError('mapping_import_historical_binding_invalid')
        children = {str(row['child_id']) for row in mapping['rows']}
        if (not isinstance(prices.get('children'), list) or not isinstance(prices.get('observations'), list)
                or any(not isinstance(row, dict) or set(row) != {'child_id', 'sku'} for row in prices['children'])
                or len(children) != len(mapping['rows']) or children != {str(row['child_id']) for row in prices['children']}
                or any(not re.fullmatch(r'[1-9][0-9]{0,12}', child) for child in children)):
            raise ValueError('mapping_import_historical_child_binding_changed')
        observed = set()
        for row in prices['observations']:
            if (not isinstance(row, dict) or set(row) != {'child_id', 'package_prices', 'tier_prices', 'status'}
                    or row['status'] != 'priced' or str(row['child_id']) not in children or str(row['child_id']) in observed):
                raise ValueError('mapping_import_historical_price_projection_invalid')
            observed.add(str(row['child_id']))
            for kind in ('package_prices', 'tier_prices'):
                values = row[kind]
                if not isinstance(values, list) or len(values) > 1000:
                    raise ValueError('mapping_import_historical_price_projection_invalid')
                seen = set()
                for value in values:
                    if (not isinstance(value, dict) or set(value) != {'quantity', 'price'}
                            or type(value['quantity']) is not int or value['quantity'] < 1 or value['quantity'] in seen
                            or type(value['price']) not in (str, int, float)
                            or not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,18})?', str(value['price']))
                            or Decimal(str(value['price'])) <= 0):
                        raise ValueError('mapping_import_historical_price_projection_invalid')
                    seen.add(value['quantity'])
        if observed != children:
            raise ValueError('mapping_import_historical_child_binding_changed')
        for key, expected in (('source_option_map_sha256', saved['fourover_option_map_evidence']['sha256']),
                              ('source_price_table_sha256', saved['fourover_price_table_evidence']['sha256'])):
            if product['fourover_rows_evidence'].get(key) != expected:
                raise ValueError('mapping_import_historical_object_join_changed')
        headers = {'product', 'quantity', 'suppliertableprice', 'currency', 'productiontimeproductpagereference',
                   'imageurl', 'verification', 'sourceurl'}
        normalize = lambda value: re.sub('[^a-z0-9]', '', value.casefold())
        headers.update(normalize(field['label']) for row in mapping['rows'] for field in row['fields'])
        reader = csv.DictReader(io.StringIO(_read(self.source, saved['fourover_rows_evidence']['object_path']).decode('utf-8-sig')))
        names = reader.fieldnames
        if not names or len(names) != len({normalize(n) for n in names}) or not {normalize(n) for n in names}.issubset(headers):
            raise ValueError('mapping_import_source_csv_schema_changed')
        count = 0
        for row in reader:
            count += 1
            if count > 100000 or row.get('Source URL') != source_url or row.get('Currency') != 'USD' or None in row:
                raise ValueError('mapping_import_source_csv_identity_changed')
        if count == 0:
            raise ValueError('mapping_import_source_csv_empty')
        return objects, saved

    def plan(self, assessment_path):
        self.inputs = {}
        assessment_raw = self._input(self.evidence, assessment_path)
        assessment_sha = hashlib.sha256(assessment_raw).hexdigest()
        assessment = _json(assessment_raw)
        if assessment.get('schema') != 'hermes-selective-mapping-import-assessment/v1':
            raise ValueError('mapping_import_reviewed_assessment_required')
        ready = assessment.get('ready_for_reviewed_mapping_only_seed')
        if not isinstance(ready, list) or len({str(r.get('product_id')) for r in ready}) != len(ready):
            raise ValueError('mapping_import_reviewed_ids_invalid')
        selected = {str(r['product_id']): r for r in ready if r.get('already_mapped_current_cycle') is False}
        if set(selected) != set(EXPECTED_SOURCES) or set(selected) & {str(pid) for pid in assessment.get('global_exclusion_tombstone_ids', [])}:
            raise ValueError('mapping_import_reviewed_thirteen_required')
        held = {str(r.get('product_id')) for r in assessment.get('held_do_not_import_as_confirmed', [])}
        if not HELD_IDS.issubset(held) or held & set(selected):
            raise ValueError('mapping_import_merchant_and_unverified_holds_required')
        inventory = self._proof(assessment['catalog_inventory']['artifact'])
        if inventory.get('inventory', {}).get('complete') is not True:
            raise ValueError('mapping_import_complete_inventory_required')
        live = {str(p['product_id']): p for p in inventory['products']}
        if (not set(selected).issubset(live) or len(live) != len(inventory['products'])
                or assessment['catalog_inventory'].get('standard_print_count') != len(live)
                or assessment.get('counts', {}).get('new_mapping_only_candidates') != 13):
            raise ValueError('mapping_import_product_not_in_reviewed_catalog')
        preconditions = self._target_preconditions()
        source_campaigns, products, copies = {}, [], []
        for pid in sorted(selected, key=int):
            reviewed = selected[pid]
            source_url = EXPECTED_SOURCES[pid]
            if reviewed.get('source_url') != source_url or re.search('all[ -]?inclusive', live[pid].get('title', ''), re.I):
                raise ValueError('mapping_import_reviewed_source_identity_changed')
            target_url = live[pid]['storefront_url']
            if not isinstance(target_url, str) or not re.fullmatch(r'https://(?:www\.)?printitprints\.com/[^?#\\]+', target_url):
                raise ValueError('mapping_import_storefront_identity_invalid')
            facts, historical = [], {}
            if pid == '6862':
                proofs = reviewed.get('mapping_evidence')
                if not isinstance(proofs, list) or len(proofs) != 3 or reviewed.get('merchant_online_quantity_maximum') != 25000:
                    raise ValueError('mapping_import_standard_card_policy_required')
                candidate, checkpoint, policy = [self._proof(p) for p in proofs]
                candidate_path = self.target / 'product-companion/candidates/6862.json'
                checkpoint_path = self.target / 'fourover-child-prices' / str(candidate.get('snapshot_id')) / (hashlib.sha256(source_url.encode()).hexdigest() + '.json')
                if (Path(proofs[0]['path']) != candidate_path or Path(proofs[1]['path']) != checkpoint_path
                        or candidate.get('product_id') != pid or candidate.get('source_url') != source_url or candidate.get('storefront_url') != target_url
                        or candidate.get('supplier_pair_status') != 'confirmed_by_importer_and_source_binding'
                        or (candidate.get('supplier_pair_evidence') or {}).get('title_only_mapping') is not False
                        or (candidate.get('supplier_pair_evidence') or {}).get('source_fields_match_site') is not True
                        or candidate.get('source_option_contract', {}).get('binding_complete') is not True
                        or candidate['source_option_contract'].get('schema') != 'hermes-fourover-option-contract/v1'
                        or checkpoint.get('schema') != 'hermes-fourover-child-price-collection/v1'
                        or checkpoint.get('source_url') != source_url or checkpoint.get('option_contract') != candidate['source_option_contract']
                        or checkpoint.get('snapshot_id') != candidate.get('snapshot_id')
                        or not re.fullmatch(r'[a-f0-9]{32}', str(candidate.get('snapshot_id')))
                        or checkpoint.get('document_body_sha256') != candidate.get('source_document_sha256')
                        or not _sha(candidate.get('source_document_sha256'))
                        or checkpoint.get('scope') != {'sets': 1, 'job_sample': False, 'digital_proof': False}
                        or type(checkpoint['scope']['sets']) is not int
                        or candidate.get('source_binding_sha256') != hashlib.sha256(json.dumps(candidate['source_option_contract'], sort_keys=True).encode()).hexdigest()
                        or policy.get('product_id') != pid or policy.get('source_url') != source_url
                        or policy.get('merchant_online_quantity_maximum') != 25000):
                    raise ValueError('mapping_import_standard_card_binding_changed')
                rows = candidate['source_option_contract'].get('rows')
                children = checkpoint.get('declared_children')
                if (not isinstance(rows, list) or not rows or len(rows) > 10000 or not isinstance(children, list)
                        or candidate.get('source_child_count') != len(rows)
                        or len({str(r.get('child_id')) for r in rows}) != len(rows)
                        or {str(r.get('child_id')) for r in rows} != {str(r.get('child_id')) for r in children}):
                    raise ValueError('mapping_import_standard_card_binding_changed')
                facts = [{'sha256': p['sha256'], 'path': str(_relative(p['path'], self.evidence))} for p in proofs]
                historical = {'merchant_selling_scope': {'authority': 'merchant', 'quantity_maximum': 25000},
                              'historical_fourover_declared_binding': {'source_binding_sha256': candidate['source_binding_sha256'],
                                  'source_child_count': candidate.get('source_child_count'), 'scope': 'historical_mapping_only'}}
            else:
                path = Path(reviewed.get('campaign_path', ''))
                if path.parent != self.source / 'campaigns' or '-before-' in path.name:
                    raise ValueError('mapping_import_canonical_campaign_required')
                if str(path) not in source_campaigns:
                    source_campaigns[str(path)] = _json(self._input(self.source, path))
                campaign = source_campaigns[str(path)]
                if campaign.get('id') != path.stem:
                    raise ValueError('mapping_import_canonical_campaign_required')
                matches = [p for p in campaign.get('products', []) if str(p.get('id')) == pid]
                if len(matches) != 1:
                    raise ValueError('mapping_import_source_product_missing_or_duplicate')
                original = matches[0]
                if (self._excluded(original) or original.get('supplier_url') != source_url or original.get('storefront_url') != target_url
                        or original.get('supplier') not in {'4over', 'fourover'}):
                    raise ValueError('mapping_import_source_mapping_changed')
                facts = [{'path': str(path.relative_to(self.evidence)), 'sha256': hashlib.sha256(_read(self.source, path)).hexdigest()}]
                if pid in TABLE_IDS:
                    if original.get('stage') != 'finalized':
                        raise ValueError('mapping_import_historical_offer_not_reviewed')
                    objects, historical = self._table_objects(original, reviewed, source_url)
                    copies.extend(objects)
                else:
                    proofs = reviewed.get('mapping_evidence')
                    if not isinstance(proofs, list) or len(proofs) != 2:
                        raise ValueError('mapping_import_family_evidence_required')
                    ledger, mapping_report = [self._proof(p) for p in proofs]
                    rows = [p for p in ledger.get('products', []) if str(p.get('product_id')) == pid]
                    mappings = [p for p in mapping_report.get('products', []) if str(p.get('woocommerce_product_id')) == pid]
                    if (len(rows) != 1 or len(mappings) != 1 or rows[0].get('source', {}).get('identity_confirmed') is not True
                            or rows[0]['source'].get('url') != source_url
                            or mappings[0].get('source_mapping', {}).get('status') != 'official_public_product_family_match'
                            or mappings[0]['source_mapping'].get('exact_supplier_product_url') != source_url
                            or not any(e.get('ledger_sha256') == proofs[0]['sha256'] for e in original.get('mapping_evidence', []))):
                        raise ValueError('mapping_import_family_identity_unverified')
                    facts.extend({'sha256': p['sha256'], 'path': str(_relative(p['path'], self.evidence))} for p in proofs)
            products.append({'id': pid, 'title': str(live[pid]['title'])[:300], 'storefront_url': target_url,
                'supplier': '4over', 'supplier_url': source_url, 'stage': 'queued', 'row_count': 0,
                'reason': 'reviewed_mapping_only_current_cycle_verification_required',
                'mapping_evidence': [{'kind': 'reviewed_selective_mapping_seed', 'product_id': pid, 'source_url': source_url,
                    'assessment_sha256': assessment_sha, 'source_artifacts': facts, 'scope': 'mapping_only_no_current_price_or_domain_claim'}],
                **historical})
        stamp = datetime.fromtimestamp(inventory['inventory']['observed_at'], timezone.utc).isoformat()
        seed = {'id': SEED_ID, 'name': 'Product Companion · Reviewed Standard Print mappings', 'status': 'queued',
                'created_at': stamp, 'updated_at': stamp, 'products': products,
                'mapping_only_seed': True, 'automatic_collection_enabled': False,
                'evidence_policy': 'Historical evidence remains historical; new cycles must revalidate source facts.'}
        seed_bytes = encoded(seed) + b'\n'
        existing_seed = _read(self.target, 'campaigns/' + SEED_ID + '.json', missing=True)
        if existing_seed is not None and existing_seed != seed_bytes:
            raise ValueError('mapping_import_seed_campaign_collision')
        manifest = {'schema': 'hermes-reviewed-mapping-import/v1', 'mode': 'mapping_only_add_only',
                    'roots': {'source_data': str(self.source), 'target_data': str(self.target), 'evidence_root': str(self.evidence)},
                    'assessment_path': str(_relative(assessment_path, self.evidence)), 'assessment_sha256': assessment_sha,
                    'product_ids': sorted(selected, key=int), 'excluded_product_ids': sorted(HELD_IDS, key=int),
                    'target_preconditions': preconditions, 'source_inputs': sorted(self.inputs.values(), key=lambda r:(r['root'],r['path'])),
                    'copy_objects': sorted(copies, key=lambda r:r['object_path']), 'seed_campaign': seed,
                    'seed_campaign_sha256': hashlib.sha256(seed_bytes).hexdigest(),
                    'no_network_requests': True, 'no_automation_or_freshness_transferred': True}
        manifest['manifest_sha256'] = digest(manifest)
        return manifest

    def write_manifest(self, manifest, path):
        self._validate_manifest(manifest, manifest.get('manifest_sha256'))
        return _add(self.evidence, path, encoded(manifest) + b'\n')

    def _validate_manifest(self, manifest, expected_sha):
        if not isinstance(manifest, dict) or not _sha(expected_sha):
            raise ValueError('mapping_import_manifest_approval_required')
        body = {key: value for key, value in manifest.items() if key != 'manifest_sha256'}
        if (manifest.get('schema') != 'hermes-reviewed-mapping-import/v1' or manifest.get('manifest_sha256') != expected_sha
                or digest(body) != expected_sha or manifest.get('roots') != {'source_data': str(self.source), 'target_data': str(self.target), 'evidence_root': str(self.evidence)}):
            raise ValueError('mapping_import_manifest_changed')

    def apply(self, manifest, *, expected_sha):
        self._validate_manifest(manifest, expected_sha)
        lock_path = '.reviewed-mapping-import.lock'
        lock = os.open(self.target / lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('mapping_import_already_running') from None
            rebuilt = self.plan(manifest['assessment_path'])
            if rebuilt != manifest:
                raise ValueError('mapping_import_source_or_target_revision_changed')
            created = 0
            for item in manifest['copy_objects']:
                raw = _read(self.source, item['source_path'])
                if hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise ValueError('mapping_import_evidence_hash_changed')
                created += _add(self.target, item['object_path'], raw)
            # Recheck the immutable inputs and current target mappings before
            # publishing one complete seed. Interrupted object copies are safe
            # content-addressed additions; rerunning never repeats source calls.
            if self.plan(manifest['assessment_path']) != manifest:
                raise ValueError('mapping_import_source_or_target_revision_changed')
            seed_created = _add(self.target, 'campaigns/' + SEED_ID + '.json', encoded(manifest['seed_campaign']) + b'\n')
            return {'status': 'imported' if seed_created else 'already_present', 'campaign_id': SEED_ID,
                    'manifest_sha256': expected_sha, 'product_ids': manifest['product_ids'],
                    'objects_created': created, 'seed_created': seed_created, 'supplier_requests': 0,
                    'automation_started': False, 'current_cycle_modified': False}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
