from copy import deepcopy
import hashlib
import json

import pytest

from crawler.companion_fourover_coverage import FourOverCoverageVerifier, bind_source_operations


SOURCE = 'https://4over.com/standard-business-cards'
SCOPE = {'sets': 1, 'job_sample': False, 'digital_proof': False}


def fixture():
    objects = {}
    def put(value, suffix):
        raw = json.dumps(value, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        path = f'objects/{sha[:2]}/{sha}.{suffix}'
        objects[path] = raw
        return {'object_path': path, 'sha256': sha}
    cycle = {'id': 'a' * 32, 'created_at': 100}
    fields = [{'attribute_id': '211', 'label': 'Size', 'value': '4', 'choice': '3.5 x 2'}]
    mapping = {'source_url': SOURCE, 'complete_child_mapping': True, 'rows': [{'child_id': '10', 'fields': fields}]}
    quantities = [100, 250, 500, 1000, 2500, 5000, 7500, 10000, 25000, 100000]
    prices = [{'quantity': q, 'price': str(q // 10) + '.00'} for q in quantities]
    collection = {'snapshot_id': 'b' * 32, 'source_url': SOURCE, 'scope': SCOPE,
                  'status': 'completed_requested_subset', 'document_body_sha256': 'c' * 64,
                  'declared_children': [{'child_id': '10'}],
                  'option_contract': {'binding_complete': True, 'rows': mapping['rows']},
                  'observations': [{'child_id': '10', 'status': 'collected', 'source_url': SOURCE, 'scope': SCOPE,
                                    'raw_response_sha256': 'd' * 64, 'package_prices': prices, 'tier_prices': []}]}
    production = {'value': '4', 'label': '4 Business Days'}
    native = {'schema': 'hermes-fourover-rendered-child-domain/v1',
              'producer': 'fourover-rendered-domain-and-conditional-controls/v1', 'cycle_id': cycle['id'],
              'snapshot_id': collection['snapshot_id'], 'source_url': SOURCE, 'child_id': '10', 'scope': SCOPE,
              'observed_at': 150, 'document_sha256': 'c' * 64, 'price_response_sha256': 'd' * 64,
              'page_generation': 'owned-page-epoch', 'selected_attributes': {'211': '4'}, 'additional_option_fields': [],
              'scope_controls': {'job_sample': {'count': 1, 'checked': False, 'enabled': True},
                                 'digital_proof': {'count': 1, 'checked': False, 'enabled': True},
                                 'sets': {'count': 1, 'value': '1', 'enabled': True, 'valid': True}},
              'attribute_controls': {'211': {'count': 1, 'selected': '4', 'enabled': True, 'visible': True,
                                            'implicit_fixed_dash': False,
                                            'options': [{'value': '4', 'enabled': True, 'label': '3.5 x 2'}]}},
              'quantity_controls': {'available': quantities, 'row_count': len(quantities), 'truncated': False,
                                    'remaining_expand_controls': 0,
                                    'expansion': {'action': 'visible_see_more', 'before': quantities[:7], 'after': quantities}},
              'production_by_quantity': {str(q): [{**production, 'enabled': True, 'selected': True, 'visible': True}] for q in quantities},
              'quote_receipts': {}}
    for row in prices:
        q = row['quantity']
        selection = {'child_id': '10', 'run_size': q, 'attributes': {'211': '4'}}
        quote = {'schema': 'fourover-automatic-source-quote/v1', 'scope': SCOPE, 'selection': selection,
                 'association': {'source_url': SOURCE}, 'source_document_sha256': 'c' * 64,
                 'page_generation': 'owned-page-epoch', 'price_response_sha256': 'd' * 64, 'observed_at': 160,
                 'request_context': {**selection, 'sets': 1, 'services': SCOPE, 'production': production},
                 'production': production, 'summary_response_sha256': hashlib.sha256(str(q).encode()).hexdigest(),
                 'current_source_table_price': row['price'], 'totals': {'subtotal': row['price'], 'grand_total': row['price']},
                 'supplier_product_total_excluding_shipping': row['price']}
        native['quote_receipts'][str(q)] = put(quote, 'fourover-automatic-quote.json')
    def verify(native_value=native):
        ref = put(native_value, 'fourover-rendered-domain.json')
        verifier = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200)
        return verifier.verify(option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
                               rendered_child_receipts={'10': ref})
    return objects, put, cycle, mapping, collection, native, verify


def test_full_expanded_current_domain_and_exact_quotes_including_100k():
    *_, result = fixture()
    coverage = result()
    assert coverage['source_domain_complete'] is True
    assert coverage['status'] == 'verified_scoped_source'
    assert len(coverage['eligible_source_rows']) == 10
    assert coverage['eligible_source_rows'][-1]['selection']['quantity'] == '100000'
    assert coverage['supplier_requests_performed'] == 0


def test_price_table_never_substitutes_for_rendered_child_evidence():
    objects, _, cycle, mapping, collection, _, _ = fixture()
    result = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200).verify(
        option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle)
    assert result['source_domain_complete'] is False
    assert result['reason'] == 'current_cycle_expanded_rendered_child_receipts_missing'
    assert result['required_driver'] == 'fourover-rendered-domain-and-conditional-controls/v1'


@pytest.mark.parametrize('mutate', [
    lambda n: n.update(cycle_id='e' * 32),
    lambda n: n.update(observed_at=99),
    lambda n: n.update(observed_at=201),
    lambda n: n.update(snapshot_id='f' * 32),
    lambda n: n.update(additional_option_fields=['unmapped_conditional_foil']),
    lambda n: n['scope_controls']['digital_proof'].update(checked=True),
    lambda n: n['quantity_controls'].update(available=n['quantity_controls']['available'][:7]),
    lambda n: n['quantity_controls'].update(truncated=True),
    lambda n: n['quantity_controls'].update(remaining_expand_controls=1),
    lambda n: n['quantity_controls']['expansion'].update(action='price_table_only'),
    lambda n: n['attribute_controls']['211']['options'].append({'value': '5', 'enabled': True, 'label': 'extra'}),
    lambda n: n['attribute_controls']['211'].update(count=2),
    lambda n: n['production_by_quantity']['100'].append({'value': '2', 'label': '2 Business Days', 'enabled': True, 'selected': False, 'visible': True}),
    lambda n: n['quote_receipts'].pop('100000'),
])
def test_stale_hidden_partial_conditional_or_duplicate_domain_never_complete(mutate):
    _, _, _, _, _, native, verify = fixture()
    mutate(native)
    result = verify()
    assert result['status'] == 'unknown'
    assert result['source_domain_complete'] is False
    assert result['eligible_source_rows'] == []


def test_source_quote_integrity_prices_and_retained_scope_are_not_canaries():
    objects, put, _, _, _, native, verify = fixture()
    oldref = native['quote_receipts']['100000']
    quote = json.loads(objects[oldref['object_path']])
    quote['totals']['subtotal'] = '1.00'
    native['quote_receipts']['100000'] = put(quote, 'fourover-automatic-quote.json')
    assert verify()['source_domain_complete'] is False
    quote['totals']['subtotal'] = quote['current_source_table_price']
    quote['request_context']['sets'] = 2
    native['quote_receipts']['100000'] = put(quote, 'fourover-automatic-quote.json')
    assert verify()['source_domain_complete'] is False


def test_tampered_bytes_and_fake_hash_only_claim_never_complete():
    objects, _, cycle, mapping, collection, native, verify = fixture()
    ref = native['quote_receipts']['100']
    objects[ref['object_path']] = b'{}'
    assert verify()['reason'] == 'coverage_evidence_hash_changed'
    verifier = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200)
    result = verifier.verify(option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
                             rendered_child_receipts={'10': {'sha256': '0' * 64, 'status': 'passed'}})
    assert result['source_domain_complete'] is False


def test_verified_changed_source_cost_binds_correction_without_requiring_site_match():
    *_, verify = fixture()
    coverage = verify()
    operation = {'kind': 'set_cost', 'selection': {'size': '3.5 x 2', 'quantity': '100'}, 'cost': '10.00'}
    bound = bind_source_operations(coverage, [operation])
    assert bound['source_bound'] is True
    assert bound['write_authorized'] is False
    # Existing website cost is deliberately not an input to source integrity.
    # A site mismatch should produce a repair rather than invalidate the source.
    operation['cost'] = '11.00'
    with pytest.raises(ValueError, match='not_bound'):
        bind_source_operations(coverage, [operation])


def test_unsupported_row_and_production_change_still_require_evidence():
    *_, verify = fixture()
    coverage = verify()
    operation = {'kind': 'add_row', 'selection': {'size': '3.5 x 2', 'quantity': '100'},
                 'cost': '10.00', 'production': '4 Business Days'}
    assert bind_source_operations(coverage, [operation])['source_bound'] is True
    operation['production'] = '1 Business Day'
    with pytest.raises(ValueError, match='production_not_bound'):
        bind_source_operations(coverage, [operation])
    operation['kind'] = 'delete_row'
    with pytest.raises(ValueError, match='supported_source_bound'):
        bind_source_operations(coverage, [operation])
    with pytest.raises(ValueError, match='verified_source_coverage'):
        bind_source_operations({'status': 'unknown'}, [operation])


def test_registered_complete_dependency_avoids_requiring_quote_per_quantity():
    objects, put, cycle, mapping, collection, native, _ = fixture()
    native['quote_receipts'] = {}
    proof = {'schema': 'hermes-fourover-proven-price-dependency/v1', 'child_id': '10', 'source_url': SOURCE,
             'snapshot_id': collection['snapshot_id'], 'scope': SCOPE, 'binding_evidence_sha256': '9' * 64,
             'rows': [{'quantity': row['quantity'], 'cost': row['price'],
                       'production': {'value': '4', 'label': '4 Business Days'}}
                      for row in collection['observations'][0]['package_prices']]}
    reference = put(proof, 'price-dependency-proof.json')
    native['price_dependency'] = {'verifier_id': 'reviewed_fixture_dependency', 'evidence': reference}
    child_ref = put(native, 'fourover-rendered-domain.json')
    calls = []
    def registered(**kwargs):
        calls.append(kwargs['child_id'])
        return kwargs['load_evidence'](kwargs['native']['price_dependency']['evidence'], 'price-dependency-proof.json')
    verifier = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200,
                                       price_dependency_verifiers={'reviewed_fixture_dependency': registered})
    result = verifier.verify(option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
                             rendered_child_receipts={'10': child_ref})
    assert result['source_domain_complete'] is True
    assert calls == ['10']
    assert all(row['price_basis'] == 'registered_complete_price_dependency' for row in result['eligible_source_rows'])
    assert all('quote_sha256' not in row for row in result['eligible_source_rows'])
    # Evidence claiming a registered id cannot register code or authorize itself.
    result = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200).verify(
        option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
        rendered_child_receipts={'10': child_ref})
    assert result['source_domain_complete'] is False


def test_broken_dependency_cannot_invent_cost_and_errors_do_not_expose_text():
    objects, put, cycle, mapping, collection, native, _ = fixture()
    native['quote_receipts'] = {}
    native['price_dependency'] = {'verifier_id': 'reviewed_fixture_dependency'}
    ref = put(native, 'fourover-rendered-domain.json')
    def failing(**kwargs):
        raise ValueError('secret_token_like_string_123')
    result = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200,
        price_dependency_verifiers={'reviewed_fixture_dependency': failing}).verify(
            option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
            rendered_child_receipts={'10': ref})
    assert result['reason'] == 'coverage_evidence_invalid'
    assert 'secret' not in json.dumps(result)


def actual_native_fixture():
    from crawler.fourover_automatic_profile import safe_native_domain_observation
    objects, put, cycle, mapping, collection, native, _ = fixture()
    quantities = native['quantity_controls']['available']
    raw = {'status': 'observed', 'child_id': '10', 'observed_run_size': '100',
           'selected_attributes': native['selected_attributes'], 'attribute_controls': native['attribute_controls'],
           'scope_controls': {'job_sample': {'count': 1, 'checked': False, 'enabled': True, 'visible': True},
                              'digital_proof': {'count': 1, 'checked': False, 'enabled': True, 'visible': True},
                              'sets': {'count': 0, 'binding': 'one_native_job'}},
           'additional_option_fields': [],
           'quantity_controls': {'rendered': [{'value': str(q), 'label': str(q), 'enabled': True} for q in quantities],
                                 'model_available': quantities, 'remaining_expand_controls': 0, 'truncated': False},
           'production_controls': native['production_by_quantity']['100']}
    selection = {'child_id': '10', 'run_size': 100, 'attributes': {'211': '4'}}
    def quote_ref(raw_value=raw):
        observation = safe_native_domain_observation(raw_value, source=SOURCE, selection=selection,
            table={'sha256': 'd' * 64, 'rows': collection['observations'][0]['package_prices']},
            document_sha256='c' * 64, page_generation='f' * 32, snapshot_id=collection['snapshot_id'], observed_at=150)
        quote = json.loads(objects[native['quote_receipts']['100']['object_path']])
        quote.update(page_generation='f' * 32, catalog_reference={'snapshot_id': collection['snapshot_id']},
                     native_domain_observation=observation,
                     native_domain_evidence=put(observation, 'fourover-native-domain.json'))
        return put(quote, 'fourover-automatic-quote.json')
    return objects, put, cycle, mapping, collection, raw, quote_ref


def test_actual_native_driver_observation_proves_quantity_but_never_full_prices():
    objects, _, cycle, mapping, collection, _, quote_ref = actual_native_fixture()
    result = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200).inspect_native_observations(
        option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle, quote_receipts=[quote_ref()])
    assert result['source_quantity_domain_complete'] is True
    assert result['expanded_rendered_domain'] == 'verified'
    assert result['checks']['native_evidence_integrity'] == 'match'
    assert result['status'] == 'unknown'
    assert result['source_domain_complete'] is False
    assert result['source_price_evidence_complete'] is False


def test_actual_native_collapsed_or_previous_cycle_never_proves_full_quantity_domain():
    objects, _, cycle, mapping, collection, raw, quote_ref = actual_native_fixture()
    raw['quantity_controls']['rendered'] = raw['quantity_controls']['rendered'][:7]
    raw['quantity_controls']['truncated'] = True
    raw['quantity_controls']['remaining_expand_controls'] = 1
    verifier = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200)
    result = verifier.inspect_native_observations(option_map=mapping, collection=collection, fields=('size', 'quantity'),
                                                cycle=cycle, quote_receipts=[quote_ref()])
    assert result['source_quantity_domain_complete'] is False
    newer = {**cycle, 'created_at': 151}
    result = verifier.inspect_native_observations(option_map=mapping, collection=collection, fields=('size', 'quantity'),
                                                cycle=newer, quote_receipts=[quote_ref()])
    assert result['checks']['native_evidence_integrity'] == 'unknown'


def test_actual_native_driver_object_binding_and_child_snapshot_cannot_be_forged():
    objects, put, cycle, mapping, collection, _, quote_ref = actual_native_fixture()
    reference = quote_ref()
    quote = json.loads(objects[reference['object_path']])
    quote['catalog_reference']['snapshot_id'] = 'e' * 32
    result = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200).inspect_native_observations(
        option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
        quote_receipts=[put(quote, 'fourover-automatic-quote.json')])
    assert result['source_quantity_domain_complete'] is False
    quote['catalog_reference']['snapshot_id'] = collection['snapshot_id']
    quote['native_domain_observation']['expanded_rendered_domain'] = 'unknown'
    result = FourOverCoverageVerifier(objects.__getitem__, now=lambda: 200).inspect_native_observations(
        option_map=mapping, collection=collection, fields=('size', 'quantity'), cycle=cycle,
        quote_receipts=[put(quote, 'fourover-automatic-quote.json')])
    assert result['source_quantity_domain_complete'] is False
