"""Native rendered SuiteCommerce table harvest. No source work at import time.

Price bands are observations, never q1 multiplication or completed Woo quotes.
Only native finite choices/quantity are changed; required free text is held.
"""
from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import httpx
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit, unquote
from .source_collection_scope import resume_scope, scoped_pages, record_resume_scope
from .transport import RateLimiter, validate_url
from .pacing import retry_after_seconds
from .store import canonical, uid

PROFILE_ID = 'wsdisplay_native_v1'
ORIGIN = 'https://www.wsdisplay.com'
MAX_NODES = 8192
MAX_DEPTH = 16
MAX_RECORDS = 4096
MAX_SEEDS = 64
MAX_OBSERVATION_PREVIEW_PER_PRODUCT = 4
MAX_OBSERVATION_PREVIEW_BYTES = 8*1024*1024
MAX_OBSERVATION_OBJECT_BYTES = 2*1024*1024
MAX_SEED_BYTES = 2*1024*1024
SETTLE_SECONDS = 45
PUBLIC_SOURCE_BODY_TIMEOUT = 3
NATIVE_READ_TIMEOUT = 10
PUBLIC_SOURCE_BODY_LIMIT = 2*1024*1024
PUBLIC_SOURCE_BUNDLES = {
 "https://www.wsdisplay.com/webstore/javascript/shopping.js?t=1790279871637": {"sha256":"d1b3d6179ed82d1e1b5b3757e1a5b206410353838c1cbed72c221a890e28138a","bytes":1182673},
 "https://www.wsdisplay.com/webstore/extensions/shopping_4.js?t=1790279871637": {"sha256":"44695dda86bc68209ea71f7f8d1cffe251c8743bb301a8e01573c85c430c8a36","bytes":185432},
}
QUIET_SECONDS = .25
ACTION_SETTLE_SECONDS = .5
POLL_SECONDS = .15

class Held(ValueError):
    def __init__(self,reason,diagnostic=None):
        super().__init__(reason);self.diagnostic=diagnostic

def now(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def require(ok, reason):
    if not ok: raise Held(reason)

def page_url(value):
    require(type(value) is str and value == value.strip() and len(value) <= 500, 'invalid_source_url')
    try: p = urlsplit(value)
    except ValueError: raise Held('invalid_source_url') from None
    require(p.scheme == 'https' and p.hostname == 'www.wsdisplay.com' and p.port is None
            and not p.username and not p.password and not p.query and not p.fragment
            and re.fullmatch(r'/[A-Za-z0-9._~!$&()*+,;=:@%/\-]+', p.path)
            and '..' not in unquote(p.path), 'invalid_source_url')
    require(not re.search(r'(?:^|/)(?:cart|checkout|account|login|logout|admin|api|services)(?:/|$)',p.path,re.I), 'invalid_source_url')
    return value

def money(text):
    require(type(text) is str, 'price_unresolved')
    m = re.fullmatch(r'(?:Online Price:\s*)?\$([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(\.[0-9]{2})',text.strip())
    require(m is not None, 'price_unresolved')
    return Decimal(m[1].replace(',','')+m[2])

def bands_from_rows(rows):
    require(type(rows) is list and 1 <= len(rows) <= 100, 'band_table_unresolved')
    out=[]
    for row in rows:
        if row in (['Quantity','Price'],['Quantity','Unit Price']): continue
        require(type(row) is list and len(row)==2, 'band_table_unresolved')
        m=re.fullmatch(r'([1-9][0-9]*)\s*(?:-\s*([1-9][0-9]*)|(\+))',row[0].strip())
        require(m is not None, 'band_table_unresolved')
        lo=int(m[1]); hi=int(m[2]) if m[2] else None
        require(hi is None or hi>=lo, 'band_table_unresolved')
        price=money(row[1]);require(price>0,'price_unresolved')
        require(not out or out[-1]['maximum'] is not None and lo==out[-1]['maximum']+1, 'band_table_gap_or_overlap')
        out.append({'minimum':lo,'maximum':hi,'unit_cost':format(price,'.2f')})
    require(out and len(out)<=64, 'band_table_unresolved')
    return out

def normalize_snapshot(raw, url, account_marker):
    require(type(raw) is dict and raw.get('url')==url, 'source_not_ready')
    if raw.get('challenge') is True:raise Held('source_challenge')
    if raw.get('login_prompt') is True or raw.get('password_login_visible') is True:raise Held('source_access_required')
    require(raw.get('ready') is True, 'source_not_ready')
    require(raw.get('challenge') is False, 'source_challenge')
    require(raw.get('login_prompt') is False and raw.get('account_marker')==account_marker, 'source_account_marker_required')
    require(type(raw.get('fields')) is list and len(raw['fields'])<=32 and type(raw.get('required_text')) is list, 'source_controls_unresolved')
    fields=[];seen=set()
    for f in raw['fields']:
        require(type(f) is dict and type(f.get('key')) is str and f['key'] not in seen and f.get('kind') in {'select','radio'},'source_controls_unresolved');seen.add(f['key'])
        require(type(f.get('label')) is str and f['label'] and len(f['label'])<=300 and type(f.get('choices')) is list and len(f['choices'])<=500,'source_controls_unresolved')
        choices=f['choices'];require(len({x.get('value') for x in choices})==len(choices),'source_control_ambiguous')
        require(all(type(x.get('value')) is str and type(x.get('label')) is str and type(x.get('disabled')) is bool for x in choices),'source_controls_unresolved')
        fields.append(deepcopy(f))
    out=deepcopy(raw);out['fields']=fields
    return out

def readiness_diagnostic(raw,reason,pending,counts):
    """Closed booleans/counts/enums only; never a label,URL,account value or error text."""
    raw=raw if type(raw) is dict else {};r=raw.get('readiness',{});r=r if type(r)is dict else {}
    out={}
    for k in ('h1_visible_count','global_info_count','scoped_info_count','global_quantity_visible_count','scoped_quantity_count','fields_count','required_text_count'):
        v=r.get(k);out[k]=min(v,10000) if type(v)is int and v>=0 else None
    for k in ('root_found','native_busy','account_marker_present','expected_marker_match','price_login_prompt','password_login_visible'):
        out[k]=r.get(k) if type(r.get(k))is bool else None
    out['root_tag']=r.get('root_tag') if r.get('root_tag')in{'SECTION','DIV','MAIN','FORM','ARTICLE','BODY','HTML','NONE'}else'OTHER'
    out['document_state']=r.get('document_state') if r.get('document_state')in{'loading','interactive','complete'}else'unknown'
    out['last_reason']=reason if reason in {'ready','source_not_ready','source_challenge','source_account_marker_required','source_controls_unresolved','source_control_ambiguous','native_settle_timeout','source_access_required'}else'other'
    out['normalization_valid']=reason=='ready';out['pending_request_count']=min(len(pending),10000)
    out['pending_permit_count']=sum(r.get('token') is not None for r in pending.values());out['waiting_permit_count']=sum(r.get('token') is None for r in pending.values())
    ages=[max(0,time.monotonic()-r['started'])for r in pending.values()if type(r.get('started'))in{int,float}]
    out['oldest_pending_ms']=min(3600000,int(max(ages,default=0)*1000))
    out['network_counts']={k:min(v,1000000)for k,v in counts.items()if k in {'guarded_gets','blocked_mutations'}and type(v)is int and v>=0}
    return out


# Literal public endpoint names from the captured versioned vendor JS only.
# This is a logging allowlist, NEVER transport permission. Unknown paths hash-only.
PUBLIC_METADATA_PATHS=frozenset(['/api/cacheable/items', '/api/items', '/api/personalized/items', '/services/Accessories.Service.ss', '/services/Address.Service.ss', '/services/GoogleTagManager.Service.ss', '/services/GraphicFiles.Service.ss', '/services/GraphicsLibrary.Service.ss', '/services/LiveOrder.Line.Service.ss', '/services/LiveOrder.Service.ss', '/services/Location.Service.ss', '/services/Newsletter.Service.ss', '/services/OrderFiles.Service.ss', '/services/PageType.Service.ss', '/services/PaymentInstrumentACH.Service.ss', '/services/PaymentMethod.Service.ss', '/services/ProductList.Item.Service.ss', '/services/ProductList.Service.ss', '/services/ProductReviews.Service.ss', '/services/Profile.Service.ss', '/services/ResaleCertificate.Fields.Service.ss', '/services/ResaleCertificate.Service.ss', '/services/StoreLocator.Service.ss', '/services/WSDSyncApp.Service.ss'])

def blocked_request_metadata(method,url,resource_type):
    """Endpoint identity only. Never query, userinfo, headers or request body."""
    try:p=urlsplit(url);host=p.hostname or '';port=p.port
    except ValueError:return {'method':'OTHER','origin':None,'path':None,'path_sha256':None,'resource_type':'other'}
    origin=(p.scheme+'://'+host+((':'+str(port))if port is not None else ''))if p.scheme in {'http','https'}and re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host)else None
    path=p.path
    # Unknown paths may contain private IDs/names even when their syntax looks
    # public. Exact static-name allowlist only; hash fallback is always available.
    public=path in PUBLIC_METADATA_PATHS
    return {'method':method if method in {'POST','PUT','PATCH','DELETE','OPTIONS','GET','HEAD'}else'OTHER','origin':origin,'path':path if public else None,'path_sha256':hashlib.sha256(path.encode()).hexdigest(),'resource_type':resource_type if resource_type in {'document','xhr','fetch','script','stylesheet','image','font','media','websocket','other'}else'other'}

def observed_items_metadata(url,resource_type):
    p=urlsplit(url)
    if p.hostname!='www.wsdisplay.com'or p.path not in {'/api/items','/api/personalized/items'}or resource_type not in {'xhr','fetch'}:return None
    meta=blocked_request_metadata('GET',url,resource_type)
    # Inspect key names only; never decode/read a query value.
    names={unquote(part.split('=',1)[0])for part in p.query.split('&')if part}
    meta['query_keys']=sorted(k for k in names if re.fullmatch(r'[A-Za-z0-9_.\[\]-]{1,60}',k))[:32]
    meta['query_keys_truncated']=len(names)>len(meta['query_keys']);return meta

def native_observation(s,blocked,observed_reads=None):
    """Closed public DOM projection, privately persisted before price parsing.

    Credentials, account text, editable text values, arbitrary properties and
    source bodies have no projection path. This is NOT a validated quote.
    """
    def text(v,limit=500):return v[:limit]if type(v)is str else None
    def scalar(v):return v if type(v)in {str,int,bool}or v is None else None
    out={k:scalar(s.get(k))for k in ('url','ready','challenge','login_prompt','password_login_visible','quantity','quantity_min','quantity_max','sku','stock','out_of_stock','artwork_controls_present')}
    out['title']=text(s.get('title'),300)
    out['material_literal']=text(s.get('material_literal'),300)
    out['price_currency_codes']=[v for v in s.get('price_currency_codes',[])if type(v)is str and re.fullmatch('[A-Z]{3}',v)][:8]
    pi=s.get('product_inspection');pi={k:v for k,v in pi.items()if k!='catalogue'}if type(pi)is dict else None
    out['product_inspection']=deepcopy(pi)if type(pi)is dict and pi.get('schema')=='wsdisplay-native-pdp-inspection/v1'and pi.get('valid_quote')is False and len(canonical(pi).encode())<=65536 else None
    out['account_proof']={'expected_marker_match':bool(s.get('account_marker')),'marker_sha256':hashlib.sha256(s['account_marker'].encode()).hexdigest()if type(s.get('account_marker'))is str else None}
    out['fields']=[]
    for f in s.get('fields',[])[:32]:
        row={k:scalar(f.get(k))for k in ('key','kind','selected','disabled','metadata_only')};row['label']=text(f.get('label'),300)
        row['choices']=[{k:(text(c.get(k),300)if k=='label'else scalar(c.get(k)))for k in ('value','label','disabled','source_active','source_available')}for c in f.get('choices',[])[:500]];out['fields'].append(row)
    out['required_text']=[{'key':text(f.get('key'),100),'label':text(f.get('label'),300),'required':f.get('required')is True}for f in s.get('required_text',[])[:32]]
    out['production']={k:scalar(s.get('production',{}).get(k))for k in ('id','label','control_disabled')}
    out['artwork_resources']=[{'kind':r['kind'],'url':r['url']} for r in s.get('artwork_resources',[])[:16]
                              if type(r) is dict and r.get('kind') in {'template','specsheet','instructions'}
                              and type(r.get('url')) is str and len(r['url'])<=500]
    out['packing']={k:scalar(s.get('packing',{}).get(k))for k in ('item_weight_literal','shipping_dimension_literal','weight_is_shipping_gross_proven','order_quantity_parcels_proven')}
    out['unit_prices']=[text(v,100)for v in s.get('unit_prices',[])[:16]];out['totals']=[text(v,100)for v in s.get('totals',[])[:16]]
    def tables(v):return [[[text(cell,500)for cell in row[:12]]for row in table[:100]]for table in v[:12]]
    visible=s.get('visible_tables',[]);out['visible_tables']=tables(visible)if type(visible)is list else []
    out['table_projection_truncated']=type(visible)is list and (len(visible)>12 or any(len(t)>100 or any(len(r)>12 or any(type(c)is str and len(c)>500 for c in r)for r in t)for t in visible))
    tier=s.get('tier_rows');out['tier_rows']=tables([tier])[0]if type(tier)is list else None
    out['blocked_requests']=deepcopy(blocked[:32]);out['observed_item_requests']=deepcopy((observed_reads or [])[:32])
    return {'schema':'wsdisplay-native-dom-observation/v1','observed_at':now(),'snapshot':out,'valid_quote':False,'retail_ready':False,'source_data_finalized':False}

def scope_binding(s):
    selected={f['key']:f['selected'] for f in s['fields'] if f.get('selected') not in (None,'')}
    return {'source_url':s['url'],'source_sku':s.get('sku'),'selected_child':selected.get('custcol_matrix_order_detail_option',''),
            'warehouse_id':selected.get('custcol_webstore_locations',''),'choices':selected,'production_id':s.get('production',{}).get('id','')}

def receipt(s, witness=None):
    binding=scope_binding(s);require(type(s.get('sku')) is str and s['sku'] and not re.search('MTX',s['sku'],re.I),'selected_source_sku_unresolved')
    pending=[f for f in s['fields'] if not f.get('metadata_only') and any(not c['disabled'] and c['value'] for c in f['choices']) and not f.get('selected')]
    require(not pending,'source_choice_unselected')
    quantity=s.get('quantity');require(type(quantity) is int and quantity>=1,'source_quantity_unresolved')
    rows=s.get('tier_rows');bands=bands_from_rows(rows)
    unit_values={format(money(v),'.2f') for v in s.get('unit_prices',[])}
    total_values={format(money(v),'.2f') for v in s.get('totals',[])}
    require(len(unit_values)==len(total_values)==1,'current_price_ambiguous')
    unit=Decimal(next(iter(unit_values)));total=Decimal(next(iter(total_values)))
    band=next((x for x in bands if x['minimum']<=quantity and (x['maximum'] is None or quantity<=x['maximum'])),None)
    require(band is not None and Decimal(band['unit_cost'])==unit and (unit*quantity).quantize(Decimal('.01'))==total,'native_unit_line_total_mismatch')
    warehouse=next((f for f in s['fields'] if f['key']=='custcol_webstore_locations'),None)
    stock=s.get('stock');available=None
    if s.get('out_of_stock') is True or warehouse and all(x['disabled'] for x in warehouse['choices']): available=False
    elif warehouse and type(stock) is int and stock>=0 and warehouse.get('selected') and any(c['value']==warehouse['selected'] and not c['disabled'] for c in warehouse['choices']): available=quantity<=stock
    required=s.get('required_text',[])
    basis = {'kind':'native_current_quantity_reconciliation','quantity':quantity} if quantity>=2 else deepcopy(witness)
    if basis is not None and quantity<2:
        require(type(basis) is dict and basis.get('binding')==binding and type(basis.get('quantity')) is int and basis['quantity']>=2 and type(basis.get('observed_at')) is str,'unit_basis_scope_mismatch')
    holds=[]
    if required:holds.append('required_text_service')
    if available is None:holds.append('stock_or_warehouse_unresolved')
    if basis is None:holds.append('unit_basis_witness_required')
    if not s.get('production',{}).get('id') and s.get('artwork_controls_present'):holds.append('production_unresolved')
    return {'schema':'wsdisplay-native-band-observation/v1','observed_at':now(),'binding':binding,
        'quantity':quantity,'unit_cost':format(unit,'.2f'),'native_total':format(total,'.2f'),
        'bands':bands,'band_domain_observed':True,'bands_are_configured_quotes':False,
        'unit_basis_explicit':basis is not None,'unit_basis_witness':basis,
        'historical_band_difference':historical_difference(bands,witness),
        'unit_line_reconciliation_sha256':digest({'quantity':quantity,'unit':str(unit),'total':str(total),'rows':rows}),
        'tier_domain_sha256':digest(bands),'availability':{'available':available,'stock':stock,'warehouse_choices':warehouse['choices'] if warehouse else [],'verified':available is not None},
        'production':deepcopy(s.get('production',{})),'required_text':deepcopy(required),
        'packing':deepcopy(s.get('packing',{})),'requires_artwork_observed':s.get('artwork_controls_present'),
        'artwork_resources':deepcopy(s.get('artwork_resources',[])),
        'artwork_template_status':'source_link_observed' if any(r.get('kind')=='template' for r in s.get('artwork_resources',[]) if type(r)is dict) else 'unresolved',
        'field_labels':[{k:f.get(k) for k in ('key','label','kind','choices','disabled','metadata_only')} for f in s['fields']],
        'holds':holds,'retail_ready':False,'source_data_finalized':False,
        'account_proof':{'kind':'explicit_native_business_header','marker_sha256':hashlib.sha256(s['account_marker'].encode()).hexdigest()}}

def historical_difference(bands,witness):
    if not witness:return None
    q=witness.get('quantity');old=witness.get('unit_cost')
    if type(q) is not int or type(old) is not str:return None
    row=next((r for r in bands if r['minimum']<=q and (r['maximum'] is None or q<=r['maximum'])),None)
    if row is None:return {'historical_quantity':q,'current_band_available':False}
    return {'historical_quantity':q,'old_unit_cost':old,'current_band_unit_cost':row['unit_cost'],'changed':Decimal(old)!=Decimal(row['unit_cost']),'historical_observed_at':witness.get('observed_at'),'not_a_new_configured_quote':True}

# Read rendered product controls only. Never hidden models, cookies, storage,
# passwords, file values, source HTML, or customer/form text values.
PDP_INSPECTION_JS=r"""({root,fields,quantity,sku,url,sourceProof,captureCatalogue=false})=>{
 const out={schema:'wsdisplay-native-pdp-inspection/v1',status:'unavailable',valid_quote:false,retail_ready:false,source_data_finalized:false};
 const contract=[
  {url:'https://www.wsdisplay.com/webstore/javascript/shopping.js?t=1790279871637',sha256:'d1b3d6179ed82d1e1b5b3757e1a5b206410353838c1cbed72c221a890e28138a'},
  {url:'https://www.wsdisplay.com/webstore/extensions/shopping_4.js?t=1790279871637',sha256:'44695dda86bc68209ea71f7f8d1cffe251c8743bb301a8e01573c85c430c8a36'}
 ];
 out.source_proof=contract.map(x=>({url:x.url,expected_sha256:x.sha256,actual_sha256:sourceProof?.[x.url]?.sha256||null,bytes:sourceProof?.[x.url]?.bytes??null,match:sourceProof?.[x.url]?.status==='verified'&&sourceProof[x.url].sha256===x.sha256}));
 if(!out.source_proof.every(x=>x.match)){out.status='source_code_unverified';return out;}
 const clean=x=>typeof x==='string'?x.replace(/\s+/g,' ').trim():null;
 const text=(x,max=200)=>typeof x==='string'&&x.length<=max&&!/[\x00-\x1f\x7f<>]/.test(x)?x:null;
 const number=x=>typeof x==='number'&&Number.isFinite(x)?x:null;
 const id=x=>typeof x==='string'?text(x):typeof x==='number'&&Number.isSafeInteger(x)&&x>=0?x:null;
 const own=(x,k)=>x!==null&&typeof x==='object'&&Object.hasOwn(x,k);
 const valueType=v=>Array.isArray(v)?'array':v===null?'null':typeof v;
 const vis=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&e.getClientRects().length>0&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
 const binding=()=>{
  if(location.href!==url||!root?.isConnected||root.matches('[aria-busy="true"]')||root.querySelector('[aria-busy="true"],[data-loading="true"]'))return null;
  const qs=[...root.querySelectorAll('input#quantity')].filter(vis),infos=[...root.querySelectorAll('#productinformation')];if(qs.length!==1||infos.length!==1)return null;
  const all=[...root.querySelectorAll('select,input[type="radio"]')];const rows=[];
  for(const f of fields){const es=all.filter(e=>(e.type==='radio'?(e.name||e.getAttribute('data-id')||e.id):(e.id||e.name||e.getAttribute('data-id')))===f.key);if(!es.length)return null;
   if(f.kind==='select'){if(es.length!==1)return null;rows.push({key:f.key,selected:es[0].value,disabled:es[0].matches(':disabled'),choices:[...es[0].options].map(o=>({value:o.value,disabled:o.disabled}))});}
   else{const checked=es.filter(e=>e.checked);rows.push({key:f.key,selected:checked.length===1?checked[0].value:'',disabled:es.every(e=>e.matches(':disabled')),choices:es.map(e=>({value:e.value,disabled:e.matches(':disabled')}))});}
  }
  const m=clean(infos[0].textContent)?.match(/SKU\s*([A-Za-z0-9_.:-]{1,100})$/i);
  return{url:location.href,quantity:qs[0].value,sku:m?m[1]:'',fields:rows};
 };
 const before=binding();if(!before||before.quantity!==String(quantity)||before.sku!==sku){out.status='dom_binding_unavailable';return out;}
 try{
  const app=window.SC?.Application;if(!app||typeof app.getComponent!=='function'){out.status='pdp_unavailable';return out;}
  const pdp=app.getComponent('PDP');if(!pdp||typeof pdp.getItemInfo!=='function'||typeof pdp.getSelectedMatrixChilds!=='function'||typeof pdp.getPrice!=='function'){out.status='pdp_unavailable';return out;}
  const info=pdp.getItemInfo(),children=pdp.getSelectedMatrixChilds();
  if(!info||typeof info!=='object'||!info.item||typeof info.item!=='object'||!Array.isArray(info.options)||!Array.isArray(children)){out.status='pdp_view_unavailable';return out;}
  const item=info.item;out.quantity=number(info.quantity);out.quantity_matches_dom=Number.isSafeInteger(out.quantity)&&out.quantity===quantity;
  out.options_count=info.options.length;out.selected_children_count=children.length;out.matrix_children_present=own(item,'matrixchilditems_detail');out.matrix_children_count=Array.isArray(item.matrixchilditems_detail)?item.matrixchilditems_detail.length:null;
  out.matrix_children_alias_valid=item.keyMapping_matrixChilds==='matrixchilditems_detail';
  if(info.options.length>32||children.length>512){out.status='projection_bound';return out;}
  out.matrix_options=[];out.unjoined_matrix_options=0;
  for(const o of info.options){
   if(o?.isMatrixDimension!==true)continue;
   const f=fields.find(f=>f.key===o.cartOptionId&&(f.kind==='select'||f.kind==='radio'));
   if(!f||!['select','radio','color'].includes(o.type)){out.unjoined_matrix_options++;continue;}
   // Read values ONLY after joining a finite DOM option. Never text/free-text options.
   const v=o.value;const selected=id(v?.internalid);out.matrix_options.push({cart_option_id:text(o.cartOptionId,100),item_option_id:text(o.itemOptionId,100),label:text(o.label,300),type:o.type,selected_id:selected,selected_label:text(v?.label,300),dom_selected:f.selected,dom_matches:selected!==null&&String(selected)===f.selected,choice_count:Array.isArray(o.values)?o.values.length:null});
  }
  out.matrix_option_count=out.matrix_options.length+out.unjoined_matrix_options;
  const detail=x=>{
   const d={source_sku:text(x?.itemid,200),mapped_sku:text(x?.keyMapping_sku,200),internalid:id(x?.internalid),minimumquantity_present:own(x,'minimumquantity'),maximumquantity_present:own(x,'maximumquantity'),minimumquantity:number(x?.minimumquantity),maximumquantity:number(x?.maximumquantity),mapped_minimumquantity:number(x?.keyMapping_minimumQuantity),mapped_maximumquantity:number(x?.keyMapping_maximumQuantity),price_details_alias_valid:x?.keyMapping_priceDetails==='onlinecustomerprice_detail',item_onlinecustomerprice:number(x?.onlinecustomerprice)};
   d.sku_matches_mapping=d.source_sku!==null&&d.source_sku===d.mapped_sku;
   const raw=d.price_details_alias_valid?x.onlinecustomerprice_detail:null;d.price_details_present=raw!==null&&typeof raw==='object'&&!Array.isArray(raw);d.price_details=null;
   if(d.price_details_present){const rows=raw.priceschedule;d.price_details={onlinecustomerprice:number(raw.onlinecustomerprice),onlinecustomerprice_formatted:text(raw.onlinecustomerprice_formatted,100),schedule_present:own(raw,'priceschedule'),schedule_state:!own(raw,'priceschedule')?'absent':!Array.isArray(rows)?'unsupported':rows.length===0?'empty':rows.length>128?'truncated':'present',schedule_count:Array.isArray(rows)?rows.length:null,maximumquantity_is_native_exclusive:true,rows:Array.isArray(rows)?rows.slice(0,128).map(r=>({minimumquantity:typeof r?.minimumquantity==='string'?text(r.minimumquantity,40):number(r?.minimumquantity),maximumquantity:typeof r?.maximumquantity==='string'?text(r.maximumquantity,40):number(r?.maximumquantity),price:number(r?.price),price_formatted:text(r?.price_formatted,100)})):[]};}
   const computed=key=>{const v=x?.[key];return{present:own(x,key),raw_type:valueType(v),value:typeof v==='boolean'?v:null};};
   d.source_computed_purchasability=computed('keyMapping_isPurchasable');d.source_computed_buildability=computed('keyMapping_isBuildable');
   d.fulfillment_metadata={available_from_ca:typeof x?.custitem_avlb_from_ca==='boolean'?x.custitem_avlb_from_ca:null,available_from_pa:typeof x?.custitem_avlb_from_pa==='boolean'?x.custitem_avlb_from_pa:null,available_from_pfc:typeof x?.custitem_avlb_from_pfc==='boolean'?x.custitem_avlb_from_pfc:null,source_preferred_location:['California','Pennsylvania','Fulfillment Center - Pacific'].includes(x?.custitem_preferred_location)?x.custitem_preferred_location:null,not_selected_warehouse_proof:true,free_ground_shipping:typeof x?.custitem_free_ground_shipping==='boolean'?x.custitem_free_ground_shipping:null,wsprime_product_eligible:typeof x?.custitem2==='boolean'?x.custitem2:null,not_membership_or_route_activation:true};
   return d;
  };
  out.direct_item=detail(item);out.selected_child=children.length===1?detail(children[0]):null;
  out.direct_item_matches_dom_sku=out.direct_item.source_sku===sku;out.direct_identity_eligible=out.matrix_children_alias_valid&&(!out.matrix_children_present||out.matrix_children_count===0)&&out.selected_children_count===0&&out.matrix_option_count===0&&out.direct_item_matches_dom_sku;
  out.unique_child_eligible=children.length===1&&out.matrix_children_count!==null&&out.matrix_children_count>0&&out.unjoined_matrix_options===0&&out.matrix_options.every(x=>x.dom_matches);
  out.native_get_price_called=false;out.native_price=null;
  const selected=out.unique_child_eligible?out.selected_child:out.direct_identity_eligible?out.direct_item:null;
  if(selected&&selected.source_sku&&selected.sku_matches_mapping&&selected.internalid!==null&&out.quantity_matches_dom){const price=pdp.getPrice();out.native_get_price_called=true;out.native_price={price:number(price?.price),price_formatted:text(price?.price_formatted,100),compare_price:number(price?.compare_price),compare_price_formatted:text(price?.compare_price_formatted,100),range_metadata_not_exposed_by_public_getter:true};}
  const feeState=(options,key)=>{
   const found=options.filter(o=>o?.cartOptionId===key);const state={option_present:found.length>0,option_count:found.length,found_unique:found.length===1,selected_internalid_present:null,selected_internalid_truthy:null};
   if(!found.length){state.selected_internalid_present=false;state.selected_internalid_truthy=false;return state;}
   if(found.length!==1)return state;
   const value=found[0].value;if(value==null){state.selected_internalid_present=false;state.selected_internalid_truthy=false;return state;}
   if(typeof value!=='object'||Array.isArray(value))return state;
   state.selected_internalid_present=Object.hasOwn(value,'internalid');state.selected_internalid_truthy=state.selected_internalid_present?!!value.internalid:false;return state;
  };
  out.fee_activation={accessories:feeState(info.options,'custcol_sc_accessories_options'),turnaround:feeState(info.options,'custcol_turn_around_shipping')};
  out.fee_activation.accessories_payload_selected=out.fee_activation.accessories.selected_internalid_truthy;
  out.fee_activation.turnaround_selected=out.fee_activation.turnaround.selected_internalid_truthy;
  const tatUnit=()=>{
   const result={status:'held',unit_fee:null,basis:null,valid_quote:false,formula:'native_unit_fee_times_quantity',scope_checks:[]};
   if(out.fee_activation.turnaround_selected===false)return{...result,status:'captured',unit_fee:0,basis:'native_no_selected_option_zero'};
   if(out.fee_activation.turnaround_selected!==true)return{...result,reason:'tat_activation_ambiguous'};
   const finite=fields.find(f=>f.key==='custcol_turn_around_shipping'),options=info.options.filter(o=>o?.cartOptionId==='custcol_turn_around_shipping');
   const selectedId=id(options.length===1?options[0].value?.internalid:null);
   if(selectedId===null||!finite||String(selectedId)!==finite.selected||!finite.choices.some(c=>c.value===finite.selected&&!c.disabled))return{...result,reason:'tat_not_joined_to_finite_dom'};
   result.selected_id=finite.selected;
   if(!(out.unique_child_eligible||out.direct_identity_eligible)||!out.quantity_matches_dom)return{...result,reason:'product_identity_unresolved'};
   if(typeof app.getLayout!=='function')return{...result,reason:'current_view_unavailable'};
   const layout=app.getLayout(),view=typeof layout?.getCurrentView==='function'?layout.getCurrentView():null;
   if(view?.attributes?.id!=='ProductDetails.Full.View')return{...result,reason:'wrong_current_view'};
   const model=view.model;if(!model||typeof model.get!=='function'||typeof model.getItem!=='function'||typeof model.getOption!=='function'||typeof model.getTurnAroundOptions!=='function')return{...result,reason:'current_product_unavailable'};
   const selected=out.unique_child_eligible?out.selected_child:out.direct_item;
   const nativeId=value=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?String(value):typeof value==='string'&&/^[0-9]{1,20}$/.test(value)?value.replace(/^0+(?=\d)/,''):null;
   const currentScope=()=>{
    const scope={parent_getter_present:false,selected_getter_present:false,parent_id_matches:null,parent_id_type:null,parent_expected_id_type:valueType(info.item.internalid),parent_id_normalized:null,parent_expected_id_normalized:nativeId(info.item.internalid),selected_id_matches:null,selected_id_type:null,selected_expected_id_type:valueType(selected.internalid),selected_id_normalized:null,selected_expected_id_normalized:nativeId(selected.internalid),quantity_matches:null,quantity_type:null,quantity_expected_type:valueType(info.quantity),finite_fields_count:fields.length,finite_fields:[],finite_fields_diagnostic_truncated:fields.length>32,fee_activation_matches:{accessories:null,turnaround:null},failure_stage:null};
    result.scope_checks.push(scope);const fail=stage=>{scope.failure_stage=stage;return false;};
    const parent=model.get('item'),current=model.getItem();scope.parent_getter_present=typeof parent?.get==='function';scope.selected_getter_present=typeof current?.get==='function';
    if(!scope.parent_getter_present||!scope.selected_getter_present)return fail('identity_getter');
    const parentId=parent.get('internalid');scope.parent_id_type=valueType(parentId);scope.parent_id_normalized=nativeId(parentId);scope.parent_id_matches=id(parentId)===id(info.item.internalid);if(!scope.parent_id_matches)return fail('parent_id');
    const selectedId=current.get('internalid');scope.selected_id_type=valueType(selectedId);scope.selected_id_normalized=nativeId(selectedId);scope.selected_id_matches=id(selectedId)===selected.internalid;if(!scope.selected_id_matches)return fail('selected_id');
    const quantity=model.get('quantity');scope.quantity_type=valueType(quantity);scope.quantity_matches=quantity===info.quantity;if(!scope.quantity_matches)return fail('quantity');
    for(const f of fields){const o=model.getOption(f.key),entry={key:typeof f.key==='string'&&/^custcol[A-Za-z0-9_]{0,100}$/.test(f.key)?f.key:'unsupported',option_present:!!o,value_getter_present:typeof o?.get==='function',value_type:null,selected_id_type:null,matches:null};if(scope.finite_fields.length<32)scope.finite_fields.push(entry);if(!o||typeof o.get!=='function')return fail('finite_option_getter');const value=o.get('value'),raw=value&&typeof value==='object'?value.internalid:null,v=value&&typeof value==='object'?id(raw):null;entry.value_type=valueType(value);entry.selected_id_type=valueType(raw);entry.matches=(v===null?'':String(v))===f.selected;entry.native_default_basis=null;
     if(!entry.matches&&f.key==='custcol_pms_match'&&value==null&&f.kind==='select'&&f.selected==='F'&&f.disabled===false&&Array.isArray(f.choices)&&f.choices.length===2&&new Set(f.choices.map(c=>c.value)).size===2&&f.choices.every(c=>(c.value==='F'||c.value==='T')&&c.disabled===false)){
      const sourceOptions=info.options.filter(o=>o?.cartOptionId==='custcol_pms_match');entry.pms_public_option_count=sourceOptions.length;entry.pms_public_value_unset=sourceOptions.length===1&&sourceOptions[0].value==null;
      if(entry.pms_public_value_unset){entry.matches=true;entry.native_default_basis='source_PMSMatch_View_unset_to_F';}
     }
     if(!entry.matches)return fail('finite_option');}
    // Compare only fixed activation booleans. Never read/decode an accessory payload.
    for(const [key,flag,name]of [['custcol_sc_accessories_options',out.fee_activation.accessories_payload_selected,'accessories'],['custcol_turn_around_shipping',out.fee_activation.turnaround_selected,'turnaround']]){const o=model.getOption(key),v=o&&typeof o.get==='function'?o.get('value'):null;scope.fee_activation_matches[name]=!!(v&&v.internalid)===flag;if(!scope.fee_activation_matches[name])return fail('fee_activation');}
    return true;
   };
   if(!currentScope())return{...result,reason:'current_product_scope_mismatch'};
   const collection=model.getTurnAroundOptions();let value=0,basis='native_absent_collection_zero',uncertain=false,count=null;
   if(collection&&collection.models){
    if(!Array.isArray(collection.models)||collection.models.length>128)return{...result,reason:'tat_collection_bound'};
    count=collection.models.length;const rows=collection.models.filter(row=>id(row?.id)!==null&&String(row.id)===finite.selected);
    if(rows.length>1)return{...result,reason:'tat_duplicate_selected_id'};
    if(!rows.length){basis='native_selected_row_missing_zero';uncertain=true;}
    else{if(typeof rows[0].get!=='function')return{...result,reason:'tat_row_unsupported'};const price=rows[0].get('price');if(price===''){value=0;basis='native_empty_price_zero';}else if(typeof price==='number'&&Number.isFinite(price)&&price>=0){value=price;basis='native_selected_row_unit_price';}else return{...result,reason:'tat_price_unsupported'};}
   }
   if(!currentScope())return{...result,reason:'current_product_scope_changed'};
   return{...result,status:'captured',unit_fee:value,basis,selected_id:finite.selected,collection_model_count:count,catalogue_scope_unconfirmed:uncertain,current_parent_id:id(info.item.internalid),current_selected_id:selected.internalid,quantity:info.quantity};
  };
  out.turnaround_unit_fee=tatUnit();
  let catalogue=null;
  if(captureCatalogue===true){
   const raw=item.matrixchilditems_detail,directCandidate=out.matrix_option_count===0&&(!own(item,'matrixchilditems_detail')||(Array.isArray(raw)&&raw.length===0)),childrenSource=directCandidate?[item]:Array.isArray(raw)?raw:[];
   const kind=directCandidate?'direct_item_candidate':Array.isArray(raw)?'native_matrix_children':'unavailable';
   catalogue={schema:'wsdisplay-native-child-catalogue/v1',source_kind:kind,source_url:url,source_proof:out.source_proof,raw_children_present:own(item,'matrixchilditems_detail'),price_mapping_basis:'fixed_raw_onlinecustomerprice_detail_from_shared_Item_KeyMapping',parent_price_mapping_alias_valid:item.keyMapping_priceDetails==='onlinecustomerprice_detail',raw_children_require_computed_alias:false,raw_child_count:Array.isArray(raw)?raw.length:null,matrix_fields:out.matrix_options.map(o=>({cart_option_id:o.cart_option_id,item_option_id:o.item_option_id,label:o.label})),matrix_field_unjoined_count:out.unjoined_matrix_options,source_row_count:childrenSource.length,root_context:{parent_internalid:id(info.item.internalid),parent_source_sku:text(info.item.itemid,200),dom_sku:sku,quantity:info.quantity,finite_selection:fields.map(f=>({key:f.key,selected:f.selected}))},rows:[],children_truncated:childrenSource.length>512,schedule_rows_truncated:false,bytes_truncated:false,valid_quote:false,retail_ready:false,source_data_finalized:false};
   const literal=(value,max=300)=>typeof value==='string'?text(value,max):typeof value==='number'&&Number.isFinite(value)?value:null;
   const bool=value=>typeof value==='boolean'?value:null;let scheduleRows=0;const byteLength=value=>new TextEncoder().encode(JSON.stringify(value)).length;let projectedBytes=byteLength(catalogue);
   for(const child of childrenSource.slice(0,512)){
    if(!child||typeof child!=='object'||Array.isArray(child)){catalogue.children_truncated=true;continue;}
    const pd=child.onlinecustomerprice_detail;const schedule=pd&&Array.isArray(pd.priceschedule)?pd.priceschedule:null;
    const take=Math.min(schedule?.length||0,128,Math.max(0,8192-scheduleRows));
    if(schedule&&take<schedule.length)catalogue.schedule_rows_truncated=true;
    const turnaround=child.custitem_turn_around_options,turnLabels=[];let turnInvalid=0;
    for(const label of Array.isArray(turnaround)?turnaround.slice(0,64):[]){const value=text(label,300);if(value===null)turnInvalid++;else turnLabels.push(value);}
    const row={source_sku:text(child.itemid,200),internalid:id(child.internalid),onlinecustomerprice:literal(child.onlinecustomerprice,100),onlinecustomerprice_detail_present:own(child,'onlinecustomerprice_detail'),minimumquantity_present:own(child,'minimumquantity'),maximumquantity_present:own(child,'maximumquantity'),minimumquantity:literal(child.minimumquantity,100),maximumquantity:literal(child.maximumquantity,100),native_matrix_labels:{},price_details:pd&&typeof pd==='object'&&!Array.isArray(pd)?{onlinecustomerprice:literal(pd.onlinecustomerprice,100),onlinecustomerprice_formatted:text(pd.onlinecustomerprice_formatted,100),schedule_present:own(pd,'priceschedule'),schedule_count:schedule?.length??null,maximumquantity_is_native_exclusive:true,priceschedule:schedule?schedule.slice(0,take).map(r=>({minimumquantity:literal(r?.minimumquantity,40),maximumquantity:literal(r?.maximumquantity,40),price:literal(r?.price,100),price_formatted:text(r?.price_formatted,100)})):null}:null,warehouse_flags:{ca:bool(child.custitem_avlb_from_ca),pa:bool(child.custitem_avlb_from_pa),pfc:bool(child.custitem_avlb_from_pfc)},free_ground_shipping:bool(child.custitem_free_ground_shipping),wsprime_product_eligible:bool(child.custitem2),not_membership_or_route_activation:true,source_preferred_location:['California','Pennsylvania','Fulfillment Center - Pacific'].includes(child.custitem_preferred_location)?child.custitem_preferred_location:null,warehouse_stock_raw:{},warehouse_stock_native_rounded:{},warehouse_doh_raw:{ca:literal(child.custitem_doh_california,100),pa:literal(child.custitem_doh_pennsylvania,100),pfc:literal(child.custitem_doh_mexico,100)},warehouse_doh_present:{ca:own(child,'custitem_doh_california'),pa:own(child,'custitem_doh_pennsylvania'),pfc:own(child,'custitem_doh_mexico')},warehouse_doh_raw_type:{ca:valueType(child.custitem_doh_california),pa:valueType(child.custitem_doh_pennsylvania),pfc:valueType(child.custitem_doh_mexico)},material_literal:literal(child.custitem49),material_thickness_weight_literal:literal(child.custitem_material_thick_weight),item_weight_lbs_literal:literal(child.weight),shipping_dimension_literal:literal(child.shippackage),warranty_literal:literal(child.custitem_warranty),default_shipmethod_internalid:id(child.defaultitemshipmethod?.internalid),turnaround_options_literal:literal(child.custitem_turn_around_options),turnaround_option_labels:turnLabels,turnaround_options_present:own(child,'custitem_turn_around_options'),turnaround_options_raw_type:Array.isArray(turnaround)?'array':turnaround===null?'null':typeof turnaround,turnaround_options_count:Array.isArray(turnaround)?turnaround.length:null,turnaround_options_inspected_count:Array.isArray(turnaround)?Math.min(turnaround.length,64):0,turnaround_options_truncated:Array.isArray(turnaround)&&turnaround.length>64,turnaround_options_invalid_count:turnInvalid,turnaround_options_labels_complete:Array.isArray(turnaround)&&turnaround.length<=64&&turnInvalid===0,itemtype:text(child.itemtype,100),isinstock:bool(child.isinstock),ispurchasable:bool(child.ispurchasable),stock_is_selected_warehouse_proof:false,weight_is_shipping_gross_proven:false,cartons_per_quantity_proven:false};
    for(const [place,key]of [['ca','custitem_wsd_inv_ca_bld'],['pa','custitem_wsd_inv_pa_bld'],['pfc','custitem_wsd_inv_pfc_bld']]){const raw=literal(child[key],100);row.warehouse_stock_raw[place]=raw;const parsed=typeof raw==='number'?raw:typeof raw==='string'&&/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw.trim())?parseFloat(raw):NaN;row.warehouse_stock_native_rounded[place]=Number.isFinite(parsed)?Number(parsed.toFixed(0)):null;}
    for(const f of catalogue.matrix_fields){const key=f.item_option_id;if(typeof key==='string'&&/^custitem(?:_[A-Za-z0-9_]{1,100}|[0-9]{1,10})$/.test(key)&&Object.hasOwn(child,key))row.native_matrix_labels[key]=literal(child[key],300);}
    const bytes=byteLength(row)+1;if(projectedBytes+bytes>2*1024*1024-1024){catalogue.bytes_truncated=true;break;}catalogue.rows.push(row);projectedBytes+=bytes;scheduleRows+=take;
   }
   catalogue.emitted_rows=catalogue.rows.length;catalogue.emitted_schedule_rows=scheduleRows;catalogue.complete_projection=!catalogue.children_truncated&&!catalogue.schedule_rows_truncated&&!catalogue.bytes_truncated&&catalogue.emitted_rows===catalogue.source_row_count&&catalogue.matrix_field_unjoined_count===0&&kind!=='unavailable'&&!(out.matrix_option_count>0&&childrenSource.length===0);
  }
  const after=binding();if(!after||JSON.stringify(before)!==JSON.stringify(after)){out.status='dom_binding_changed';delete out.direct_item;delete out.selected_child;out.native_price=null;return out;}
  out.dom_binding_unchanged=true;out.dom_quantity=quantity;out.dom_sku=sku;
  out.status='captured';out.native_copy_internal_allocation_is_not_output_bounded=true;
  if(new TextEncoder().encode(JSON.stringify(out)).length>65536)return{schema:out.schema,status:'projection_bound',source_proof:out.source_proof,valid_quote:false,retail_ready:false,source_data_finalized:false};
  if(catalogue){catalogue.root_dom_binding_unchanged=true;out.catalogue=catalogue;}
  return out;
 }catch(_){return{schema:out.schema,status:'pdp_getter_failed',source_proof:out.source_proof,valid_quote:false,retail_ready:false,source_data_finalized:false};}
}
"""

READ_JS=r"""({accountMarker,publicSourceProof={},expectedUrl,captureCatalogue=false})=>{
 const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
 const vis=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&e.getClientRects().length>0&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
 const visibleText=e=>{const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);const parts=[];let node;while((node=walker.nextNode()))if(vis(node.parentElement)&&!node.parentElement.closest('textarea,[contenteditable],script,style,noscript'))parts.push(node.nodeValue);return clean(parts.join(' '));};
 const priceLogin=[...document.querySelectorAll('a,span,p,label,div')].some(e=>vis(e)&&/(?:please\s+)?(?:log|sign)\s*in\s+to\s+(?:view|see|access)\s+(?:the\s+)?(?:price|prices|pricing)\b|unit price\s*:?\s*please\s+log\s*in/i.test(visibleText(e)));
 const passwordLogin=[...document.querySelectorAll('input[type="password"]')].some(e=>vis(e)&&!!e.closest('form,[role="dialog"],main'));
 const challenge=[...document.querySelectorAll('title,h1,p')].some(e=>(e.tagName==='TITLE'||vis(e))&&/access denied|too many requests|just a moment|verify you are human|checking your browser/i.test(clean(e.textContent)));
 const h=[...document.querySelectorAll('h1')].filter(vis);let root=h.length===1?h[0].parentElement:null;
 while(root&&root.parentElement&&!root.querySelector('#productinformation'))root=root.parentElement;
 const accounts=[...document.querySelectorAll('header a,.site-header a')].filter(vis).map(e=>clean(e.textContent));
 const account=accounts.find(s=>s===accountMarker)||'';
 const readiness={h1_visible_count:h.length,root_found:!!root&&root!==document.body&&root!==document.documentElement,root_tag:root?.tagName||'NONE',global_info_count:document.querySelectorAll('#productinformation').length,scoped_info_count:root?.querySelectorAll('#productinformation').length||0,global_quantity_visible_count:[...document.querySelectorAll('input#quantity')].filter(vis).length,scoped_quantity_count:root?[...root.querySelectorAll('input#quantity')].filter(vis).length:0,document_state:document.readyState,native_busy:!!root&&(root.matches('[aria-busy="true"]')||!!root.querySelector('[aria-busy="true"],[data-loading="true"]')),account_marker_present:accounts.some(s=>/^My Account\s+\S/.test(s)),expected_marker_match:!!account,price_login_prompt:priceLogin,password_login_visible:passwordLogin};
 if(!root||root===document.body)return {url:location.href,ready:false,readiness,login_prompt:priceLogin,password_login_visible:passwordLogin,challenge};
 const info=[...root.querySelectorAll('#productinformation')];const qty=[...root.querySelectorAll('input#quantity')].filter(vis);
 const text=clean(root.innerText);
 const controls=[...root.querySelectorAll('select,input[type="radio"]')].filter(e=>vis(e)||[...e.labels||[]].some(vis));const fields=[],groups=new Map();
 const label=e=>{let s=clean([...e.labels||[]].map(x=>x.textContent).join(' '));if(s)return s;for(let p=e.parentElement,n=0;p&&n<3;n++,p=p.parentElement){const l=p.querySelector('label');if(l&&vis(l)&&clean(l.textContent))return clean(l.textContent);const t=[...p.childNodes].filter(x=>x.nodeType===3).map(x=>x.textContent).join(' ');if(clean(t))return clean(t);const without=clean(p.innerText).replace(clean(e.innerText),'').trim();if(without&&without.length<=300)return without;}return '';};
 for(const e of controls){const key=e.id||e.name||e.getAttribute('data-id')||'';if(!key||!/^(?:custcol|quantity)/.test(key))continue;
  if(e.type==='radio'){const k=e.name||e.getAttribute('data-id');if(!k)continue;let f=groups.get(k);if(!f){f={key:k,kind:'radio',label:k==='custcol_webstore_locations'?'Webstore Locations':label(e),selected:'',disabled:true,choices:[]};groups.set(k,f);fields.push(f);}f.choices.push({value:e.value,label:label(e),disabled:e.matches(':disabled')});f.disabled&&=e.matches(':disabled');if(e.checked)f.selected=e.value;
  }else{const opts=[...e.options].map(o=>({value:o.value,label:clean(o.textContent),disabled:o.disabled,source_active:o.getAttribute('data-active'),source_available:o.getAttribute('data-available')}));const metadata=e.id==='custcol_turn_around_shipping'&&e.matches(':disabled');fields.push({key,kind:'select',label:label(e),selected:e.value,disabled:e.matches(':disabled'),metadata_only:metadata,choices:opts});}}
 const prod=fields.find(f=>f.key==='custcol_turn_around_shipping');const production=prod?{id:prod.selected,label:prod.choices.find(c=>c.value===prod.selected)?.label||'',control_disabled:prod.disabled}:{};
 const selectedWarehouse=fields.find(f=>f.key==='custcol_webstore_locations');const wl=selectedWarehouse?.choices.find(c=>c.value===selectedWarehouse.selected)?.label||'';const sm=wl.match(/(?:^|\s)(\d+)$/);
 const pi=clean(info[0]?.textContent);const im=pi.match(/Item Weight(.*?)Shipping Dimension(.*?)Warranty.*?SKU(.*)$/i);const material=pi.match(/Material(.*?)Material Thickness\/Weight/i);const materialLiteral=material&&material[1].trim().length<=300?material[1].trim():null;
 const tables=[...root.querySelectorAll('table')].filter(vis).map(t=>[...t.querySelectorAll('tr')].map(tr=>[...tr.querySelectorAll('th,td')].map(td=>visibleText(td))));const tiers=tables.filter(r=>r[0]?.[0]==='Quantity'&&/Price/.test(r[0]?.[1]||''));
 const units=[...root.querySelectorAll('[itemprop="price"]')].filter(vis).map(e=>clean(e.textContent)).filter(Boolean);
 const totals=[...root.querySelectorAll('span,div,p,strong')].filter(vis).map(e=>clean(e.innerText)).filter(s=>/^Total Price:\s*\$[\d,.]+$/.test(s)).map(s=>s.replace(/^Total Price:\s*/,''));
 const priceCurrencyCodes=[...new Set([...root.querySelectorAll('[itemprop="priceCurrency"]')].map(e=>clean(e.getAttribute('content')||e.textContent)).filter(x=>/^[A-Z]{3}$/.test(x)))].slice(0,8);
 const required=[...root.querySelectorAll('input[type="text"],textarea')].filter(e=>vis(e)&&!e.disabled&&(e.required||e.getAttribute('aria-required')==='true')).map(e=>({key:e.id||e.name||e.getAttribute('data-id')||'',label:label(e),required:true}));
 const artworkResources=[...root.querySelectorAll('#productdownloads a[href]')].slice(0,32).map(a=>{
  const label=clean(a.textContent).toLowerCase();const kind=label==='template'?'template':label==='specsheet'?'specsheet':label==='instructions'?'instructions':null;
  let u;try{u=new URL(a.getAttribute('href'),location.href);}catch(_){return null;}
  const approved=u.hostname==='display-templates.s3.us-west-2.amazonaws.com'&&/^\/(?:PDF|PSB)\//.test(u.pathname)
    ||u.hostname==='s3-us-west-2.amazonaws.com'&&/^\/display-templates\/(?:PDF|PSB)\//.test(u.pathname)
    ||u.hostname==='www.wsdisplay.com';
  if(!kind||u.protocol!=='https:'||!approved||u.username||u.password||u.search||u.hash)return null;
  return{kind,url:u.href.slice(0,500)};
 }).filter(Boolean).slice(0,16);
 readiness.fields_count=fields.length;readiness.required_text_count=required.length;
 return {readiness,url:location.href,ready:document.readyState!=='loading'&&h.length===1&&info.length===1&&qty.length===1&&!root.matches('[aria-busy="true"]')&&!root.querySelector('[aria-busy="true"],[data-loading="true"]'),account_marker:account,account_marker_present:accounts.some(s=>/^My Account\s+\S/.test(s)),
 challenge,login_prompt:priceLogin,password_login_visible:passwordLogin,title:clean(h[0]?.textContent),fields,required_text:required,
 quantity:qty.length===1&&/^\d+$/.test(qty[0].value)?Number(qty[0].value):null,quantity_min:qty[0]?.getAttribute('min'),quantity_max:qty[0]?.getAttribute('max'),unit_prices:units,totals,tier_rows:tiers.length===1?tiers[0]:null,visible_tables:tables,
 price_currency_codes:priceCurrencyCodes,material_literal:materialLiteral||null,product_inspection:account===accountMarker&&!challenge&&!priceLogin&&!passwordLogin&&location.href===expectedUrl&&document.readyState!=='loading'&&h.length===1&&info.length===1&&qty.length===1&&!root.matches('[aria-busy="true"]')&&!root.querySelector('[aria-busy="true"],[data-loading="true"]')?(__PDP_INSPECTION__)({root,fields,quantity:/^\d+$/.test(qty[0].value)?Number(qty[0].value):null,sku:im?clean(im[3]):'',url:expectedUrl,sourceProof:publicSourceProof,captureCatalogue}):{schema:'wsdisplay-native-pdp-inspection/v1',status:'source_context_unapproved',valid_quote:false,retail_ready:false,source_data_finalized:false},sku:im?clean(im[3]):'',packing:{item_weight_literal:im?clean(im[1]):null,shipping_dimension_literal:im?clean(im[2]):null,weight_is_shipping_gross_proven:false,order_quantity_parcels_proven:false},
 stock:sm?Number(sm[1]):null,out_of_stock:/\bOut of Stock\b|Item stock low, Call for Order/i.test(text),production,artwork_controls_present:[...root.querySelectorAll('input[type="file"]')].some(e=>!e.disabled)&&/Upload CMYK Artwork|Print Ready Artwork Only|Upload Artwork/i.test(text),artwork_resources:artworkResources};
}"""

READ_JS=READ_JS.replace('__PDP_INSPECTION__',PDP_INSPECTION_JS)

ACTION_JS=r"""({url,key,value,prefix})=>{
 const vis=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&e.getClientRects().length>0&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
 if(location.href!==url)return false;const hs=[...document.querySelectorAll('h1')].filter(vis);let root=hs.length===1?hs[0].parentElement:null;while(root&&root.parentElement&&!root.querySelector('#productinformation'))root=root.parentElement;if(!root||root===document.body||root.querySelectorAll('#productinformation').length!==1)return false;
 const all=[...root.querySelectorAll('select,input[type="radio"]')];const by=k=>all.filter(e=>(e.type==='radio'?(e.name||e.getAttribute('data-id')||e.id):(e.id||e.name||e.getAttribute('data-id')))===k);
 for(const[k,v]of Object.entries(prefix)){if(k===key)continue;const es=by(k);if(!es.length)return false;const x=es[0].tagName==='SELECT'?(es.length===1?es[0].value:null):es.filter(e=>e.checked).length===1?es.find(e=>e.checked).value:null;if(x!==v)return false;}
 const es=by(key);if(es.length===1&&es[0].tagName==='SELECT'){const e=es[0];const options=[...e.options].filter(o=>o.value===value&&!o.disabled);if(!vis(e)||e.matches(':disabled')||options.length!==1)return false;if(e.value===value)return true;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true;}
 const targets=es.filter(e=>e.type==='radio'&&e.value===value&&!e.matches(':disabled')&&(vis(e)||[...e.labels||[]].some(vis)));if(targets.length!==1)return false;if(!targets[0].checked)HTMLElement.prototype.click.call(targets[0]);return true;
}"""
QTY_JS=r"""({url,expected,value,binding})=>{
 const hold=reason=>({ok:false,dispatched:false,reason});
 const visible=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&e.getClientRects().length>0;
 if(location.href!==url)return hold('source_url_changed');
 const hs=[...document.querySelectorAll('h1')].filter(visible);let root=hs.length===1?hs[0].parentElement:null;while(root&&root.parentElement&&!root.querySelector('#productinformation'))root=root.parentElement;
 if(!root||root===document.body)return hold('native_root_unresolved');
 const qs=[...root.querySelectorAll('input#quantity')].filter(visible),infos=[...root.querySelectorAll('#productinformation')];
 if(qs.length!==1||infos.length!==1)return hold('native_quantity_control_unresolved');
 const sku=String(infos[0].textContent||'').replace(/\s+/g,' ').trim().match(/SKU\s*([A-Za-z0-9_.:-]{1,100})$/i);
 if(!sku||sku[1]!==binding.source_sku)return hold('source_sku_changed');
 const controls=[...root.querySelectorAll('select,input[type="radio"]')];
 for(const[k,v]of Object.entries(binding.choices)){const es=controls.filter(e=>(e.type==='radio'?(e.name||e.getAttribute('data-id')||e.id):(e.id||e.name||e.getAttribute('data-id')))===k);const got=es.length===1&&es[0].tagName==='SELECT'?es[0].value:es.filter(e=>e.checked).length===1?es.find(e=>e.checked).value:null;if(got!==v)return hold('source_prefix_changed');}
 const e=qs[0];if(e.type!=='number'||e.disabled||e.value!==String(expected)||!/^\d+$/.test(String(value))||Number(value)<Number(e.min||1)||(e.max&&Number(value)>Number(e.max)))return hold('native_quantity_bounds_changed');
 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,String(value));
 e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));e.dispatchEvent(new Event('blur',{bubbles:true}));
 return{ok:true,dispatched:true,reason:null};
}"""

def reviewed_witnesses(options):
    require(type(options) is dict and set(options)<= {'account_marker','reviewed_seed_evidence','native_q2_checks','max_nodes_per_product','template_only'},'profile_options_invalid')
    marker=options.get('account_marker')
    require(type(marker) is str and 12<=len(marker)<=200 and marker.startswith('My Account ') and not re.search(r'[\x00-\x1f\x7f]',marker),'explicit_account_marker_required')
    require(type(options.get('native_q2_checks',False)) is bool,'profile_options_invalid')
    require(type(options.get('template_only',False)) is bool and not (options.get('template_only',False) and options.get('native_q2_checks',False)),'profile_options_invalid')
    bound=options.get('max_nodes_per_product',MAX_NODES);require(type(bound) is int and 1<=bound<=MAX_NODES,'profile_options_invalid')
    seeds=options.get('reviewed_seed_evidence',[]);require(type(seeds) is list and len(seeds)<=MAX_SEEDS,'seed_evidence_invalid');out={};bodies={};total_bytes=0
    for seed in seeds:
        require(type(seed) is dict and set(seed)=={'sha256','base64','operator_reviewed'} and seed['operator_reviewed'] is True and re.fullmatch('[a-f0-9]{64}',str(seed['sha256'])),'seed_evidence_invalid')
        require(type(seed['base64']) is str and len(seed['base64'])<=MAX_SEED_BYTES*4//3+8,'seed_evidence_invalid')
        try: b=base64.b64decode(seed['base64'],validate=True);d=json.loads(b)
        except (ValueError,TypeError):raise Held('seed_evidence_invalid') from None
        total_bytes+=len(b);require(len(b)<=MAX_SEED_BYTES and total_bytes<=8*1024*1024 and hashlib.sha256(b).hexdigest()==seed['sha256'],'seed_evidence_invalid');bodies[seed['sha256']]=b
        require(type(d) is dict and d.get('schema') in {'wsdisplay-authorized-native-q2-price-basis/v1','wsdisplay-native-band-observation/v1'},'seed_evidence_invalid')
        if d['schema']=='wsdisplay-native-band-observation/v1':
            binding=d.get('binding');q=d.get('quantity');stamp=d.get('observed_at')
            try:unit=Decimal(str(d.get('unit_cost')));total=Decimal(str(d.get('native_total')))
            except InvalidOperation:raise Held('seed_evidence_invalid') from None
            require(type(binding) is dict and set(binding)=={'source_url','source_sku','selected_child','warehouse_id','choices','production_id'},'seed_evidence_invalid');page_url(binding['source_url'])
        else:
            dom=d.get('dom',{});controls=dom.get('controls',[]);selected={}
            for c in controls:
                key=c.get('id') or c.get('name') or c.get('data-id')
                if not key or not key.startswith('custcol'):continue
                if c.get('tag')=='SELECT' or c.get('checked') is True:selected[key]=c.get('value')
            info=dom.get('productInformation','');m=re.search(r'SKU\s*([A-Za-z0-9_.:-]{1,100})$',info)
            require(m is not None and type(dom.get('priceNodes')) is list,'seed_evidence_invalid')
            binding={'source_url':page_url(d['source_url']),'source_sku':m[1].strip(),'selected_child':selected.get('custcol_matrix_order_detail_option',''),'warehouse_id':selected.get('custcol_webstore_locations',''),'choices':selected,'production_id':selected.get('custcol_turn_around_shipping','')}
            q=next((int(c['value']) for c in controls if c.get('id')=='quantity' and re.fullmatch('[0-9]+',str(c.get('value')))),None)
            units={money(x['text']) for x in dom['priceNodes'] if x.get('itemprop')=='price'};totals={money(x) for x in re.findall(r'Total Price:\s*(\$[0-9,.]+)',dom.get('productText',''))}
            require(len(units)==len(totals)==1,'seed_evidence_invalid');unit=next(iter(units));total=next(iter(totals));stamp=d.get('observed_at')
        require(type(q) is int and q>=2 and unit>0 and unit*q==total and type(stamp) is str and binding.get('source_sku') and binding.get('warehouse_id') and type(binding.get('choices')) is dict,'seed_evidence_invalid')
        key=digest(binding);w={'kind':'operator_reviewed_immutable_native_quantity_witness','evidence_sha256':seed['sha256'],'observed_at':stamp,'quantity':q,'unit_cost':format(unit,'.2f'),'native_total':format(total,'.2f'),'binding':binding,'basis_only_not_new_quote':True}
        require(key not in out or out[key]==w,'seed_evidence_conflict');out[key]=w
    return marker,out,bound,bodies

class NativeSession:
    def __init__(self,page,limiter,url,marker,check):
        self.page,self.limiter,self.url,self.marker,self.check=page,limiter,url,marker,check
        self.verified_origins={ORIGIN};self.pending={};self.release_tasks=set();self.guard_tasks=set();self.failure=None;self.closing=False;self.last_activity=time.monotonic();self.last_action=0;self.last_readiness={};self.last_reason='source_not_ready';self.counts={'guarded_gets':0,'blocked_mutations':0};self.blocked_requests=[];self.blocked_requests_overflow=0;self.observed_item_requests=[];self.observed_item_requests_overflow=0;self.public_source_tasks=set();self.public_source_proof={url:{'status':'not_captured'}for url in PUBLIC_SOURCE_BUNDLES}
    async def guard(self,route):
        task=asyncio.current_task();self.guard_tasks.add(task)
        try:await self._guard(route)
        finally:self.guard_tasks.discard(task)
    async def _guard(self,route):
        req=route.request;p=urlsplit(req.url)
        if self.closing:await route.abort();return
        if p.scheme not in {'http','https'}:await route.continue_();return
        resource_origin=f'{p.scheme}://{p.netloc}'
        if resource_origin not in self.verified_origins:
            try:await validate_url(req.url)
            except Exception:
                self.failure='invalid_source_resource';await route.abort();return
            self.verified_origins.add(resource_origin)
        if req.method not in {'GET','HEAD'}:
            self.counts['blocked_mutations']+=1
            meta=blocked_request_metadata(req.method,req.url,req.resource_type)
            existing=next((x for x in self.blocked_requests if all(x[k]==v for k,v in meta.items())),None)
            if existing is not None:existing['count']=min(existing['count']+1,1000000)
            elif len(self.blocked_requests)<32:self.blocked_requests.append({**meta,'count':1})
            else:self.blocked_requests_overflow+=1
            await route.abort();return
        if re.search(r'(?:^|/)(?:cart|checkout|logout|orders?|upload|payment)(?:/|\.|$)',unquote(p.path),re.I):await route.abort();return
        if req.resource_type=='document' and req.frame==self.page.main_frame and req.url!=self.url:
            self.failure='source_navigation_changed';await route.abort();return
        if p.hostname!='www.wsdisplay.com' or req.resource_type not in {'document','xhr','fetch'}:await route.continue_();return
        rec={'token':None,'started':time.monotonic(),'status':0,'retry_after':30}
        self.pending[req]=rec;self.last_activity=time.monotonic();abandoned=False
        async def scope():
            if self.closing or abandoned or self.pending.get(req) is not rec:raise asyncio.CancelledError()
            await self.check()
        acquire=asyncio.create_task(self.limiter.acquire(ORIGIN,1,2,scope));token=None
        try:
            # Do not cancel a DB grant between committing a permit and returning it.
            token=await asyncio.shield(acquire)
        except BaseException:
            abandoned=True
            try:token=await acquire
            except BaseException:pass
            if token is not None:self._release({'token':token,'started':time.monotonic(),'status':0,'retry_after':30})
            self.finish(req);raise
        if self.closing or self.pending.get(req) is not rec:
            self._release({'token':token,'started':time.monotonic(),'status':0,'retry_after':30});self.finish(req)
            try:await route.abort()
            except Exception:pass
            return
        rec['token']=token;rec['started']=time.monotonic();self.counts['guarded_gets']+=1
        meta=observed_items_metadata(req.url,req.resource_type)if req.method=='GET'else None
        if meta is not None:
            entry=next((x for x in self.observed_item_requests if all(x[k]==v for k,v in meta.items())),None)
            if entry is None and len(self.observed_item_requests)<32:
                entry={**meta,'guard_admitted_count':0,'response_count':0,'last_http_status':None};self.observed_item_requests.append(entry)
            if entry is not None:entry['guard_admitted_count']=min(entry['guard_admitted_count']+1,1000000);rec['item_metadata']=entry
            else:self.observed_item_requests_overflow+=1
        try:await scope();await route.continue_()
        except BaseException:self.finish(req);raise
    def _release(self,rec):
        task=asyncio.create_task(asyncio.to_thread(self.limiter.release,ORIGIN,rec['token'],time.monotonic()-rec['started'],rec['status'],rec['retry_after'],1));self.release_tasks.add(task);task.add_done_callback(self.release_tasks.discard)
    def observe_public_source(self,response):
        if self.closing:return
        req=response.request;url=getattr(req,'url',None)
        if url not in PUBLIC_SOURCE_BUNDLES or req.method!='GET'or req.resource_type!='script'or req.frame!=self.page.main_frame:return
        proof=self.public_source_proof[url]
        if proof['status']!='not_captured':return
        if response.status!=200:proof.update(status='unsupported_response');return
        proof.update(status='pending')
        async def capture():
            try:
                headers=response.headers;length=headers.get('content-length');mime=headers.get('content-type','').split(';',1)[0].strip().lower()
                if mime not in {'text/javascript','application/javascript'}:proof.update(status='unsupported_response');return
                if length is not None and (not str(length).isdigit()or int(length)>PUBLIC_SOURCE_BODY_LIMIT):proof.update(status='oversized');return
                if self.closing:proof.update(status='unavailable');return
                body=await asyncio.wait_for(response.body(),timeout=PUBLIC_SOURCE_BODY_TIMEOUT)
                if type(body)is not bytes or len(body)>PUBLIC_SOURCE_BODY_LIMIT:proof.update(status='oversized');return
                actual=hashlib.sha256(body).hexdigest();size=len(body);expected=PUBLIC_SOURCE_BUNDLES[url]
                proof.update(status='verified'if actual==expected['sha256']and size==expected['bytes']else'mismatch',sha256=actual,bytes=size)
                del body
            except (Exception,asyncio.CancelledError):proof.update(status='unavailable')
        task=asyncio.create_task(capture());self.public_source_tasks.add(task);task.add_done_callback(self.public_source_tasks.discard)
    def response(self,response):
        self.observe_public_source(response)
        rec=self.pending.get(response.request)
        if rec is not None:
            rec['status']=response.status
            if rec.get('item_metadata')is not None:
                meta=rec['item_metadata'];meta['response_count']=min(meta['response_count']+1,1000000);meta['last_http_status']=response.status if type(response.status)is int and 100<=response.status<=599 else None
            try:rec['retry_after']=retry_after_seconds(response.headers.get('retry-after'))
            except Exception:pass
            if response.status in {401,403}:self.failure='source_access_required'
            elif response.status in {429,503}:self.failure='source_backoff_required'
            elif response.status>=500:self.failure='source_unavailable'
    def finish(self,req):
        rec=self.pending.pop(req,None);self.last_activity=time.monotonic()
        if rec is not None and rec['token'] is not None:self._release(rec)
    async def open(self):
        await self.page.route('**/*',self.guard);self.page.on('response',self.response);self.page.on('requestfinished',self.finish);self.page.on('requestfailed',self.finish)
        await self.page.goto(self.url,wait_until='domcontentloaded',timeout=60000)
    async def snapshot(self,*,capture_catalogue=False):
        deadline=time.monotonic()+SETTLE_SECONDS;last=None;stable=0;last_reason='source_not_ready';authentication_settled=False
        while time.monotonic()<deadline:
            await self.check()
            if self.failure:raise Held(self.failure,self.diagnostic())
            try:raw=await asyncio.wait_for(self.page.evaluate(READ_JS,{'accountMarker':self.marker,'publicSourceProof':deepcopy(self.public_source_proof),'expectedUrl':self.url,'captureCatalogue':capture_catalogue}),timeout=NATIVE_READ_TIMEOUT)
            except asyncio.TimeoutError:
                self.last_readiness={};self.last_reason='native_product_inspection_timeout';raise Held('native_product_inspection_timeout',self.diagnostic())from None
            if self.failure:raise Held(self.failure,self.diagnostic())
            self.last_readiness=raw
            self.last_reason=last_reason
            try:s=normalize_snapshot(raw,self.url,self.marker)
            except Held as exc:
                last_reason=str(exc);self.last_reason=last_reason
                if str(exc)=='source_challenge':raise
                if str(exc)in{'source_access_required','source_account_marker_required'}:
                    readiness=raw.get('readiness',{});readiness=readiness if type(readiness)is dict else {}
                    authentication_settled=not self.pending and not self.public_source_tasks and time.monotonic()-self.last_activity>=QUIET_SECONDS and time.monotonic()-self.last_action>=ACTION_SETTLE_SECONDS and readiness.get('document_state')!='loading' and readiness.get('native_busy')is not True
                    if authentication_settled and (str(exc)=='source_access_required' or raw.get('login_prompt') or raw.get('account_marker_present')):raise
                else:authentication_settled=False
                await asyncio.sleep(POLL_SECONDS);continue
            last_reason='ready';self.last_reason='ready'
            fingerprint=digest(s)
            stable=stable+1 if fingerprint==last else 0;last=fingerprint
            if stable>=1 and not self.pending and not self.public_source_tasks and time.monotonic()-self.last_activity>=QUIET_SECONDS and time.monotonic()-self.last_action>=ACTION_SETTLE_SECONDS:return s
            await asyncio.sleep(POLL_SECONDS)
        raise Held('source_account_marker_required' if last_reason=='source_account_marker_required' and authentication_settled else 'native_settle_timeout',self.diagnostic())
    def diagnostic(self):
        out=readiness_diagnostic(self.last_readiness,self.last_reason,self.pending,self.counts)
        out['last_native_action']=deepcopy(getattr(self,'last_native_action',None));out['blocked_requests']=deepcopy(self.blocked_requests);out['blocked_requests_overflow']=min(self.blocked_requests_overflow,1000000);out['observed_item_requests']=deepcopy(self.observed_item_requests);out['observed_item_requests_overflow']=min(self.observed_item_requests_overflow,1000000);out['public_source_proof']=deepcopy(self.public_source_proof);return out
    async def choose(self,key,value,prefix):
        await self.check();self.last_action=time.monotonic();self.last_native_action={'kind':'choice','dispatched':None,'retained':False}
        ok=await self.page.evaluate(ACTION_JS,{'url':self.url,'key':key,'value':value,'prefix':prefix})
        self.last_native_action['dispatched']=ok is True;require(ok is True,'native_choice_not_retained')
        # Native handlers may replace/uncheck the old node synchronously. Only
        # the settled current controls prove retention; never that detached node.
        out=await self.snapshot()
        # Later dependent choices may reset while their parent changes; the
        # product boundary still checks the complete restored prefix before any receipt.
        good=any(f['key']==key and f.get('selected')==value for f in out['fields'])
        self.last_native_action['retained']=good;require(good,'native_choice_not_retained');return out
    async def quantity(self,s,value,*,on_action=None):
        binding=scope_binding(s)
        require(type(value) is int and value>=1 and (s.get('stock') is None or s['stock']>=value),'native_quantity_not_available')
        await self.check();self.last_action=time.monotonic();self.last_native_action={'kind':'quantity','dispatched':None,'retained':False}
        result=await self.page.evaluate(QTY_JS,{'url':self.url,'expected':s['quantity'],'value':value,'binding':binding})
        reasons={'source_url_changed','native_root_unresolved','native_quantity_control_unresolved','source_sku_changed','source_prefix_changed','native_quantity_bounds_changed'}
        require(type(result)is dict and set(result)=={'ok','dispatched','reason'}and type(result['ok'])is bool and result['dispatched']is result['ok'] and (result['reason']is None if result['ok']else result['reason']in reasons),'native_quantity_result_unresolved')
        self.last_native_action.update(dispatched=result['dispatched'],reason=result['reason'])
        if on_action is not None:on_action(deepcopy(result))
        require(result['ok'],'native_quantity_not_retained');out=await self.snapshot()
        good=out['quantity']==value and scope_binding(out)==binding;self.last_native_action['retained']=good
        require(good,'quantity_source_identity_changed');return out
    async def quantity_two(self,s,*,on_action=None):
        require(s['quantity']==1 and type(s.get('stock')) is int and s['stock']>=2,'native_q2_not_available')
        require(s.get('quantity_max') in (None,'') or str(s['quantity_max']).isdigit() and int(s['quantity_max'])>=2,'native_q2_not_available')
        return await self.quantity(s,2,on_action=on_action)
    async def close(self):
        self.closing=True
        if not self.page.is_closed():await self.page.close()
        for req in list(self.pending):self.finish(req)
        if self.guard_tasks:await asyncio.gather(*list(self.guard_tasks),return_exceptions=True)
        if self.public_source_tasks:await asyncio.gather(*list(self.public_source_tasks),return_exceptions=True)
        if self.release_tasks:await asyncio.gather(*list(self.release_tasks),return_exceptions=True)

class WsdisplaySourceCollector:
    """Durable generic collection lifecycle; maximum2owned WS pages across processes."""
    def __init__(self,store,pool,*,limiter=None):
        self.store,self.pool=store,pool;self.root=Path(store.root)/'wsdisplay-source-collections';self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.root.chmod(0o700)
        self.limiter=limiter or RateLimiter(store);self.tasks={};self.closed=False;self._template_locks={}
        self._template_library=None;self._template_library_lock=asyncio.Lock()
    async def template_library(self,check):
        """Read WSDisplay's public size-indexed template table once per process."""
        if self._template_library is not None:return self._template_library
        async with self._template_library_lock:
            if self._template_library is not None:return self._template_library
            url=ORIGIN+'/templates';await validate_url(url)
            token=await self.limiter.acquire(ORIGIN,1,2,check);started=time.monotonic();status=0
            try:
                async with self.pool.page(ORIGIN,headless=False) as page:
                    response=await page.goto(url,wait_until='domcontentloaded',timeout=30000)
                    status=response.status if response else 0
                    require(status==200,'template_library_unavailable')
                    records=await page.evaluate(r"""() => {
                      const table=[...document.querySelectorAll('table.specsTable')].find(t=>
                        /Diameter 15/.test(t.textContent||'')&&/Tapered Square/.test(t.textContent||''));
                      if(!table)return [];
                      const rows=[...table.rows],out=[];let shape='',widths=[];
                      for(const row of rows){const cells=[...row.cells],first=(cells[0]?.textContent||'').trim();
                        if(cells[0]?.querySelector('b')&&/^(Circle|Square|Triangle|Tapered Circle|Tapered Square|Tapered Triangle|Wave)$/.test(first)){
                          shape=first;widths=cells.map(c=>(c.textContent||'').trim());continue;}
                        const height=/^(\d+)in$/.exec(first);if(!height||!shape)continue;
                        for(let i=1;i<cells.length;i++){const width=/Diameter (\d+)'/.exec(widths[i]||'');
                          const a=cells[i].querySelector('a[href]');if(!width||!a)continue;
                          const u=new URL(a.href);if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash)continue;
                          if(!['display-templates.s3.us-west-2.amazonaws.com','s3-us-west-2.amazonaws.com'].includes(u.hostname))continue;
                          if(!/\.(pdf|tif|tif\.zip)$/i.test(u.pathname))continue;
                          out.push({shape,width_ft:Number(width[1]),height_in:Number(height[1]),url:u.href});
                        }}return out.slice(0,600);
                    }""")
                    require(type(records) is list and 1<=len(records)<=600,'template_library_projection_invalid')
                    self._template_library={(r['shape'].lower(),r['width_ft'],r['height_in']):r['url'] for r in records}
                    return self._template_library
            finally:self.limiter.release(ORIGIN,token,time.monotonic()-started,status,30,1)
    async def library_template_for(self,s,check):
        """Resolve a selected Skybox width/height against the official table."""
        title=str(s.get('title',''))
        width=re.search(r'\b(\d+)\s*-?\s*ft\.?(?=\s|$)',title,re.I)
        shape=next((name for name in ('tapered triangle','tapered square','tapered circle','triangle','square','circle','wave')
                    if re.search(r'\b'+re.escape(name)+r'\b',title,re.I)),None)
        height_field=next((f for f in s.get('fields',[]) if re.search(r'\bheight\b',f.get('label',''),re.I)
                           and f.get('selected')),None)
        if not (width and shape and height_field):return None
        selected=next((c.get('label','') for c in height_field.get('choices',[])
                       if c.get('value')==height_field['selected']), '')
        height=re.search(r'\b(\d+)\s*in\b',selected,re.I)
        if not height:return None
        library=await self.template_library(check)
        return library.get((shape,int(width[1]),int(height[1])))
    async def save_artwork_template(self,url,check):
        """Fetch a rendered WSDisplay PDF or TIF ZIP from its public asset host.

        The file is bounded and hashed; no supplier cookies or account data are
        sent to the asset host. A cache hit never repeats a supplier request.
        """
        parsed=urlsplit(url)
        relative=(parsed.path[len('/display-templates'):] if parsed.hostname=='s3-us-west-2.amazonaws.com'
                  and parsed.path.startswith('/display-templates/') else parsed.path)
        approved=(parsed.hostname in {'display-templates.s3.us-west-2.amazonaws.com','s3-us-west-2.amazonaws.com'}
                  and (re.fullmatch(r'/PDF/[A-Za-z0-9._+%\-]+\.(?:pdf|tif)',relative,re.I)
                       or re.fullmatch(r'/PSB/[A-Za-z0-9._+%\-]+\.tif\.zip',relative,re.I)))
        require(parsed.scheme=='https' and approved and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment, 'template_source_unapproved')
        is_zip=relative.lower().endswith('.tif.zip');is_tif=relative.lower().endswith('.tif')
        limit=(256 if is_zip or is_tif else 64)*1024*1024
        magics=(b'PK\x03\x04',) if is_zip else ((b'II*\x00',b'MM\x00*') if is_tif else (b'%PDF-',))
        kind_name='zip' if is_zip else 'tif' if is_tif else 'pdf'
        key=hashlib.sha256(url.encode()).hexdigest();folder=self.root/'templates';folder.mkdir(exist_ok=True,mode=0o700);folder.chmod(0o700)
        path=folder/(key+('.zip' if is_zip else '.tif' if is_tif else '.pdf'))
        lock=self._template_locks.setdefault(key,asyncio.Lock())
        async with lock:
            if path.is_file():
                require(path.stat().st_size<=limit,'template_cache_invalid')
                body=path.read_bytes()
                if len(body)<=limit and body.startswith(magics):
                    return {'status':'verified_'+kind_name,'path':str(path.relative_to(self.store.root)),'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body)}
                raise Held('template_cache_invalid')
            await validate_url(url)
            origin='https://'+parsed.hostname
            token=await self.limiter.acquire(origin,1,2,check);started=time.monotonic();status=0
            try:
                async with httpx.AsyncClient(follow_redirects=False,timeout=25) as client:
                    accept='image/tiff' if is_tif else 'application/zip' if is_zip else 'application/pdf'
                    async with client.stream('GET',url,headers={'Accept':accept,'User-Agent':'Hermes-TemplateCollector/1.0'}) as response:
                        status=response.status_code;require(status==200,'template_fetch_unavailable')
                        kind=response.headers.get('content-type','').split(';',1)[0].strip().lower()
                        expected=({'application/zip','application/x-zip-compressed','application/octet-stream'} if is_zip
                                  else {'image/tiff','image/tif','application/octet-stream'} if is_tif
                                  else {'application/pdf','application/octet-stream'})
                        require(kind in expected,'template_content_type_invalid')
                        size=response.headers.get('content-length')
                        require(size is None or size.isdigit() and int(size)<=limit,'template_size_invalid')
                        body=bytearray()
                        async for chunk in response.aiter_bytes():
                            await check();body.extend(chunk);require(len(body)<=limit,'template_size_invalid')
                require(body.startswith(magics),'template_file_invalid')
                tmp=folder/(key+'.'+uid()+'.tmp');tmp.write_bytes(body);tmp.chmod(0o600);os.replace(tmp,path)
                return {'status':'verified_'+kind_name,'path':str(path.relative_to(self.store.root)),'sha256':hashlib.sha256(body).hexdigest(),'bytes':len(body)}
            finally:self.limiter.release(origin,token,time.monotonic()-started,status,30,1)
    def path(self,jid):
        require(type(jid) is str and re.fullmatch('[a-f0-9]{32}',jid),'unknown_collection');return self.root/(jid+'.json')
    def save(self,j):
        j['updated_at']=now();p=self.path(j['id']);t=p.with_name(p.name+'.'+uid()+'.tmp')
        fd=os.open(t,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w')as f:json.dump(j,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(t,p)
    def load(self,jid):
        try:return json.loads(self.path(jid).read_text())
        except FileNotFoundError:raise ValueError('Unknown source collection') from None
    def active(self,jid):
        fd=os.open(self.root/(jid+'.lock'),os.O_CREAT|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
            fcntl.flock(fd,fcntl.LOCK_UN);return False
        finally:os.close(fd)
    def read_observation_bounded(self,ref,budget):
        """Local-only, pre-stat and capped reads BEFORE JSON parsing. No S3 fetch."""
        if type(ref)is not dict or not re.fullmatch('[a-f0-9]{64}',str(ref.get('sha256',''))):return None,0,'invalid_reference'
        key=ref.get('object_path');sha=ref['sha256']
        if type(key)is not str:return None,0,'invalid_reference'
        parts=Path(key).parts;filename=sha+'.wsdisplay-dom-observation.json'
        if not (parts==('objects',filename)or parts==('objects',sha[:2],filename)):return None,0,'invalid_reference'
        if budget<=0:return None,0,'job_byte_budget'
        root=Path(self.store.root).resolve();path=root/key
        try:
            resolved=path.resolve()
            if not resolved.is_relative_to(root/'objects')or not resolved.is_relative_to(root):return None,0,'invalid_reference'
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        except OSError:return None,0,'local_unavailable'
        read_bytes=0
        try:
            before=os.fstat(fd);size=before.st_size
            if not stat.S_ISREG(before.st_mode)or size<1:return None,0,'invalid_local_object'
            if size>MAX_OBSERVATION_OBJECT_BYTES:return None,0,'object_byte_bound'
            if size>budget:return None,0,'job_byte_budget'
            chunks=[]
            while read_bytes<size:
                chunk=os.read(fd,min(65536,size-read_bytes))
                if not chunk:return None,read_bytes,'short_local_read'
                read_bytes+=len(chunk);chunks.append(chunk)
            after=os.fstat(fd)
            if after.st_size!=size or after.st_mtime_ns!=before.st_mtime_ns:return None,read_bytes,'local_object_changed'
            data=b''.join(chunks)
            if hashlib.sha256(data).hexdigest()!=sha:return None,read_bytes,'digest_mismatch'
            try:value=json.loads(data)
            except (ValueError,UnicodeDecodeError,RecursionError):return None,read_bytes,'invalid_json'
            if type(value)is not dict or value.get('schema')!='wsdisplay-native-dom-observation/v1'or value.get('valid_quote')is not False:return None,read_bytes,'invalid_observation'
            return value,read_bytes,None
        except OSError:return None,read_bytes,'local_read_error'
        finally:os.close(fd)
    def get(self,jid,*,include_data=False):
        j=self.load(jid);j['worker_active']=self.active(jid)
        if j['status']=='running' and not j['worker_active']:j['status']='interrupted'
        j['planned_pages']=len(j['pages']);j['completed_pages']=sum(p['status']=='completed' for p in j['pages']);j['resumable']=j['status']!='completed'
        remaining=MAX_OBSERVATION_PREVIEW_BYTES;total={'available_count':0,'loaded_count':0,'loaded_bytes':0,'read_bytes':0,'max_per_product':MAX_OBSERVATION_PREVIEW_PER_PRODUCT,'byte_limit':MAX_OBSERVATION_PREVIEW_BYTES,'local_only':True,'truncated':False}
        for p in j['pages']:
            p.pop('frontier',None);p.pop('seen',None);p.pop('basis_witnesses',None);p.pop('q2_reservations',None)
            if include_data:
                # Existing parsed price receipt data remains byte/shape compatible.
                p['data']=[json.loads(self.store.get_object(x['object_path'])) for x in p['receipts']]
                refs=p.get('evidence_snapshots',[]);meta={'available_count':len(refs),'loaded_count':0,'loaded_bytes':0,'read_bytes':0,'max_recent':MAX_OBSERVATION_PREVIEW_PER_PRODUCT,'truncated':False,'omitted_counts':{}}
                p['observations_data']=[];older=max(0,len(refs)-MAX_OBSERVATION_PREVIEW_PER_PRODUCT)
                if older:meta['omitted_counts']['older_than_recent_limit']=older
                # Most recent first gets the budget. Reverse successful results
                # back to chronological order for existing diagnostic clients.
                for ref in reversed(refs[-MAX_OBSERVATION_PREVIEW_PER_PRODUCT:]):
                    value,used,reason=self.read_observation_bounded(ref,remaining);remaining-=used;meta['read_bytes']+=used
                    if reason:meta['omitted_counts'][reason]=meta['omitted_counts'].get(reason,0)+1;continue
                    p['observations_data'].insert(0,value);meta['loaded_count']+=1;meta['loaded_bytes']+=used
                meta['truncated']=meta['loaded_count']<len(refs);p['observations_preview']=meta
                for key in ('available_count','loaded_count','loaded_bytes','read_bytes'):total[key]+=meta[key]
                total['truncated']|=meta['truncated']
        if include_data:j['observations_preview']=total
        j.pop('seeds',None);j.pop('account_marker',None);return j
    def list(self,limit=20):return[self.get(p.stem) for p in sorted(self.root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:max(1,min(limit,100))]]
    async def start(self,urls,options):
        require(not self.closed and self.pool is not None,'native_collector_unavailable');require(type(urls) is list and 1<=len(urls)<=28 and all(type(u)is str for u in urls),'supply_1_to_28_source_urls')
        urls=list(dict.fromkeys(urls));marker,seeds,bound,bodies=reviewed_witnesses(options)
        for u in urls:page_url(u);await validate_url(u)
        for w in seeds.values():
            body=bodies[w['evidence_sha256']];obj=self.store.put_object(body,'wsdisplay-reviewed-dom.json');w['source_object_path']=obj;local=Path(self.store.root)/obj
            if local.exists():local.chmod(0o600)
        j={'schema':'hermes-native-source-collection/v1','id':uid(),'profile_id':PROFILE_ID,'status':'queued','created_at':now(),'updated_at':now(),'account_marker':marker,'seeds':seeds,'native_q2_checks':options.get('native_q2_checks',False),'template_only':options.get('template_only',False),'max_nodes_per_product':bound,'source_concurrency':2,'requests_per_second':1,'catalog_complete':False,'source_data_finalized':False,'error':None,'pages':[{'url':u,'status':'pending','frontier':[{}],'seen':[],'pending':None,'receipts':[],'holds':[],'basis_witnesses':{},'q2_reservations':{},'nodes':0}for u in urls]}
        self.save(j);self.schedule(j['id']);return self.get(j['id'])
    def schedule(self,jid,*,urls=None,max_nodes_per_product=None):
        require(not self.closed,'collector_stopped')
        if jid not in self.tasks or self.tasks[jid].done():
            t=asyncio.create_task(self.run(jid,urls=urls,max_nodes_per_product=max_nodes_per_product));self.tasks[jid]=t;t.add_done_callback(lambda done:self.tasks.pop(jid,None) if self.tasks.get(jid)is done else None)
    async def resume(self,jid,*,urls=None,max_nodes_per_product=None):
        j=self.load(jid);selected=resume_scope(j,urls,max_nodes_per_product=max_nodes_per_product,node_limit_max=MAX_NODES)
        if selected is not None and ((jid in self.tasks and not self.tasks[jid].done())or self.active(jid)):
            raise ValueError('Pause the source collection before a selected-page resume')
        if j['status']!='completed'and (selected is None or any(p['status']!='completed'for p in scoped_pages(j,selected))):
            self.schedule(jid,urls=selected,max_nodes_per_product=max_nodes_per_product)
        return self.get(jid)
    async def pause(self,jid):
        t=self.tasks.get(jid)
        if t:t.cancel();await asyncio.gather(t,return_exceptions=True)
        else:
            require(not self.active(jid),'collection_owned_elsewhere');j=self.load(jid)
            if j['status']!='completed':j['status']='paused';self.save(j)
        return self.get(jid)
    async def close(self):
        self.closed=True
        for t in list(self.tasks.values()):t.cancel()
        await asyncio.gather(*list(self.tasks.values()),return_exceptions=True)
    @asynccontextmanager
    async def slot(self,check):
        fd=None
        while fd is None:
            await check()
            for n in range(2):
                f=os.open(self.root/f'.page-slot-{n}.lock',os.O_CREAT|os.O_RDWR,0o600)
                try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:os.close(f)
                else:fd=f;break
            if fd is None:await asyncio.sleep(.1)
        try:yield
        finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
    def record(self,j,p,data,prefix=None):
        payload=canonical(data).encode();key=self.store.put_object(payload,'wsdisplay-native.json');local=Path(self.store.root)/key
        if local.exists():local.chmod(0o600)
        p['receipts'].append({'object_path':key,'sha256':hashlib.sha256(payload).hexdigest(),'binding_sha256':digest(data.get('binding',{})),'observed_at':data.get('observed_at'),'holds':data.get('holds',[]),'prefix_sha256':digest(prefix) if prefix is not None else None})
        require(len(p['receipts'])<=MAX_RECORDS,'record_bound');self.save(j)
    def observe(self,j,p,s,native,prefix,phase):
        require(phase in {'initial_root','before_receipt','before_q2','after_q2','failure','template_selection'},'observation_phase_invalid')
        data=native_observation(s,native.blocked_requests,native.observed_item_requests);data['phase']=phase;payload=canonical(data).encode()
        require(len(payload)<=2*1024*1024,'observation_byte_bound')
        refs=p.setdefault('evidence_snapshots',[]);require(len(refs)<MAX_RECORDS*3,'observation_count_bound')
        obj=self.store.put_object(payload,'wsdisplay-dom-observation.json');local=Path(self.store.root)/obj
        if local.exists():local.chmod(0o600)
        ref={'object_path':obj,'sha256':hashlib.sha256(payload).hexdigest(),'phase':phase,'prefix_sha256':digest(prefix)if prefix is not None else None,'valid_quote':False}
        refs.append(ref);self.save(j);return ref
    def save_root_catalogue(self,j,p,s):
        pi=s.get('product_inspection');catalogue=pi.pop('catalogue',None)if type(pi)is dict else None
        if catalogue is None:return None
        require(not p.get('native_catalogue_ref'),'root_catalogue_already_saved')
        require(type(catalogue)is dict and catalogue.get('schema')=='wsdisplay-native-child-catalogue/v1'and catalogue.get('valid_quote')is False and catalogue.get('source_data_finalized')is False,'root_catalogue_schema_invalid')
        catalogue['observed_at']=now();payload=canonical(catalogue).encode();require(len(payload)<=2*1024*1024,'root_catalogue_byte_bound')
        obj=self.store.put_object(payload,'wsdisplay-child-catalogue.json');local=Path(self.store.root)/obj
        if local.exists():local.chmod(0o600)
        ref={'object_path':obj,'sha256':hashlib.sha256(payload).hexdigest(),'schema':catalogue['schema'],'rows':len(catalogue['rows']),'complete_projection':catalogue['complete_projection'],'valid_quote':False}
        p['native_catalogue_ref']=ref;self.save(j);return ref
    async def product(self,j,p,check,*,node_limit=None):
        if p['status']=='completed':return
        if p['pending'] is not None:
            key=digest(p['pending'])
            if any(r.get('prefix_sha256')==key for r in p['receipts']):
                if key not in p['seen']:p['seen'].append(key)
            else:p['frontier'].append(p['pending'])
            p['interruptions']=p.get('interruptions',0)+1;p['pending']=None;self.save(j)
        async with self.slot(check):
            async with self.pool.page(ORIGIN,headless=False)as page:
                native=NativeSession(page,self.limiter,p['url'],j['account_marker'],check)
                try:
                    p['status']='collecting';self.save(j);await native.open();initial=not p.get('native_catalogue_ref');s=await native.snapshot(capture_catalogue=initial)
                    if initial:
                        self.save_root_catalogue(j,p,s)
                        self.observe(j,p,s,native,None,'initial_root')
                    # A template belongs to the rendered matrix child. Warehouse,
                    # turnaround, and PMS controls describe fulfillment/services;
                    # traversing their Cartesian product repeats the same PDF.
                    # Rebuild only this template frontier from persisted receipts
                    # after a pause, so already observed children are not replayed.
                    template_field=next((f for f in s['fields'] if f['key']=='custcol_matrix_order_detail_option'
                                         and not f['disabled']),None) if j.get('template_only') else None
                    if template_field is not None:
                        children=[c['value'] for c in template_field['choices'] if c['value'] and not c['disabled']]
                        # Rebuild child prefixes, then skip exact persisted leaf
                        # receipts. A dimension may appear only after the child
                        # is selected, so a child-level receipt is not proof that
                        # every artwork size has been covered.
                        p['frontier']=[{template_field['key']:v}for v in reversed(children)]
                        p['seen']=[]
                        p['template_child_scope']={'field':template_field['key'],'observed_children':len(children)-len(p['frontier']),
                                                   'remaining_children':len(p['frontier'])}
                        self.save(j)
                    base_qty=int(s['quantity_min']) if str(s.get('quantity_min','')).isdigit() and int(s['quantity_min'])>0 else s['quantity']
                    while p['frontier']:
                        await check();prefix=p['frontier'][-1];key=digest(prefix)
                        if key in p['seen'] or any(r.get('prefix_sha256')==key for r in p['receipts']):
                            p['frontier'].pop();continue
                        require(p['nodes']<(j['max_nodes_per_product']if node_limit is None else node_limit),'native_frontier_bound')
                        p['frontier'].pop();p['pending']=prefix;self.save(j)
                        try:
                            # Never carry a q2 probe into another source child/context.
                            # Restore the source's actual minimum before a new finite path.
                            base_qty=int(s['quantity_min']) if str(s.get('quantity_min','')).isdigit() and int(s['quantity_min'])>0 else s['quantity']
                            if s['quantity']!=base_qty and not s.get('out_of_stock'):s=await native.quantity(s,base_qty)
                            for fkey,value in prefix.items():
                                f=next((f for f in s['fields'] if f['key']==fkey),None)
                                require(f is not None and any(c['value']==value and not c['disabled'] for c in f['choices']),'source_prefix_unavailable')
                                if f['selected']!=value:
                                    retained={k:v for k,v in prefix.items()if k!=fkey and any(x['key']==k and x['selected']==v for x in s['fields'])}
                                    s=await native.choose(fkey,value,retained)
                            require(all(any(f['key']==k and f['selected']==v for f in s['fields'])for k,v in prefix.items()),'source_prefix_not_retained')
                            field=(next((f for f in s['fields'] if f['key'] not in prefix
                                         and f['kind']=='select' and not f.get('metadata_only') and not f['disabled']
                                         and f['key']!='custcol_matrix_order_detail_option'
                                         and re.search(r'\b(?:size|height|width|diameter|dimension|orientation)\b',f['label'],re.I)
                                         and any(c['value'] and not c['disabled'] for c in f['choices'])),None)
                                   if j.get('template_only') else next((f for f in s['fields'] if f['key'] not in prefix and not f.get('metadata_only') and not f['disabled'] and any(c['value'] and not c['disabled'] for c in f['choices'])),None))
                            if field is not None:
                                require(len(prefix)<MAX_DEPTH,'native_frontier_depth')
                                for c in reversed(field['choices']):
                                    if c['value'] and not c['disabled']:p['frontier'].append({**prefix,field['key']:c['value']})
                                require(len(p['frontier'])<=MAX_NODES,'native_frontier_bound')
                            elif j.get('template_only'):
                                self.observe(j,p,s,native,prefix,'template_selection')
                                resources=deepcopy(s.get('artwork_resources',[]))
                                child_field=next((f for f in s['fields'] if f['key']=='custcol_matrix_order_detail_option'),None)
                                child_label=next((c['label'] for c in child_field['choices'] if c['value']==child_field['selected']), '') if child_field else ''
                                hardware_only=bool(re.search(r'\bhardware\s+only\b',child_label,re.I))
                                if hardware_only:
                                    resources=[r for r in resources if r.get('kind')!='template']
                                else:
                                    native_links=[r['url'] for r in resources if r.get('kind')=='template']
                                    need_library=(not native_links or any(u.lower().endswith('.tif.zip') for u in native_links))
                                    library_url=await self.library_template_for(s,check) if need_library else None
                                    if library_url and not native_links:
                                        resources.append({'kind':'template','url':library_url,'source':'wsdisplay_official_template_library'})
                                    elif library_url and library_url not in native_links and library_url.lower().endswith('.tif'):
                                        resources=[r for r in resources if r.get('kind')!='template']
                                        resources.append({'kind':'template','url':library_url,'source':'wsdisplay_official_template_library',
                                                          'pdp_template_url':native_links[0]})
                                for resource in resources:
                                    if resource.get('kind')!='template':continue
                                    try:resource['file']=await self.save_artwork_template(resource['url'],check)
                                    except Held as exc:resource['file']={'status':'unavailable','reason':str(exc)}
                                    except (httpx.HTTPError,OSError):resource['file']={'status':'unavailable','reason':'template_fetch_unavailable'}
                                data={'schema':'wsdisplay-native-template-observation/v1','observed_at':now(),
                                      'binding':scope_binding(s),'artwork_resources':resources,
                                      'template_choice_scope':'matrix_child_and_dimensions' if template_field is not None and len(prefix)>1 else 'matrix_child_only' if template_field is not None else 'template_dimensions_or_single',
                                      'template_status':'source_link_observed' if any(r.get('kind')=='template' for r in resources) else 'not_applicable_hardware_only' if hardware_only else 'unresolved',
                                      'asset_files_downloaded':any(r.get('kind')=='template' for r in resources) and all(r.get('file',{}).get('status') in {'verified_pdf','verified_zip','verified_tif'} for r in resources if r.get('kind')=='template'),
                                      'valid_quote':False,'retail_ready':False,'source_data_finalized':False}
                                self.record(j,p,data,prefix)
                                if data['template_status']=='unresolved':p['holds'].append({'reason':'template_unresolved','prefix':prefix})
                            elif s['required_text']:
                                self.observe(j,p,s,native,prefix,'before_receipt')
                                data={'schema':'wsdisplay-native-required-service-hold/v1','observed_at':now(),'binding':scope_binding(s),'required_text':s['required_text'],'production':s['production'],'packing':s['packing'],'holds':['required_text_service'],'retail_ready':False,'source_data_finalized':False}
                                self.record(j,p,data,prefix);p['holds'].append({'reason':'required_text_service','prefix':prefix,'fields':s['required_text']})
                            else:
                                self.observe(j,p,s,native,prefix,'before_receipt')
                                binding=scope_binding(s);bkey=digest(binding);w=p['basis_witnesses'].get(bkey,j['seeds'].get(bkey))
                                prior_probe=p.setdefault('q2_reservations',{}).get(bkey)
                                if not w and not prior_probe and j['native_q2_checks'] and s['quantity']==1 and type(s.get('stock'))is int and s['stock']>=2 and not s.get('out_of_stock'):
                                    self.observe(j,p,s,native,prefix,'before_q2')
                                    p['q2_reservations'][bkey]={'state':'inflight','quantity':2,'started_at':now(),'action_contract':'wsdisplay-native-quantity-v2'};self.save(j)
                                    def remember_action(result):
                                        p['q2_reservations'][bkey].update(state='dispatched'if result['dispatched']else'not_dispatched',action_result=result,action_observed_at=now());self.save(j)
                                    s=await native.quantity_two(s,on_action=remember_action);require(scope_binding(s)==binding,'q2_source_identity_changed')
                                    self.observe(j,p,s,native,prefix,'after_q2')
                                data=receipt(s,w)
                                if prior_probe and not w and s['quantity']<2:data['holds'].append('prior_q2_outcome_uncertain')
                                self.record(j,p,data,prefix)
                                if s['quantity']>=2:
                                    p['q2_reservations'][bkey]={'state':'complete','quantity':s['quantity'],'evidence_sha256':digest(data)}
                                    p['basis_witnesses'][bkey]={'kind':'native_quantity_line_total','quantity':s['quantity'],'observed_at':data['observed_at'],'binding':binding,'unit_cost':data['unit_cost'],'native_total':data['native_total'],'basis_only_not_new_quote':True,'evidence_sha256':digest(data)}
                        except Held as exc:
                            if str(exc) not in {'source_prefix_unavailable','source_prefix_not_retained','native_choice_not_retained','band_table_unresolved','selected_source_sku_unresolved'}:raise
                            hold={'reason':str(exc),'prefix':prefix}
                            if str(exc)in{'band_table_unresolved','selected_source_sku_unresolved'}:
                                # Complete finite selection was observed before parsing.
                                # Preserve it as a typed held leaf, never a price receipt.
                                refs=p.get('evidence_snapshots',[])
                                require(bool(refs),'parser_hold_missing_observation');hold.update(evidence_sha256=refs[-1]['sha256'],valid_quote=False)
                            p['holds'].append(hold)
                        p['nodes']+=1;p['seen'].append(key);p['pending']=None;p['network_counts']=deepcopy(native.counts);self.save(j)
                    p['status']='completed';p['domain_exhausted']=True;p['coverage_status']='partial_held_selections' if p['holds'] or any(x['holds']for x in p['receipts']) else 'native_finite_choices_observed';self.save(j)
                except Held as exc:
                    # A parsing failure must not discard already-arrived source
                    # evidence. Revalidate auth/readiness before preserving only
                    # the closed projection; account/challenge failures stop.
                    try:
                        latest=normalize_snapshot(native.last_readiness,p['url'],j['account_marker'])
                        self.observe(j,p,latest,native,p.get('pending'),'failure')
                    except Held:pass
                    if exc.diagnostic is None:exc.diagnostic=native.diagnostic()
                    if p.get('evidence_snapshots'):exc.diagnostic['last_observation_sha256']=p['evidence_snapshots'][-1]['sha256']
                    raise
                except Exception as exc:
                    diag=native.diagnostic();kind=type(exc).__name__;diag['exception_type']=kind if kind in {'TimeoutError','Error','ValueError','OSError','RuntimeError','TypeError'} else 'Other'
                    raise Held('native_page_collection_stopped',diag) from None
                finally:await native.close()
    async def run(self,jid,*,urls=None,max_nodes_per_product=None):
        fd=os.open(self.root/(jid+'.lock'),os.O_CREAT|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            j=self.load(jid);selected=resume_scope(j,urls,max_nodes_per_product=max_nodes_per_product,node_limit_max=MAX_NODES);pages=scoped_pages(j,selected)
            if selected is not None and all(p['status']=='completed'for p in pages):return
            record_resume_scope(j,selected,max_nodes_per_product,now())
            j['status']='running';j['error']=None;self.save(j);halt=None
            async def check():
                if self.closed:raise asyncio.CancelledError()
                if halt:raise Held(halt)
            sem=asyncio.Semaphore(2)
            async def one(p):
                nonlocal halt
                async with sem:
                    if p['status']=='completed':return
                    try:
                        if max_nodes_per_product is None:await self.product(j,p,check)
                        else:await self.product(j,p,check,node_limit=max_nodes_per_product)
                    except Held as exc:
                        reason=str(exc);p['status']='held';p['error']=reason
                        if exc.diagnostic is not None:p['last_failure_diagnostic']=exc.diagnostic
                        if reason in {'source_access_required','source_challenge','source_account_marker_required','source_backoff_required'}:halt=reason
                        self.save(j)
                    except asyncio.CancelledError:raise
                    except Exception:
                        p['status']='held';p['error']='native_page_collection_stopped';self.save(j)
            children=[asyncio.create_task(one(p))for p in pages if p['status']!='completed']
            try:
                await asyncio.gather(*children);j['status']='completed' if all(p['status']=='completed'for p in j['pages'])else'attention';j['error']=halt;self.save(j)
            except asyncio.CancelledError:
                for child in children:child.cancel()
                await asyncio.gather(*children,return_exceptions=True)
                j['status']='paused';self.save(j);raise
            except Exception:j['status']='attention';j['error']='native_collection_stopped';self.save(j)
        finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
