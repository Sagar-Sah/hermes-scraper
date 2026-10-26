"""Pinned cache attachment is atomic and cannot materialize/finalize products."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from crawler.fourover_collected_evidence import attach_cache,load_cache,mapped_product,PRODUCT,CHILD,digest
from fourover_observer_fixture import build,CAMPAIGN,SNAPSHOT,PRIVATE,rewrite_child,rewrite_job

@pytest.fixture
def env(tmp_path):
    value=build(tmp_path)
    yield value
    value.store.engine.dispose()

def load(env):return load_cache(env.store,SNAPSHOT,env.checkpoint,env.rows)
def attach(env):return attach_cache(env.manager,CAMPAIGN,SNAPSHOT,env.checkpoint,env.rows)


def test_complete_705_bindings_and_12810_rows_attach_once_without_import(env,monkeypatch):
    before=env.manager.get(CAMPAIGN)['products'][1]
    writer=Mock(wraps=env.manager._write);monkeypatch.setattr(env.manager,'_write',writer)
    result=attach(env)
    assert result['exact_source_rows']==12810 and result['children']==705
    assert result['supplier_requests']==result['rows_materialized']==0 and not result['final_eligible']
    writer.assert_called_once()
    current=env.manager.get(CAMPAIGN);product=current['products'][0]
    assert product['stage']=='queued' and product['row_count']==0
    assert not product.get('diagnosis_id') and not product.get('verified_at') and not product.get('finalized_at')
    assert current['products'][1]==before and current['counts']['finalized']==0
    price=json.loads(env.store.get_object(product['fourover_price_table_evidence']['object_path']))
    mapping=json.loads(env.store.get_object(product['fourover_option_map_evidence']['object_path']))
    assert len(price['observations'])==len(mapping['rows'])==705
    assert all(not row['payable_total_verified'] for row in price['observations'])
    assert not price['production_verified'] and not price['shipping_verified']
    assert PRIVATE not in json.dumps((price,mapping))
    assert not list(Path(env.store.root).rglob('*.csv'))
    assert attach(env)['status']=='already_attached_historical_evidence'
    writer.assert_called_once()


def test_second_object_failure_preserves_entire_campaign_bytes(env,monkeypatch):
    path=env.manager._path(CAMPAIGN);before=path.read_bytes();put=env.store.put_object;calls=[]
    def fail(raw,suffix):
        calls.append(suffix)
        if len(calls)==2:raise OSError('local fixture failure')
        return put(raw,suffix)
    monkeypatch.setattr(env.store,'put_object',fail)
    with pytest.raises(OSError):attach(env)
    assert path.read_bytes()==before and len(calls)==2

@pytest.mark.parametrize('change',[
    lambda e:e.manager.tasks.update({CAMPAIGN:object()}),
    lambda e:_edit_product(e,stage='extracted'),
    lambda e:_edit_product(e,row_count=1),
    lambda e:_edit_product(e,verified_at='x'),
    lambda e:_edit_product(e,supplier_url='https://4over.com/other'),
    lambda e:_edit_product(e,supplier='other'),
])
def test_campaign_state_or_mapping_change_blocks_attachment(env,change):
    change(env)
    with pytest.raises(ValueError):attach(env)

def _edit_product(env,**changes):
    data=env.manager.get(CAMPAIGN);data['products'][0].update(changes);env.manager._write(data)

@pytest.mark.parametrize('bad',[True,12.34,'NaN','Infinity','-1','1e999999999','0','1.'+'0'*19])
def test_noncanonical_prices_fail_closed(env,bad):
    rewrite_child(env,lambda v:v['package_prices'][0].update(price=bad))
    with pytest.raises(ValueError,match='package price'):load(env)

@pytest.mark.parametrize('change',[
    lambda v:v['scope'].update(sets=True),lambda v:v['scope'].update(job_sample=0),
    lambda v:v.update(child_id='999'),lambda v:v['package_prices'].append(v['package_prices'][0]),
    lambda v:v.update(tier_prices=[{'price':'1'}]),
])
def test_corrupt_evidence_scope_quantity_and_domain_rejected(env,change):
    # Deep-copy the shared SCOPE value, so fixture mutations never affect constants.
    for value in env.objects.values():value['scope']=deepcopy(value['scope'])
    rewrite_child(env,change)
    with pytest.raises(ValueError):load(env)


def test_hash_membership_and_predecode_size_bounds(env):
    with pytest.raises(ValueError,match='checkpoint changed'):
        load_cache(env.store,SNAPSHOT,'0'*64,env.rows)
    record=env.job['observations'][CHILD];path=Path(env.store.root)/record['object_path']
    path.write_bytes(b' '*(2*1024*1024+1))
    with pytest.raises(ValueError,match='Bounded immutable'):load(env)


def test_binding_membership_cannot_be_changed_with_recomputed_row_pin(env):
    env.job['option_contract']['rows'][0]['fields'][0]['value']='999999'
    rewrite_job(env)
    with pytest.raises(ValueError,match='memberships'):load(env)


def test_checkpoint_and_record_paths_cannot_escape_or_silently_drop_child(env):
    env.job['observations'][CHILD]['object_path']='../external.json';rewrite_job(env)
    with pytest.raises(ValueError,match='path invalid'):load(env)
    env.job['observations'].pop(CHILD);rewrite_job(env)
    with pytest.raises(ValueError,match='membership'):load(env)
