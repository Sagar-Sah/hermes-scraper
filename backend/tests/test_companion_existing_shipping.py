import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler.companion_environments import digest
from crawler.companion_existing_shipping import ExistingShippingEvidence, PROFILE, SOURCE_PRODUCERS, SITE_PRODUCER
from crawler.companion_shipping_audit import CompanionShippingAudit


def write(root, path, value):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True).encode() if isinstance(value, dict) else value
    destination.write_bytes(data)
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest()}


def prepared(tmp_path, *, free=False):
    root = tmp_path / 'evidence'
    root.mkdir()
    model_artifact = write(root, 'model.php', b'<?php /* installed model fixture */')
    fixtures = []
    for index, (state, postcode) in enumerate([('AZ', '85001'), ('FL', '33101'), ('NY', '10001')]):
        for tier in ('metropolitan', 'suburban', 'rural'):
            key = state.lower() + '_' + tier
            context = {'product_id': 6669, 'source_product_id': '83545', 'fulfillment_id': 'source_verified_facility',
                       'configuration': {'size': '18x24' if index == 0 else '24x30', 'sets': 1 + index},
                       'destination': {'country': 'US', 'state': state, 'postcode': postcode, 'tier': tier},
                       'service': 'Ground', 'currency': 'USD'}
            fixtures.append({'id': key, 'context': context})
    approved = write(root, 'approved.json', {'schema': 'hermes-approved-shipping-fixtures/v1', 'approved_by': 'human',
                                           'approved_at': 50, 'fixtures': fixtures})
    model = {'policy': 'vendor_free' if free else 'estimated', 'model_sha256': model_artifact['sha256'],
             'scope': {'family': 'real_estate_post', 'service': 'Ground'}, 'product_ids': [6669],
             'frozen_at': 100, 'max_quote_age_hours': 48, 'training_observation_ids': ['training_az'],
             'predeclared_holdout_ids': [f['id'] for f in fixtures],
             'holdout_contexts_sha256': {f['id']: digest(f['context']) for f in fixtures}}
    contract = {'schema': 'hermes-existing-shipping-contract/v1', 'model_id': 'real_estate_post_v1',
                'supplier': '4over', 'model': model, 'model_artifact': model_artifact,
                'approved_fixtures': approved, 'fixtures': fixtures}
    if free:
        policy = {'schema': 'hermes-verified-vendor-free-policy/v1', 'supplier': '4over', 'service': 'Ground',
                  'scope': model['scope'], 'approved_merchant_site': 'https://printitprints.com',
                  'membership_active': True, 'retail_site_approved_by_vendor': True,
                  'eligible_products': [6669], 'valid_until': 1000}
        for key in ('vendor_terms', 'eligibility', 'merchant_approval'):
            policy[key] = write(root, key + '.txt', (key + ' saved evidence').encode())
        contract['free_policy_artifact'] = write(root, 'policy.json', policy)
        model['free_shipping_evidence'] = {'vendor_policy_verified': True, 'retail_site_approved': True,
                                          'eligibility_verified': True, 'valid_until': 1000,
                                          'sha256': contract['free_policy_artifact']['sha256']}
    c = write(root, 'contract.json', contract)
    cycle = {'id': 'a' * 32, 'created_at': 200}
    bundle = {'schema': 'hermes-shipping-evidence-bundle/v1', 'cycle_id': cycle['id'], 'model_id': contract['model_id'],
              'contract_sha256': c['sha256'], 'pairs': {}}
    for fixture in fixtures:
        pair = {}
        for kind, producer in [('source', SOURCE_PRODUCERS['4over']), ('site', SITE_PRODUCER)]:
            context = fixture['context']
            quote = {'schema': 'hermes-bound-shipping-quote/v1', 'producer': producer,
                     'cycle_id': cycle['id'], 'fixture_id': fixture['id'], 'observed_at': 250,
                     'request': context, 'retained': context, 'http_status': 200,
                     'response': {'context': context, 'service': 'Ground', 'currency': 'USD',
                                  'shipping_total': '0.00' if free else '100.00' if kind == 'source' else '102.00'},
                     'side_effects': {'orders': 0, 'payments': 0, 'customer_records': 0, 'account_defaults': 0},
                     'native_binding': {'request_identity': fixture['id'], 'response_identity': fixture['id'],
                                        'selected_service': 'Ground'}}
            if kind == 'site':
                quote.update(environment='staging', model_sha256=model['model_sha256'])
            pair[kind] = write(root, f'quotes/{fixture["id"]}-{kind}.json', quote)
        bundle['pairs'][fixture['id']] = pair
    bundle_path = f'cycles/{cycle["id"]}/{contract["model_id"]}.json'
    write(root, bundle_path, bundle)
    monthly_root = tmp_path / 'monthly'
    monthly_root.mkdir()
    monthly = SimpleNamespace(root=monthly_root, collectors={})
    loader = ExistingShippingEvidence(root, now=lambda: 300)
    loader.register(monthly, 'contract.json')
    manifest = json.loads((monthly_root / (contract['model_id'] + '.json')).read_text())
    return root, loader, monthly, manifest, contract, cycle, bundle, bundle_path


def update_quote(root, bundle, bundle_path, key, **changes):
    ref = bundle['pairs'][key]['source']
    quote = json.loads((root / ref['path']).read_text())
    quote.update(changes)
    bundle['pairs'][key]['source'] = write(root, ref['path'], quote)
    write(root, bundle_path, bundle)


def test_registered_paid_model_loads_nine_independent_exact_pairs(tmp_path):
    root, loader, monthly, manifest, contract, cycle, bundle, path = prepared(tmp_path)
    assert monthly.collectors[PROFILE] is loader
    rows = asyncio.run(loader(cycle, manifest))
    assert len(rows) == 9
    result = CompanionShippingAudit.assess(manifest, rows, {'model_sha256': manifest['model_sha256']}, now=300)
    assert result['status'] == 'match'
    assert result['national_or_all_configuration_accuracy_verified'] is False
    assert loader.last_result[contract['model_id']]['live_requests_performed'] == 0


@pytest.mark.parametrize('change', [
    {'observed_at': 199}, {'observed_at': 301}, {'cycle_id': 'b' * 32}, {'producer': 'untrusted'},
    {'http_status': 504}, {'retained': {'selection': 'different'}}, {'status': 'passed', 'response': {}},
    {'native_binding': {'request_identity': 'a', 'response_identity': 'b', 'selected_service': 'Ground'}},
    {'side_effects': {'orders': 1, 'payments': 0, 'customer_records': 0, 'account_defaults': 0}},
])
def test_bad_stale_or_claimed_evidence_is_unknown_without_requests(tmp_path, change):
    root, loader, _, manifest, contract, cycle, bundle, path = prepared(tmp_path)
    update_quote(root, bundle, path, 'az_metropolitan', **change)
    assert asyncio.run(loader(cycle, manifest)) == []
    assert loader.last_result[contract['model_id']]['status'] == 'unknown'


def test_quote_tamper_missing_bundle_and_new_month_never_reuse_old_data(tmp_path):
    root, loader, _, manifest, _, cycle, bundle, path = prepared(tmp_path)
    quote = root / bundle['pairs']['az_metropolitan']['source']['path']
    quote.write_text('{}')
    assert asyncio.run(loader(cycle, manifest)) == []
    next_cycle = {'id': 'b' * 32, 'created_at': 300}
    assert asyncio.run(loader(next_cycle, manifest)) == []
    assert asyncio.run(loader({'id': '../outside', 'created_at': 200}, manifest)) == []


def test_model_changes_and_wrong_site_runtime_are_unknown(tmp_path):
    root, loader, _, manifest, _, cycle, bundle, path = prepared(tmp_path)
    wrong = deepcopy(manifest)
    wrong['model_sha256'] = '0' * 64
    assert asyncio.run(loader(cycle, wrong)) == []
    ref = bundle['pairs']['az_metropolitan']['site']
    quote = json.loads((root / ref['path']).read_text())
    quote['model_sha256'] = '0' * 64
    bundle['pairs']['az_metropolitan']['site'] = write(root, ref['path'], quote)
    write(root, path, bundle)
    assert asyncio.run(loader(cycle, manifest)) == []


def test_rate_drift_is_reported_not_refitted(tmp_path):
    root, loader, _, manifest, _, cycle, bundle, path = prepared(tmp_path)
    ref = bundle['pairs']['az_metropolitan']['site']
    quote = json.loads((root / ref['path']).read_text())
    quote['response']['shipping_total'] = '102.01'
    bundle['pairs']['az_metropolitan']['site'] = write(root, ref['path'], quote)
    write(root, path, bundle)
    rows = asyncio.run(loader(cycle, manifest))
    result = CompanionShippingAudit.assess(manifest, rows, {'model_sha256': manifest['model_sha256']}, now=300)
    assert result['status'] == 'mismatch'
    assert result['automatic_model_changes_allowed'] is False


@pytest.mark.parametrize('change', ['missing_tier', 'single_configuration', 'training_overlap', 'unapproved'])
def test_fixture_contract_requires_nine_approved_regions_and_independence(tmp_path, change):
    root, _, monthly, _, contract, _, _, _ = prepared(tmp_path)
    if change == 'missing_tier':
        contract['fixtures'].pop()
    elif change == 'single_configuration':
        for fixture in contract['fixtures']:
            fixture['context']['configuration'] = {'size': '18x24', 'sets': 1}
    elif change == 'training_overlap':
        contract['model']['training_observation_ids'] = ['az_metropolitan']
    else:
        contract['approved_fixtures'] = {'path': 'approved.json', 'sha256': '0' * 64}
    # Keep the independent approval artifact unmodified; changing a context or
    # removing a fixture therefore also fails the approved-contract binding.
    write(root, 'bad-contract.json', contract)
    with pytest.raises(ValueError):
        ExistingShippingEvidence(root, now=lambda: 300).register(monthly, 'bad-contract.json')


def test_actual_model_artifact_and_symlink_pins_required(tmp_path):
    root, _, monthly, _, contract, _, _, _ = prepared(tmp_path)
    (root / 'model.php').write_bytes(b'changed runtime')
    with pytest.raises(ValueError, match='hash_changed'):
        ExistingShippingEvidence(root, now=lambda: 300).register(monthly, 'contract.json')
    outside = tmp_path / 'outside'
    outside.write_text('{}')
    (root / 'symlink.json').symlink_to(outside)
    with pytest.raises(ValueError, match='path_invalid'):
        ExistingShippingEvidence(root)._read('symlink.json')
    with pytest.raises(ValueError, match='path_invalid'):
        ExistingShippingEvidence(root)._read('../outside')


def test_vendor_free_requires_actual_policy_approval_and_eligibility_artifacts(tmp_path):
    root, loader, _, manifest, contract, cycle, _, _ = prepared(tmp_path, free=True)
    rows = asyncio.run(loader(cycle, manifest))
    assert len(rows) == 9
    result = CompanionShippingAudit.assess(manifest, rows, {'model_sha256': manifest['model_sha256']}, now=300)
    assert result['status'] == 'match'
    assert result['checks']['independent_holdouts'] == 'not_applicable'
    (root / 'merchant_approval.txt').write_bytes(b'changed')
    assert asyncio.run(loader(cycle, manifest)) == []


def test_expired_free_policy_and_source_scope_changes_are_unknown(tmp_path):
    root, loader, _, manifest, contract, cycle, bundle, path = prepared(tmp_path, free=True)
    loader.now = lambda: 1001
    assert asyncio.run(loader(cycle, manifest)) == []
    loader.now = lambda: 300
    ref = bundle['pairs']['az_metropolitan']['source']
    quote = json.loads((root / ref['path']).read_text())
    quote['response']['context']['fulfillment_id'] = 'another_warehouse'
    bundle['pairs']['az_metropolitan']['source'] = write(root, ref['path'], quote)
    write(root, path, bundle)
    assert asyncio.run(loader(cycle, manifest)) == []


def test_restart_registration_is_local_and_bad_model_does_not_block_other_models(tmp_path):
    root, _, _, _, _, _, _, _ = prepared(tmp_path)
    folder = root / 'contracts'
    folder.mkdir()
    (folder / 'real_estate_post_v1.json').write_bytes((root / 'contract.json').read_bytes())
    (folder / 'bad.json').write_text('{}')
    (folder / 'unusual filename.json').write_text('{}')
    monthly_root = tmp_path / 'restarted'
    monthly_root.mkdir()
    monthly = SimpleNamespace(root=monthly_root, collectors={})
    loader = ExistingShippingEvidence(root, now=lambda: 300)
    result = loader.register_available(monthly)
    assert [x['status'] for x in result] == ['unknown', 'registered']
    assert len(monthly.collectors) == 1
    assert (monthly_root / 'real_estate_post_v1.json').exists()
    assert (monthly_root / 'bad.json').exists() is False
