"""Synthetic native dependency traversal; local disposable Chromium only.

Prices here exercise traversal, not supplier quote validation. The modern quote,
production and proof gates remain byte-identical and run in their own regressions.
"""
import ast
import hashlib
import json
from pathlib import Path

import pytest
from test_browser_traversal import local_page, coverage
from crawler.adapters.printograph import AdapterError

ROOT = Path(__file__).resolve().parent / 'fixtures/printograph_board_orientation'


def page(product_type, *, late=False, shapes=False):
    shape = ('<label for=shape>Shape</label><select id=shape><option value=15>Rectangle</option>'
             '<option value=5>Rounded rectangle</option></select>') if shapes else ''
    return f'''<!doctype html><h1>Synthetic board dependency</h1>
<div id=order-form data-printproducttype="{product_type}">{shape}
<label for=size>Size</label><select id=size><option value=10667>6 x 18</option></select>
<label for=quantity>Quantity</label><select id=quantity><option value=1>1</option></select>
<div id=orientation-slot></div>
<label for=productionTime>Production time</label><select id=productionTime><option value=3>Rush 1 business day</option></select>
</div><div id=subtotal>$5.00</div><script>
const root=document.querySelector('#order-form');
function show(){{document.querySelector('#subtotal').textContent='$'+(5+Number(document.querySelector('#quantity').value)+Number(document.querySelector('#productionTime').value)).toFixed(2);}}
function sizeOptions(){{const mode=document.querySelector('[name=orientation]:checked')?.value||'1';const shape=document.querySelector('#shape')?.value||'15';const base=shape==='15'?10667:10669;document.querySelector('#size').innerHTML='<option value="'+(base+(mode==='2'?1:0))+'">6 x 18</option>';show();}}
function reveal(){{if(document.querySelector('[name=orientation]'))return;document.querySelector('#orientation-slot').innerHTML='<fieldset><legend>Orientation</legend><input id=horizontal name=orientation type=radio value=1 checked><label for=horizontal>Landscape</label><input id=vertical name=orientation type=radio value=2><label for=vertical>Portrait</label></fieldset>';document.querySelectorAll('[name=orientation]').forEach(e=>e.addEventListener('change',sizeOptions));}}
if(!{str(late).lower()})reveal();
document.querySelector('#size').addEventListener('change',()=>{{reveal();show();}});
document.querySelector('#quantity').addEventListener('change',show);
document.querySelector('#productionTime').addEventListener('change',show);
document.querySelector('#shape')?.addEventListener('change',()=>{{document.querySelector('#orientation-slot').innerHTML='';sizeOptions();reveal();}});
show();</script>'''


@pytest.mark.asyncio
@pytest.mark.parametrize('product_type', ['10016', '10019'])
async def test_generic_board_orientation_is_selected_before_size(local_page, product_type):
    rows, events = await local_page(page(product_type))
    expected = {(o, s, q, p) for o, s in [('1', '10667'), ('2', '10668')]
                for q in [1] for p in ['3']}
    actual = {(r['configuration']['orientation']['value'], r['configuration']['size']['value'],
               r['quantity'], r['production']['value']) for r in rows}
    assert actual == expected
    assert len(rows) == len(expected)
    assert coverage(events)['count_is_exact']
    assert not coverage(events)['reasons']
    assert not any(k == 'browser_restore_reset' for k, _ in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('product_type', ['10019'])
async def test_board_shape_orientation_size_domains_remain_bound(local_page, product_type):
    rows, events = await local_page(page(product_type, shapes=True))
    actual = {(r['configuration']['shape']['value'], r['configuration']['orientation']['value'],
               r['configuration']['size']['value']) for r in rows}
    assert actual == {('15', '1', '10667'), ('15', '2', '10668'),
                      ('5', '1', '10669'), ('5', '2', '10670')}
    assert len(rows) == 4
    assert coverage(events)['count_is_exact']
    assert not coverage(events)['reasons']


@pytest.mark.asyncio
@pytest.mark.parametrize('product_type', ['10016'])
async def test_board_filtered_portrait_restores_exact_source_size(local_page, product_type):
    rows, events = await local_page(page(product_type), filters={'orientation': '2', 'size': '10668'})
    assert len(rows) == 1
    assert all(r['configuration']['orientation']['value'] == '2' and
               r['configuration']['size']['value'] == '10668' for r in rows)
    assert not coverage(events)['reasons']


@pytest.mark.asyncio
@pytest.mark.parametrize('product_type', ['10019'])
async def test_impossible_size_orientation_pair_is_not_published(local_page, product_type):
    rows = []
    with pytest.raises(AdapterError, match='No fully selected source configuration produced a quote'):
        await local_page(page(product_type), filters={'orientation': '2', 'size': '10667'}, rows_out=rows)
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize('product_type', ['10016'])
async def test_board_orientation_that_mounts_after_size_is_discovered(local_page, product_type):
    rows, events = await local_page(page(product_type, late=True), filters={'orientation': '2', 'size': '10668'})
    assert len(rows) == 1
    assert all(r['configuration']['size']['value'] == '10668' for r in rows)
    assert len([v for k, v in events if k == 'size_dependency_warmup']) == 1
    assert not coverage(events)['reasons']




def test_saved_native_catalog_and_source_explain_actual_restoration_failure():
    saved = json.loads((ROOT / 'source/saved-run-projection.json').read_bytes())
    assert saved['SQLite_read_only'] and saved['supplier_calls'] == saved['runtime_effects'] == 0
    assert saved['private_Run_response_object_and_event_identifiers_omitted'] is True
    assert all('run' not in record for record in saved['records'])
    assert {r['product_id'] for r in saved['records']} == {'17875', '17898'}
    for record in saved['records']:
        assert record['catalog']['projection']['size_10667']['alternativeSizeId'] == '10668'
        reset = record['latest_resets'][0]
        assert reset['key'] == 'size' and reset['reason'] == 'choice_unavailable'
        assert reset['prefix']['size'] == '10667'
        assert reset['effective_configuration']['orientation'] == '2'
        assert reset['effective_configuration']['size'] == '10668'
    proof = json.loads((ROOT / 'source/public-source-proof.json').read_bytes())
    for item in proof['slices']:
        assert hashlib.sha256((ROOT / item['file']).read_bytes()).hexdigest() == item['sha256']
    assert 's.idv=o.alternativeSizeId' in (ROOT / 'source/size-schema.js').read_text()
    assert '2==a.jsonObj.userSelectedOptions.orientation' in (ROOT / 'source/native-option-value.js').read_text()
    assert 't.jsonObj.userSelectedOptions.size=1==e?t.schema.size.list[r].id:t.schema.size.list[r].idv' in (ROOT / 'source/orientation-action.js').read_text()
