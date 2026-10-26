"""Exact native wire dependency reuse; no source, browser, config or store."""
from copy import deepcopy
import importlib.util,json,subprocess,shutil
from pathlib import Path
import pytest
import printograph_price_dependency_support as s
from crawler import printograph_modern_browser as m
s.m=m
PAIRS=[('https://www.printograph.com/products/cardboards/order','10016'),('https://www.printograph.com/products/acrylic-boards/order','10019')]
def raw(pair=PAIRS[0],color='2'):
 v=s.raw();u,t=pair;v['source_url']=u;v['product_type']=t;g=v['generic_price_context'];g['properties']['printProductType']=t;g['turnaround']['printProduct']['productType']=t
 v['selection_context']['specs']['color']=color
 # Exact native color2/3 -> printProduct.color3 and hidSideId2 mapping.
 g['properties']['other'].update(color='3',sideId='2');g['turnaround']['printProduct']['color']='3';g['turnaround']['other']['hidSideId']='2'
 return v
def controls(v):
 q=v['selection_context']['quantity'];return [{'key':'quantity','kind':'select','value':q,'choices':[{'value':q,'disabled':False,'placeholder':False}]},{'key':'color','kind':'select','value':v['selection_context']['specs']['color']}]
def verify(g,v):return g.verify(v,{'value':v['selection_context']['production'],'selected_label':'Regular 3–4 Business Days'},controls(v),'13.43')
def gate(v):
 g=m.FreshQuoteGate();s.observe(g,v);s.observe(g,v,kind='production');return g
@pytest.mark.parametrize('pair',PAIRS)
def test_same_native_POST_reuses_base_across_source_mapped_color_alias_with_fresh_production(pair):
 a=raw(pair,'2');g=gate(a);origin=deepcopy(next(iter(g.generic_prices.values()))['origin_context']);b=raw(pair,'3')
 assert a['generic_price_context']!=b['generic_price_context'] and s.price_body(a)==s.price_body(b)
 assert m._generic_price_cache_identity(a)==m._generic_price_cache_identity(b)
 with pytest.raises(m.ModernHeld,match='production_evidence_missing'):verify(g,b)
 s.observe(g,b,kind='production');out=verify(g,b)
 assert out['price_table_reused_across_selection'] is True and len(out['base_price_dependency_sha256'])==64 and len(out['origin_context_sha256'])==64
 assert out['price_evidence_id']=='price1' and out['native_computed_subtotal_matches_display'] and not out['component_arithmetic_verified'] and not out['source_data_finalized']
 assert len(g.generic_prices)==1 and next(iter(g.generic_prices.values()))['origin_context']==origin
@pytest.mark.parametrize('pair',PAIRS)
def test_metadata_label_only_change_reuses_price_but_not_production(pair):
 a=raw(pair);g=gate(a);b=deepcopy(a);b['generic_price_context']['turnaround']['other']['Material']='A source-offered replacement label'
 assert s.price_body(a)==s.price_body(b)
 with pytest.raises(m.ModernHeld,match='production_evidence_missing'):verify(g,b)
 s.observe(g,b,kind='production');assert verify(g,b)['price_evidence_id']=='price1'
@pytest.mark.parametrize('change',['width','height','size','material','range','production','process','source','type','policy'])
def test_any_price_request_or_native_process_scope_dependency_change_does_not_reuse(change):
 a=raw();g=gate(a);b=deepcopy(a);p=b['generic_price_context']['properties']
 if change in {'width','height'}:b['generic_price_context']['dimensions'][change]='48';p['other'][change+'Id']='48'
 if change=='size':b['selection_context']['specs']['size']='12';p['size']='12'
 if change=='material':p['other']['materialId']='9'
 if change=='range':b['generic_price_context']['quantity_choices']=['1','2','300'];p['quantity']='1-300'
 if change=='production':b['selection_context']['production']='3';b['base_context']['production']='3';b['generic_price_context']['binding']['production']='3';p['turnaround']='3'
 if change=='process':b['model_services']['proofType']='101'
 if change=='source':b['source_url']=PAIRS[1][0]
 if change=='type':b['product_type']='10019';p['printProductType']='10019'
 if change=='policy':assert m._generic_price_cache_identity(a,None)!=m._generic_price_cache_identity(a,m.PDF_SERVICE_POLICY);return
 assert m._generic_price_cache_identity(a)!=m._generic_price_cache_identity(b)
 with pytest.raises(m.ModernHeld):verify(g,b)
def test_unknown_family_keeps_exact_old_full_context_cache_key():
 a=s.raw();g=gate(a);assert list(g.generic_prices)==[m._key(m._generic_context(a))] and 'origin_context' not in next(iter(g.generic_prices.values()))
 b=deepcopy(a);b['generic_price_context']['turnaround']['other']['Material']='Changed'
 s.observe(g,b,kind='production')
 with pytest.raises(m.ModernHeld,match='price_evidence_missing'):verify(g,b)
@pytest.mark.parametrize('change',['service','disabled','stale_display','stale_controller','stale_base','production','private_query','after_drift','old_page','old_epoch'])
def test_price_reuse_preserves_all_existing_freshness_service_and_native_guards(change):
 a=raw();g=gate(a);b=raw(color='3');s.observe(g,b,kind='production')
 if change=='service':b['model_services']['manualProofType']='1'
 if change=='disabled':b['groups'][0]['choices'][0]['disabled']=True
 if change=='stale_display':
  with pytest.raises(m.ModernHeld,match='subtotal_unproven'):g.verify(b,{'value':'1','selected_label':'Regular 3–4 Business Days'},controls(b),'13.44')
  return
 if change=='stale_controller':b['native_total']['controller_total']='13.44'
 if change=='stale_base':b['native_total']['applied_product_price']='13.44'
 if change=='production':b['selection_context']['production']='3';b['base_context']['production']='3';b['generic_price_context']['binding']['production']='3';b['generic_price_context']['properties']['turnaround']='3'
 if change=='private_query':s.observe(g,b,url=s.PRICE_URL+'PRIVATE')
 if change=='after_drift':s.observe(g,b,after=a)
 if change=='old_page':s.observe(g,b,generation=2)
 if change=='old_epoch':s.observe(g,b,epoch=8)
 with pytest.raises(m.ModernHeld):verify(g,b)
def test_new_owned_page_gate_has_no_base_or_production_from_old_page():
 a=raw();g=gate(a);verify(g,a)
 with pytest.raises(m.ModernHeld,match='price_evidence_missing'):verify(m.FreshQuoteGate(),a)
def test_origin_context_is_detached_and_new_response_price_change_is_not_hidden():
 a=raw();g=gate(a);origin=deepcopy(next(iter(g.generic_prices.values()))['origin_context']);a['generic_price_context']['turnaround']['other']['Material']='Modified caller'
 assert next(iter(g.generic_prices.values()))['origin_context']==origin
 b=raw(color='3');s.observe(g,b,data={'items':[{'quantity':2,'productPrice':15.43,'available':True}]});s.observe(g,b,kind='production')
 with pytest.raises(m.ModernHeld,match='subtotal_unproven'):verify(g,b)
def test_full_request_before_after_checks_are_still_required_before_cache_admission():
 a=raw();b=deepcopy(a);b['generic_price_context']['turnaround']['other']['Material']='Changed during response';g=m.FreshQuoteGate();s.observe(g,a,after=b)
 assert g.invalid=='modern_foreign_or_changed_response' and not g.generic_prices
def test_false_reference_availability_guard_is_not_removed_by_dependency_reuse():
 a=raw();g=m.FreshQuoteGate();s.observe(g,a,data={'items':[{'quantity':2,'productPrice':13.43,'available':False}]});b=raw(color='3');s.observe(g,b,kind='production')
 assert verify(g,b)['stock_availability_verified'] is False
 with pytest.raises(m.ModernHeld,match='quantity_unavailable'):g.verify(b,{'value':'1','selected_label':'Regular 3–4 Business Days'},[], '13.43')

def test_audit_distinguishes_new_capture_and_quantity_only_reuse_without_exposing_origin_values():
 a=raw();g=m.FreshQuoteGate();s.observe(g,a,data={'items':[{'quantity':2,'productPrice':13.43,'available':True},{'quantity':10,'productPrice':13.43,'available':True}]});s.observe(g,a,kind='production');fresh=verify(g,a)
 assert fresh['price_table_reused_across_selection'] is False
 b=deepcopy(a);b['selection_context']['quantity']='10';s.observe(g,b,kind='production');reused=verify(g,b)
 assert reused['price_table_reused_across_selection'] is True and reused['origin_context_sha256']==fresh['origin_context_sha256'] and reused['base_price_dependency_sha256']==fresh['base_price_dependency_sha256']
 assert 'origin_context' not in reused and 'selection_context' not in reused

def test_missing_origin_audit_does_not_silently_claim_a_reusable_scoped_table():
 a=raw();g=gate(a);next(iter(g.generic_prices.values())).pop('origin_context')
 with pytest.raises(m.ModernHeld,match='price_evidence_missing'):verify(g,a)
