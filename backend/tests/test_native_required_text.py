"""Saved native fragments + Node metadata harness, no browser or supplier I/O."""
import ast
import asyncio
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from crawler.adapters import browser as b
from crawler.browser_progress import CoverageNotes
from crawler.product_diagnosis import DIAGNOSIS_JS, normalize_snapshot as diagnosis_snapshot
from crawler.diagnosis_policy import normalize_selection, SelectionPolicyError
from crawler.diagnosis_run import compile_browser_policy, _binding

HERE=Path(__file__).parent/'fixtures/native_required_text'

@pytest.fixture(scope='module')
def dom_probe():
    node=os.environ.get('NODE_BINARY') or shutil.which('node')
    if not node:pytest.skip('Node is required for the no-browser metadata harness')
    result=subprocess.run([node,str(HERE/'probe.js')],input=json.dumps({'snapshot':b.SNAPSHOT_JS,'diagnosis':DIAGNOSIS_JS}),text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)

def test_exact_saved_fragments_preserve_native_identity_and_required_dependency():
    class Tags(HTMLParser):
        def __init__(self):super().__init__();self.tags=[]
        def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))
    data=json.loads((HERE/'controls.json').read_bytes())
    for name in ('no_match_control','yes_match_control'):
        parsed=Tags();parsed.feed(data[name]);select=next(a for t,a in parsed.tags if t=='select')
        assert select['data-id']=='custcol_pms_match' and not select.get('id') and not select.get('name')
        assert [a['value'] for t,a in parsed.tags if t=='option']==['F','T']
    parsed=Tags();parsed.feed(data['required_text_control']);field=next(a for t,a in parsed.tags if t=='input')
    assert field['id']==field['data-id']=='custcol_pms_number' and field['type']=='text' and 'required' in field and not field.get('name')

def test_literal_data_id_selector_parent_label_and_named_control_compatibility(dom_probe):
    assert dom_probe['fallback']=={'key':'custcol_pms_match','label':'Use PMS Match','selector':'select[data-id="custcol_pms_match"]'}
    assert dom_probe['standard']=={'key':'size','label':'size','selector':'#size_dynamic123'}
    assert dom_probe['named']=={'key':'paper','label':'paper'}
    assert dom_probe['duplicate_selector']!='select[data-id="custcol_pms_match"]'
    assert dom_probe['diagnosis_labels']=={'select':'Use PMS Match','required':'PMS Number/s'}

def test_required_text_metadata_never_reads_a_text_value_even_when_filled(dom_probe):
    assert dom_probe['required'][0]['raw_key']=='custcol_pms_number'
    assert dom_probe['required'][0]['label']=='PMS Number/s'
    assert dom_probe['required'][0]['value_missing'] is True
    assert dom_probe['filled'][0]['value_missing'] is False
    assert dom_probe['readonly'][0]['value_missing'] is False
    for field in [*dom_probe['required'],*dom_probe['filled']]:assert 'value' not in field

def test_woo_review_contact_chat_search_and_hidden_inputs_are_not_product_text(dom_probe):
    assert dom_probe['external']==[[],[],[],[],[],[],[]]
    assert dom_probe['excluded']==dom_probe['hidden']==dom_probe['optional']==[]
    assert dom_probe['width']==[]  # Existing numeric dimension handling remains separate.
    assert all(raw['fields']==[] for raw in dom_probe['diagnosis']['external'])

def test_legitimate_product_message_and_name_still_hold(dom_probe):
    assert dom_probe['message'][0]['raw_key']=='custom_message'
    assert dom_probe['name'][0]['raw_key']=='custom_name'
    for key in ('message','name'):
        raw=dom_probe['diagnosis'][key]
        assert len(raw['fields'])==1 and raw['fields'][0]['kind']=='required_text'

def ready(raw):
    value=diagnosis_snapshot(raw);value.update(status='ready',revision=1,url='https://fixture.example/product');return value

def test_no_policy_compiles_literal_source_data_id(dom_probe):
    diagnosis=ready(dom_probe['diagnosis']['no']);field=diagnosis['fields'][0]
    assert field['source_data_id']=='custcol_pms_match' and field['source_name']==field['source_id']==''
    diagnosis['selection']=normalize_selection(diagnosis,1,[{'key':field['key'],'include':False,'fixed_values':['F']}])
    result=compile_browser_policy(diagnosis,[diagnosis['url']],'browser',{})
    assert result['filters']=={'custcolpmsmatch':'F'}
    assert _binding({'label':'Old','kind':'select','source_id':'size_dynamic123','source_name':'size','source_data_id':'ignored'},required=True)['browser_key']=='size'

def test_existing_named_fields_and_policy_preserved_with_additive_document_metadata():
    saved=json.loads((HERE/'legacy-named.json').read_bytes())
    current=diagnosis_snapshot(saved['raw'])
    assert current==saved['expected_snapshot']
    assert {key:current['content'][key] for key in saved['legacy_content_fields']}==saved['legacy_content_fields']
    assert {key:current['content'][key] for key in saved['document_metadata_additions']}==saved['document_metadata_additions']
    assert set(current['content'])==set(saved['legacy_content_fields'])|set(saved['document_metadata_additions'])
    current.update(status='ready',revision=1,url='https://fixture.example/product')
    assert normalize_selection(current,1,saved['selection'])==saved['expected_policy']
    assert 'source_data_id' not in current['fields'][0]

def test_yes_required_text_cannot_compile_or_be_omitted(dom_probe):
    diagnosis=ready(dom_probe['diagnosis']['yes']);required=next(f for f in diagnosis['fields'] if f['kind']=='required_text')
    assert required['label']=='PMS Number/s' and required['required'] and required['configurable']
    assert required['choices']==required['selected_values']==[]
    assert 'required_free_text_control:PMS Number/s' in diagnosis['warnings']
    choices=[{'key':f['key'],'include':True,'fixed_values':[]} for f in diagnosis['fields']]
    diagnosis['selection']=normalize_selection(diagnosis,1,choices)
    with pytest.raises(ValueError,match='Required product text cannot be guessed'):compile_browser_policy(diagnosis,[diagnosis['url']],'browser',{})
    next(c for c in choices if c['key']==required['key'])['include']=False
    with pytest.raises(SelectionPolicyError):normalize_selection(diagnosis,1,choices)

def test_required_text_normalization_discards_any_accidental_raw_values():
    raw={'fields':[{'kind':'required_text','label':'PMS Number/s','source_key':'custcol_pms_number','source_id':'custcol_pms_number','required':True,'choices':[{'value':'PRIVATE_TEXT','label':'PRIVATE_TEXT'}],'selected_values':['PRIVATE_TEXT']}]}
    result=diagnosis_snapshot(raw)
    assert 'PRIVATE_TEXT' not in json.dumps(result)

def snapshot(required=False,quantity='1',basis='source_display_price'):
    return {'title':'Synthetic fixture','controls':[{'key':'quantity','kind':'number','value':quantity,'selected_label':quantity}],
        'required_text_fields':[{'raw_key':'custcol_pms_number','label':'PMS Number/s','value_missing':True}] if required else [],
        'price':'$10.00','basis':basis,'images':[],'currency':'USD','service_context':[],'service_context_observed':False}

@pytest.mark.parametrize('value_missing',[True,False])
def test_make_record_blocks_required_text_before_reusing_unchanged_price(value_missing):
    state=snapshot(True);state['required_text_fields'][0]['value_missing']=value_missing
    with pytest.raises(b.AdapterError) as error:b.make_record('https://fixture.example/product',state,['fixture'],set(),{})
    assert error.value.code=='required_product_text'

def leaf(initial,final=None):
    source=Path(b.__file__);tree=ast.parse(source.read_text());names={'hold_required_text','walk_live'}
    functions=[n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name in names]
    counters={'rows':0,'invalid_branches':0};events=[];calls=[];partial=CoverageNotes()
    async def emit(kind,payload):events.append((kind,payload))
    async def record(state,prefix):calls.append(state);return b.make_record('https://fixture.example/product',state,['fixture'],partial,{})
    async def counted(row):counters['rows']+=1;return row
    env={'limit':None,'restore':AsyncMock(return_value=initial),'settle':AsyncMock(return_value=final or initial),'counters':counters,
         'partial':partial,'bulk_requested':False,'filters':{},'modern_requested':False,'emit':emit,'record_quote':record,'counted_row':counted,
         'field_key':b.field_key,'public_control_text':b.public_control_text,'AdapterError':b.AdapterError,'unit_price_or_subtotal_unproven':b.unit_price_or_subtotal_unproven}
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),env)
    async def run():return [row async for row in env['walk_live']({'quantity':initial['controls'][0]['value']})]
    return asyncio.run(run()),counters,events,calls,partial

def test_actual_leaf_no_yields_once_yes_yields_zero_with_unchanged_display():
    rows,counters,events,calls,partial=leaf(snapshot())
    assert len(rows)==len(calls)==1 and rows[0]['subtotal']=='10.00' and rows[0]['verification_status']=='not_selected'
    rows,counters,events,calls,partial=leaf(snapshot(True))
    assert rows==calls==[] and counters['invalid_branches']==1
    assert 'required_free_text_control:custcolpmsnumber' in partial
    assert events[0][0]=='required_product_text' and events[0][1]['published_rows']==0

def test_actual_leaf_late_required_text_after_settle_is_also_held():
    rows,_,events,calls,_=leaf(snapshot(),snapshot(True))
    assert rows==calls==[] and events[0][0]=='required_product_text'

def test_generic_quantity_two_fallback_is_not_misrecorded_as_a_line_total():
    state=snapshot(quantity='2')
    with pytest.raises(b.AdapterError) as error:b.make_record('https://fixture.example/product',state,['fixture'],set(),{})
    assert error.value.code=='unit_price_or_subtotal_not_established'
    rows,_,events,calls,partial=leaf(state)
    assert rows==calls==[] and events[0][0]=='quote_basis_held'
    assert 'unit_price_or_subtotal_not_established' in partial

@pytest.mark.parametrize('basis',['explicit_source_selector','Subtotal'])
def test_existing_explicit_line_total_basis_stays_allowed(basis):
    state=snapshot(quantity='2',basis=basis);state['price']='$20.00'
    row=b.make_record('https://fixture.example/product',state,['fixture'],set(),{})
    assert row['subtotal']=='20.00' and row['unit_price']=='10.00000000'

def test_make_record_and_bulk_entry_have_the_same_prepublication_guards():
    source=Path(b.__file__).read_text();tree=ast.parse(source)
    quote=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='record_quote')
    assert 'required_text_fields' in ast.unparse(quote.body[0])
    assert 'unit_price_or_subtotal_unproven' in ast.unparse(quote.body[1])
    bulk=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='try_bulk_quantities')
    text=ast.unparse(bulk)
    assert 'required_text_fields' in text and 'unit_price_or_subtotal_unproven(state)' in text
