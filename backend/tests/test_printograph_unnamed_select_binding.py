"""Current unnamed formSelect controls: pinned props, localhost TLS only.

No supplier account/profile, real host, or remote asset is used by these tests.
"""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_printograph_passive_manager import candidate_manager, browser_page, opened, native_field, d, fixtures
import test_printograph_passive_manager as integration

# Cached literal snippets from the exact source bundle; never downloaded here.
# Origin: https://static.printograph.com/tl/cli-app/dist/production/js/orderForm.js
# Observed version: 0109_20261007_202205-101. Full bundle SHA256 is the pinned
# SPECIAL_SHAPES_SOURCE_CONTRACT order_form_sha256; byte offsets/hashes are below.
IDS = {'shape':'shape_oi5z0rkl6','size':'size_wa2bt3xvi','orientation':'orientation_syn1','paper':'paper_tpvnariw5','color':'color_vfftmdhcm','quantity':'quantity_syn1'}
PINNED_TEMPLATE_EXCERPTS = {'compiled_native_select_props': {'role': 'compiled_native_select_props', 'byte_offset': 1403661, 'bytes': 588, 'sha256': '6d9b27c94452e4f7c19cfee2954fba1599e4d8b653d47d206f2cbeb314ee2413', 'text': '{key:11,ref:"currentInput","onUpdate:modelValue":t[0]||(t[0]=e=>a.jsonObj.userSelectedOptions[i.spec]=e),id:i.spec+"_"+a.uniqueId,class:"custom-select regularShadow",onChange:t[1]||(t[1]=function(){return n.specChange&&n.specChange(...arguments)}),disabled:n.disabledField(i.spec),style:(0,ee.Tr)(""!=a.jsonObj.userSelectedOptions[i.spec]?"flagAccessories"!=i.spec?"border-color:#8ec647":"border-color:#8ec647;text-overflow: ellipsis;padding-right: 6.7rem;":"yes"==a.schema[i.spec].required&&""==a.jsonObj.userSelectedOptions[i.spec]&&a.jsonObj.others.validate?"border-color:#EB0026":"")}'}, 'declared_component_spec_props': {'role': 'declared_component_spec_props', 'byte_offset': 1412364, 'bytes': 133, 'sha256': '50c8f75d34368c2a378e33fa81adcfd4907fbca8a37623e78d6e1ff3a52af393', 'text': 'name:"formSelect",components:{validateComponent:Cp},props:{spec:String,rid:{type:Number,default:0},optStyle:{type:Number,default:0}},'}, 'source_unique_id_generator': {'role': 'source_unique_id_generator', 'byte_offset': 1413559, 'bytes': 48, 'sha256': 'f95ea7f8fc4aa9666e1434aea7bab9f4619df293ecc9daacabbc839a0bca0a9b', 'text': 'uniqueId:Math.random().toString(36).substr(2,9)}'}}


def pinned_props():
    assert d.SPECIAL_SHAPES_SOURCE_CONTRACT['order_form_sha256'] == '0aef40e4995a0ecfbcae99f8ad4dd900fdc2201147a37e0b5bbbf0a7fa5525f6'
    for entry in PINNED_TEMPLATE_EXCERPTS.values():
        body = entry['text'].encode()
        assert len(body) == entry['bytes']
        assert hashlib.sha256(body).hexdigest() == entry['sha256']
    props = PINNED_TEMPLATE_EXCERPTS['compiled_native_select_props']['text']
    assert 'e=>a.jsonObj.userSelectedOptions[i.spec]=e' in props
    assert 'id:i.spec+"_"+a.uniqueId' in props and 'name:' not in props
    assert PINNED_TEMPLATE_EXCERPTS['declared_component_spec_props']['text'].startswith('name:"formSelect",components:{validateComponent:Cp},props:{spec:String,rid:{type:Number,default:0},optStyle:{type:Number,default:0}}')
    assert PINNED_TEMPLATE_EXCERPTS['source_unique_id_generator']['text'] == 'uniqueId:Math.random().toString(36).substr(2,9)}'
    return props


def unnamed_html():
    html = integration.local_html_original()
    # Keep actual unnamed source IDs; labels deliberately cannot identify fields.
    names = ['shape', 'size', 'orientation', 'paper', 'color', 'quantity']
    for name in names:
        source_id = IDS[name]
        html = html.replace('name="'+name+'" id="'+name+'"', 'id="'+source_id+'"')
        html = html.replace('for="'+name+'">'+name.title(), 'for="'+source_id+'">Unrelated visible label')
    html = html.replace("document.querySelector('#quantity')", "document.querySelector('#quantity_syn1')")
    html = html.replace("selected[e.name]=e.value;", "const spec=window.selectOwners.get(e).props.spec; window.selectOwners.get(e).model(e.value);")
    html = html.replace("if(e.name==='color'", "if(spec==='color'")
    injected = '''
      const root=document.querySelector('#order-form'), propsSource=__PINNED_PROPS__;
      window.selectOwners=new Map();
      const children=[...root.querySelectorAll('select')].map((el,index)=>{
        const spec=__NAMES__[index], a={jsonObj:c.jsonObj,uniqueId:el.id.slice(spec.length+1),schema:{[spec]:{required:'yes'}}};
        const p=Function('t','a','i','n','ee','return ('+propsSource+')')([],a,{spec},{disabledField:()=>false},{Tr:x=>x});
        // The first source ID is intentionally the exact observed generated ID.
        if(p.id!==el.id)throw Error('pinned ID mismatch');
        const owner={type:{name:'formSelect'},props:{spec},refs:{currentInput:el},data:a,model:p['onUpdate:modelValue']};
        window.selectOwners.set(el,owner);
        return {type:'select',el,ref:{i:owner,r:'currentInput'},props:p,children:[]};
      });
      window.bindingRoot=root._vnode;
      root._vnode.component.subTree={type:'div',children};
      window.bindingNodes=children;
    '''.replace('__PINNED_PROPS__', json.dumps(pinned_props())).replace('__NAMES__', json.dumps(names))
    return html.replace('let table=null;', injected+'let table=null;')


integration.local_html_original = integration.local_html


@pytest.fixture(autouse=True)
def actual_unnamed_template(monkeypatch):
    monkeypatch.setattr(integration, 'local_html', unnamed_html)


def unnamed_field(record, spec):
    source_id = IDS[spec]
    return next(f for f in record['fields'] if f.get('source_id') == source_id)


async def binding(value, record, name='shape', target='15'):
    field = unnamed_field(record,name)
    return await value._pages[record['id']].evaluate(d.PASSIVE_UNNAMED_SELECT_BINDING_JS,
        {'source_contract':d.SPECIAL_SHAPES_SOURCE_CONTRACT,'source_id':field['source_id'],
         'selected_values':field['selected_values'],'target_value':target})


@pytest.mark.asyncio
async def test_pinned_real_unnamed_select_chain_and_no_invented_name(candidate_manager):
    value,server=candidate_manager
    record=await opened(value,observe_printograph_prices=True)
    assert record['status']=='ready'
    before=list(server.seen)
    shape=unnamed_field(record,'shape')
    assert shape['source_name']=='' and shape['source_id']=='shape_oi5z0rkl6'
    assert shape['label']=='Unrelated visible label'
    assert await binding(value,record)=={'observed':True,'field':'shape','binding_kind':'pinned_vue_form_select_spec','retained_value':''}
    assert server.seen==before
    sequence=[('shape','15'),('size','101'),('orientation','2'),('paper','1'),('color','3'),('quantity','100')]
    for name,target in sequence:
        field=unnamed_field(record,name)
        record=await value.select_observed_choice(record['id'],record['revision'],field['key'],target)
        assert unnamed_field(record,name)['selected_values']==[target]
        assert unnamed_field(record,name)['source_name']==''
    evidence=await value.read_passive_price_evidence(record['id'],record['revision'])
    assert evidence['status']=='observed',evidence
    assert [e.field for e in value._passive_journals[record['id']]._epochs.values()]==[name for name,_ in sequence]
    assert len(server.seen)==len(before)+2
    stats=await value._pages[record['id']].evaluate('stats')
    assert stats['coupon']==0 and stats['httpMethod']==0 and stats['nativeFetch']==2
    assert 'Unrelated' not in json.dumps(evidence) and 'PRIVATE' not in json.dumps(evidence)
    assert value._choice_limiter.acquire.await_count==6
    assert evidence['source_data_finalized'] is False and evidence['checkout_verified'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    "delete document.querySelector('#order-form')._vnode",
    "bindingNodes[0].ref.i.props.spec='notReviewed'",
    "bindingNodes[0].ref.i.props.spec='size'",
    "bindingNodes[0].ref.i.data.uniqueId='wrong'",
    "delete bindingNodes[0].ref.i.data.uniqueId",
    "bindingNodes[0].ref.i.type.name='otherComponent'",
    "bindingNodes[0].ref.r='notCurrentInput'",
    "bindingNodes[0].ref.i.refs.currentInput=document.querySelector('#size_wa2bt3xvi')",
    "bindingNodes[0].props.id='notTheControl'",
    "delete bindingNodes[0].props['onUpdate:modelValue']",
    "bindingNodes[0].type='input'",
    "document.querySelector('#shape_oi5z0rkl6').insertAdjacentHTML('beforeend','<option value=\\\"15\\\">Duplicate</option>')",
    "document.querySelector('#shape_oi5z0rkl6').insertAdjacentHTML('afterbegin','<option disabled value=\\\"15\\\">Duplicate</option>')",
    "document.querySelector('#shape_oi5z0rkl6').insertAdjacentHTML('afterbegin','<optgroup disabled><option value=\\\"15\\\">Duplicate</option></optgroup>')",
    "bindingRoot.component.subTree.children.push({...bindingNodes[0]})",
    "document.querySelector('#shape_oi5z0rkl6').name='shape'",
    "document.querySelector('#shape_oi5z0rkl6').disabled=true",
    "document.querySelector('#shape_oi5z0rkl6').multiple=true",
    "document.querySelector('#shape_oi5z0rkl6').value='15'",
    "document.querySelector('#shape_oi5z0rkl6 option[value=\\\"15\\\"]').disabled=true",
    "document.querySelector('#shape_oi5z0rkl6').insertAdjacentHTML('afterend','<select id=\\\"shape_oi5z0rkl6\\\"><option></option></select>')",
    "document.querySelector('#order-form').dataset.printproducttype='10019'",
    "document.querySelector('#order-form').dataset.currentstoretype='retail'",
    "Object.defineProperty(bindingNodes[0].ref.i.props,'spec',{get(){throw Error('PRIVATE-accessor')}})",
    "Object.defineProperty(bindingNodes[0].ref.i.refs,'currentInput',{get(){throw Error('PRIVATE-accessor')}})",
    "Object.defineProperty(bindingRoot.component,'subTree',{get(){throw Error('PRIVATE-accessor')}})",
    "bindingRoot.component.subTree.children=Array.from({length:2049},()=>({type:'div'}))",
])
async def test_binding_fail_closed_before_epoch_and_action(candidate_manager,mutation):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];before=list(server.seen)
    await page.evaluate('() => {'+mutation+'}')
    result=await binding(value,record)
    assert result=={'observed':False,'reason':'passive_native_model_binding_unproven'}
    assert server.seen==before


@pytest.mark.asyncio
async def test_unmapped_unnamed_rejects_before_native_selection_and_epoch(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];before=list(server.seen)
    await page.evaluate("bindingNodes[0].ref.i.props.spec='wrong'")
    field=unnamed_field(record,'shape')
    with pytest.raises(ValueError,match='passive_native_choice_failed'):
        await value.select_observed_choice(record['id'],record['revision'],field['key'],'15')
    assert await page.locator('#shape_oi5z0rkl6').input_value()==''
    assert value._records[record['id']]['revision']==record['revision']
    assert not value._passive_journals[record['id']]._epochs
    assert value._passive_journals[record['id']]._invalid=='adapter_error'
    assert server.seen==before


@pytest.mark.asyncio
async def test_binding_semantic_conflict_with_controller_rejected(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];before=list(server.seen)
    await page.evaluate("bindingNodes[0].ref.i.props.spec='orientation'")
    field=unnamed_field(record,'shape')
    with pytest.raises(ValueError,match='passive_native_choice_failed'):
        await value.select_observed_choice(record['id'],record['revision'],field['key'],'15')
    assert await page.locator('#shape_oi5z0rkl6').input_value()==''
    assert not value._passive_journals[record['id']]._epochs
    assert server.seen==before


@pytest.mark.asyncio
async def test_wrong_contract_and_url_and_private_accessors_never_called(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];before=list(server.seen)
    await page.evaluate('''()=>{
      window.privateReads=0;
      for(const obj of [bindingNodes[0].ref.i,bindingNodes[0].props])
        Object.defineProperty(obj,'privateAccount',{get(){privateReads++;throw Error('PRIVATE')}});
    }''')
    assert (await binding(value,record))['observed'] is True
    assert await page.evaluate('privateReads')==0
    field=unnamed_field(record,'shape')
    args={'source_contract':dict(d.SPECIAL_SHAPES_SOURCE_CONTRACT),'source_id':field['source_id'],'selected_values':[''],'target_value':'15'}
    args['source_contract']['common_sha256']='a'*64
    assert (await page.evaluate(d.PASSIVE_UNNAMED_SELECT_BINDING_JS,args))['observed'] is False
    await page.evaluate("history.pushState({},'','/products/catalogs/order')")
    args['source_contract']=dict(d.SPECIAL_SHAPES_SOURCE_CONTRACT)
    assert (await page.evaluate(d.PASSIVE_UNNAMED_SELECT_BINDING_JS,args))['observed'] is False
    assert server.seen==before
