"""Public release fixtures must not reacquire private captures or actual prices."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parent / 'fixtures'


def load(path):
    return json.loads((ROOT / path).read_bytes())


def test_public_source_provenance_has_no_personal_capture_paths():
    for name in ('printograph_board_orientation/source/public-source-proof.json',
                 'printograph_native_core_color/source-proof.json'):
        raw = (ROOT / name).read_text()
        assert '/Users/' not in raw and '/private-' not in raw
        assert 'readonly_actual_run_snapshot' not in load(name)


def test_board_and_catalog_option_regressions_omit_private_capture_ids():
    board = load('printograph_board_orientation/source/saved-run-projection.json')
    assert board['private_Run_response_object_and_event_identifiers_omitted'] is True
    for record in board['records']:
        assert set(record) == {'product_id', 'catalog', 'latest_resets'}
        assert set(record['catalog']) == {'projection'}
        assert set(record['latest_resets'][0]) == {'key', 'reason', 'prefix', 'effective_configuration'}
    catalog = load('printograph_catalog_orientation/type-proof.json')
    assert catalog['private_capture_identifiers_omitted'] is True
    assert 'run_id' not in catalog
    public_options = load('printograph_manual_pdf_policy/observed-public-proof-options.json')
    assert public_options['private_capture_paths_omitted'] is True
    assert all('object_path' not in option for record in public_options['records']
               for option in record['saved_public_product_options'])
    assert all(set(record) == {'status', 'origin', 'path', 'product_type_query'} for record in catalog['records'])


def test_private_production_snapshot_is_not_distributed():
    value = load('printograph_production_cache/saved-projections.json')
    assert value['private_capture_records_omitted'] is True
    assert value['source_price_evidence'] is False
    assert 'records' not in value and 'at' not in value


def test_unavailable_price_fixture_is_explicitly_synthetic_and_complete():
    value = load('printograph_modern_post/native-unavailable-price-response.json')
    assert value['fixture_provenance']['synthetic'] is True
    assert value['fixture_provenance']['source_price_evidence'] is False
    assert [row['quantity'] for row in value['items']] == list(range(1, 151))
    assert all(row == {'quantity': q, 'productPrice': 13.43, 'subtotal': None, 'available': False}
               for q, row in enumerate(value['items'], 1))
    proof = load('printograph_native_proof_badge/observed-no-proof-price-table.json')
    assert proof['fixture_provenance']['synthetic'] is True
    assert proof['fixture_provenance']['source_price_evidence'] is False
    assert len(proof['items']) == 27 and all(row['markupPrice'] == '7.25' for row in proof['items'])


def test_historical_availability_aggregates_have_no_quotes_or_run_state():
    value = load('printograph_native_availability/source/readonly-evidence.json')
    assert value['private_capture_records_and_price_values_omitted'] is True
    assert value['current_quote_or_stock_proof'] is False
    assert not {'Run', 'products', 'batches', 'branches', 'safe_events'} & set(value)
    for item in value['public_response_projections']:
        assert set(item) == {'ref', 'items_count', 'available_true_count', 'available_false_count'}
        assert set(item['ref']) == {'path'}
        assert item['available_false_count'] == 150
    for item in load('printograph_native_availability/source/reason-domain-projection.json'):
        assert set(item) == {'quantities_exact_1_through150', 'reason_type_counts', 'unavailable_positive_product_price_count'}


def test_changed_fixture_manifests_preserve_exact_local_file_hashes():
    for directory, key in (('printograph_manual_pdf_policy', 'files'),
                           ('printograph_modern_post', 'files'),
                           ('printograph_production_cache', 'files'),
                           ('printograph_native_availability', 'files'),
                           ('printograph_native_proof_badge', 'members')):
        manifest = load(directory + '/manifest.json')
        for name, digest in manifest[key].items():
            assert hashlib.sha256((ROOT / directory / name).read_bytes()).hexdigest() == digest
