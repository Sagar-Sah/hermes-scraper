"""Reviewed mapping migration fixtures. No runtime stores, source or site I/O."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from crawler.companion_mapping_import import (ReviewedMappingImporter, EXPECTED_SOURCES, TABLE_IDS, FAMILY_IDS,
    HELD_IDS, OBJECT_SUFFIXES, SEED_ID, encoded, digest)


REPO = Path(__file__).resolve().parents[2]
BASE = 'https://printitprints.com/shop/'


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, indent=2).encode() if isinstance(value, dict) else value
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def object_ref(source, value, suffix):
    raw = json.dumps(value).encode() if isinstance(value, dict) else value
    sha = hashlib.sha256(raw).hexdigest()
    relative = f'objects/{sha[:2]}/{sha}.{suffix}'
    put(source / relative, raw)
    return {'object_path': relative, 'sha256': sha}


def setup(tmp_path):
    work = tmp_path / 'work'
    source = work / 'atlas-data'
    target = work / 'hermes/data'
    for root in (source, target):
        (root / 'campaigns').mkdir(parents=True)
    put(target / 'atlas.db', b'CURRENT DATABASE MUST NEVER CHANGE')
    put(target / 'encryption.key', b'CURRENT KEY MUST NEVER CHANGE')
    put(source / 'encryption.key', b'OLD KEY MUST NEVER COPY')
    put(source / 'source-profiles.json', b'PRIVATE AUTH MUST NEVER COPY')
    put(target / 'campaigns/existing.json', {'id': 'existing', 'status': 'completed', 'created_at': 'old', 'updated_at': 'old',
        'products': [{'id': '9532', 'title': 'Existing Brown Kraft Business Cards', 'stage': 'finalized',
                      'supplier': '4over', 'supplier_url': 'https://4over.com/brown-kraft-business-cards',
                      'storefront_url': BASE + '9532/'}]})
    put(target / 'product-companion/pairs.json', {})
    live = [{'product_id': pid, 'title': f'Product {pid}', 'storefront_url': BASE + pid + '/'} for pid in sorted(EXPECTED_SOURCES, key=int)]
    live.append({'product_id': '9532', 'title': 'Existing Brown Kraft Business Cards', 'storefront_url': BASE + '9532/'})
    inventory = {'id': 'a' * 32, 'inventory': {'complete': True, 'observed_at': 1793316215.0}, 'products': live}
    inventory_proof = put(work / 'assessment/inventory.json', inventory)
    ready, main_products = [], []
    for pid in sorted(TABLE_IDS, key=int):
        url = EXPECTED_SOURCES[pid]
        prices = {'schema': 'hermes-fourover-child-price-tables/v1', 'source_url': url, 'final_eligible': False,
                  'children': [{'child_id': pid, 'sku': 'PUBLIC-' + pid}],
                  'observations': [{'child_id': pid, 'status': 'priced', 'package_prices': [{'quantity': 100, 'price': 10.0}], 'tier_prices': []}]}
        price_ref = object_ref(source, prices, OBJECT_SUFFIXES['fourover_price_table_evidence'])
        mapping = {'schema': 'hermes-fourover-option-map/v1', 'source_url': url, 'complete_child_mapping': True,
                   'final_eligible': False, 'price_table_sha256': price_ref['sha256'],
                   'rows': [{'child_id': pid, 'fields': [{'label': 'Size', 'choice': '3.5 x 2', 'value': '1'}]}]}
        map_ref = object_ref(source, mapping, OBJECT_SUFFIXES['fourover_option_map_evidence'])
        raw = ('Product,Size,Quantity,Supplier Table Price,Currency,Source URL\n'
               f'Product {pid},3.5 x 2,100,10.00,USD,{url}\n').encode()
        rows_ref = object_ref(source, raw, OBJECT_SUFFIXES['fourover_rows_evidence'])
        product = {'id': pid, 'title': f'Historical {pid}', 'storefront_url': BASE + pid + '/', 'supplier': 'fourover',
                   'supplier_url': url, 'stage': 'finalized', 'finalized_at': 'historic', 'fresh_source_observed_at': 42,
                   'run_id': 'MUST NOT TRANSFER', 'diagnosis_id': 'MUST NOT TRANSFER', 'mapping_evidence': [{'source_url': url}]}
        refs = {'fourover_rows_evidence': rows_ref, 'fourover_option_map_evidence': map_ref, 'fourover_price_table_evidence': price_ref}
        for field, ref in refs.items():
            product[field] = {**ref, 'source_url': url, 'observed_at': '2026-09-27T00:00:00+00:00', 'final_eligible': True}
        product['fourover_rows_evidence'].update(source_option_map_sha256=map_ref['sha256'], source_price_table_sha256=price_ref['sha256'])
        main_products.append(product)
        ready.append({'product_id': pid, 'source_url': url, 'campaign_path': str(source / 'campaigns/legacy.json'),
                      'already_mapped_current_cycle': False,
                      'evidence_objects': [{'field': field, **ref, 'verified': True, 'already_in_current_root': False} for field, ref in refs.items()]})
    put(source / 'campaigns/legacy.json', {'id': 'legacy', 'products': main_products})
    ledger = {'products': [{'product_id': int(pid), 'source': {'identity_confirmed': True, 'url': EXPECTED_SOURCES[pid]}} for pid in sorted(FAMILY_IDS)]}
    ledger_ref = put(work / 'ledger.json', ledger)
    public_mapping = {'products': [{'woocommerce_product_id': int(pid), 'source_mapping': {'status': 'official_public_product_family_match',
                                  'exact_supplier_product_url': EXPECTED_SOURCES[pid]}} for pid in sorted(FAMILY_IDS)]}
    mapping_ref = put(work / 'mapping.json', public_mapping)
    family_products = []
    for pid in sorted(FAMILY_IDS):
        family_products.append({'id': pid, 'title': 'Family ' + pid, 'supplier': '4over', 'supplier_url': EXPECTED_SOURCES[pid],
                                'storefront_url': BASE + pid + '/', 'stage': 'queued', 'mapping_evidence': [{'ledger_sha256': ledger_ref['sha256']}]})
        ready.append({'product_id': pid, 'source_url': EXPECTED_SOURCES[pid], 'campaign_path': str(source / 'campaigns/family.json'),
                      'already_mapped_current_cycle': False, 'mapping_evidence': [ledger_ref, mapping_ref]})
    put(source / 'campaigns/family.json', {'id': 'family', 'products': family_products})
    source_url = EXPECTED_SOURCES['6862']
    contract = {'schema': 'hermes-fourover-option-contract/v1', 'binding_complete': True, 'rows': [{'child_id': '123', 'fields': []}]}
    candidate = {'product_id': '6862', 'source_url': source_url, 'storefront_url': BASE + '6862/', 'snapshot_id': 'b' * 32,
                 'supplier_pair_status': 'confirmed_by_importer_and_source_binding', 'source_child_count': 1,
                 'supplier_pair_evidence': {'title_only_mapping': False, 'source_fields_match_site': True},
                 'source_option_contract': contract, 'source_document_sha256': 'c' * 64,
                 'source_binding_sha256': hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()}
    candidate_ref = put(target / 'product-companion/candidates/6862.json', candidate)
    checkpoint = {'schema': 'hermes-fourover-child-price-collection/v1', 'source_url': source_url, 'snapshot_id': 'b' * 32,
                  'option_contract': contract, 'declared_children': [{'child_id': '123'}], 'document_body_sha256': 'c' * 64,
                  'scope': {'sets': 1, 'job_sample': False, 'digital_proof': False}}
    checkpoint_ref = put(target / 'fourover-child-prices' / ('b' * 32) / (hashlib.sha256(source_url.encode()).hexdigest() + '.json'), checkpoint)
    policy_ref = put(work / 'policy.json', {'product_id': '6862', 'source_url': source_url, 'merchant_online_quantity_maximum': 25000})
    ready.append({'product_id': '6862', 'source_url': source_url, 'already_mapped_current_cycle': False,
                  'merchant_online_quantity_maximum': 25000, 'mapping_evidence': [candidate_ref, checkpoint_ref, policy_ref]})
    ready.append({'product_id': '9532', 'already_mapped_current_cycle': True, 'source_url': 'https://4over.com/brown-kraft-business-cards'})
    assessment = {'schema': 'hermes-selective-mapping-import-assessment/v1', 'counts': {'new_mapping_only_candidates': 13},
                  'catalog_inventory': {'artifact': inventory_proof, 'standard_print_count': len(live)},
                  'ready_for_reviewed_mapping_only_seed': ready, 'global_exclusion_tombstone_ids': ['17413', '17442', '20958', '9844'],
                  'held_do_not_import_as_confirmed': [{'product_id': pid} for pid in HELD_IDS]}
    assessment_path = work / 'assessment/assessment.json'
    put(assessment_path, assessment)
    importer = ReviewedMappingImporter(source_data=source, target_data=target, evidence_root=work)
    return importer, work, source, target, assessment_path


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def rehash(manifest):
    manifest['manifest_sha256'] = digest({k:v for k,v in manifest.items() if k != 'manifest_sha256'})


def test_plan_is_read_only_exactly_thirteen_with_historical_only_seed(tmp_path):
    importer, work, source, target, assessment = setup(tmp_path)
    before_source, before_target = tree_bytes(source), tree_bytes(target)
    manifest = importer.plan(assessment)
    assert tree_bytes(source) == before_source and tree_bytes(target) == before_target
    assert manifest['product_ids'] == sorted(EXPECTED_SOURCES, key=int)
    assert len(manifest['copy_objects']) == 27
    assert '9532' not in manifest['product_ids']
    assert not set(manifest['product_ids']) & HELD_IDS
    seed = manifest['seed_campaign']
    assert seed['automatic_collection_enabled'] is False and seed['mapping_only_seed'] is True
    for product in seed['products']:
        assert product['stage'] == 'queued' and product['row_count'] == 0
        assert product['mapping_evidence'] and product['reason'] == 'reviewed_mapping_only_current_cycle_verification_required'
        assert not set(product) & {'run_id', 'diagnosis_id', 'finalization_id', 'finalized_at', 'fresh_source_observed_at'}
        for key in OBJECT_SUFFIXES:
            if key in product:
                assert product[key]['historical'] is True
                assert product[key]['observed_at'].startswith('2026-09-27')
                assert 'final_eligible' not in product[key]
    standard = next(p for p in seed['products'] if p['id'] == '6862')
    assert standard['merchant_selling_scope'] == {'authority': 'merchant', 'quantity_maximum': 25000}
    assert not any(key in standard for key in OBJECT_SUFFIXES)
    assert '10.00' not in json.dumps(manifest)
    assert 'PRIVATE AUTH' not in json.dumps(manifest)


def test_apply_is_add_only_atomic_idempotent_and_mapping_index_recognizes_seed(tmp_path):
    importer, work, source, target, assessment = setup(tmp_path)
    before_source, before_target = tree_bytes(source), tree_bytes(target)
    manifest = importer.plan(assessment)
    result = importer.apply(manifest, expected_sha=manifest['manifest_sha256'])
    assert result['status'] == 'imported' and result['objects_created'] == 27
    assert result['supplier_requests'] == 0 and result['automation_started'] is False
    for relative, raw in before_target.items():
        assert (target / relative).read_bytes() == raw
    assert tree_bytes(source) == before_source
    again = importer.apply(manifest, expected_sha=manifest['manifest_sha256'])
    assert again['status'] == 'already_present' and again['objects_created'] == 0
    assert importer.plan(assessment) == manifest
    for entry in manifest['copy_objects']:
        file = target / entry['object_path']
        assert file.read_bytes() == (source / entry['source_path']).read_bytes()
        assert file.stat().st_mode & 0o777 == 0o600
    from crawler.autonomous_product_pipeline import SourceMappingIndex
    class Campaigns:
        root = target / 'campaigns'
        def get(self, name):
            return json.loads((self.root / (name + '.json')).read_bytes())
    companion = SimpleNamespace(campaigns=Campaigns(), pairs={})
    index = SourceMappingIndex(companion)
    for pid, url in EXPECTED_SOURCES.items():
        mapping = index.resolve({'product_id': pid})
        assert mapping['mapping_status'] == 'confirmed' and mapping['source_url'] == url
        assert mapping['product']['stage'] == 'queued'
    assert index.resolve({'product_id': '17442'})['mapping_status'] == 'unknown'


@pytest.mark.parametrize('kind', ['missing_approval', 'wrong_approval', 'manifest_changed', 'source_changed', 'target_campaign_changed', 'target_pairs_changed', 'candidate_changed'])
def test_approval_and_revision_guards_block_before_seed_publication(tmp_path, kind):
    importer, _, source, target, assessment = setup(tmp_path)
    manifest = importer.plan(assessment)
    expected = manifest['manifest_sha256']
    if kind == 'missing_approval':
        expected = None
    elif kind == 'wrong_approval':
        expected = '0' * 64
    elif kind == 'manifest_changed':
        manifest['product_ids'].append('17442')
    elif kind == 'source_changed':
        (source / manifest['copy_objects'][0]['source_path']).write_bytes(b'changed')
    elif kind == 'target_campaign_changed':
        put(target / 'campaigns/new-campaign.json', {'id': 'new-campaign', 'products': []})
    elif kind == 'target_pairs_changed':
        put(target / 'product-companion/pairs.json', {'new': {'product_id': '999', 'source_url': 'https://4over.com/other'}})
    else:
        path = target / 'product-companion/candidates/6862.json'
        data = json.loads(path.read_bytes());data['source_child_count'] = 2;put(path, data)
    with pytest.raises(ValueError):
        importer.apply(manifest, expected_sha=expected)
    assert not (target / 'campaigns' / (SEED_ID + '.json')).exists()


@pytest.mark.parametrize('kind', ['extra_unresolved', 'merchant_excluded', 'wrong_source', 'wrong_permalink', 'alias_campaign', 'missing_hold', 'quantity_cap'])
def test_unreviewed_merchant_tabled_or_mismatched_mappings_cannot_enter_seed(tmp_path, kind):
    importer, _, source, target, path = setup(tmp_path)
    data = json.loads(path.read_bytes())
    if kind == 'extra_unresolved':
        data['ready_for_reviewed_mapping_only_seed'].append({'product_id': '17394', 'source_url': 'https://www.printograph.com/products/special-shapes/order', 'already_mapped_current_cycle': False})
    elif kind == 'merchant_excluded':
        data['global_exclusion_tombstone_ids'].append('9533')
    elif kind == 'wrong_source':
        data['ready_for_reviewed_mapping_only_seed'][0]['source_url'] = 'https://4over.com/all-inclusive-postcards'
    elif kind == 'wrong_permalink':
        campaign = json.loads((source / 'campaigns/legacy.json').read_bytes());campaign['products'][0]['storefront_url'] = BASE + 'other/';put(source / 'campaigns/legacy.json', campaign)
    elif kind == 'alias_campaign':
        data['ready_for_reviewed_mapping_only_seed'][0]['campaign_path'] = str(source / 'campaigns/legacy-before-group.json')
    elif kind == 'missing_hold':
        data['held_do_not_import_as_confirmed'].pop()
    else:
        next(r for r in data['ready_for_reviewed_mapping_only_seed'] if r['product_id'] == '6862')['merchant_online_quantity_maximum'] = 100000
    put(path, data)
    with pytest.raises(ValueError):
        importer.plan(path)
    assert not (target / 'campaigns' / (SEED_ID + '.json')).exists()


@pytest.mark.parametrize('kind', ['source_object_symlink', 'source_parent_symlink', 'target_parent_symlink', 'target_collision', 'seed_collision', 'target_exclusion', 'target_mapping_conflict'])
def test_symlink_collision_and_existing_exclusion_guards(tmp_path, kind):
    importer, work, source, target, assessment = setup(tmp_path)
    manifest = importer.plan(assessment)
    first = manifest['copy_objects'][0]
    if kind == 'source_object_symlink':
        original = source / first['source_path']; copy = work / 'elsewhere.bin';copy.write_bytes(original.read_bytes());original.unlink();original.symlink_to(copy)
    elif kind == 'source_parent_symlink':
        parent = (source / first['source_path']).parent; moved = work / 'moved-parent';parent.rename(moved);parent.symlink_to(moved, target_is_directory=True)
    elif kind == 'target_parent_symlink':
        destination = work / 'outside-target';destination.mkdir();(target / 'objects').symlink_to(destination, target_is_directory=True)
    elif kind == 'target_collision':
        put(target / first['object_path'], b'conflicting-existing-content')
    elif kind == 'seed_collision':
        put(target / 'campaigns' / (SEED_ID + '.json'), {'id': SEED_ID, 'products': [], 'marker': 'must remain'})
    else:
        existing = json.loads((target / 'campaigns/existing.json').read_bytes())
        existing['products'].append({'id': '9533', 'stage': 'excluded' if kind == 'target_exclusion' else 'queued',
                                     'supplier_url': EXPECTED_SOURCES['9533'] if kind == 'target_exclusion' else 'https://4over.com/other'})
        put(target / 'campaigns/existing.json', existing)
    with pytest.raises((ValueError, OSError)):
        importer.apply(manifest, expected_sha=manifest['manifest_sha256'])
    if kind == 'target_parent_symlink':
        assert list((work / 'outside-target').iterdir()) == []


def test_interrupted_object_copies_resume_without_source_calls_or_duplicate_seed(tmp_path, monkeypatch):
    import crawler.companion_mapping_import as module
    importer, _, _, target, assessment = setup(tmp_path)
    manifest = importer.plan(assessment)
    original = module._add
    calls = []
    def interrupt(root, path, raw):
        if str(path).startswith('objects/'):
            calls.append(str(path))
            if len(calls) == 3:
                raise OSError('synthetic-interruption')
        return original(root, path, raw)
    monkeypatch.setattr(module, '_add', interrupt)
    with pytest.raises(OSError):
        importer.apply(manifest, expected_sha=manifest['manifest_sha256'])
    assert not (target / 'campaigns' / (SEED_ID + '.json')).exists()
    assert len(list((target / 'objects').glob('*/*'))) == 2
    monkeypatch.setattr(module, '_add', original)
    result = importer.apply(manifest, expected_sha=manifest['manifest_sha256'])
    assert result['status'] == 'imported' and result['objects_created'] == 25
    assert importer.apply(manifest, expected_sha=manifest['manifest_sha256'])['status'] == 'already_present'


def test_manifest_file_is_immutable_and_cli_plan_does_not_touch_runtime(tmp_path):
    importer, work, source, target, assessment = setup(tmp_path)
    manifest = importer.plan(assessment)
    path = work / 'review/manifest.json'
    before = tree_bytes(target)
    assert importer.write_manifest(manifest, path) is True
    assert importer.write_manifest(manifest, path) is False
    assert path.stat().st_mode & 0o777 == 0o600
    changed = deepcopy(manifest);changed['seed_campaign']['name'] = 'different';rehash(changed)
    with pytest.raises(ValueError, match='collision'):
        importer.write_manifest(changed, path)
    command = [sys.executable, str(REPO / 'scripts/import-companion-mappings.py'), 'plan', '--source-data', str(source),
               '--target-data', str(target), '--evidence-root', str(work), '--assessment', str(assessment), '--manifest', str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    assert output['status'] == 'review_required' and output['runtime_writes'] == 0
    assert output['product_ids'] == sorted(EXPECTED_SOURCES, key=int)
    assert tree_bytes(target) == before
    apply_command = [sys.executable, str(REPO / 'scripts/import-companion-mappings.py'), 'apply', '--source-data', str(source),
                     '--target-data', str(target), '--evidence-root', str(work), '--manifest', str(path)]
    result = subprocess.run(apply_command, capture_output=True, text=True)
    assert result.returncode == 1 and json.loads(result.stdout)['status'] == 'held'
    assert 'PRIVATE' not in result.stdout
