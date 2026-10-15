"""B2Sign DOM contracts reproduced locally; no supplier requests."""

import json

import pytest

from test_product_diagnosis import manager, completed
from crawler.b2sign_diagnosis import inspect_b2sign
from crawler.b2sign_canary import FIELD_UI_JS


B2_HTML = r'''<!doctype html><body><div class="container _item-main_fixture">
<h1 id="item-view-title">Pole Banner Set</h1><img alt="Pole Banner Set" width="40" height="40" src="https://www.b2sign.com/image/thumb/pole.jpg">
<div class="_instant-quote_outer"><div class="_instant-quote_inner"><div class="_attributes_fixture">
<div class="_attribute_fixture"><label>Size</label><div class="_component_fixture _select_fixture"><div class="MuiSelect-root"><button id="size-button" role="combobox" class="MuiSelect-button" aria-controls="size-list" aria-expanded="false">18&quot; x 24&quot;</button></div></div></div>
<div class="_attribute_fixture" id="attr-hardware"><div class="_component_fixture _button_fixture"><div class="_item_fixture _active_fixture">Banner + Hardware</div><div class="_item_fixture">Banner Only</div><div class="_item_fixture">Hardware Only</div></div></div>
<div class="_attribute_fixture" id="attr-windslit"><label>Windslit</label><button id="wind-button" role="combobox" class="MuiSelect-button" aria-controls="wind-list" aria-expanded="false">No Windslits</button></div>
<div class="_attribute_fixture" id="attr-sides"><label>Graphic</label><div>Double Sided</div></div>
<div class="_attribute_fixture" id="attr-material"><label>Material</label><div>18oz. Blockout Vinyl Banner</div></div>
<div class="_attribute_fixture" id="attr-polepocket"><label>Pole Pocket</label><div>2&quot; - Top and Bottom</div></div>
<div class="_attribute_fixture" id="attr-grommet"><label>Grommet</label><div>4 Corner Only</div></div>
</div><div class="_content_fixture"><div class="_jobname_fixture"><input value="PRIVATE_JOB_NAME"></div>
<div class="_label_fixture">Qty</div><div class="_quantity_fixture"><input maxlength="4" value="1"></div><div class="_subtotal_fixture">$56.26</div></div>
<div class="_item_fixture _turnaround_fixture"><span class="_label_fixture">Turnaround</span><div class="_content_fixture"><div role="radiogroup">
<label><input type="radio" name=":r8:" value="1" checked><span class="_option_fixture">Next Day</span><span>Order before 4pm</span></label>
<label><input type="radio" name=":r8:" value="0"><span class="_option_fixture">Same Day</span><span>+ surcharge shown separately</span></label></div></div></div>
<div class="_item_fixture _shipping-method_fixture"><span class="_label_fixture">Shipping</span><div class="_content_fixture"><div role="radiogroup">
<label><input type="radio" name=":rb:" value="dropship"><span class="_option_fixture">Drop Ship</span></label>
<label><input type="radio" name=":rb:" value="storepickup"><span class="_option_fixture">Store Pickup</span></label></div></div></div>
<label class="MuiCheckbox-root"><input type="checkbox" value="">Email Proof</label>
</div></div><div class="_rich-text_fixture"><h4>Production</h4><p>Over100 needs2 extra businessdays.</p><h4 id="description">Description</h4><p>Light pole banners for display.</p></div></div><nav><a href="/orders">Orders</a></nav>
<script>
window.opens={size:0,wind:0};window.selectionChanges=0;
const size=document.querySelector('#size-button'), wind=document.querySelector('#wind-button');
function open(button,values,key){
 window.opens[key]++;
 const old=document.getElementById(button.getAttribute('aria-controls'));if(old)old.remove();
 const list=document.createElement('ul');list.id=button.getAttribute('aria-controls');list.setAttribute('role','listbox');
 for(const value of values){const li=document.createElement('li');li.setAttribute('role','option');li.textContent=value;
 li.addEventListener('click',()=>{window.selectionChanges++;button.textContent=value});list.appendChild(li)}
 document.body.appendChild(list);button.setAttribute('aria-expanded','true');
 // A separate visible popup must never be mixed into this button's choices.
 if(!document.querySelector('#unrelated-list')){const extra=document.createElement('ul');extra.id='unrelated-list';extra.setAttribute('role','listbox');extra.innerHTML='<li role="option">Unrelated A</li><li role="option">Unrelated B</li>';document.body.appendChild(extra)}
}
size.addEventListener('click',()=>open(size,[18,24,30].flatMap(w=>Array.from({length:73},(_,i)=>w+'" x '+(i+24)+'"')),'size'));
wind.addEventListener('click',()=>open(wind,['No Windslits','Standard Windslits'],'wind'));
document.addEventListener('keydown',e=>{if(e.key==='Escape')for(const button of [size,wind]){if(button.getAttribute('aria-expanded')==='true'){document.getElementById(button.getAttribute('aria-controls'))?.remove();button.setAttribute('aria-expanded','false')}}});
</script></body>'''


@pytest.mark.asyncio
async def test_b2sign_managed_page_captures_custom_choices_without_changing_selection(manager):
    m, pool = manager
    pool.html['/pole-banner-set'] = B2_HTML
    row = await completed(m, 'https://www.b2sign.com/pole-banner-set')
    assert row['status'] == 'ready'
    assert row['title'] == 'Pole Banner Set'
    assert row['session_status'] == 'unknown'  # Orders link is not authentication.
    fields = {field['label']: field for field in row['fields']}
    assert len(fields['Size']['choices']) == 219
    assert len(fields['Windslit']['choices']) == 2
    assert fields['Size']['selected_values'] == ['18" x 24"']
    assert fields['Size']['binding_kind'] == 'display_label_only'
    assert fields['Size']['value_origin'] == 'display_label_surrogate'
    assert fields['Size']['source_id'] == ''
    assert fields['Windslit']['source_id'] == 'attr-windslit'
    assert fields['Graphic']['kind'] == 'static'
    assert fields['Graphic']['selected_values'] == ['Double Sided']
    assert fields['Material']['selected_values'] == ['18oz. Blockout Vinyl Banner']
    assert fields['Pole Pocket']['selected_values'] == ['2" - Top and Bottom']
    assert fields['Grommet']['selected_values'] == ['4 Corner Only']
    hardware = fields['Banner + Hardware / Banner Only / Hardware Only']
    assert len(hardware['choices']) == 3 and hardware['selected_values'] == ['Banner + Hardware']
    assert hardware['warnings'] == ['field_label_from_choices']
    assert fields['Qty']['selected_values'] == ['1']
    assert fields['Qty']['min'] is None and fields['Qty']['max'] is None and fields['Qty']['step'] is None
    assert fields['Turnaround']['source_name'] == ':r8:'
    assert fields['Turnaround']['choices'] == [
        {'value':'1','label':'Next Day','disabled':False}, {'value':'0','label':'Same Day','disabled':False}]
    assert fields['Turnaround']['selected_values'] == ['1']
    assert fields['Shipping']['selected_values'] == []
    assert {c['value'] for c in fields['Shipping']['choices']} == {'dropship','storepickup'}
    assert fields['Email Proof']['selected_values'] == []
    assert fields['Email Proof']['multiple'] is True
    assert row['content']['images'][0]['url'] == 'https://www.b2sign.com/image/thumb/pole.jpg'
    assert row['content']['description'] == 'Light pole banners for display.'
    assert 'extra businessdays' not in json.dumps(row)
    assert 'PRIVATE_JOB_NAME' not in json.dumps(row)
    assert 'Unrelated' not in json.dumps(row)
    page = m._pages[row['id']]
    assert await page.evaluate('window.opens') == {'size':1,'wind':1}
    assert await page.evaluate('window.selectionChanges') == 0
    assert await page.locator('#size-button').get_attribute('aria-expanded') == 'false'
    assert await page.locator('#wind-button').get_attribute('aria-expanded') == 'false'
    assert page.navigations == 1
    refreshed = await m.refresh(row['id'])
    assert refreshed['revision'] == row['revision']
    assert await page.evaluate('window.opens') == {'size':2,'wind':2}
    assert await page.evaluate('window.selectionChanges') == 0
    assert page.navigations == 1


@pytest.mark.asyncio
async def test_product_specific_unlabeled_button_group_is_a_reviewable_field(manager):
    m, pool = manager
    pool.html['/event-tent-canopy-graphic'] = B2_HTML.replace('id="attr-hardware"', 'id="attr-tent_hardware"')
    row = await completed(m, 'https://www.b2sign.com/event-tent-canopy-graphic')
    assert 'unsupported_visible_attribute' not in row['warnings']
    assert any(field['source_id'] == 'attr-tent_hardware' and len(field['choices']) == 3
               for field in row['fields'])


@pytest.mark.asyncio
async def test_open_size_wrapper_is_recognized_as_two_native_axes(manager):
    m, pool = manager
    pool.html['/pole-banner-set'] = B2_HTML
    row = await completed(m, 'https://www.b2sign.com/pole-banner-set')
    page = m._pages[row['id']]
    await page.evaluate('''() => document.querySelector('._attributes_fixture').insertAdjacentHTML('afterbegin',
      '<div id="attr-_size" class="_size-wrapper_fixture"><div class="_attribute_fixture"><label>Size</label><div class="_input-wrapper_fixture"><span class="MuiInput-startDecorator">Width</span><input value="12"></div><div class="_attribute_fixture"><div class="_input-wrapper_fixture"><span class="MuiInput-startDecorator">Height</span><input value="24"></div></div></div></div>')''')
    await page.evaluate('''() => document.querySelector('._attributes_fixture').insertAdjacentHTML('beforeend',
      '<div id="attr-lamination" class="_attribute_fixture"><label>Lamination</label><div class="_component_fixture _static_fixture"><div>Dry Erase Coating</div></div></div>')''')
    await page.evaluate('''() => document.querySelector('._attributes_fixture').insertAdjacentHTML('beforeend',
      '<div id="attr-lam" class="_attribute_fixture"><label>Finish</label><div class="_component_fixture _static_fixture"><div>No Lamination</div></div></div>')''')
    snapshot = await inspect_b2sign(page, expand=False)
    assert 'unsupported_visible_attribute' not in snapshot['warnings']
    assert any(field['kind'] == 'size' and field['selected_values'] == ['12x24'] for field in snapshot['fields'])
    assert any(field['label'] == 'Lamination' and field['selected_values'] == ['Dry Erase Coating'] for field in snapshot['fields'])
    assert any(field['label'] == 'Finish' and field['selected_values'] == ['No Lamination'] for field in snapshot['fields'])
    state = await page.evaluate(FIELD_UI_JS, {'key': '_size', 'label': 'Size'})
    assert state['kind'] == 'size' and state['value'] == '12x24'
    await page.evaluate('''() => document.querySelector('._attributes_fixture').insertAdjacentHTML('beforeend',
      '<div id="attr-single" class="_size-wrapper_fixture"><div class="_attribute_fixture"><label>Graphic Height</label><button role="combobox" aria-controls="height-list">26&quot;</button></div></div>')''')
    single = await page.evaluate(FIELD_UI_JS, {'key': 'single', 'label': 'Size'})
    assert single['kind'] == 'custom' and single['label'] == '26"'


@pytest.mark.asyncio
async def test_b2sign_duplicate_display_labels_are_explicitly_ambiguous(manager):
    m, pool = manager
    pool.html['/pole-banner-set'] = B2_HTML.replace("['No Windslits','Standard Windslits']", "['No Windslits','No Windslits']")
    row = await completed(m, 'https://www.b2sign.com/pole-banner-set')
    assert row['status'] == 'attention'
    assert 'custom_dropdown_labels_ambiguous' in row['warnings']
    assert await m._pages[row['id']].evaluate('window.selectionChanges') == 0


@pytest.mark.asyncio
async def test_no_b2sign_panel_does_not_invent_product_fields(manager):
    m, pool = manager
    pool.html['/nothing'] = '<h1 id="item-view-title">Pole Banner Set</h1><button role="combobox">Other control</button>'
    origin, hosts, _ = await m._validated_target('https://www.b2sign.com/nothing')
    page = await m._new_page('fixture', origin, hosts)
    await page.goto('https://www.b2sign.com/nothing')
    snapshot = await inspect_b2sign(page, expand=True)
    assert snapshot['root_found'] is False
    assert snapshot['fields'] == []


@pytest.mark.parametrize('unknown', [
    '<div class="_attribute_fixture" id="attr-new-finish"><label>Finish</label><div class="_component_fixture _static_fixture">Brushed Aluminum</div></div>',
    '<div class="_attribute_fixture" id="attr-hanger"><label>Hanger</label><div class="_component_fixture _button_fixture"><div class="_item_fixture _active_fixture">Top Only</div><div class="_item_fixture">Top and Bottom</div></div></div>',
    '<div class="_attribute_fixture" id="attr-unlabelled"><div role="slider" aria-valuenow="10">Unreviewed</div></div>',
])
@pytest.mark.asyncio
async def test_unhandled_visible_attribute_requires_attention_without_inventing_fields(manager, unknown):
    m, pool = manager
    pool.html['/snap-poster-hanger'] = B2_HTML.replace('<div class="_attributes_fixture">', '<div class="_attributes_fixture">' + unknown)
    row = await completed(m, 'https://www.b2sign.com/snap-poster-hanger')
    assert row['status'] == 'attention'
    assert 'unsupported_visible_attribute' in row['warnings']
    assert not {'Finish', 'Hanger', 'Unreviewed'} & {field['label'] for field in row['fields']}
    page = m._pages[row['id']]
    assert await page.evaluate('window.selectionChanges') == 0
    snapshot = await inspect_b2sign(page, expand=False)
    assert 'unsupported_visible_attribute' in snapshot['warnings']  # Quote pilots see the same gap.
    assert next(field for field in row['fields'] if field['label'] == 'Material')['selected_values'] == ['18oz. Blockout Vinyl Banner']


@pytest.mark.asyncio
async def test_hidden_or_outside_quote_attributes_are_not_product_coverage_gaps(manager):
    m, pool = manager
    hidden = '<div class="_attribute_fixture" hidden><label>Hidden unrelated</label><button>Unknown</button></div>'
    outside = '<div class="_attribute_fixture"><label>Footer selector</label><button>Unknown</button></div>'
    pool.html['/pole-banner-set'] = B2_HTML.replace('<div class="_attributes_fixture">', '<div class="_attributes_fixture">' + hidden).replace('</body>', outside + '</body>')
    row = await completed(m, 'https://www.b2sign.com/pole-banner-set')
    assert row['status'] == 'ready'
    assert 'unsupported_visible_attribute' not in row['warnings']


@pytest.mark.asyncio
async def test_supported_native_attribute_wrapper_is_not_reported_as_unhandled(manager):
    m, pool = manager
    native = '<div class="_attribute_fixture" id="attr-emailproof"><label for="extra-proof"><input id="extra-proof" name="emailProof" type="checkbox" value="yes">Email Proof</label></div>'
    # Replace the original standalone proof with the same native control in a wrapper.
    pool.html['/pole-banner-set'] = B2_HTML.replace('<label class="MuiCheckbox-root"><input type="checkbox" value="">Email Proof</label>', native)
    row = await completed(m, 'https://www.b2sign.com/pole-banner-set')
    assert row['status'] == 'ready'
    assert 'unsupported_visible_attribute' not in row['warnings']
    proof = next(field for field in row['fields'] if field['label'] == 'Email Proof')
    assert proof['source_name'] == 'emailProof' and proof['selected_values'] == []
