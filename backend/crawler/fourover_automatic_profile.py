"""Automatic finite 4over package collection using one owned browser per batch.

Saved catalogs establish product membership, not current prices. Only normal
source-generated requests can establish current totals. No orders or shipping.
"""
from __future__ import annotations
import asyncio
from math import isfinite
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlsplit
from .adapters.browser import browser_slot
from .fourover_child_prices import FourOverChildPriceCollector, _encoded, _now, _url
from .fourover_options import _bind, _unique_object, inspect_option_contract
from .fourover_bulk import inspect_child_configurations, child_configuration_limit
from .fourover_native_observation import (NativeObserver,Held,STATE_JS,ATTRIBUTE_DIAGNOSTIC_JS,POPUP_GUARD_JS,
    attributes_retained,services_off,context_retained,request_context,q1000_context_retained,parse_summary,bounded_prices,
    document_static_prefix,generic_public_template_suffix,public_static_relative_path,bootstrap_static_body_metadata,
    NATIVE_STATIC_ASSETS,STATIC_XHR_MAX_RESPONSE,STATIC_XHR_MAX_TOTAL,ASSET_DIAGNOSIS_STATIC_LIMIT,
    NATIVE_STATIC_CONSUMERS_PER_ASSET,NATIVE_STATIC_CONSUMERS_TOTAL,source_declares_tracker,known_tracker_request,blocked_request_classification,safe_exception_type)
from .pacing import retry_after_seconds, source_pacing
from .transport import validate_url

PROFILE_ID='fourover_configurable_package_v16'
SCOPE={'sets':1,'job_sample':False,'digital_proof':False}
MAX_DOMAIN=100000
MAX_QUOTES=128
MAX_CATALOG_BYTES=8*1024*1024
QUARANTINED={'https://4over.com/16pt-postcards':frozenset({'28749'}),
 'https://4over.com/specialty-folds-brochures':frozenset({'148252','150616'})}
HISTORICAL_SPENT={'https://4over.com/z-fold-brochures':frozenset({('160399',500),('160492',500),('160495',500),('160495',1000),('160495',2000)})}
ID=re.compile(r'[1-9][0-9]{0,12}\Z')
HEX=re.compile(r'[a-f0-9]{64}\Z')

def digest(raw):return hashlib.sha256(raw).hexdigest()

def safe_read(path,root,limit):
    path=Path(path);root=Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in path.parents if p!=root and root in p.parents):
        raise Held('catalog_path_invalid')
    if not path.is_file() or path.stat().st_size>limit:raise Held('catalog_unavailable')
    raw=path.read_bytes()
    if len(raw)>limit:raise Held('catalog_limit')
    return raw

def association_context(association):
    if (type(association) is not dict or set(association)!={'campaign_id','product_id','supplier','source_url'}
            or association.get('supplier') not in {'4over','fourover'}
            or any(type(association.get(k)) is not str or not 1<=len(association[k])<=160 or any(ord(c)<32 for c in association[k]) for k in ('campaign_id','product_id'))):
        raise Held('association_invalid')
    try:parts=_url(association['source_url'])
    except (ValueError,TypeError,KeyError):raise Held('source_url_unsupported') from None
    if parts.netloc!='4over.com' or parts.path.startswith('/all-inclusive-'):raise Held('source_family_unsupported')
    return {**association,'supplier':'4over'}

def supports(association):
    try:association_context(association);return True
    except Held:return False

def normalize_configuration(configuration):
    if type(configuration) is not dict or set(configuration)!={'scope','catalog_reference','max_quotes_per_session'}:raise Held('configuration_invalid')
    validate_scope(configuration['scope']);reference=configuration['catalog_reference'];limit=configuration['max_quotes_per_session']
    if (type(reference) is not dict or set(reference)!={'snapshot_id','checkpoint_sha256','option_rows_sha256'}
            or type(reference.get('snapshot_id')) is not str or not re.fullmatch('[a-f0-9]{32}',reference['snapshot_id'])
            or any(type(reference.get(k)) is not str or not HEX.fullmatch(reference[k]) for k in ('checkpoint_sha256','option_rows_sha256'))
            or type(limit) is not int or not 1<=limit<=MAX_QUOTES):raise Held('configuration_invalid')
    return {'scope':dict(SCOPE),'catalog_reference':dict(reference),'max_quotes_per_session':limit}

def validate_scope(value):
    if type(value) is not dict or value!=SCOPE or type(value.get('sets')) is not int or value.get('job_sample') is not False or value.get('digital_proof') is not False:
        raise Held('scope_unsupported')
    return dict(SCOPE)

def semantic_key(source_url,child_id,quantity):
    return digest(_encoded({'source_url':source_url,'child':child_id,'run_size':quantity,'scope':SCOPE}))

def spent(root,source_url,child_id,quantity):
    if (child_id,quantity) in HISTORICAL_SPENT.get(source_url,()):return True
    key=semantic_key(source_url,child_id,quantity)
    path=Path(root)/'fourover-native-observation-selections'/key[:2]/(key+'.json')
    return path.exists() or path.is_symlink()

def reserve_once(root,source_url,selection,journal_key):
    child=selection.get('child_id');q=selection.get('run_size');attrs=selection.get('attributes')
    if (type(child) is not str or not ID.fullmatch(child) or type(q) is not int or not 1<=q<=10**12
            or type(attrs) is not dict or not 1<=len(attrs)<=32
            or any(type(k) is not str or not ID.fullmatch(k) or type(v) is not str or not ID.fullmatch(v) for k,v in attrs.items())
            or not HEX.fullmatch(journal_key)):
        raise Held('selection_invalid')
    association_context({'campaign_id':'reservation','product_id':'reservation','supplier':'fourover','source_url':source_url})
    if spent(root,source_url,child,q):return False
    root=Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():raise Held('reservation_root_invalid')
    key=semantic_key(source_url,child,q);flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|os.O_NOFOLLOW
    descriptors=[]
    try:
        descriptor=os.open(root,flags);descriptors.append(descriptor)
        for component in ('fourover-native-observation-selections',key[:2]):
            try:os.mkdir(component,mode=0o700,dir_fd=descriptor)
            except FileExistsError:pass
            descriptor=os.open(component,flags,dir_fd=descriptor);descriptors.append(descriptor)
            if os.fstat(descriptor).st_mode&0o077:raise Held('reservation_directory_invalid')
        try:fd=os.open(key+'.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=descriptor)
        except FileExistsError:return False
        with os.fdopen(fd,'wb') as out:
            out.write(_encoded({'schema':'fourover-native-selection-reservation/v1','key':key,'journal_key':journal_key,'selection':selection,'source_url':source_url,'automatic_profile':PROFILE_ID,'at':_now()}));out.flush();os.fsync(out.fileno())
        for descriptor in descriptors:os.fsync(descriptor)
        return True
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)

def catalog_bindings(job,source):
    children=job.get('declared_children');contract=job.get('option_contract') or {};limit=child_configuration_limit(source)
    if (job.get('schema')!='hermes-fourover-child-price-collection/v1' or job.get('source_url')!=source
            or job.get('status')!='completed_requested_subset' or job.get('error')
            or type(children) is not list or not 1<=len(children)<=limit or contract.get('binding_complete') is not True
            or job.get('document_body_sha256')!=job.get('option_contract_document_sha256')
            or not HEX.fullmatch(str(job.get('document_body_sha256','')))):
        raise Held('catalog_not_usable')
    validate_scope(job.get('scope'))
    ids=[v.get('child_id') for v in children if type(v) is dict]
    if len(ids)!=len(children) or len(set(ids))!=len(ids) or any(type(v) is not str or not ID.fullmatch(v) for v in ids):raise Held('catalog_children_invalid')
    try:
        attrs={a['id']:a for a in contract['attributes']};rows=contract['rows']
        if not 1<=len(attrs)<=32:raise ValueError()
        index={r['child_id']:{f['attribute_id']:f['value'] for f in r['fields']} for r in rows}
        _,rebuilt=_bind({'attributes':attrs,'index':index},ids,limit)
    except (ValueError,TypeError,KeyError):raise Held('catalog_bindings_invalid') from None
    if rebuilt!=rows:raise Held('catalog_bindings_invalid')
    return children,rows

def load_catalog(store,association,reference=None):
    association=association_context(association);source=association['source_url'];root=Path(store.root)
    directory=root/'fourover-child-prices';name=digest(source.encode())+'.json'
    candidates=[]
    for count,path in enumerate(sorted(directory.glob('*/'+name)),1):
        if count>256:raise Held('catalog_checkpoint_limit')
        if not re.fullmatch('[a-f0-9]{32}',path.parent.name):continue
        raw=safe_read(path,root,MAX_CATALOG_BYTES)
        try:job=json.loads(raw,object_pairs_hook=_unique_object);children,rows=catalog_bindings(job,source)
        except (ValueError,TypeError,Held):continue
        if job.get('snapshot_id')!=path.parent.name:continue
        ref={'snapshot_id':path.parent.name,'checkpoint_sha256':digest(raw),'option_rows_sha256':digest(_encoded(rows))}
        if reference is not None and reference!=ref:continue
        candidates.append((str(job.get('updated_at','')),path,raw,job,children,rows,ref))
    if not candidates:raise Held('catalog_not_available')
    _,path,raw,job,children,rows,ref=max(candidates,key=lambda v:(v[0],v[1].parent.name))
    observations=job.get('observations')
    if type(observations) is not dict:raise Held('catalog_observations_invalid')
    entries=[];excluded=[];total_bytes=0;total_domain=0
    row_index={row['child_id']:row for row in rows}
    for child in children:
        cid=child['child_id'];record=observations.get(cid)
        if cid in QUARANTINED.get(source,()) or not isinstance(record,dict) or record.get('status')!='collected':
            excluded.append(cid);continue
        if record.get('child_id')!=cid:raise Held('catalog_price_provenance_invalid')
        sha=record.get('sha256');relative=record.get('object_path')
        if type(sha) is not str or not HEX.fullmatch(sha) or relative!=f'objects/{sha[:2]}/{sha}.fourover-child-price.json':raise Held('catalog_price_provenance_invalid')
        body=safe_read(root/relative,root,2*1024*1024);total_bytes+=len(body)
        if total_bytes>96*1024*1024 or digest(body)!=sha:raise Held('catalog_price_provenance_invalid')
        try:value=json.loads(body,object_pairs_hook=_unique_object)
        except (ValueError,TypeError):raise Held('catalog_price_invalid') from None
        if value.get('source_url')!=source or value.get('child_id')!=cid:raise Held('catalog_price_context_invalid')
        validate_scope(value.get('scope'))
        package=value.get('package_prices');tiers=value.get('tier_prices')
        if type(package) is not list or not 1<=len(package)<=1000 or tiers!=[]:raise Held('package_family_unsupported')
        prices={}
        for row in package:
            q=row.get('quantity');text=row.get('price')
            if type(q) is not int or not 1<=q<=10**12 or q in prices or type(text) is not str or not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,18})?',text):raise Held('catalog_price_invalid')
            amount=Decimal(text)
            if not amount.is_finite() or amount<=0:raise Held('catalog_price_invalid')
            prices[q]=text
        total_domain+=len(prices)
        if total_domain>MAX_DOMAIN:raise Held('catalog_domain_limit')
        fields=row_index[cid]['fields']
        entries.append({'child_id':cid,'attributes':{f['attribute_id']:f['value'] for f in fields},'historical_prices':prices,'fields':fields})
    if not entries:raise Held('catalog_domain_empty')
    return {'association':association,'source_url':source,'job':job,'rows':rows,'entries':entries,'excluded_children':excluded,
        'reference':ref,'checkpoint_path':path,'checkpoint_identity':file_identity(path),'declared_domain_count':total_domain}

def file_identity(path):
    s=Path(path).stat();return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

def discover_fourover_profile(store,association,max_jobs=99):
    if type(max_jobs) is not int or not 1<=max_jobs<=99:raise ValueError('Product job bound must be1..99')
    try:catalog=load_catalog(store,association)
    except Held as exc:return {'configurations':[],'blockers':[exc.code]}
    return {'configurations':[{'scope':dict(SCOPE),'catalog_reference':catalog['reference'],'max_quotes_per_session':MAX_QUOTES}],
        'blockers':[],'profile_id':PROFILE_ID,'catalog_children':len(catalog['entries']),
        'catalog_quantities':catalog['declared_domain_count'],'excluded_child_count':len(catalog['excluded_children'])}

def cursor_path(store,catalog):
    key=digest(_encoded({'profile':PROFILE_ID,'association':catalog['association'],'catalog_reference':catalog['reference'],'scope':SCOPE}))
    return Path(store.root)/'fourover-automatic-products'/(key+'.json'),key

def load_cursor(store,catalog):
    path,key=cursor_path(store,catalog)
    value={'schema':'fourover-automatic-product-cursor/v1','key':key,'association':catalog['association'],
        'catalog_reference':catalog['reference'],'scope':SCOPE,'progress_count':0,'processed_count':0,'verified_count':0,'pipeline_retry_count':0,'consecutive_read_retries':0,'receipts':{},'inflight':None}
    if path.is_symlink():raise Held('cursor_path_invalid')
    if path.exists():
        value=json.loads(safe_read(path,store.root,32*1024*1024),object_pairs_hook=_unique_object)
        if (value.get('schema')!='fourover-automatic-product-cursor/v1' or value.get('key')!=key or value.get('association')!=catalog['association']
                or value.get('catalog_reference')!=catalog['reference'] or value.get('scope')!=SCOPE or type(value.get('receipts')) is not dict
                or type(value.get('progress_count')) is not int or type(value.get('processed_count')) is not int or type(value.get('verified_count')) is not int or type(value.get('pipeline_retry_count')) is not int or type(value.get('consecutive_read_retries')) is not int):raise Held('cursor_context_changed')
        if len(value['receipts'])>MAX_DOMAIN:raise Held('cursor_limit')
        for semantic,receipt in value['receipts'].items():
            if (type(semantic) is not str or not HEX.fullmatch(semantic) or type(receipt) is not dict
                    or set(receipt)!={'status','child_id','quantity','evidence','reason_code'}
                    or receipt.get('status') not in {'verified','held'} or type(receipt.get('child_id')) is not str or not ID.fullmatch(receipt['child_id'])
                    or type(receipt.get('quantity')) is not int or receipt['quantity']<1
                    or semantic!=semantic_key(catalog['source_url'],receipt['child_id'],receipt['quantity'])):raise Held('cursor_context_changed')
            evidence=receipt['evidence']
            if receipt['status']=='verified':
                if (type(evidence) is not dict or set(evidence)!={'object_path','sha256'} or type(evidence.get('sha256')) is not str or not HEX.fullmatch(evidence['sha256'])
                        or evidence.get('object_path')!=f"objects/{evidence['sha256'][:2]}/{evidence['sha256']}.fourover-automatic-quote.json"):raise Held('cursor_evidence_invalid')
                if digest(safe_read(Path(store.root)/evidence['object_path'],store.root,2*1024*1024))!=evidence['sha256']:raise Held('cursor_evidence_invalid')
            elif evidence is not None:raise Held('cursor_evidence_invalid')
        if not 0<=value['pipeline_retry_count']<=3*MAX_DOMAIN or not 0<=value['consecutive_read_retries']<=3 or value['processed_count']!=len(value['receipts']) or value['progress_count']!=value['processed_count']+value['pipeline_retry_count'] or value['verified_count']!=sum(v['status']=='verified' for v in value['receipts'].values()):raise Held('cursor_progress_invalid')
    return path,value

def save_cursor(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_mode&0o077:raise Held('cursor_directory_invalid')
    temporary=path.with_suffix('.'+os.urandom(12).hex()+'.tmp')
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as out:out.write(_encoded(value));out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

def automatic_state_js(attributes):
    if type(attributes) is not dict or not 1<=len(attributes)<=32 or any(not ID.fullmatch(k) for k in attributes):raise Held('selection_invalid')
    old="['211','197','204','199','205','216']"
    if STATE_JS.count(old)!=1:raise Held('state_reader_changed')
    production="const productions=[...root.querySelectorAll('input[name=\"turnaround_time\"]')].filter(e=>e.checked&&!e.disabled).map(e=>({value:clean(e.value),label:label(e),visible:visible(e)}));"
    if STATE_JS.count(production)!=1:raise Held('state_reader_changed')
    measured=r'''const productionVisible=e=>{
   if(visible(e))return true;
   if(!e.id||[...document.querySelectorAll('[id]')].filter(n=>n.id===e.id).length!==1)return false;
   const associated=[...document.querySelectorAll('label')].filter(l=>l.htmlFor===e.id&&l.control===e);
   if(associated.length!==1||!root.contains(associated[0]))return false;
   for(let n=associated[0];n;n=n.parentElement){const style=getComputedStyle(n);if(n.hidden||n.hasAttribute?.('inert')||style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return false;}
   return visible(associated[0]);
 };
 const productions=[...root.querySelectorAll('input[name="turnaround_time"]')].filter(e=>e.checked&&!e.disabled).map(e=>({value:clean(e.value),label:label(e),visible:productionVisible(e)}));'''
    return STATE_JS.replace(old,json.dumps(list(attributes))).replace(production,measured)

def automatic_quote_button_js(attributes):
    """Ordinary visible quote action; final retained DOM check and click are atomic."""
    reader=automatic_state_js(attributes)
    return r'''({source,selection,click}) => {
 const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=="hidden";
 const clean=e=>String(e.textContent??"").replace(/\s+/g," ").trim().toLowerCase();
 const labels=new Map([["calculate","calculate"],["calculate price","calculate_price"],["proceed to shipping","proceed_to_shipping"]]);
 const roots=[...document.querySelectorAll(".column.main")];
 const root=roots.length===1?roots[0]:roots.length===0&&document.querySelectorAll("main").length===1?document.querySelector("main"):null;
 const buttons=root?[...root.querySelectorAll("button")].filter(visible):[];
 const labelClass=e=>labels.get(clean(e))||(/cart|checkout|order|upload|purchase|buy|pay|submit/i.test(clean(e))?"commerce_or_submit":"other");
 const eligible=e=>!e.disabled&&!e.matches(":disabled")&&e.getAttribute("aria-disabled")!=="true"&&!e.closest("[inert],a[href]")
    &&e.type!=="reset"&&!e.hasAttribute("formaction")&&!e.hasAttribute("formmethod")
    &&(e.type==="button"||!e.form||!e.form.hasAttribute("action")||!e.form.getAttribute("action")||e.form.action===source);
 const recognized=buttons.filter(e=>labels.has(clean(e)));
 const valid=recognized.filter(eligible);
 const diagnostic={visible_button_count:Math.min(buttons.length,100),recognized_count:Math.min(recognized.length,3),
    eligible_count:Math.min(valid.length,3),labels:{calculate:0,calculate_price:0,proceed_to_shipping:0,commerce_or_submit:0,other:0}};
 for(const e of buttons)diagnostic.labels[labelClass(e)]=Math.min(100,diagnostic.labels[labelClass(e)]+1);
 if(location.href!==source||!root)return {status:"context_changed",diagnostic};
 const state=(READER)();
 const retained=state.child_id===selection.child_id&&state.run_size===String(selection.run_size)
    &&Object.entries(selection.attributes).every(([id,value])=>{const e=state.attributes[id];return e&&e.count===1&&e.value===value&&e.enabled===true&&e.choice_enabled===true&&(e.visible===true||e.implicit_fixed_dash===true);})
    &&["job_sample","digital_proof"].every(k=>{const e=state.services[k];return e&&e.count===1&&e.checked===false&&e.enabled===true;})
    &&(state.sets===null||(state.sets.value==="1"&&state.sets.valid===true&&state.sets.enabled===true))
    &&!state.blocked_popup_attempts;
 if(!retained)return {status:"context_changed",diagnostic};
 if(recognized.length!==1||valid.length!==1)return {status:"unavailable",diagnostic};
 if(!click)return {status:"available",diagnostic};
 HTMLElement.prototype.click.call(valid[0]);
 return {status:"clicked",diagnostic};
}'''.replace('READER',reader)

# The finite known quote-only source form validator is the reviewed native schema;
# only its diagnostic NEXT_NATIVE_PAIR admission is removed for a validated catalog.
def automatic_request_context(post_data,state,*,selection,literal_blank_trim=False):
    """Exact source-owned quote form only; private native metadata is never emitted."""
    try:body_bytes=post_data.encode('utf-8') if type(post_data) is str else None
    except UnicodeError:raise Held('native_quote_body_unavailable') from None
    if (not context_retained(state,selection=selection)
            or body_bytes is None or len(body_bytes)>65536
            or re.search(r'%(?![0-9A-Fa-f]{2})',post_data)
            or any(ord(c)<32 or ord(c)==127 for c in post_data)):
        raise Held('native_quote_body_unavailable')
    try:
        pairs=parse_qsl(post_data,keep_blank_values=True,strict_parsing=True,
                        encoding='utf-8',errors='strict',max_num_fields=256)
    except (ValueError,UnicodeError):
        raise Held('native_quote_body_unavailable') from None
    if type(literal_blank_trim) is not bool:raise Held('native_quote_body_schema_changed')
    options={f'product_options[super_attribute][{k}]' for k in selection['attributes']}
    flat={'uuid','name','product_id','run_size','custom_run_size','product_options[turnaround_time]',
        'product_options[job_sample]','product_options[digital_proof]',
        'shipments[0][uuid]','shipments[0][shipping_address]','shipments[0][drop_ship_address]',
        'shipments[0][shipping_method]','shipments[0][group_shipping_applied]',
        'shipments[0][shipping_time_from_carrier]',
        'shipments[0][jobs][0][name]','shipments[0][jobs][0][uuid]',
        'shipments[0][jobs][0][artwork_status]',*options}
    if literal_blank_trim:flat.add('product_options[trim_size]')
    address=re.compile(r'^shipments\[0\]\[(shipping_address|drop_ship_address)\]((?:\[(?:[A-Za-z_][A-Za-z0-9_]{0,63}|0|[1-9][0-9]{0,2})\]){1,4})(\[\])?$')
    values={};address_counts={};address_arrays={}
    for key,value in pairs:
        if (not key or len(key.encode())>256 or len(value.encode())>2048
                or any(ord(c)<32 or ord(c)==127 for c in key+value)):
            raise Held('native_quote_body_unavailable')
        match=address.fullmatch(key)
        if key not in flat and match is None:raise Held('native_quote_body_schema_changed')
        if match is not None:
            segments=re.findall(r'\[([^\]]*)\]',key)
            if any(v in {'__proto__','prototype','constructor'} for v in segments):
                raise Held('native_quote_body_schema_changed')
            address_counts[match[1]]=address_counts.get(match[1],0)+1
            if address_counts[match[1]]>64:raise Held('native_quote_body_unavailable')
        if key in values:
            # jQuery's native opaque address arrays may use repeated street[].
            # They never become product choices, copied values or evidence hashes.
            if match is None or not match[3]:raise Held('native_quote_body_schema_changed')
            address_arrays[key]=address_arrays.get(key,1)+1
            if address_arrays[key]>8:raise Held('native_quote_body_unavailable')
        values[key]=value
    keys=set(values)
    if literal_blank_trim and ('product_options[trim_size]' not in keys or values['product_options[trim_size]']!=''):raise Held('native_quote_body_schema_changed')
    for key in keys:
        if any(other.startswith(key+'[') for other in keys):
            raise Held('native_quote_body_schema_changed')
    required={'uuid','product_id','run_size','custom_run_size','product_options[turnaround_time]',
        'shipments[0][uuid]','shipments[0][jobs][0][uuid]',*options}
    if not required<=keys or any(not values[k] for k in ('uuid','shipments[0][uuid]','shipments[0][jobs][0][uuid]')):
        raise Held('native_quote_body_schema_changed')
    if (any(values.get(key,'') for key in ('shipments[0][shipping_address]','shipments[0][drop_ship_address]',
                'shipments[0][shipping_method]','shipments[0][shipping_time_from_carrier]'))
            or values.get('shipments[0][group_shipping_applied]','false') not in {'','0','false'}
            or any(values.get('product_options['+name+']','false') not in {'','0','false'}
                   for name in ('job_sample','digital_proof'))):
        raise Held('native_quote_shipping_or_services_changed')
    # Reuse the existing exact retained product/production reducer only after the
    # complete finite form schema has excluded every unknown action/option/job.
    result=request_context(post_data,state,selection=selection)
    if (result['set_binding']!='one_native_job'
            or not q1000_context_retained(state,selection=selection,request_projection=result)):
        raise Held('native_quote_set_binding_changed')
    return {**result,'quote_body_schema':'current_source_project_one_job/v1',**({'native_trim_size_binding':'source_literal_blank'} if literal_blank_trim else {})}

def automatic_context_diagnostic(state, *, selection, request_projection=None):
    """Fixed booleans/counts only. Informational; never consulted by admission."""
    values=selection.get('attributes') if type(selection) is dict else None
    ids=tuple(values) if type(values) is dict and 1<=len(values)<=32 and all(type(k) is str and ID.fullmatch(k) for k in values) else ()
    observed=state if type(state) is dict else {}
    expected=selection if type(selection) is dict else {}
    values=expected.get('attributes')
    attribute_expected_valid=(bool(ids) and type(values) is dict and set(values)==set(ids)
        and all(type(values[k]) is str and re.fullmatch(r'[1-9][0-9]{0,12}',values[k]) for k in ids)
        and type(expected.get('child_id')) is str and re.fullmatch(r'[1-9][0-9]{0,12}',expected['child_id']) is not None)
    expected_valid=attribute_expected_valid and type(expected.get('run_size')) is int and 1<=expected['run_size']<=10**12
    def mapping(value):return value if type(value) is dict else {}
    def count(value,maximum=2):return min(value,maximum) if type(value) is int and value>=0 else None
    def predicate(call):
        try:return expected_valid is True and call() is True
        except Exception:return False
    attribute_rows={}
    attributes=mapping(observed.get('attributes'))
    for key in ids:
        row=mapping(attributes.get(key))
        attribute_rows[key]={'control_count':count(row.get('count')),
            'control_count_one':row.get('count')==1,
            'value_matches':bool(attribute_expected_valid and type(row.get('value')) is str and row['value']==values[key]),
            'enabled':row.get('enabled') is True,'visible':row.get('visible') is True,
            'implicit_fixed_dash':row.get('implicit_fixed_dash') is True,
            'choice_enabled':row.get('choice_enabled') is True}
    service_rows={}
    services=mapping(observed.get('services'))
    for key in ('job_sample','digital_proof'):
        row=mapping(services.get(key))
        service_rows[key]={'control_count':count(row.get('count')),
            'unchecked':row.get('checked') is False,'enabled':row.get('enabled') is True,
            'exact_off_shape':row=={'count':1,'checked':False,'enabled':True}}
    sets=mapping(observed.get('sets'));production=mapping(observed.get('production'))
    projection=mapping(request_projection);projected_production=mapping(projection.get('production'))
    quantity_values=observed.get('run_size_values');available=observed.get('available_run_sizes')
    return {'schema':'automatic-retained-context-diagnostic/v1','observed_state_is_object':type(state) is dict,
        'expected_context_valid':bool(expected_valid),'attributes':attribute_rows,
        'child_matches':bool(attribute_expected_valid and type(observed.get('child_id')) is str and observed['child_id']==expected['child_id']),
        'quantity':{'matches':bool(expected_valid and type(observed.get('run_size')) is str and observed['run_size']==str(expected['run_size'])),
            'recognized_value_count':min(len(quantity_values),5) if type(quantity_values) is list else None,
            'expected_available':bool(expected_valid and type(available) is list and str(expected['run_size']) in available)},
        'services':service_rows,
        'sets':{'absent':observed.get('sets') is None,'control_count':count(observed.get('set_control_count')),
            'one':type(sets.get('value')) is str and sets['value']=='1','valid':sets.get('valid') is True,
            'enabled':sets.get('enabled') is True},
        'production':{'informational_before_summary':True,'present':type(observed.get('production')) is dict,
            'control_count':count(observed.get('production_control_count')),'visible':production.get('visible') is True,
            'value_present':type(production.get('value')) is str and 1<=len(production['value'])<=160,
            'label_present':type(production.get('label')) is str and 1<=len(production['label'])<=160,
            'matches_request_projection':({k:production.get(k) for k in ('value','label')}==projected_production
                if projected_production else None)},
        'native_predicates':{
            'attributes_retained_visible':bool(attribute_expected_valid and attributes_retained(observed,visible=True,selection=selection)),
            'services_off':services_off(observed) is True,
            'context_retained_before_summary':predicate(lambda:context_retained(observed,selection=selection)),
            'context_retained_final':predicate(lambda:context_retained(observed,final=True,selection=selection)),
            'q1000_context_retained_final':predicate(lambda:q1000_context_retained(observed,selection=selection,request_projection=request_projection))}}


class _CustomerSectionDeclaration(HTMLParser):
    def __init__(self):super().__init__();self.active=False;self.parts=[];self.urls=[]
    def handle_starttag(self,tag,attrs):
        if tag.lower()=='script':self.active=dict(attrs).get('type')=='text/x-magento-init';self.parts=[]
    def handle_data(self,data):
        if self.active:self.parts.append(data)
    def handle_endtag(self,tag):
        if tag.lower()!='script' or not self.active:return
        self.active=False
        try:
            value=json.loads(''.join(self.parts),object_pairs_hook=_unique_object)
            config=value.get('*',{}).get('Magento_Customer/js/customer-data',{})
            if type(config) is dict and 'sectionLoadUrl' in config:self.urls.append(config['sectionLoadUrl'])
        except (ValueError,TypeError,AttributeError):pass

def customer_section_declared(raw):
    try:
        parser=_CustomerSectionDeclaration();parser.feed(raw.decode('utf8'));parser.close()
        return parser.urls==['https://4over.com/customer/section/load/']
    except (UnicodeError,ValueError,TypeError):return False

CUSTOMER_MODULE_SUFFIX='Magento_Customer/js/customer-data.js'
CUSTOMER_MODULE_SHA256='1c675dbc82a478a591fa0515163c1e6ef6fbcc572038cfc80a2ed14d0fdab57a'

def customer_module_declared(suffix,raw):
    # Exact current public source module declares its default readonly route.
    # The native response stays opaque; this is a source-code identity proof.
    return suffix==CUSTOMER_MODULE_SUFFIX and digest(raw)==CUSTOMER_MODULE_SHA256

def critical_public_asset(suffix):
    return suffix.startswith('FourOver_') or suffix.endswith('.html') or suffix in {
        CUSTOMER_MODULE_SUFFIX,'Magento_Customer/js/section-config.js','requirejs-config.js'}

CONTROLLER_PUBLIC_MODULES={'FourOver_ProductRunSize/js/view/register-price-processor.js': 'e13dc7af33f35b9076fff2f64fefe0ca4437a724e025d2bde17951f85a76b834', 'FourOver_Project/js/model/product-selected-id.js': '34341da962968168abbb194d5be7426c78461d2936721490e013d55216de8bfb', 'FourOver_Project/js/model/product-package-prices.js': 'a80bd0448414bbf28f4aa3aad3c166caac1177d8592484aba123f019c6342a54', 'FourOver_Project/js/model/selected-runsize.js': '0bddd560b3a51d1cdefefb1fa87a018b4de239900cf40dfc11a81c66ea9352c5', 'FourOver_Project/js/model/product-price-processor.js': '98454e865c070b82e6c768081b89678b86794e22e9c92350d68880b534219552', 'FourOver_ProductRunSize/js/model/price-processor.js': '89fad984856cc83532261a9a43bbff1eb4d1cb8b6490c2937d5c8ba80182d19f', 'FourOver_TurnaroundTime/js/model/product/turnaround-time.js': '3e77ec80fbd7bcd91fbd572bcf857ccd0a1d3faf5c2939224d28c00aa3459505', 'FourOver_TurnaroundTime/js/view/product/turnaround-time.js': 'de4715c368b0505e35336c65b0dcba85da953251a9bb8a04d61b270b376382cc'}
CONTROLLER_PROBE_JS=r'''({source,dom,modules_proved}) => {
 const out={schema:"native-controller-readiness/v1",modules_proved:modules_proved===true,available:false,
  registration_defined:false,child_matches_dom:false,quantity_matches_dom:false,quantity_strict_row:false,
  unit_price_positive:false,item_total_positive:false,tat_matches_dom:false,tat_input_visible:false,tat_label_visible:false,tat_ready:false,ready:false};
 if(location.href!==source||modules_proved!==true||!dom||typeof dom!=="object")return out;
 const r=window.require;
 const names=NAMES;
 if(typeof r!=="function"||typeof r.defined!=="function"||!names.every(n=>r.defined(n)))return out;
 out.registration_defined=true;
 try{
  // Synchronous get of already-defined modules ONLY: no array/async require.
  const selected=r("FourOver_Project/js/model/product-selected-id"),qtyModel=r("FourOver_Project/js/model/selected-runsize"),rowsModel=r("FourOver_Project/js/model/product-package-prices"),tatModel=r("FourOver_TurnaroundTime/js/model/product/turnaround-time");
  const price=r("FourOver_Project/js/model/product-price-processor");
  if(![selected,qtyModel,rowsModel,tatModel].every(v=>typeof v==="function"&&typeof v.peek==="function")||!price||typeof price.getUnitPrice!=="function"||typeof price.getItemTotalPrice!=="function")return out;
  const child=selected.peek(),qty=qtyModel.peek(),rows=rowsModel.peek(),tat=tatModel.peek();
  out.available=true;
  const scalar=v=>(typeof v==="string"||typeof v==="number")&&String(v).length<=160;
  out.child_matches_dom=scalar(child)&&String(child)===dom.child_id;
  out.quantity_matches_dom=Number.isSafeInteger(qty)&&qty>0&&String(qty)===dom.run_size;
  if(!Array.isArray(rows)||rows.length>2048)return out;
  out.quantity_strict_row=Number.isSafeInteger(qty)&&rows.filter(row=>row&&row.qty===qty).length===1;
  if(!out.child_matches_dom||!out.quantity_matches_dom||!out.quantity_strict_row)return out;
  // Exact saved getters call only the registered package-row calculator.
  // These positive predicates are readiness, NEVER supplier quote evidence.
  const unit=price.getUnitPrice(qty),total=price.getItemTotalPrice(qty);
  out.unit_price_positive=typeof unit==="number"&&Number.isFinite(unit)&&unit>0;
  out.item_total_positive=typeof total==="number"&&Number.isFinite(total)&&total>0;
  out.tat_matches_dom=scalar(tat)&&String(tat)!==""&&!!dom.production&&String(tat)===dom.production.value;
  out.ready=out.registration_defined&&out.child_matches_dom&&out.quantity_matches_dom&&out.quantity_strict_row&&out.unit_price_positive&&out.item_total_positive;
  const inputs=[...document.querySelectorAll('input[name="turnaround_time"]')].filter(e=>e.checked);
  if(inputs.length===1){const e=inputs[0],visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=="hidden";out.tat_input_visible=visible(e);out.tat_label_visible=visible(e.closest('label'))||[...document.querySelectorAll('label')].some(l=>l.htmlFor===e.id&&visible(l));}
  out.tat_ready=out.ready&&out.tat_matches_dom&&dom.production.visible===true;
  return out;
 }catch(_){return out;}
}'''.replace('NAMES',json.dumps([p.removesuffix('.js') for p in CONTROLLER_PUBLIC_MODULES]))

def controller_ready(state,*,tat=False):
    v=state.get('source_controller') if type(state) is dict else None
    keys=('modules_proved','available','registration_defined','child_matches_dom','quantity_matches_dom','quantity_strict_row','unit_price_positive','item_total_positive','ready')
    return type(v) is dict and v.get('schema')=='native-controller-readiness/v1' and all(v.get(k) is True for k in keys) and (not tat or v.get('tat_matches_dom') is True and v.get('tat_ready') is True)

def controller_diagnostic(state):
    value=state.get('source_controller') if type(state) is dict else None
    if type(value) is not dict:return None
    keys=('modules_proved','available','registration_defined','child_matches_dom','quantity_matches_dom','quantity_strict_row','unit_price_positive','item_total_positive','tat_matches_dom','tat_input_visible','tat_label_visible','tat_ready','ready')
    return {'schema':'native-controller-readiness/v1',**{k:value[k] for k in keys if type(value.get(k)) is bool}}

def automatic_controller_state_js(attributes,source,proved):
    # DOM and already-defined native getters are read in one synchronous turn.
    return "() => {const dom=("+automatic_state_js(attributes)+")();dom.source_controller=("+CONTROLLER_PROBE_JS+")({source:"+json.dumps(source)+",dom,modules_proved:"+json.dumps(proved is True)+"});return dom;}"

UI_PUBLIC_MODULES={
 'Magento_Ui/js/lib/registry/registry.js':'f6a9135d5d2cd61c8c11e2d39c693daeaaf6e5a1a85de570980f5ed6b254bd70',
 'FourOver_Project/js/view/project-tabs.js':'861824cc1243a53d35b9998959e50ae8a04300768be50d1e4b0b3e8c53f3f332',
 'FourOver_Project/js/view/project.js':'8190f0529ba686c99594b4ec3162acc3535517baf469ba856dde762505a7016e'}
QUOTE_LABEL='Hermes source quote'
NAME_PUBLIC_MODULES={
 'FourOver_Project/js/view/project/name.js':'05e6c78d14f51950dede0377ad6e1c2340b594ca99747102066b501b88b40786',
 'FourOver_Project/js/model/project.js':'99ece405a5d506875a32845953fa69ce65a9e4c94f3769fabb7ca7eb2c925b8f',
 'FourOver_Project/js/model/project-builder.js':'5ca25247a50dac80b55008ac5cd9742e069eadeb5efee7b1b25adc62b1e7fb00'}
QUOTE_LABEL_ACTION_JS=r'''({source,modules_proved}) => {
 const answer=status=>({status}),visible=e=>{if(!e||!e.getClientRects().length)return false;for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(n.hidden||n.hasAttribute?.('inert')||s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse')return false;}return true;};
 if(location.href!==source||location.origin!=='https://4over.com'||location.search||location.hash||modules_proved!==true)return answer('unproved');
 const r=window.require,names=['uiRegistry','FourOver_Project/js/view/project-tabs','FourOver_Project/js/view/project','FourOver_Project/js/view/project/name','FourOver_Project/js/model/project','FourOver_Project/js/model/project-builder'];
 if(typeof r!=='function'||typeof r.defined!=='function'||!names.every(n=>r.defined(n)===true))return answer('unproved');
 try{
  const registry=r('uiRegistry'),model=r('FourOver_Project/js/model/project');
  if(!registry||typeof registry.get!=='function'||typeof model!=='function'||typeof model.peek!=='function')return answer('unproved');
  const data=model.peek(),tabs=registry.get('project-tabs'),view=registry.get('project'),nameView=registry.get('project-name');
  if(!data||data.state!=='default')return answer('not_default');
  if(typeof data.name!=='function'||typeof data.name.peek!=='function'||view?.projectName!==data.name||nameView?.projectName!==data.name)return answer('binding_changed');
  if(tabs?.guestShippingRestricted!==false||typeof tabs.selectedTab?.peek!=='function'||typeof view.isShippingAndSetsActive?.peek!=='function'||view.isShippingAndSetsActive.peek()!==false)return answer('not_options');
  const selected=tabs.selectedTab.peek(),domain=tabs.tabsData;
  if(typeof selected!=='string'||!Array.isArray(domain)||!domain.length||domain.length>32||domain.some(t=>!t||typeof t.alias!=='string'||!t.alias)||new Set(domain.map(t=>t.alias)).size!==domain.length)return answer('not_options');
  const i=domain.findIndex(t=>t.alias===selected);if(i<0||selected!==(tabs.productOptionsTab||domain[0].alias)||domain[i+1]?.alias!==tabs.shippingAndSetsTab||!tabs.shippingAndSetsTab)return answer('not_options');
  const forms=[...document.querySelectorAll('#product_addtocart_form')],fields=[...document.querySelectorAll('#project_name')],steps=[...document.querySelectorAll('[data-tab-id]')].filter(e=>e.getAttribute('data-tab-id')===selected);
  if(forms.length!==1||fields.length!==1||steps.length!==1)return answer('field_changed');const field=fields[0];
  if(!forms[0].contains(field)||!steps[0].contains(field)||field.tagName!=='INPUT'||field.type!=='text'||field.name!=='project_name'||field.disabled||field.readOnly||!visible(field)||!Number.isInteger(field.maxLength)||field.maxLength<QUOTE_LABEL.length||field.maxLength>60)return answer('field_changed');
  const value=data.name.peek();if(typeof value!=='string'||typeof field.value!=='string')return answer('binding_changed');
  if(value!==''||field.value!=='')return answer(value===field.value?'already_named':'binding_changed');
  // Only this new/default local model's empty visible native text input is set.
  // Its normal textInput handler owns validation/model updates; no model setter,
  // save/bookmark/cart callback, hidden input, or synthetic request is invoked.
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;if(typeof setter!=='function')return answer('field_changed');
  setter.call(field,QUOTE_LABEL);field.dispatchEvent(new Event('input',{bubbles:true}));field.dispatchEvent(new Event('change',{bubbles:true}));
  if(field.value!==QUOTE_LABEL||data.name.peek()!==QUOTE_LABEL||model.peek()!==data||location.href!==source)return answer('input_unretained');
  return answer('filled');
 }catch(_){return answer('unproved');}
}'''.replace('QUOTE_LABEL',json.dumps(QUOTE_LABEL))
UI_BOOL_KEYS=('modules_proved','registry_defined','tabs_module_defined','project_module_defined','registry_available',
 'tabs_registered','project_registered','tab_domain_observed','selected_tab_in_domain','next_tab_available',
 'current_is_product_options','current_is_shipping','current_is_addresses','next_is_shipping','next_is_addresses',
 'guest_restriction_observed','guest_restricted','tabs_loading_observed','tabs_loading',
 'project_loading_observed','project_loading','shipping_active_observed','shipping_active',
 'project_name_invalid_observed','project_name_invalid','current_project_name_present')
UI_FIELDS=('attributes','quantity','production','project_name','services','other')
UI_COUNTS=('count','visible_count','native_invalid_visible','value_missing_visible','aria_invalid_visible','mage_error_visible')
UI_PROBE_JS=r'''({source,dom,modules_proved}) => {
 const out={schema:"native-project-ui-readiness/v1",...Object.fromEntries(BOOLS.map(k=>[k,false])),form_count:0,visible_error_count:0,
  fields:Object.fromEntries(FIELDS.map(k=>[k,Object.fromEntries(COUNTS.map(n=>[n,0]))]))};
 out.modules_proved=modules_proved===true;
 if(location.href!==source)return out;
 const visible=e=>{if(!e||!e.getClientRects().length)return false;for(let n=e;n;n=n.parentElement){const c=getComputedStyle(n);if(n.hidden||n.hasAttribute?.('inert')||c.display==="none"||c.visibility==="hidden"||c.visibility==="collapse")return false;}return true;};
 // Fixed known form selectors; no value/name/message text is read or returned.
 const forms=[...document.querySelectorAll("#product_addtocart_form")];out.form_count=Math.min(2,forms.length);
 if(forms.length===1){const form=forms[0],sets={attributes:[...form.querySelectorAll('select[name^="super_attribute["]')],quantity:[...form.querySelectorAll('input[name="run_size"]')],
  production:[...form.querySelectorAll('input[name="turnaround_time"]')],project_name:[...form.querySelectorAll('#project_name')],services:[...form.querySelectorAll('input[name="job_sample"],input[name="digital_proof"]')]};
  const known=new Set(Object.values(sets).flat());sets.other=[...form.querySelectorAll('input,select,textarea')].filter(e=>!known.has(e));
  for(const key of FIELDS){const controls=sets[key].slice(0,256),shown=controls.filter(visible),c=out.fields[key];c.count=controls.length;c.visible_count=shown.length;
   c.native_invalid_visible=shown.filter(e=>e.validity?.valid===false).length;c.value_missing_visible=shown.filter(e=>e.validity?.valueMissing===true).length;
   c.aria_invalid_visible=shown.filter(e=>e.getAttribute('aria-invalid')==='true').length;c.mage_error_visible=shown.filter(e=>e.classList?.contains('mage-error')).length;}
  out.visible_error_count=Math.min(100,[...form.querySelectorAll('.mage-error,[aria-invalid="true"]')].filter(visible).length);
 }
 const r=window.require;if(typeof r!=="function"||typeof r.defined!=="function")return out;
 out.registry_defined=r.defined("uiRegistry")===true;out.tabs_module_defined=r.defined("FourOver_Project/js/view/project-tabs")===true;out.project_module_defined=r.defined("FourOver_Project/js/view/project")===true;
 if(!out.modules_proved||!out.registry_defined||!out.tabs_module_defined||!out.project_module_defined)return out;
 try{
  const registry=r("uiRegistry");if(!registry||typeof registry.get!=="function")return out;out.registry_available=true;
  // Exact current source registry.get(string), no callback: synchronous map read.
  const tabs=registry.get("project-tabs"),project=registry.get("project");out.tabs_registered=!!tabs&&typeof tabs==="object";out.project_registered=!!project&&typeof project==="object";
  const boolean=(owner,key,outKey)=>{const o=owner?.[key];if(typeof o==="function"&&typeof o.peek==="function"){const v=o.peek();if(typeof v==="boolean"){out[outKey+"_observed"]=true;out[outKey]=v;}}};
  boolean(tabs,"isLoading","tabs_loading");boolean(project,"isLoading","project_loading");boolean(project,"isShippingAndSetsActive","shipping_active");boolean(tabs,"isProjectNameInvalid","project_name_invalid");
  if(tabs&&typeof tabs.guestShippingRestricted==="boolean"){out.guest_restriction_observed=true;out.guest_restricted=tabs.guestShippingRestricted;}
  const read=tabs?.selectedTab;if(typeof read!=="function"||typeof read.peek!=="function")return out;const selected=read.peek(),domain=tabs.tabsData;
  if(typeof selected!=="string"||!selected||selected.length>160||!Array.isArray(domain)||!domain.length||domain.length>32||domain.some(v=>!v||typeof v.alias!=="string"||!v.alias||v.alias.length>160)||new Set(domain.map(v=>v.alias)).size!==domain.length)return out;
  out.tab_domain_observed=true;const index=domain.findIndex(v=>v.alias===selected);out.selected_tab_in_domain=index>=0;if(index<0)return out;
  const next=domain[index+1]?.alias,shipping=tabs.shippingAndSetsTab;out.next_tab_available=typeof next==="string";
  out.current_is_product_options=selected===(tabs.productOptionsTab||domain[0].alias||"product.configuration");out.current_is_shipping=typeof shipping==="string"&&!!shipping&&selected===shipping;
  out.current_is_addresses=selected==="product.addresses";out.next_is_shipping=typeof shipping==="string"&&!!shipping&&next===shipping;out.next_is_addresses=next==="product.addresses";
  // Match the same source step selector without persisting its public alias.
  const step=[...document.querySelectorAll('[data-tab-id]')].filter(e=>e.getAttribute('data-tab-id')===selected);
  out.current_project_name_present=step.length===1&&step[0].querySelectorAll('#project_name').length===1;
 }catch(_){return out;}
 return out;
}'''.replace('BOOLS',json.dumps(UI_BOOL_KEYS)).replace('FIELDS',json.dumps(UI_FIELDS)).replace('COUNTS',json.dumps(UI_COUNTS))

def ui_diagnostic(state):
    raw=state.get('source_ui') if type(state) is dict else None
    if type(raw) is not dict:return None
    def count(v):return min(256,max(0,v)) if type(v) is int else 0
    fields=raw.get('fields') if type(raw.get('fields')) is dict else {}
    return {'schema':'native-project-ui-readiness/v1',**{k:raw.get(k) is True for k in UI_BOOL_KEYS},
      'form_count':min(2,count(raw.get('form_count'))),'visible_error_count':min(100,count(raw.get('visible_error_count'))),
      'fields':{k:{n:count((fields.get(k) if type(fields.get(k)) is dict else {}).get(n)) for n in UI_COUNTS} for k in UI_FIELDS}}

def ui_ready(state):
    raw=state.get('source_ui') if type(state) is dict else None
    required=('modules_proved','registry_defined','tabs_module_defined','project_module_defined','registry_available','tabs_registered','project_registered','tab_domain_observed','selected_tab_in_domain','guest_restriction_observed')
    return (type(raw) is dict and raw.get('schema')=='native-project-ui-readiness/v1' and type(raw.get('form_count')) is int and raw['form_count']==1
      and all(raw.get(k) is True for k in required) and raw.get('guest_restricted') is False
      and (raw.get('next_tab_available') is True or raw.get('current_is_shipping') is True and raw.get('shipping_active_observed') is True and raw.get('shipping_active') is True))

def ui_action_requirement(state):
    raw=state.get('source_ui') if type(state) is dict else None
    if (type(raw) is not dict or raw.get('schema')!='native-project-ui-readiness/v1'
      or not all(raw.get(k) is True for k in ('modules_proved','registry_available','tabs_registered','project_registered','tab_domain_observed','selected_tab_in_domain'))):return None
    if raw.get('guest_restriction_observed') is True and raw.get('guest_restricted') is True:return 'source_auth_required'
    fields=raw.get('fields') if type(raw.get('fields')) is dict else {}
    field=fields.get('project_name') if type(fields.get('project_name')) is dict else {}
    if (raw.get('current_project_name_present') is True and type(field.get('visible_count')) is int and field['visible_count']==1
       and (raw.get('project_name_invalid_observed') is True and raw.get('project_name_invalid') is True or type(field.get('value_missing_visible')) is int and field['value_missing_visible']>0)):
        return 'source_project_details_required'
    return None

SUMMARY_PUBLIC_MODULE='FourOver_Project/js/view/project/summary.js'
SUMMARY_PUBLIC_MODULE_SHA='2fb7fd2816385e51f0ef401fc560b3f42a9b56bdcaefa59bb03d8e8bc3acf974'
NATIVE_SUMMARY_MONEY_JS=r'''({source,proved})=>{
 const out={schema:"native-summary-money/v1",status:"not_present",root_count:0,totals_count:0,subtotal_count:0,total_count:0,money:[]};
 if(location.href!==source||location.origin!=="https://4over.com"||location.search||location.hash){out.status="unproved";return out;}
 const visible=e=>{if(!e||!e.getClientRects().length)return false;for(let n=e;n;n=n.parentElement){const c=getComputedStyle(n);if(n.hidden||n.hasAttribute('inert')||c.display==='none'||c.visibility==='hidden'||c.visibility==='collapse')return false;}return true;};
 const roots=[...document.querySelectorAll('.project-summary')].filter(visible);out.root_count=Math.min(2,roots.length);if(!roots.length)return out;
 if(roots.length!==1||proved!==true){out.status="unproved";return out;}out.status="pending";
 const totals=[...roots[0].querySelectorAll('.project-summary_totals')].filter(visible);out.totals_count=Math.min(2,totals.length);if(!totals.length)return out;
 if(totals.length!==1){out.status="ambiguous";return out;}
 const lists=[...totals[0].children].filter(e=>e.tagName==='UL'&&visible(e));if(lists.length!==1){out.status="unproved";return out;}
 const norm=e=>(e.getAttribute('data-bind')||'').replace(/\s+/g,'');
 const text=e=>String(e.textContent||'').replace(/\s+/g,' ').trim();
 const currency=e=>{const m=text(e).match(/^\$((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,13})\.[0-9]{2})$/);return m?m[1].replace(/,/g,''):null;};
 const rows=[...lists[0].children].filter(e=>e.tagName==='LI'&&visible(e));
 const subs=rows.filter(e=>[...e.children].some(s=>s.tagName==='SPAN'&&visible(s)&&norm(s)==="css:'label',i18n:'Subtotal'"));
 const grands=rows.filter(e=>e.classList.contains('grand-total'));out.subtotal_count=Math.min(2,subs.length);out.total_count=Math.min(2,grands.length);
 if(!subs.length||!grands.length)return out;if(subs.length!==1||grands.length!==1){out.status="ambiguous";return out;}
 const sub=[...subs[0].children].filter(visible),total=[...grands[0].children].filter(visible);
 if(subs[0]===grands[0]||sub.length!==2||sub.some(e=>e.tagName!=='SPAN')||total.length!==2||total.some(e=>e.tagName!=='SPAN')
  ||!sub[0].classList.contains('label')||norm(sub[0])!=="css:'label',i18n:'Subtotal'"||text(sub[0])!=='Subtotal'
  ||!sub[1].classList.contains('value')||norm(sub[1])!=="css:'value',text:formatPrice(getTotals()['subtotal'])"||sub[1].children.length!==0
  ||!total[0].classList.contains('label')||![["css:'label',i18n:'Total'","Total"],["css:'label',i18n:'Total:'","Total:"]].some(([b,t])=>norm(total[0])===b&&text(total[0])===t)
  ||!total[1].classList.contains('value')||norm(total[1])!=="css:'value'"){out.status="unproved";return out;}
 const inside=[...total[1].children];
 if(inside.length!==2||inside[0].tagName!=='SPAN'||!visible(inside[0])||norm(inside[0])!=="text:formatPrice(getTotals()['grand_total'])"||inside[0].children.length!==0
   ||inside[1].tagName!=='I'||norm(inside[1])!=="i18n:'Taxesmayapply'"||text(inside[1])!=='Taxes may apply'){out.status="unproved";return out;}
 const subtotal=currency(sub[1]),grand=currency(inside[0]);if(subtotal===null||grand===null)return out;
 out.status="observed";out.money=[{kind:"subtotal",value:subtotal},{kind:"total",value:grand}];return out;
}'''
def public_money_diagnostic(state):
    raw=state.get('native_summary_money') if type(state) is dict else None
    out={'schema':'native-summary-money-diagnostic/v1','native_status':None,'money':[]}
    if type(raw) is dict and raw.get('schema')=='native-summary-money/v1' and type(raw.get('status')) is str and raw['status'] in {'not_present','unproved','pending','ambiguous','observed'}:
        out['native_status']=raw['status']
        for key in ('root_count','totals_count','subtotal_count','total_count'):
            value=raw.get(key);out[key]=value if type(value) is int and 0<=value<=2 else 0
    rows=state.get('money') if type(state) is dict else None
    if type(rows) is list:
        for row in rows[:20]:
            if (type(row) is dict and set(row)=={'kind','value'} and type(row.get('kind')) is str and row['kind'] in {'subtotal','total','price per set'}
                and type(row.get('value')) is str and re.fullmatch(r'[0-9]{1,13}\.[0-9]{2}',row['value'])):out['money'].append(dict(row))
    return out

def public_captured_totals(summary):
    totals=summary.get('totals') if type(summary) is dict else None
    if type(totals) is not dict or set(totals)!={'subtotal','grand_total','small_qty_fee','turnaround_time','job_sample','digital_proof'}:return None
    if any(type(v) is not str or not re.fullmatch(r'[0-9]{1,13}\.[0-9]{2}',v) for v in totals.values()):return None
    return dict(totals)

def automatic_observed_state_js(attributes,source,controller_proved,ui_proved,summary_proved=False):
    return "() => {const dom=("+automatic_controller_state_js(attributes,source,controller_proved)+")();dom.source_ui=("+UI_PROBE_JS+")({source:"+json.dumps(source)+",dom,modules_proved:"+json.dumps(ui_proved is True)+"});dom.native_summary_money=("+NATIVE_SUMMARY_MONEY_JS+")({source:"+json.dumps(source)+",proved:"+json.dumps(summary_proved is True)+"});if(dom.native_summary_money.status!=='not_present')dom.money=dom.native_summary_money.status==='observed'?dom.native_summary_money.money:[];return dom;}"

QUANTITY_EXPAND_PUBLIC_ASSETS={
 'FourOver_ProductRunSize/js/view/product-run-size.js':'c3e017c7385ea35882adab433d5a721157dd7670b8353592569843ba133bf216',
 'FourOver_ProductRunSize/template/product-run-size.html':'6419edbca1adeb6df447fbd32e7e275d8a77e214e110e060d2158be28f45cee3'}
QUANTITY_EXPAND_REASONS=frozenset(('source_unproved','context_unretained','form_unretained','table_unmounted','search_active','table_unretained','quantity_unproved','collapsed_rows_unmounted','collapsed_action_unmounted','action_binding_changed','action_unmounted'))
def safe_quantity_expand_diagnostic(value):
    if type(value) is not dict or type(value.get('status')) is not str:return None
    status=value['status']
    if status in {'clicked','already_available'} and set(value)=={'status'}:return {'schema':'native-quantity-expansion/v1','status':status}
    if status=='unavailable' and set(value)=={'status','reason'} and type(value['reason']) is str and value['reason'] in QUANTITY_EXPAND_REASONS:return {'schema':'native-quantity-expansion/v1','status':status,'reason':value['reason']}
    return None
def quantity_expand_js(attributes,source,*,controller_proved,ui_proved,assets_proved,full_domain=False):
    return r'''({selection,rows})=>{
 const no=reason=>({status:"unavailable",reason});
 if(!PROVED||location.href!==SOURCE||location.origin!=="https://4over.com"||location.search||location.hash)return no("source_unproved");
 const read=(READER),dom=read(),c=dom.source_controller,ui=dom.source_ui;
 const yes=(o,ks)=>o&&ks.every(k=>o[k]===true),clear=(o,ks)=>o&&ks.every(k=>o[k]===false);
 if(dom.child_id!==selection.child_id||!Object.entries(selection.attributes).every(([k,v])=>{const f=dom.attributes[k];return f?.count===1&&f.value===v&&f.enabled===true&&f.choice_enabled===true&&(f.visible===true||f.implicit_fixed_dash===true);})
   ||!["job_sample","digital_proof"].every(k=>dom.services[k]?.count===1&&dom.services[k].enabled===true&&dom.services[k].checked===false)
   ||!yes(c,["modules_proved","available","registration_defined","child_matches_dom","quantity_matches_dom","quantity_strict_row","unit_price_positive","item_total_positive","ready"])
   ||!yes(ui,["modules_proved","registry_available","tabs_registered","project_registered","tab_domain_observed","selected_tab_in_domain","current_is_product_options","next_is_shipping","guest_restriction_observed","tabs_loading_observed","project_loading_observed","shipping_active_observed"])
   ||!clear(ui,["guest_restricted","tabs_loading","project_loading","shipping_active"])||ui.form_count!==1)return no("context_unretained");
 const visible=e=>{if(!e||!e.getClientRects().length)return false;for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(n.hidden||n.hasAttribute('inert')||s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse')return false;}return true;};
 const forms=[...document.querySelectorAll('#product_addtocart_form')];if(forms.length!==1)return no("form_unretained");
 const tables=[...forms[0].querySelectorAll('ul.runsizes-table')].filter(visible);if(tables.length!==1)return no("table_unmounted");const table=tables[0];
 const search=[...forms[0].querySelectorAll('#run-size-search')];if(search.length>1||search.some(e=>String(e.value)!==''))return no("search_active");
 const r=window.require,name='FourOver_Project/js/model/product-package-prices';
 if(typeof r!=='function'||typeof r.defined!=='function'||r.defined(name)!==true)return no("table_unretained");
 try{
  const model=r(name);if(typeof model!=='function'||typeof model.peek!=='function')return no("table_unretained");const current=model.peek();
  if(!Array.isArray(rows)||!rows.length||rows.length>1000||!Array.isArray(current)||current.length!==rows.length
   ||!rows.every((v,i)=>Number.isSafeInteger(v.quantity)&&v.quantity>0&&typeof v.price==='string'&&/^[0-9]+(?:\.[0-9]+)?$/.test(v.price)&&Number(v.price)>0
       &&current[i]?.qty===v.quantity&&["number","string"].includes(typeof current[i]?.price)&&Number(current[i].price)===Number(v.price)))return no("table_unretained");
  const quantities=rows.map(v=>v.quantity);if(new Set(quantities).size!==quantities.length||!quantities.includes(selection.run_size))return no("quantity_unproved");
  const shown=[...table.querySelectorAll('li.runsizes_row:not(.custom_runsize_row)')].filter(visible).map(e=>String(e.querySelector('.runsize')?.textContent||'').trim().replace(/,/g,''));
  if(shown.includes(String(selection.run_size))&&!FULL_DOMAIN)return {status:"already_available"};
  if((!FULL_DOMAIN&&quantities.indexOf(selection.run_size)<7)||shown.length!==7||!shown.every((v,i)=>v===String(quantities[i])))return no("collapsed_rows_unmounted");
  const actions=[...table.querySelectorAll('li.runsizes-table_actions')].filter(visible);if(actions.length!==1||actions[0].classList.contains('active'))return no("collapsed_action_unmounted");
  const action=actions[0];if(action.getAttribute('data-bind')?.replace(/\s+/g,' ').trim()!=="css: {'active': isExpanded()}, visible: isCollapsable()")return no("action_binding_changed");
  const spans=[...action.querySelectorAll(':scope > span')].filter(visible);if(spans.length!==1)return no("action_unmounted");const e=spans[0];
  if(String(e.textContent||'').replace(/\s+/g,' ').trim()!=='Show More'||e.getAttribute('data-bind')?.replace(/\s+/g,' ').trim()!=="click: collapsibleToggle, i18n: isExpanded() ? 'Show Less' : 'Show More'"
    ||e.hasAttribute('onclick')||e.closest('a,button')||e.getAttribute('aria-disabled')==='true'||action.getAttribute('aria-disabled')==='true')return no("action_binding_changed");
  HTMLElement.prototype.click.call(e);return {status:"clicked"};
 }catch(_){return no("table_unretained");}
}'''.replace('PROVED',json.dumps(controller_proved is True and ui_proved is True and assets_proved is True)).replace('FULL_DOMAIN',json.dumps(full_domain is True)).replace('SOURCE',json.dumps(source)).replace('READER',automatic_observed_state_js(attributes,source,controller_proved,ui_proved))


def native_domain_read_js(attributes,source,*,controller_proved,ui_proved):
    """Read product controls only; no click, model write or private project read.

    The public package model is recorded separately from the visible quantity
    rows. A collapsed list therefore stays partial even if the model has every
    tier. Conditional production is the observed quantity's disclosure only.
    """
    return r'''({selection,rows})=>{
 const no=reason=>({status:"unknown",reason});
 if(!PROVED||location.href!==SOURCE||location.origin!=="https://4over.com"||location.search||location.hash)return no("source_unproved");
 const dom=(READER)(),yes=(o,keys)=>o&&keys.every(k=>o[k]===true);
 if(dom.child_id!==selection.child_id||!yes(dom.source_controller,["modules_proved","available","registration_defined","child_matches_dom","quantity_matches_dom","quantity_strict_row","ready"])
   ||!yes(dom.source_ui,["modules_proved","registry_available","tabs_registered","project_registered","current_is_product_options"]))return no("retained_product_options_unproved");
 const forms=[...document.querySelectorAll('#product_addtocart_form')];if(forms.length!==1)return no("form_unproved");const form=forms[0];
 const visible=e=>{if(!e||!e.getClientRects().length)return false;for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(n.hidden||n.hasAttribute('inert')||s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse')return false;}return true;};
 const text=e=>String(e?.textContent||'').replace(/\s+/g,' ').trim();
 const label=e=>{const labels=[...(e.labels||[])];return labels.length===1?text(labels[0]):text(e.closest('label'));};
 const attrs={},keys=Object.keys(selection.attributes);
 if(!keys.length||keys.length>32)return no("attribute_domain_unproved");
 for(const key of keys){
  const nodes=[...form.querySelectorAll('select')].filter(e=>e.name==='super_attribute['+key+']'),observed=dom.attributes[key];
  if(nodes.length!==1||!observed||observed.count!==1||observed.value!==selection.attributes[key]||observed.enabled!==true||observed.choice_enabled!==true||!(observed.visible===true||observed.implicit_fixed_dash===true))return no("attribute_domain_unretained");
  const e=nodes[0];if(e.options.length>512)return no("attribute_domain_too_large");
  attrs[key]={count:1,selected:e.value,enabled:!e.disabled,visible:visible(e),implicit_fixed_dash:observed.implicit_fixed_dash===true,
   options:[...e.options].map(o=>({value:o.value,label:text(o),enabled:!o.disabled&&!o.parentElement?.disabled}))};
 }
 const scope={};for(const key of ["job_sample","digital_proof"]){const nodes=[...form.querySelectorAll('input[type="checkbox"]')].filter(e=>e.name===key),known=dom.services[key];
  if(nodes.length!==1||known?.count!==1||known.checked!==false||known.enabled!==true)return no("scope_controls_unretained");
  scope[key]={count:1,checked:nodes[0].checked,enabled:!nodes[0].disabled,visible:visible(nodes[0])};}
 if(dom.set_control_count===0&&dom.sets===null)scope.sets={count:0,binding:"one_native_job"};
 else if(dom.set_control_count===1&&dom.sets?.value==="1"&&dom.sets.valid===true&&dom.sets.enabled===true)scope.sets={count:1,binding:"retained_control",value:1,enabled:true,valid:true};
 else return no("scope_controls_unretained");
 const tables=[...form.querySelectorAll('ul.runsizes-table')].filter(visible);if(tables.length!==1)return no("quantity_table_unmounted");const table=tables[0];
 const searches=[...form.querySelectorAll('#run-size-search')];if(searches.length>1||searches.some(e=>String(e.value)!==''))return no("quantity_search_active");
 const native=[...table.querySelectorAll('li.runsizes_row:not(.custom_runsize_row)')].filter(visible);
 if(!native.length||native.length>1000)return no("quantity_rows_unproved");
 const shown=native.map(e=>{const nodes=e.querySelectorAll('.runsize'),label=nodes.length===1?text(nodes[0]):'';return {value:label.replace(/,/g,''),label,enabled:!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'};});
 if(shown.some(v=>!/^\d+$/.test(v.value)||Number(v.value)<1||!Number.isSafeInteger(Number(v.value))))return no("quantity_rows_unproved");
 const modelName='FourOver_Project/js/model/product-package-prices',r=window.require;
 if(typeof r!=='function'||typeof r.defined!=='function'||r.defined(modelName)!==true)return no("quantity_model_unproved");
 const model=r(modelName);if(typeof model!=='function'||typeof model.peek!=='function')return no("quantity_model_unproved");const current=model.peek();
 if(!Array.isArray(rows)||!rows.length||rows.length>1000||!Array.isArray(current)||current.length!==rows.length
   ||!rows.every((v,i)=>Number.isSafeInteger(v.quantity)&&v.quantity>0&&typeof v.price==='string'&&/^[0-9]+(?:\.[0-9]+)?$/.test(v.price)&&Number(v.price)>0&&current[i]?.qty===v.quantity&&Number(current[i].price)===Number(v.price)))return no("quantity_model_unretained");
 const actions=[...table.querySelectorAll('li.runsizes-table_actions')].filter(visible),spans=actions.flatMap(e=>[...e.querySelectorAll(':scope > span')].filter(visible));
 if(actions.length>1||spans.length>1||spans.some(e=>!["Show More","Show Less"].includes(text(e))))return no("quantity_expansion_unproved");
 const more=spans.filter(e=>text(e)==='Show More').length;
 const extra=[];for(const e of [...form.querySelectorAll('select,input:not([type="hidden"]),textarea')].filter(visible)){
  const name=String(e.name||'');if(keys.some(k=>name==='super_attribute['+k+']')||["run_size","custom_run_size","turnaround_time","job_sample","digital_proof","qty","sets","project_name","trim_size"].includes(name)||e.id==='run-size-search'||e.id==='project_name'||['button','submit','reset'].includes(e.type))continue;
  extra.push({tag:e.tagName.toLowerCase(),type:String(e.type||''),name:/^[A-Za-z0-9_\[\]-]{1,100}$/.test(name)?name:"unrecognized"});}
 if(extra.length>32)return no("additional_controls_too_large");
 const production=[...form.querySelectorAll('input[name="turnaround_time"]')].map(e=>({value:e.value,label:label(e),selected:e.checked===true,enabled:!e.disabled,visible:visible(e)||[...(e.labels||[])].some(visible)}));
 if(production.length>32)return no("production_domain_too_large");
 return {status:"observed",child_id:dom.child_id,observed_run_size:dom.run_size,selected_attributes:selection.attributes,attribute_controls:attrs,scope_controls:scope,additional_option_fields:extra,
  quantity_controls:{rendered:shown,model_available:current.map(e=>e.qty),remaining_expand_controls:more,truncated:more!==0||shown.length!==current.length},production_controls:production};
}'''.replace('PROVED',json.dumps(controller_proved is True and ui_proved is True)).replace('SOURCE',json.dumps(source)).replace('READER',automatic_observed_state_js(attributes,source,controller_proved,ui_proved))


NATIVE_DOMAIN_REASONS=frozenset(('source_unproved','retained_product_options_unproved','form_unproved','attribute_domain_unproved',
    'attribute_domain_unretained','attribute_domain_too_large','scope_controls_unretained','quantity_table_unmounted','quantity_search_active',
    'quantity_rows_unproved','quantity_model_unproved','quantity_model_unretained','quantity_expansion_unproved','additional_controls_too_large',
    'production_domain_too_large','native_domain_observation_unavailable','native_domain_observation_invalid','table_identity_unproved'))


def safe_native_domain_observation(raw,*,source,selection,table,document_sha256,page_generation,snapshot_id,observed_at):
    """Closed public evidence projection. Never interprets omitted data as pass."""
    unknown={'schema':'fourover-native-domain-observation/v1','status':'unknown','reason':'native_domain_observation_invalid',
             'expanded_rendered_domain':'unknown','source_domain_complete':False}
    if type(raw) is not dict:return unknown
    if raw.get('status')=='unknown':
        reason=raw.get('reason')
        if set(raw)=={'status','reason'} and type(reason) is str and reason in NATIVE_DOMAIN_REASONS:return {**unknown,'reason':reason}
        return unknown
    try:
        if (_url(source).netloc!='4over.com' or set(raw)!={'status','child_id','observed_run_size','selected_attributes','attribute_controls','scope_controls','additional_option_fields','quantity_controls','production_controls'}
                or raw['status']!='observed' or raw['child_id']!=selection['child_id'] or raw['selected_attributes']!=selection['attributes']
                or type(raw['observed_run_size']) is not str or not ID.fullmatch(raw['observed_run_size'])
                or not HEX.fullmatch(document_sha256) or not HEX.fullmatch(table['sha256'])
                or not re.fullmatch('[a-f0-9]{32}',page_generation) or not re.fullmatch('[a-f0-9]{32}',snapshot_id)
                or type(observed_at) not in (int,float) or not isfinite(observed_at) or observed_at<=0):return unknown
        attrs=raw['attribute_controls'];scope=raw['scope_controls'];quantities=raw['quantity_controls'];production=raw['production_controls'];extra=raw['additional_option_fields']
        if type(attrs) is not dict or set(attrs)!=set(selection['attributes']) or type(scope) is not dict or set(scope)!={'sets','job_sample','digital_proof'}:return unknown
        def label(text):return type(text) is str and len(text)<=160 and not any(ord(c)<32 for c in text)
        for key,value in attrs.items():
            if (type(value) is not dict or set(value)!={'count','selected','enabled','visible','implicit_fixed_dash','options'} or value['count']!=1 or type(value['count']) is not int
                    or value['selected']!=selection['attributes'][key] or value['enabled'] is not True or type(value['visible']) is not bool or type(value['implicit_fixed_dash']) is not bool
                    or not(value['visible'] or value['implicit_fixed_dash']) or type(value['options']) is not list or not 1<=len(value['options'])<=512):return unknown
            seen=set()
            for option in value['options']:
                if (type(option) is not dict or set(option)!={'value','label','enabled'} or type(option['value']) is not str or option['value'] and not ID.fullmatch(option['value'])
                        or option['value'] in seen or not label(option['label']) or type(option['enabled']) is not bool):return unknown
                seen.add(option['value'])
            if sum(o['value']==value['selected'] and o['enabled'] for o in value['options'])!=1:return unknown
        for key in ('job_sample','digital_proof'):
            value=scope[key]
            if type(value) is not dict or set(value)!={'count','checked','enabled','visible'} or type(value['count']) is not int or value['count']!=1 or value['checked'] is not False or value['enabled'] is not True or type(value['visible']) is not bool:return unknown
        sets=scope['sets']
        if sets!={'count':0,'binding':'one_native_job'} and sets!={'count':1,'binding':'retained_control','value':1,'enabled':True,'valid':True}:return unknown
        if type(sets.get('count')) is not int or 'value' in sets and type(sets['value']) is not int:return unknown
        if (type(quantities) is not dict or set(quantities)!={'rendered','model_available','remaining_expand_controls','truncated'} or type(quantities['rendered']) is not list
                or not 1<=len(quantities['rendered'])<=1000 or type(quantities['model_available']) is not list or not 1<=len(quantities['model_available'])<=1000
                or type(quantities['remaining_expand_controls']) is not int or not 0<=quantities['remaining_expand_controls']<=1 or type(quantities['truncated']) is not bool):return unknown
        expected=[r['quantity'] for r in table['rows']]
        if quantities['model_available']!=expected or any(type(q) is not int or q<1 for q in expected) or len(expected)!=len(set(expected)):return unknown
        shown=[]
        for row in quantities['rendered']:
            if type(row) is not dict or set(row)!={'value','label','enabled'} or type(row['value']) is not str or not ID.fullmatch(row['value']) or not label(row['label']) or type(row['enabled']) is not bool:return unknown
            if row['label'].replace(',','')!=row['value']:return unknown
            shown.append(int(row['value']))
        if len(shown)!=len(set(shown)) or any(q not in expected for q in shown):return unknown
        if type(extra) is not list or len(extra)>32:return unknown
        for field in extra:
            if (type(field) is not dict or set(field)!={'tag','type','name'} or field['tag'] not in {'input','select','textarea'}
                    or not label(field['type']) or not re.fullmatch(r'[A-Za-z0-9_\[\]-]{1,100}',field['name'])):return unknown
        if type(production) is not list or len(production)>32:return unknown
        for row in production:
            if (type(row) is not dict or set(row)!={'value','label','selected','enabled','visible'} or type(row['value']) is not str or not ID.fullmatch(row['value'])
                    or not label(row['label']) or any(type(row[k]) is not bool for k in ('selected','enabled','visible'))):return unknown
        complete=(shown==expected and not quantities['truncated'] and quantities['remaining_expand_controls']==0 and all(row['enabled'] for row in quantities['rendered']))
        result={'schema':'fourover-native-domain-observation/v1',**raw,'source_url':source,'scope':dict(SCOPE),'observed_at':observed_at,
                'document_sha256':document_sha256,'price_response_sha256':table['sha256'],'page_generation':page_generation,'snapshot_id':snapshot_id,
                'expanded_rendered_domain':'verified' if complete else 'unknown','source_domain_complete':False,
                'production_scope':'observed_current_quantity_only','model_values_sha256':digest(_encoded(quantities['model_available']))}
        result['rendered_controls_sha256']=digest(_encoded({key:raw[key] for key in ('selected_attributes','attribute_controls','scope_controls','quantity_controls','production_controls','additional_option_fields')}))
        if len(_encoded(result))>256*1024:return unknown
        return result
    except (KeyError,TypeError,ValueError,OverflowError):return unknown

TRIM_PUBLIC_MODULES={
 'FourOver_TrimSize/js/view/register-options-manager.js':'9fd9d14b87a9976f23adb22485f070512fae3f2e442d5553779b2020dc9163c4',
 'FourOver_TrimSize/js/model/product/trim-size-options-manager.js':'6c64b1c058c7dd03ec5ea32abe0a92af2cee7000cc7faef324d672be711d56ec',
 'FourOver_TrimSize/js/model/custom-trim-size.js':'d8b19144993cfd8accd304e147f09f9aa8b5f856f36b7462471dee746292c24d'}
PROCEED_REASONS=frozenset(('source_unproved','modules_unavailable','registry_model_unavailable','model_identity_changed','form_unavailable','attribute_control_count','control_identity_changed','project_not_default','product_quantity_binding_changed','options_shape_changed','options_keys_unrecognized','attribute_values_changed','service_values_changed','trim_literal_unproved','production_binding_changed','tab_domain_changed','context_exception','visible_prerequisites_unretained','prior_model_unretained','post_model_unretained','production_disclosure_changed'))
def safe_proceed_diagnostic(value):
    if type(value) is not dict or set(value)!={'schema','phase','reason'} or value.get('schema')!='native-proceed-probe-diagnostic/v1' or value.get('phase') not in {'atomic_before','model_before','model_after'} or value.get('reason') not in PROCEED_REASONS:return None
    return dict(value)

PROCEED_MODEL_PROBE_JS=r'''({source,dom,selection,proved,trim_proved,identity,production_required}) => {
 const no=reason=>({context:{schema:"native-proceed-project-context/v1",observed:false,reason},identity:null});
 if(proved!==true||location.href!==source||location.origin!=="https://4over.com"||location.search||location.hash)return no('source_unproved');
 const r=window.require,names=["uiRegistry","FourOver_Project/js/view/project-tabs","FourOver_Project/js/view/project","FourOver_Project/js/model/project"];
 if(typeof r!=="function"||typeof r.defined!=="function"||!names.every(n=>r.defined(n)===true))return no('modules_unavailable');
 try{
  const registry=r("uiRegistry"),model=r("FourOver_Project/js/model/project");
  if(!registry||typeof registry.get!=="function"||typeof model!=="function"||typeof model.peek!=="function")return no('registry_model_unavailable');
  const data=model.peek(),tabs=registry.get("project-tabs"),view=registry.get("project");
  if(!data||typeof data!=="object"||!tabs||!view||identity&&(identity.model!==data||identity.tabs!==tabs||identity.view!==view))return no('model_identity_changed');
  const forms=[...document.querySelectorAll('#product_addtocart_form')];if(forms.length!==1)return no('form_unavailable');const form=forms[0],controls={};
  for(const key of Object.keys(selection.attributes)){const es=[...form.querySelectorAll(`select[name="super_attribute[${key}]"]`)];if(es.length!==1)return no('attribute_control_count');controls[key]=es[0];}
  const productionControls=[...form.querySelectorAll('input[name="turnaround_time"]')].filter(e=>e.checked&&!e.disabled);
  const serviceControls=["job_sample","digital_proof"].map(k=>[...form.querySelectorAll(`input[name="${k}"][type="checkbox"]`)]);
  if(productionControls.length!==1||serviceControls.some(v=>v.length!==1)||identity&&(identity.form!==form||identity.production!==productionControls[0]||Object.keys(controls).some(k=>identity.controls[k]!==controls[k])||serviceControls.some((v,i)=>identity.services[i]!==v[0])))return no('control_identity_changed');
  // Only these public product scalars are read. Private UUID/address/name fields
  // remain opaque on the RAM-only identity handle and are never serialized.
  const own=(obj,key)=>{const d=Object.getOwnPropertyDescriptor(obj,key);return d&&"value" in d?d.value:undefined;};
  const scalar=v=>typeof v==="number"&&Number.isSafeInteger(v)?String(v):typeof v==="string"?v:null;
  const read=(obj,key)=>{const v=own(obj,key);return typeof v==="function"&&typeof v.peek==="function"?v.peek():undefined;};
  if(own(data,"state")!=="default")return no('project_not_default');
  const product=scalar(own(data,"productId")),quantity=scalar(read(data,"runSize")),custom=scalar(read(data,"customRunSize"));
  const options=read(data,"productOptions"),attrs=options&&own(options,"super_attribute");
  if(!product||!dom.parent_ids.includes(product)&&product!==selection.child_id||quantity!==String(selection.run_size)||quantity!==dom.run_size||custom!=="0")return no('product_quantity_binding_changed');
  if(!options||typeof options!=="object"||Array.isArray(options)||!attrs||typeof attrs!=="object"||Array.isArray(attrs))return no('options_shape_changed');
  if(Object.keys(options).some(k=>!["super_attribute","turnaround_time","job_sample","digital_proof","trim_size"].includes(k))||Object.keys(attrs).sort().join("|")!==Object.keys(selection.attributes).sort().join("|"))return no('options_keys_unrecognized');
  const trimPresent=Object.prototype.hasOwnProperty.call(options,"trim_size");
  if(trimPresent){
   const trimNames=TRIM_NAMES;
   if(trim_proved!==true||!trimNames.every(n=>r.defined(n)===true)||own(options,"trim_size")!=="")return no('trim_literal_unproved');
   const customTrim=r("FourOver_TrimSize/js/model/custom-trim-size");
   if(typeof customTrim!=="function"||typeof customTrim.peek!=="function"||customTrim.peek()!==false)return no('trim_literal_unproved');
  }
  const values={};for(const [id,value] of Object.entries(selection.attributes)){if(scalar(own(attrs,id))!==value||dom.attributes[id]?.value!==value)return no('attribute_values_changed');values[id]=value;}
  const off=v=>v===undefined||v===false||v===0||v===""||v==="0"||v==="false";
  if(!["job_sample","digital_proof"].every(k=>off(own(options,k))&&dom.services[k]?.count===1&&dom.services[k]?.checked===false&&dom.services[k]?.enabled===true))return no('service_values_changed');
  const production=scalar(own(options,"turnaround_time"));
  if(production_required===true&&(!dom.production||production!==dom.production.value))return no('production_binding_changed');
  const selected=read(tabs,"selectedTab"),domain=tabs.tabsData,shipping=tabs.shippingAndSetsTab;
  const alias=v=>typeof v==="string"&&/^[A-Za-z0-9_.-]{1,160}$/.test(v);
  if(!alias(selected)||!alias(shipping)||!Array.isArray(domain)||!domain.length||domain.length>32||!domain.every(v=>v&&alias(v.alias)))return no('tab_domain_changed');
  const aliases=domain.map(v=>v.alias),productTab=tabs.productOptionsTab||aliases[0];
  if(new Set(aliases).size!==aliases.length||!alias(productTab)||!aliases.includes(selected)||aliases.indexOf(shipping)!==aliases.indexOf(productTab)+1)return no('tab_domain_changed');
  return {context:{schema:"native-proceed-project-context/v1",observed:true,product_id:product,child_id:selection.child_id,run_size:selection.run_size,custom_run_size:0,
   attributes:values,services_off:true,trim_size_binding:trimPresent?"source_literal_blank":"absent",production,tab_domain:aliases,product_tab:productTab,shipping_tab:shipping,selected_tab:selected},identity:{model:data,tabs,view,form,controls,production:productionControls[0],services:serviceControls.map(v=>v[0])}};
 }catch(_){return no('context_exception');}
}'''.replace('TRIM_NAMES',json.dumps([p.removesuffix('.js') for p in TRIM_PUBLIC_MODULES]))

def automatic_proceed_action_js(attributes, *, proved, trim_proved=False):
    """Exact normal Proceed; retain model identity without serializing it."""
    return r'''({source,selection,click}) => {
 const action=(ACTION),read=(READER),probe=(PROBE);
 const diag=(phase,reason)=>({schema:"native-proceed-probe-diagnostic/v1",phase,reason});
 const available=action({source,selection,click:false});
 if(available.status!=="available")return {result:available,identity:null};
 if(available.diagnostic.labels.proceed_to_shipping!==1)return {result:action({source,selection,click}),identity:null};
 const before=read(),ui=before.source_ui,c=before.source_controller;
 const yes=(o,ks)=>o&&ks.every(k=>o[k]===true),clear=(o,ks)=>o&&ks.every(k=>o[k]===false);
 if(!yes(c,["modules_proved","available","registration_defined","child_matches_dom","quantity_matches_dom","quantity_strict_row","unit_price_positive","item_total_positive","ready","tat_matches_dom","tat_ready"])
   ||!yes(ui,["modules_proved","registry_defined","tabs_module_defined","project_module_defined","registry_available","tabs_registered","project_registered","tab_domain_observed","selected_tab_in_domain","current_is_product_options","next_is_shipping","next_tab_available","guest_restriction_observed","tabs_loading_observed","project_loading_observed","shipping_active_observed","project_name_invalid_observed"])
   ||!clear(ui,["guest_restricted","tabs_loading","project_loading","shipping_active","project_name_invalid"])||ui.form_count!==1||ui.visible_error_count!==0
   ||before.production_control_count!==1||before.production?.visible!==true)return {result:{status:"context_changed",diagnostic:available.diagnostic,proceed_probe:diag("atomic_before","visible_prerequisites_unretained")},identity:null};
 const prior=probe({source,dom:before,selection,proved:PROVED,trim_proved:TRIM_PROVED,identity:null,production_required:false});
 if(prior.context.observed!==true||prior.context.selected_tab!==prior.context.product_tab)return {result:{status:"context_changed",diagnostic:available.diagnostic,proceed_probe:diag("model_before",prior.context.reason||"prior_model_unretained")},identity:null};
 const result=action({source,selection,click});
 if(result.status!=="clicked")return {result,identity:null};
 const after=read(),current=probe({source,dom:after,selection,proved:PROVED,trim_proved:TRIM_PROVED,identity:prior.identity,production_required:true});
 if(current.context.observed!==true||current.context.selected_tab!==current.context.shipping_tab
   ||JSON.stringify(current.context.tab_domain)!==JSON.stringify(prior.context.tab_domain)
   ||current.context.product_tab!==prior.context.product_tab||current.context.shipping_tab!==prior.context.shipping_tab
   ||current.context.product_id!==prior.context.product_id
   ||after.production?.value!==before.production.value||after.production?.label!==before.production.label)return {result:{status:"context_changed",diagnostic:result.diagnostic,proceed_probe:diag("model_after",current.context.reason||"production_disclosure_changed")},identity:null};
 return {result:{...result,proceed_context:{schema:"native-proceed-transition/v1",before,project_context:current.context}},identity:prior.identity};
}'''.replace('ACTION',automatic_quote_button_js(attributes)).replace('READER',automatic_observed_state_js(attributes,'__SOURCE__',True,True).replace('"__SOURCE__"','source')).replace('PROBE',PROCEED_MODEL_PROBE_JS).replace('TRIM_PROVED',json.dumps(trim_proved is True)).replace('PROVED',json.dumps(proved is True))

def proceed_transition_state_js(attributes,source,selection,controller_proved,ui_proved,name_proved,trim_proved=False,summary_proved=False):
    reader=automatic_observed_state_js(attributes,source,controller_proved,ui_proved,summary_proved)
    return '(identity)=>{const dom=('+reader+')();dom.proceed_project_context=('+PROCEED_MODEL_PROBE_JS+')({source:'+json.dumps(source)+',dom,selection:'+json.dumps(selection)+',proved:'+json.dumps(controller_proved and ui_proved and name_proved)+',trim_proved:'+json.dumps(trim_proved is True)+',identity,production_required:true}).context;return dom;}'

def proceed_retained_context(state,selection,receipt,*,epoch,generation):
    """Only an exact same-page atomic normal-Proceed handoff may hide controls."""
    if type(receipt) is not dict or receipt.get('schema')!='native-proceed-retention/v1' or type(receipt.get('epoch')) is not int or receipt.get('epoch')!=epoch or receipt.get('generation')!=generation or receipt.get('selection')!=selection:
        return False
    before=receipt.get('before');project=receipt.get('project_context');current=state.get('proceed_project_context')
    if (type(before) is not dict or not context_retained(before,selection=selection) or not controller_ready(before,tat=True)
            or not ui_ready(before) or not controller_ready(state) or not services_off(state)
            or not attributes_retained(state,selection=selection) or state.get('run_size')!=str(selection['run_size'])
            or type(project) is not dict or project.get('observed') is not True or type(current) is not dict or current.get('observed') is not True or current!=project
            or current.get('selected_tab')!=current.get('shipping_tab') or current.get('production')!=before.get('production',{}).get('value')):
        return False
    ui=state.get('source_ui');previous=before.get('source_ui')
    if (type(ui) is not dict or type(previous) is not dict or not ui_ready(state)
            or any(previous.get(k) is not True for k in ('current_is_product_options','next_is_shipping'))
            or any(ui.get(k) is not True for k in ('current_is_shipping','shipping_active_observed','shipping_active','tabs_loading_observed','project_loading_observed','project_name_invalid_observed'))
            or any(ui.get(k) is not False for k in ('current_is_product_options','current_is_addresses','tabs_loading','project_loading','project_name_invalid'))
            or ui.get('visible_error_count')!=0):
        return False
    production=state.get('production');prior=before.get('production')
    if (type(production) is not dict or type(prior) is not dict or prior.get('visible') is not True
            or state.get('production_control_count')!=1 or type(production.get('visible')) is not bool
            or {k:production.get(k) for k in ('value','label')}!={k:prior.get(k) for k in ('value','label')}
            or state.get('source_controller',{}).get('tat_matches_dom') is not True):
        return False
    return all(type(state['attributes'][key].get('visible')) is bool for key in selection['attributes'])


ADDRESS_LOOKUP_URL='https://4over.com/rest/default/V1/project/mine/addresses'
ADDRESS_PUBLIC_MODULES={
 'FourOver_Project/js/model/project/address-manager.js':'ddcd935cd9246d866eaa2bb163da76c1d141a1bbc5807e254d7e483d075440e8',
 'FourOver_Project/js/action/project/load-addresses.js':'04304d24fab6d22c1be590a079c09a3ffa93bcd8bd58c4f24048f3f778095dec'}
ACCOUNT_PROFILE_GATE_JS=r'''() => [...document.querySelectorAll('#b2c-company-modal')].some(e=>{
 const shell=e.closest('.b2c-company-popup._show');
 return !!shell&&shell.getClientRects().length>0&&e.getClientRects().length>0
   &&getComputedStyle(shell).visibility!=="hidden"&&getComputedStyle(e).visibility!=="hidden";
})'''

def native_address_lookup_body(raw):
    # Source getList passes only source-owned address IDs. This bounded supported
    # subset is not an invented default or a projection of private identifiers.
    if type(raw) is not str:return False
    try:
        if len(raw.encode('utf8'))>16384:return False
        value=json.loads(raw,object_pairs_hook=_unique_object)
    except (UnicodeError,ValueError,TypeError):return False
    if type(value) is not dict or set(value)!={'addressIds'} or type(value['addressIds']) is not list or len(value['addressIds'])>64:return False
    return all((type(v) is int and 1<=v<=9007199254740991)
        or (type(v) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',v)) for v in value['addressIds'])

def public_template_diagnostic(url,prefix):
    # Logging ONLY. Wider public namespace recognition never changes admission.
    try:
        parts=urlsplit(url);base=urlsplit(prefix)
        if (parts.scheme!='https' or parts.netloc!='4over.com' or parts.fragment
                or not parts.path.startswith(base.path) or not prefix.startswith('https://4over.com/')):return None
        suffix=parts.path[len(base.path):];segments=suffix.split('/')
        if len(suffix)>256 or re.search(r'(?:^|[-_./])[a-fA-F0-9]{16,}(?=$|[-_./])',suffix) or re.search(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}',suffix):return None
        if len(segments)<3 or not re.fullmatch('[A-Za-z][A-Za-z0-9]{0,63}_[A-Za-z][A-Za-z0-9]{0,63}',segments[0]) or segments[1] not in {'template','templates'}:return None
        # Namespace is part of the original public path privacy check too.
        if re.search(r'(?:^|[-_./])(?:private|token|session|password|passwd|secret|credential|authorization|email|formkey)(?:$|[-_./])',suffix,re.I):return None
        # Reuse existing canonical/private-identifier grammar for the remainder.
        normalized=prefix+'Magento_Ui/'+ '/'.join(segments[1:])
        safe,redaction=public_static_relative_path(normalized,prefix)
        if redaction is not None or not safe or not safe.endswith('.html'):return None
        query='none'
        if parts.query:
            query='other'
            try:
                pairs=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True,max_num_fields=1)
                if len(pairs)==1 and pairs[0][0]=='_' and re.fullmatch('[0-9]{1,20}',pairs[0][1]):query='cache_buster_only'
            except ValueError:pass
        return {'public_template_suffix':suffix,'query_class':query,'admission_changed':False}
    except (ValueError,TypeError,AttributeError):return None

def native_customer_query(parts):
    if parts.scheme!='https' or parts.netloc!='4over.com' or parts.path!='/customer/section/load/' or parts.fragment or len(parts.query)>4096:return False
    try:pairs=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True,max_num_fields=3)
    except ValueError:return False
    if len({k for k,v in pairs})!=len(pairs):return False
    for key,value in pairs:
        if key=='sections':
            names=value.split(',')
            if value and (not 1<=len(names)<=32 or any(not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',v) for v in names)):return False
        elif key=='force_new_section_timestamp':
            if value not in {'','true','false','0','1'}:return False
        elif key=='_':
            if not re.fullmatch('[A-Za-z0-9_-]{1,64}',value):return False
        else:return False
    return True

class AutomaticFourOverSession:
    """One owned page, fresh child table once, many retained quantity quotes."""
    def __init__(self,observer,loaded,profile,check_scope):
        if type(profile) is not dict or type(loaded) is not dict or profile.get('profile_id')!=PROFILE_ID or set(loaded)!={'catalog','cursor_path','cursor'}:raise Held('profile_invalid')
        self.observer=observer;self.catalog=loaded['catalog'];self.path=loaded['cursor_path'];self.cursor=loaded['cursor']
        self.store=observer.store;self.pool=observer.pool;self.check_scope=check_scope;self.source=self.catalog['source_url']
        self.limit=profile['max_quotes_per_session']
        if type(self.limit) is not int or not 1<=self.limit<=MAX_QUOTES:raise Held('profile_limit_invalid')
        self.target=self.catalog['entries'][0];self.selection=None;self.epoch=0;self.stage='bootstrap';self.deadline=None
        self.session_id=os.urandom(16).hex();self.tables={};self.price_pending=set();self.summary_pending=False;self.summary_seen=False;self.summary=None
        self.document_reserved=False;self.counts={'document':0,'price':0,'summary':0,'static':0};self.blocked={};self.static={};self.static_bytes=0;self.consumers=0
        self.handlers=set();self.public_static_handlers=set();self.failure=None;self.closed=False;self.prefix=None;self.tracker=False;self.last_static=time.monotonic()
        self.receipts_this_call=0;self.completed_this_call=0;self.initial_progress=self.cursor['progress_count'];self.visited=0;self.delay_seconds=0;self.last_failed_kind=None;self.session_deadline=time.monotonic()+300
        self.last_state=None;self.diagnostic=None;self.failure_request=None;self.failure_stage=None;self.failure_exception_type=None
        self.native_pricing_ready=False;self.cold_deadline=None;self.first_target_table_ready=False
        self.bootstrap_started=None;self.bootstrap_initial=None;self.bootstrap_hard_deadline=None;self.bootstrap_last_clock=None;self.bootstrap_progress_at=None;self.bootstrap_progress=set();self.name_module_expected=False
        self.price_flights={};self.price_metadata_handlers=set();self.price_bytes=0;self.customer_reads=0;self.customer_metadata_handlers=set();self.customer_declared=False;self.customer_module_declared=False;self.customer_flights={};self.other_routes={};self.public_assets={};self.public_asset_tasks=set();self.customer_module_tasks=set();self.public_asset_bytes=0;self.address_module_tasks=set();self.address_modules=set();self.controller_modules=set();self.ui_modules=set();self.address_reads=0;self.address_ready=False;self.address_flights={};self.address_metadata_handlers=set();self.account_profile_gate_observed=False
        self.name_modules=set();self.trim_modules=set();self.project_label_status=None;self.proceed_probe_diagnostic=None;self.quantity_expand_diagnostic=None;self.quantity_expand_assets=set();self.summary_module_proved=False
        self.summary_request_observed=False;self.quote_button_clicked=False;self.quote_button_diagnostic=None
        self.proceed_receipt=None;self.proceed_identity=None;self.quote_action_pending=False
        self.declared_bindings={row['child_id']:{f['attribute_id']:f['value'] for f in row['fields']} for row in self.catalog['rows']}
        if not self.declared_bindings:self.declared_bindings={e['child_id']:dict(e['attributes']) for e in self.catalog['entries']}
        self.rate,self.concurrency=source_pacing(next((p for p in self.store.profiles('source') if str(p.get('host',p.get('domain',''))).removeprefix('www.')=='4over.com'),{}),self.store.config)
    async def check(self):
        if self.closed:raise Held('automatic_session_closed')
        await self.check_scope()
        if file_identity(self.catalog['checkpoint_path'])!=self.catalog['checkpoint_identity']:raise Held('catalog_changed')
        if self.failure:raise Held(self.failure)
        self.phase_check()
    def phase_check(self):
        if time.monotonic()>=self.session_deadline:raise Held('automatic_session_budget')
        if self.deadline is not None and time.monotonic()>=self.deadline:raise Held('source_phase_timeout')
    async def state(self):
        if self.proceed_identity is not None and self.selection is not None:
            value=await self.page.evaluate(proceed_transition_state_js(self.target['attributes'],self.source,self.selection,
                len(self.controller_modules)==len(CONTROLLER_PUBLIC_MODULES),len(self.ui_modules)==len(UI_PUBLIC_MODULES),len(self.name_modules)==len(NAME_PUBLIC_MODULES),len(self.trim_modules)==len(TRIM_PUBLIC_MODULES),self.summary_module_proved),self.proceed_identity)
        else:
            value=await self.page.evaluate(automatic_observed_state_js(self.target['attributes'],self.source,len(self.controller_modules)==len(CONTROLLER_PUBLIC_MODULES),len(self.ui_modules)==len(UI_PUBLIC_MODULES),self.summary_module_proved))
        if await self.page.evaluate(ACCOUNT_PROFILE_GATE_JS) is True:
            self.account_profile_gate_observed=True;self.failure='source_account_profile_action_required';raise Held(self.failure)
        if value.get('blocked_popup_attempts',0):self.failure='unexpected_source_popup';raise Held(self.failure)
        self.last_state=value
        requirement=ui_action_requirement(value)
        if requirement and not (requirement=='source_project_details_required' and self.stage in {'bootstrap','child'} and not self.cursor.get('inflight')):
            self.capture_diagnostic(requirement,value);raise Held(requirement)
        return value
    def quote_context_state(self,state,selection):
        if self.proceed_receipt is None:return state
        if not proceed_retained_context(state,selection,self.proceed_receipt,epoch=self.epoch,generation=self.session_id):
            raise Held('source_proceed_transition_unretained')
        # Internal parser adapter only. Actual observations/receipt retain hidden
        # visibility and cite the pre-Proceed visible production disclosure.
        projected={**state,'attributes':{k:dict(v) for k,v in state['attributes'].items()},
            'production':dict(state['production']),'source_controller':dict(state['source_controller'])}
        for key in selection['attributes']:projected['attributes'][key]['visible']=True
        projected['production']['visible']=True;projected['source_controller']['tat_ready']=True
        return projected
    def quote_request_context(self,post_data,state,selection):
        literal_trim=self.proceed_receipt is not None and self.proceed_receipt['project_context'].get('trim_size_binding')=='source_literal_blank'
        projected=automatic_request_context(post_data,self.quote_context_state(state,selection),selection=selection,literal_blank_trim=literal_trim)
        if self.proceed_receipt is not None and projected['product_id']!=self.proceed_receipt['project_context']['product_id']:
            raise Held('source_quote_context_changed')
        return projected
    async def ordinary_quote_action(self,selection):
        if self.quote_action_pending or self.proceed_receipt is not None:raise Held('stale_quote_epoch')
        self.quote_action_pending=True;handle=None;identity=None
        try:
            proved=(len(self.controller_modules)==len(CONTROLLER_PUBLIC_MODULES) and len(self.ui_modules)==len(UI_PUBLIC_MODULES) and len(self.name_modules)==len(NAME_PUBLIC_MODULES))
            handle=await self.page.evaluate_handle(automatic_proceed_action_js(selection['attributes'],proved=proved,trim_proved=len(self.trim_modules)==len(TRIM_PUBLIC_MODULES)),{'source':self.source,'selection':selection,'click':True})
            result_handle=await handle.get_property('result')
            try:result=await result_handle.json_value()
            finally:await result_handle.dispose()
            await self.check()
            if self.stage!='quantity' or self.selection is not selection:raise Held('stale_quote_epoch')
            self.proceed_probe_diagnostic=safe_proceed_diagnostic(result.get('proceed_probe')) if type(result) is dict else None
            handoff=result.get('proceed_context') if type(result) is dict else None
            if handoff is not None:
                if type(handoff) is not dict or handoff.get('schema')!='native-proceed-transition/v1' or result.get('status')!='clicked':raise Held('source_proceed_transition_unretained')
                identity=await handle.get_property('identity')
                self.proceed_identity=identity;identity=None
                self.proceed_receipt={'schema':'native-proceed-retention/v1','epoch':self.epoch,'generation':self.session_id,
                    'selection':{'child_id':selection['child_id'],'run_size':selection['run_size'],'attributes':dict(selection['attributes'])},
                    'before':handoff['before'],'project_context':handoff['project_context']}
            return result
        finally:
            if identity is not None:await identity.dispose()
            if handle is not None:await handle.dispose()
            self.quote_action_pending=False
    def capture_diagnostic(self,reason,state=None,*,error=None,request=None):
        if self.failure_stage is None:self.failure_stage=self.stage
        if error is not None:self.failure_exception_type=safe_exception_type(error)
        if request is not None and self.failure_request is None:
            try:
                main=request.frame==self.page.main_frame
                projection=blocked_request_classification(request.url,request.method,request.resource_type,main)
                if request.url==self.source:projection['path_class']='product_document'
                self.failure_request=projection
            except Exception:self.failure_request={'origin_class':'invalid','path_class':'invalid','method_class':'OTHER','resource_type':'other','main_frame':False}
        bootstrap=self.bootstrap_diagnostic(state if state is not None else self.last_state)
        if self.diagnostic is not None:
            self.diagnostic.update(final_reason_code=reason,final_exception_type=self.failure_exception_type,
                project_label_preparation=self.project_label_status,native_proceed_probe=safe_proceed_diagnostic(self.proceed_probe_diagnostic),native_quantity_expansion=self.quantity_expand_diagnostic,rendered_money=public_money_diagnostic(state if state is not None else self.last_state),captured_quote_totals=public_captured_totals(self.summary),native_proof_counts={"quantity_expand":len(self.quantity_expand_assets),"summary_module":self.summary_module_proved},
                final_controlled_requests=dict(self.counts),final_blocked_requests=dict(self.blocked),final_bootstrap_readiness=bootstrap,final_source_controller=controller_diagnostic(state if state is not None else self.last_state),final_source_ui=ui_diagnostic(state if state is not None else self.last_state),address_readiness={'modules_proved':len(self.address_modules)==len(ADDRESS_PUBLIC_MODULES),'native_requests_sent':self.address_reads,'ready':self.address_ready,'account_profile_action_required':self.account_profile_gate_observed},customer_readiness={'document_declared':self.customer_declared,'public_module_declared':self.customer_module_declared,'native_requests_sent':self.customer_reads},blocked_route_fingerprints=list(self.other_routes.values()),public_source_assets=list(self.public_assets.values()))
            if self.diagnostic.get('request_classification') is None:self.diagnostic['request_classification']=self.failure_request
            if self.diagnostic.get('exception_type') is None:self.diagnostic['exception_type']=self.failure_exception_type
            self.cursor['last_failure_diagnostic']=self.diagnostic;self.persist();return
        selection=self.selection or {'child_id':self.target['child_id'],'run_size':0,'attributes':self.target['attributes']}
        projection=self.summary.get('request_context') if isinstance(self.summary,dict) else None
        self.diagnostic={'reason_code':reason,'stage':self.failure_stage if self.failure_stage in {'bootstrap','child','quantity','between'} else 'unknown',
            'project_label_preparation':self.project_label_status,'native_proceed_probe':safe_proceed_diagnostic(self.proceed_probe_diagnostic),'native_quantity_expansion':self.quantity_expand_diagnostic,'rendered_money':public_money_diagnostic(state if state is not None else self.last_state),'captured_quote_totals':public_captured_totals(self.summary),'native_proof_counts':{'quantity_expand':len(self.quantity_expand_assets),'summary_module':self.summary_module_proved},
            'exception_type':self.failure_exception_type,'request_classification':self.failure_request,'bootstrap_readiness':bootstrap,
            'final_bootstrap_readiness':bootstrap,
            'summary_seen':self.summary_seen is True,'summary_pending':self.summary_pending is True,'summary_captured':self.summary is not None,
            'summary_request_observed':self.summary_request_observed is True,'quote_button_clicked':self.quote_button_clicked is True,'quote_button':self.quote_button_diagnostic,
            'controlled_requests':dict(self.counts),'blocked_requests':dict(self.blocked),
            'address_readiness':{'modules_proved':len(self.address_modules)==len(ADDRESS_PUBLIC_MODULES),'native_requests_sent':self.address_reads,'ready':self.address_ready,'account_profile_action_required':self.account_profile_gate_observed},
            'customer_readiness':{'document_declared':self.customer_declared,'public_module_declared':self.customer_module_declared,'native_requests_sent':self.customer_reads},
            'final_reason_code':reason,'final_exception_type':self.failure_exception_type,
            'final_controlled_requests':dict(self.counts),'final_blocked_requests':dict(self.blocked),
            'blocked_route_fingerprints':list(self.other_routes.values()),'public_source_assets':list(self.public_assets.values()),
            'source_controller':controller_diagnostic(state if state is not None else self.last_state),
            'source_ui':ui_diagnostic(state if state is not None else self.last_state),
            'retained_context':automatic_context_diagnostic(state if state is not None else self.last_state,selection=selection,request_projection=projection)}
        self.cursor['last_failure_diagnostic']=self.diagnostic;self.persist()
    def bootstrap_diagnostic(self,state):
        now=time.monotonic();state=state if type(state) is dict else {}
        first=next(iter(self.target['attributes']));attrs=state.get('attributes',{});field=attrs.get(first,{}) if type(attrs) is dict else {}
        if type(field) is not dict:field={}
        public=[]
        for suffix,flight in self.static.items():
            if flight['pending']:
                public.append({'public_asset_suffix':suffix,'pending_consumers':min(4,max(0,flight['pending'])),
                    'consumers':min(4,max(0,flight['consumers'])),'ready':flight['ready'].done(),
                    'verified_response_available':flight['response'] is not None,
                    'age_seconds':round(min(330,max(0,now-flight.get('started_at',now))),3)})
        return {'prefix_present':bool(self.prefix),'document_captured':getattr(self,'document_sha256',None) is not None,
            'unique_static_count':len(self.static),'pending_asset_count':len(public),
            'pending_consumer_count':sum(v['pending_consumers'] for v in public),'pending_assets':public[:64],
            'verified_static_count':sum(f['response'] is not None for f in self.static.values()),
            'static_quiet_seconds':round(min(330,max(0,now-self.last_static)),3),
            'static_quiet_ready':now-self.last_static>=.25,'pending_price_count':len(self.price_pending),
            'native_pricing_handlers_observed':self.native_pricing_ready,'first_target_table_ready':self.first_target_table_ready,
            'cold_remaining_seconds':round(max(0,min(180,self.cold_deadline-now)),3) if self.cold_deadline is not None else None,
            'bootstrap_progress_events':len(self.bootstrap_progress),'essential_fields_mounted':self.bootstrap_fields_ready(state),
            'cold_hard_remaining_seconds':round(max(0,min(180,self.bootstrap_hard_deadline-now)),3) if self.bootstrap_hard_deadline is not None else None,
            'first_attribute':{'id':first,'count_one':field.get('count')==1,'enabled':field.get('enabled') is True,
                'visible':field.get('visible') is True,'implicit_fixed_dash':field.get('implicit_fixed_dash') is True,
                'source_control_ready':field.get('count')==1 and field.get('enabled') is True
                    and (field.get('visible') is True or field.get('implicit_fixed_dash') is True)}}
    def begin_bootstrap(self):
        now=time.monotonic()
        if type(now) not in (int,float) or not isfinite(now) or now<0 or type(self.session_deadline) not in (int,float) or not isfinite(self.session_deadline):raise Held('source_bootstrap_clock_changed')
        hard=min(now+180,self.session_deadline-36)
        if hard<=now:raise Held('automatic_session_budget')
        self.bootstrap_started=now;self.bootstrap_initial=min(now+95,hard);self.bootstrap_hard_deadline=hard
        self.bootstrap_last_clock=now;self.bootstrap_progress_at=now;self.bootstrap_progress=set()
        self.cold_deadline=self.bootstrap_initial;self.deadline=self.cold_deadline
    def note_bootstrap_progress(self,kind,key):
        if (kind not in {'admitted','verified'} or self.bootstrap_started is None or self.stage!='bootstrap'
                or self.closed or self.failure or self.selection is not None or self.cursor.get('inflight') is not None
                or self.summary_request_observed or self.counts['summary'] or self.page.url!=self.source
                or type(key) is not str or key not in self.static):return False
        now=time.monotonic()
        if type(now) not in (int,float) or not isfinite(now) or now<self.bootstrap_last_clock:raise Held('source_bootstrap_clock_changed')
        self.bootstrap_last_clock=now
        if now>=self.cold_deadline or now>=self.bootstrap_hard_deadline:raise Held('source_phase_timeout')
        identity=(kind,key)
        if identity in self.bootstrap_progress:return False
        self.bootstrap_progress.add(identity);self.bootstrap_progress_at=now
        self.cold_deadline=min(self.bootstrap_hard_deadline,max(self.bootstrap_initial,now+35))
        self.deadline=self.cold_deadline
        return True
    def bootstrap_fields_ready(self,state):
        if type(state) is not dict:return False
        if not services_off(state) or not ui_ready(state):return False
        ui=state.get('source_ui',{})
        if ui.get('current_is_product_options') is not True:return False
        expected=(self.name_module_expected or 'FourOver_Project/js/view/project/name.js' in self.name_modules
            or 'FourOver_Project/template/project/name.html' in self.static)
        field=ui.get('fields',{}).get('project_name',{})
        if expected and not (type(field.get('count')) is int and field['count']==1
                and type(field.get('visible_count')) is int and field['visible_count']==1):return False
        return True
    async def wait_bootstrap(self):
        # A dynamic bootstrap-only deadline follows distinct legitimate public
        # dependency progress. Child/quantity wait() and35s budgets are unchanged.
        while True:
            await self.check()
            now=time.monotonic()
            if type(now) not in (int,float) or not isfinite(now) or now<self.bootstrap_last_clock:raise Held('source_bootstrap_clock_changed')
            self.bootstrap_last_clock=now
            state=await self.state();await self.check()
            now=time.monotonic()
            if type(now) not in (int,float) or not isfinite(now) or now<self.bootstrap_last_clock:raise Held('source_bootstrap_clock_changed')
            self.bootstrap_last_clock=now
            if self.stage!='bootstrap' or self.selection is not None or self.cursor.get('inflight') is not None:raise Held('stale_quote_epoch')
            if now>=self.cold_deadline or now>=self.bootstrap_hard_deadline:raise Held('source_phase_timeout')
            if self.bootstrap_ready(state):return state
            await asyncio.sleep(.1)
    def bootstrap_ready(self,state):
        first=next(iter(self.target['attributes']));field=state.get('attributes',{}).get(first,{})
        control_ready=(field.get('count')==1 and field.get('enabled') is True
            and (field.get('visible') is True or field.get('implicit_fixed_dash') is True))
        static_ready=not any(f['pending'] for f in self.static.values()) and all(f.get('response') is not None for f in self.static.values()) and time.monotonic()-self.last_static>=.25
        return bool(self.prefix) and control_ready and self.bootstrap_fields_ready(state) and not self.public_asset_tasks and not self.price_pending and (self.native_pricing_ready or static_ready)
    async def observe_native_readiness(self,request,parts,child,query):
        binding=self.declared_bindings.get(child)
        if (self.stage!='bootstrap' or self.native_pricing_ready or not self.prefix or not binding
                or request.frame!=self.page.main_frame or request.method!='GET' or request.resource_type not in {'xhr','fetch'}
                or parts.scheme!='https' or parts.netloc!='4over.com' or parts.fragment
                or parts.path!='/rest/default/V1/product/prices/'+child
                or not (not query or len(query)==1 and query[0][0]=='_' and re.fullmatch('[A-Za-z0-9_-]{1,64}',query[0][1]))
                or self.page.url!=self.source):return
        await self.check()
        state=await self.page.evaluate(automatic_state_js(binding))
        await self.check()
        if (self.stage=='bootstrap' and self.page.url==self.source
                and attributes_retained(state,visible=True,selection={'child_id':child,'attributes':binding})):
            self.native_pricing_ready=True
    def record_blocked_route(self,request):
        # Unknown path identity is hashed WITHOUT query, never exposed verbatim.
        try:
            parts=urlsplit(request.url);main=request.frame==self.page.main_frame
            projection=blocked_request_classification(request.url,request.method,request.resource_type,main)
            if projection['path_class']=='static_asset':
                projection['declared_public_prefix_matches']=bool(self.prefix and request.url.startswith(self.prefix))
                public=public_template_diagnostic(request.url,self.prefix) if self.prefix else None
                if public is not None:projection['public_template']=public
            key=digest(_encoded({'origin_class':projection['origin_class'],'path_sha256':digest(parts.path.encode()),'method_class':projection['method_class'],'resource_type':projection['resource_type'],'main_frame':main}))
            if key not in self.other_routes and len(self.other_routes)>=64:return
            record=self.other_routes.setdefault(key,{**projection,'path_sha256':digest(parts.path.encode()),'count':0})
            record['count']=min(1000,record['count']+1)
        except (ValueError,TypeError,AttributeError):pass
    def retain_public_asset(self,suffix,raw,mime):
        # Functional current-session source proof precedes bounded diagnostic
        # metadata storage. Capacity never fabricates or erases loaded proof.
        body_sha=digest(raw)
        scoped=not self.closed and bool(self.prefix) and getattr(self,'page',None) is not None and self.page.url==self.source
        expected_mime='text/html' if suffix.endswith('.html') else 'application/javascript'
        if scoped and mime==expected_mime and QUANTITY_EXPAND_PUBLIC_ASSETS.get(suffix)==body_sha:self.quantity_expand_assets.add(suffix)
        if scoped and mime=='application/javascript' and suffix==SUMMARY_PUBLIC_MODULE and body_sha==SUMMARY_PUBLIC_MODULE_SHA:self.summary_module_proved=True
        if suffix in self.public_assets:return
        # Reserve128 of256 metadata slots for late source-owned UI modules
        # and templates; early generic Magento scripts cannot crowd them out.
        if not critical_public_asset(suffix) and sum(not critical_public_asset(s) for s in self.public_assets)>=128:return
        if len(self.public_assets)>=256 or len(raw)>2*1024*1024 or self.public_asset_bytes+len(raw)>16*1024*1024:return
        # Only exact current-prefix public static filenames enter this function.
        relative=self.store.put_object(raw,'fourover-public-source-asset')
        self.public_asset_bytes+=len(raw)
        self.public_assets[suffix]={'public_asset_suffix':suffix,'mime':mime,'bytes':len(raw),'sha256':digest(raw),'object_path':relative,'source_static_prefix':self.prefix}
        self.cursor['last_public_source_assets']=list(self.public_assets.values());self.persist()
    async def capture_public_script(self,response):
        try:
            request=response.request
            if self.closed or not self.prefix or request.frame!=self.page.main_frame or request.method!='GET' or request.resource_type!='script':return
            suffix,redaction=public_static_relative_path(response.url,self.prefix)
            if redaction is not None or suffix is None or not suffix.endswith('.js') or suffix in self.public_assets or response.url!=request.url or response.status!=200:return
            mime=response.headers.get('content-type','').split(';')[0].strip().lower()
            if mime not in {'application/javascript','text/javascript','application/x-javascript'}:return
            length=response.headers.get('content-length','')
            if length.isdigit() and int(length)>2*1024*1024:return
            await self.check_scope();raw=await response.body();await self.check_scope()
            if self.closed or self.page.url!=self.source or len(raw)>2*1024*1024:return
            raw.decode('utf8')
            if customer_module_declared(suffix,raw):self.customer_module_declared=True
            if ADDRESS_PUBLIC_MODULES.get(suffix)==digest(raw):self.address_modules.add(suffix)
            if CONTROLLER_PUBLIC_MODULES.get(suffix)==digest(raw):self.controller_modules.add(suffix)
            if UI_PUBLIC_MODULES.get(suffix)==digest(raw):self.ui_modules.add(suffix)
            if NAME_PUBLIC_MODULES.get(suffix)==digest(raw):self.name_modules.add(suffix)
            if TRIM_PUBLIC_MODULES.get(suffix)==digest(raw):self.trim_modules.add(suffix)
            self.retain_public_asset(suffix,raw,mime)
        except (Exception,asyncio.CancelledError):return
    def public_response_seen(self,response):
        task=asyncio.create_task(self.capture_public_script(response));self.public_asset_tasks.add(task);task.add_done_callback(self.public_asset_tasks.discard)
        try:
            request=response.request
            if (self.prefix and response.url==self.prefix+'FourOver_Project/js/view/project/name.js' and request.url==response.url and request.method=='GET' and request.resource_type=='script' and request.frame==self.page.main_frame):self.name_module_expected=True
            if (self.prefix and response.url==self.prefix+CUSTOMER_MODULE_SUFFIX and request.url==response.url
                    and request.method=='GET' and request.resource_type=='script' and request.frame==self.page.main_frame):
                self.customer_module_tasks.add(task);task.add_done_callback(self.customer_module_tasks.discard)
            if (self.prefix and request.url==response.url and request.method=='GET' and request.resource_type=='script'
                    and request.frame==self.page.main_frame and any(response.url==self.prefix+s for s in ADDRESS_PUBLIC_MODULES)):
                self.address_module_tasks.add(task);task.add_done_callback(self.address_module_tasks.discard)
        except (ValueError,TypeError,AttributeError):pass
    async def customer_response(self,route):
        # Native cookie-authenticated Magento read only. No body is projected,
        # parsed, logged or persisted, and the original response is unmodified.
        key=route.request.url;flight=self.customer_flights.get(key)
        if flight is None:
            if len(self.customer_flights)>=4:self.block('customer_read_limit');return await route.abort()
            flight={'ready':asyncio.get_running_loop().create_future(),'owned':None,'consumers':0};self.customer_flights[key]=flight;producer=True
        else:producer=False
        flight['consumers']+=1
        if flight['consumers']>4:self.block('customer_read_limit');return await route.abort()
        token=None;started=None;status=0;backoff=0;task=asyncio.current_task();self.customer_metadata_handlers.add(task)
        try:
            await self.check()
            if producer:
                token=await self.observer._acquire(self.rate,self.concurrency,self.check);await self.check();self.phase_check()
                self.customer_reads+=1;started=time.monotonic();response=await self.observer._fetch(route);flight['owned']=response
                status=response.status;backoff=retry_after_seconds(response.headers.get('retry-after'),default=30)
                if status in {401,403}:raise Held('source_auth_required')
                if status in {429,503}:
                    self.last_failed_kind='customer';self.delay_seconds=min(86400,max(1,int(float(backoff)+.999)));raise Held('source_rate_or_availability_stop')
                if status!=200 or response.url!=key:raise Held('customer_read_http_changed')
                if response.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise Held('customer_read_mime_changed')
                size=response.headers.get('content-length','')
                if size.isdigit() and int(size)>2*1024*1024:raise Held('customer_read_limit')
                await self.check();self.phase_check();flight['ready'].set_result(response)
            else:
                response=await asyncio.wait_for(asyncio.shield(flight['ready']),max(0,self.deadline-time.monotonic()))
                if response is None:raise Held('customer_read_stopped')
                await self.check();self.phase_check()
            if self.page.url!=self.source:raise Held('source_catalog_context_changed')
            await self.observer._fulfill(route,response)
        finally:
            if producer and not flight['ready'].done():flight['ready'].set_result(None)
            self.customer_metadata_handlers.discard(task)
            if token is not None:await asyncio.shield(asyncio.to_thread(self.observer.limiter.release,'https://4over.com',token,time.monotonic()-started if started is not None else 0,status,backoff,self.rate))
    async def address_response(self,route,body_key):
        # Native source-owned address list lookup. No private response body is projected,
        # parsed, logged or persisted, and the original response is unmodified.
        key=body_key;flight=self.address_flights.get(key)
        if flight is None:
            if len(self.address_flights)>=1:raise Held('address_read_limit')
            flight={'ready':asyncio.get_running_loop().create_future(),'owned':None,'consumers':0};self.address_flights[key]=flight;producer=True
        else:producer=False
        flight['consumers']+=1
        if flight['consumers']>4:raise Held('address_read_limit')
        token=None;started=None;status=0;backoff=0;task=asyncio.current_task();self.address_metadata_handlers.add(task)
        try:
            await self.check()
            if producer:
                token=await self.observer._acquire(self.rate,self.concurrency,self.check);await self.check();self.phase_check()
                if self.page.url!=self.source or len(self.address_modules)!=len(ADDRESS_PUBLIC_MODULES):raise Held('source_address_dependency_unproved')
                self.address_reads+=1;started=time.monotonic();response=await self.observer._fetch(route);flight['owned']=response
                status=response.status;backoff=retry_after_seconds(response.headers.get('retry-after'),default=30)
                if status in {401,403}:raise Held('source_auth_required')
                if status in {429,503}:
                    self.last_failed_kind='address';self.delay_seconds=min(86400,max(1,int(float(backoff)+.999)));raise Held('source_rate_or_availability_stop')
                if status!=200 or response.url!=ADDRESS_LOOKUP_URL:raise Held('address_read_http_changed')
                if response.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise Held('address_read_mime_changed')
                size=response.headers.get('content-length','')
                if size.isdigit() and int(size)>2*1024*1024:raise Held('address_read_limit')
                await self.check();self.phase_check();flight['ready'].set_result(response)
            else:
                response=await asyncio.wait_for(asyncio.shield(flight['ready']),max(0,self.deadline-time.monotonic()))
                if response is None:raise Held('address_read_stopped')
                await self.check();self.phase_check()
            if self.page.url!=self.source:raise Held('source_catalog_context_changed')
            await self.observer._fulfill(route,response)
            await self.check();self.phase_check();self.address_ready=True
        finally:
            if producer and not flight['ready'].done():flight['ready'].set_result(None)
            self.address_metadata_handlers.discard(task)
            if token is not None:await asyncio.shield(asyncio.to_thread(self.observer.limiter.release,'https://4over.com',token,time.monotonic()-started if started is not None else 0,status,backoff,self.rate))
    async def catalog_price_response(self,route,child):
        # Public full-table metadata may arrive before dependent DOM rendering.
        # That transient presentation state must not suppress source callbacks.
        request=route.request;key=(request.url,request.method,request.resource_type,request.frame)
        flight=self.price_flights.get(child)
        if flight is None:
            if len(self.price_flights)>=MAX_QUOTES:raise Held('source_request_budget')
            flight={'key':key,'ready':asyncio.get_running_loop().create_future(),'response':None,'owned':None,'consumers':0};self.price_flights[child]=flight;producer=True
        else:producer=False
        flight['consumers']+=1
        old_parts=urlsplit(flight['key'][0]);new_parts=urlsplit(key[0])
        timestamp_alias=(flight['key'][1:]==key[1:] and (old_parts.scheme,old_parts.netloc,old_parts.path)==(new_parts.scheme,new_parts.netloc,new_parts.path)
            and re.fullmatch(r'_=[0-9]{1,20}',old_parts.query) is not None and re.fullmatch(r'_=[0-9]{1,20}',new_parts.query) is not None)
        if flight['consumers']>4 or (flight['key']!=key and not timestamp_alias):self.block('repeated_price_variant');return await route.abort()
        task=asyncio.current_task();self.price_metadata_handlers.add(task);self.price_pending.add(child)
        try:
            if producer:
                epoch=self.epoch;binding=self.declared_bindings[child]
                async def current():
                    await self.check()
                    if self.page.url!=self.source or self.declared_bindings.get(child)!=binding:raise Held('source_catalog_context_changed')
                async def prices(response,raw):
                    self.price_bytes+=len(raw)
                    if self.price_bytes>16*1024*1024:raise Held('source_table_aggregate_limit')
                    table=bounded_prices(raw)
                    if not table['package_prices'] or table['tier_prices']:raise Held('package_family_unsupported')
                    return {'rows':table['package_prices'],'sha256':digest(raw),'observed_at':_now(),'attributes':dict(binding),'page_generation':self.session_id,'price_epoch':epoch}
                response,captured=await self.fetch(route,'price',prices,context_check=current,static_flight=flight)
                await current();self.phase_check()
                public_table={'schema':'fourover-automatic-child-table/v1','profile_id':PROFILE_ID,'source_url':self.source,'child_id':child,'attributes':dict(binding),'scope':SCOPE,
                    'package_prices':captured['rows'],'tier_prices':[],'raw_response_sha256':captured['sha256'],'parser':'bounded_prices/parse_positive_prices','observed_at':captured['observed_at'],
                    'page_generation':self.session_id,'price_epoch':epoch,'catalog_reference':self.catalog['reference'],'admission_basis':'current_declared_child_native_public_table','source_quote_verified':False,'literal_request_url_sha256':digest(request.url.encode())}
                raw=_encoded(public_table);relative=self.store.put_object(raw,'fourover-automatic-child-table.json');captured['evidence']={'object_path':relative,'sha256':digest(raw)}
                self.tables[child]=captured;flight['response']=response;flight['ready'].set_result(response)
            else:
                response=await asyncio.wait_for(asyncio.shield(flight['ready']),max(0,self.deadline-time.monotonic()))
                if response is None or response is not flight['response']:raise Held('source_catalog_table_stopped')
                await self.check();self.phase_check()
            await self.observer._fulfill(route,response)
        finally:
            if producer and not flight['ready'].done():flight['ready'].set_result(None)
            self.price_pending.discard(child);self.price_metadata_handlers.discard(task)
    def block(self,kind):self.blocked[kind]=self.blocked.get(kind,0)+1
    def static_suffix(self,request):
        if request.method!='GET' or request.resource_type!='xhr' or request.frame!=self.page.main_frame or not self.prefix:return None
        if request.url==self.prefix+'js-translation.json':return 'js-translation.json'
        suffix=generic_public_template_suffix(request.url,self.prefix)
        if suffix is not None and re.search(r'(?:^|[-_./])[a-fA-F0-9]{16,}(?=$|[-_./])',suffix):return None
        return suffix
    async def static_response(self,route,suffix):
        request=route.request;key=(request.url,request.method,request.resource_type,request.frame);flight=self.static.get(suffix)
        if flight is None:
            if len(self.static)>=ASSET_DIAGNOSIS_STATIC_LIMIT or self.consumers>=NATIVE_STATIC_CONSUMERS_TOTAL:raise Held('static_asset_limit')
            flight={'key':key,'ready':asyncio.get_running_loop().create_future(),'response':None,'owned':None,'pending':1,'consumers':1,'error':None,'started_at':time.monotonic()}
            self.static[suffix]=flight;self.consumers+=1;producer=True
            self.note_bootstrap_progress('admitted',suffix)
        else:
            if flight['key']!=key or flight['consumers']>=NATIVE_STATIC_CONSUMERS_PER_ASSET or self.consumers>=NATIVE_STATIC_CONSUMERS_TOTAL:raise Held('static_consumer_limit')
            flight['pending']+=1;flight['consumers']+=1;self.consumers+=1;producer=False
        self.last_static=time.monotonic();consumer_task=asyncio.current_task();self.public_static_handlers.add(consumer_task)
        try:
            if producer:
                async def consume(response,raw):
                    self.static_bytes+=len(raw)
                    if self.static_bytes>STATIC_XHR_MAX_TOTAL:raise Held('static_aggregate_limit')
                    mime='application/json' if suffix=='js-translation.json' else 'text/html'
                    if response.url!=request.url or response.headers.get('content-type','').split(';')[0].strip().lower()!=mime:raise Held('static_response_changed')
                    bootstrap_static_body_metadata(25 if suffix=='js-translation.json' else 30,raw,generic_template=suffix!='js-translation.json')
                    self.retain_public_asset(suffix,raw,mime)
                response,_=await self.fetch(route,'static',consume,static_flight=flight)
                await self.check()
                if self.static_suffix(request)!=suffix or key!=flight['key']:raise Held('static_context_changed')
                self.phase_check();flight['response']=response;self.note_bootstrap_progress('verified',suffix);flight['ready'].set_result(response)
            else:
                await self.check();remaining=self.deadline-time.monotonic()
                response=await asyncio.wait_for(asyncio.shield(flight['ready']),remaining)
                await self.check()
                if response is None or response is not flight['response'] or self.static_suffix(request)!=suffix or key!=flight['key']:raise Held(flight['error'] or 'static_context_changed')
            self.phase_check();await self.observer._fulfill(route,response)
        except asyncio.CancelledError:
            # A callback owns one delivery, not every waiter or the product.
            # Its cancelled supplier fetch is never repeated by another consumer.
            self.block('cancelled_public_static_consumer')
            if producer and flight['response'] is None:flight['error']='static_consumer_cancelled'
            try:await route.abort()
            except Exception:pass
            raise
        except Exception as exc:
            code=exc.code if isinstance(exc,Held) else 'static_capture_stopped'
            if code=='static_consumer_cancelled' and flight['error']=='static_consumer_cancelled':
                self.block('cancelled_public_static_consumer');return await route.abort()
            if self.closed and code=='automatic_session_closed':return await route.abort()
            if self.failure is None:self.failure=code
            raise
        finally:
            if producer and not flight['ready'].done():flight['error']=flight['error'] or self.failure or 'static_capture_stopped';flight['ready'].set_result(None)
            flight['pending']-=1;self.last_static=time.monotonic();self.public_static_handlers.discard(consumer_task)
    async def fetch(self,route,kind,consume,*,context_check=None,static_flight=None):
        token=None;response=None;started=None;elapsed=0;status=0;backoff=0
        try:
            await self.check()
            token=await self.observer._acquire(self.rate,self.concurrency,self.check)
            await self.check()
            if context_check is not None:await context_check()
            self.phase_check()
            self.counts[kind]+=1;started=time.monotonic();response=await self.observer._fetch(route)
            if static_flight is not None:static_flight['owned']=response
            status=response.status;backoff=retry_after_seconds(response.headers.get('retry-after'),default=30)
            if status in {401,403}:raise Held('source_auth_required')
            if status in {301,302,303,307,308} and response.headers.get('location','') in {'https://4over.com/customer/account/login','https://4over.com/customer/account/login/','/customer/account/login','/customer/account/login/'}:raise Held('source_auth_required')
            if status in {429,503}:
                self.last_failed_kind=kind
                self.delay_seconds=min(86400,max(1,int(float(backoff)+.999)))
                raise Held('source_rate_or_availability_stop')
            if status!=200 or response.url!=route.request.url:raise Held('source_http_or_redirect_stop')
            if kind in {'price','summary'} and response.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':raise Held('source_json_mime_changed')
            maximum=STATIC_XHR_MAX_RESPONSE if kind=='static' else 2*1024*1024
            length=response.headers.get('content-length','')
            if length.isdigit() and int(length)>maximum:raise Held('source_response_limit')
            raw=await response.body()
            if len(raw)>maximum:raise Held('source_response_limit')
            await self.check()
            if context_check is not None:await context_check()
            self.phase_check()
            projection=await consume(response,raw)
            await self.check()
            if context_check is not None:await context_check()
            self.phase_check()
            if static_flight is None:await self.observer._fulfill(route,response)
            return response,projection
        finally:
            if started is not None:elapsed=time.monotonic()-started
            if token is not None:await asyncio.shield(asyncio.to_thread(self.observer.limiter.release,'https://4over.com',token,elapsed,status,backoff,self.rate))
            if response is not None and static_flight is None:await response.dispose()
    async def guarded(self,route):
        request=route.request
        if self.closed:return await route.abort()
        try:
            main=request.frame==self.page.main_frame;parts=urlsplit(request.url)
            if not main:return await route.abort()
            suffix=self.static_suffix(request)
            if suffix is not None:return await self.static_response(route,suffix)
            if request.method=='GET' and request.resource_type=='document' and request.url==self.source and not self.document_reserved:
                # Reservation happens synchronously before the first document await.
                self.document_reserved=True
                async def document(response,raw):
                    if response.headers.get('content-type','').split(';')[0].strip().lower()!='text/html':raise Held('source_document_mime')
                    children,_=inspect_child_configurations(raw.decode('utf8'),source_url=self.source)
                    contract=inspect_option_contract(raw.decode('utf8'),children,source_url=self.source)
                    if children!=self.catalog['job']['declared_children'] or contract.get('binding_complete') is not True or contract.get('rows')!=self.catalog['rows']:raise Held('source_declaration_changed')
                    self.document_sha256=digest(raw);self.prefix=document_static_prefix(raw);self.tracker=source_declares_tracker(raw);self.customer_declared=customer_section_declared(raw)
                return await self.fetch(route,'document',document)
            if (request.method=='GET' and request.resource_type in {'script','stylesheet','image','font'}
                    and parts.scheme=='https' and parts.netloc=='4over.com' and parts.path.startswith(('/static/','/media/'))):
                return await route.continue_()
            if request.method=='POST' and request.resource_type=='xhr' and request.url=='https://4over.com/print/project/summary/':
                if self.stage!='quantity' or self.selection is None or self.summary_pending or self.summary_seen:
                    self.block('unarmed_summary');return await route.abort()
                epoch=self.epoch;selection=self.selection;self.summary_request_observed=True;self.summary_pending=True
                try:
                    while self.quote_action_pending:
                        await asyncio.sleep(.01);await self.check()
                        if self.epoch!=epoch or self.selection is not selection or self.stage!='quantity':raise Held('stale_quote_epoch')
                    before=await self.state()
                    if not context_retained(self.quote_context_state(before,selection),selection=selection):
                        self.block('unretained_summary');self.capture_diagnostic('source_summary_context_unretained',before,request=request)
                        self.failure='source_summary_context_unretained';return await route.abort()
                    if (await request.header_value('content-type') or '').split(';')[0].strip().lower()!='application/x-www-form-urlencoded':raise Held('source_quote_schema_changed')
                    projection=self.quote_request_context(request.post_data,before,selection)
                    async def current():
                        fresh=await self.state()
                        if self.epoch!=epoch or self.selection is not selection or self.stage!='quantity':raise Held('stale_quote_epoch')
                        if self.quote_request_context(request.post_data,fresh,selection)!=projection:raise Held('source_quote_context_changed')
                    async def summary(response,raw):
                        totals=parse_summary(raw)
                        # This public projection alone is retained; opaque source IDs/addresses never are.
                        return {'totals':totals,'request_context':projection,'response_sha256':digest(raw),'epoch':epoch,'observed_at':_now()}
                    if self.counts['summary']>=self.limit:raise Held('source_request_budget')
                    _,captured=await self.fetch(route,'summary',summary,context_check=current)
                    await self.check();await current();self.phase_check()
                    self.summary=captured;self.summary_seen=True;return
                finally:self.summary_pending=False
            if (request.method=='GET' and request.resource_type in {'xhr','fetch'} and parts.scheme=='https' and parts.netloc=='4over.com'
                    and parts.path.startswith('/rest/default/V1/product/prices/')):
                child=parts.path.rsplit('/',1)[-1]
                try:query=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True,max_num_fields=1)
                except ValueError:query=[('invalid','')]
                await self.observe_native_readiness(request,parts,child,query)
                if (child not in self.declared_bindings or child in QUARANTINED.get(self.source,())
                        or not (not query or len(query)==1 and query[0][0]=='_' and re.fullmatch('[A-Za-z0-9_-]{1,64}',query[0][1]))
                        or parts.fragment or self.page.url!=self.source):
                    self.block('unknown_or_unsupported_price');return await route.abort()
                return await self.catalog_price_response(route,child)
            if (request.method=='GET' and request.resource_type=='xhr' and self.page.url==self.source
                    and parts.scheme=='https' and parts.netloc=='4over.com' and parts.path=='/customer/section/load/'):
                if not native_customer_query(parts):
                    self.block('customer_query_unrecognized');self.record_blocked_route(request);return await route.abort()
                # Response event capture can settle one turn after native module
                # execution creates its GET. Wait for already-arrived proof only;
                # never fetch a dependency or manufacture customer data here.
                while not (self.customer_declared or self.customer_module_declared) and self.customer_module_tasks:
                    await asyncio.sleep(.01);await self.check();self.phase_check()
                if self.customer_declared or self.customer_module_declared:return await self.customer_response(route)
                self.block('customer_dependency_unproved')
                self.record_blocked_route(request);return await route.abort()
            if request.method=='POST' and request.resource_type=='xhr' and request.url==ADDRESS_LOOKUP_URL:
                task=asyncio.current_task();self.address_metadata_handlers.add(task)
                try:
                    while len(self.address_modules)!=len(ADDRESS_PUBLIC_MODULES) and self.address_module_tasks:
                        await asyncio.sleep(.01);await self.check();self.phase_check()
                    if len(self.address_modules)!=len(ADDRESS_PUBLIC_MODULES) or self.page.url!=self.source:raise Held('source_address_dependency_unproved')
                    if (await request.header_value('content-type') or '').split(';')[0].strip().lower()!='application/json':raise Held('source_address_lookup_schema_changed')
                    await self.check();self.phase_check()
                    if self.page.url!=self.source:raise Held('source_address_dependency_unproved')
                    body=request.post_data
                    if not native_address_lookup_body(body):raise Held('source_address_lookup_schema_changed')
                    # Private payload identity is RAM-only; never copied to a receipt.
                    return await self.address_response(route,digest(body.encode('utf8')))
                finally:self.address_metadata_handlers.discard(task)
            if (request.method=='POST' and request.resource_type=='xhr' and parts.scheme=='https' and parts.netloc=='4over.com'
                    and parts.path.startswith('/print/shipment/') and self.page.url==self.source
                    and not re.search(r'/checkout|/cart(?:/|$)|/order|/upload',parts.path,re.I)):
                # Source-owned background shipping is irrelevant to cost quotes.
                # Abort before private body/header reads; never forward or price it.
                self.block('background_shipping');return await route.abort()
            if known_tracker_request(parts,request.method,request.resource_type,True,self.tracker):
                self.block('telemetry');return await route.abort()
            if (request.method not in {'GET','HEAD'} or re.search(r'/checkout|/cart(?:/|$)|/order|/upload|/print/shipment/',parts.path,re.I)):
                raise Held('forbidden_source_action')
            self.record_blocked_route(request);self.block('other');await route.abort()
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=exc.code if isinstance(exc,Held) else 'source_capture_stopped'
            if code.startswith(('source_address_','address_read_')) or code in {'source_auth_required','source_rate_or_availability_stop','source_declaration_changed','forbidden_source_action','source_json_mime_changed','native_quote_body_schema_changed','native_quote_shipping_or_services_changed','stale_price_epoch','stale_quote_epoch'} or self.stage=='bootstrap':self.failure=code
            else:self.quote_failure=code
            self.capture_diagnostic(code,error=exc,request=request)
            await route.abort()
    async def guard(self,route):
        task=asyncio.current_task();self.handlers.add(task)
        try:return await self.guarded(route)
        finally:self.handlers.discard(task)
    async def wait(self,predicate,seconds=35):
        until=min(time.monotonic()+seconds,self.deadline)
        try:
            while time.monotonic()<until:
                await self.check()
                if getattr(self,'quote_failure',None):raise Held(self.quote_failure)
                state=await self.state()
                await self.check()
                if predicate(state):return state
                await asyncio.sleep(.1)
            raise Held('source_context_timeout')
        except Held as exc:
            if exc.code in {'source_context_timeout','source_phase_timeout'}:self.capture_diagnostic(exc.code)
            raise
    async def wait_rendered_quote(self,selection):
        summary=self.summary;epoch=self.epoch
        if type(summary) is not dict or summary.get('epoch')!=epoch:raise Held('source_quote_not_retained')
        def observed(state):
            if self.summary is not summary or self.epoch!=epoch or self.selection is not selection or self.stage!='quantity':raise Held('stale_quote_epoch')
            retained=self.quote_context_state(state,selection)
            if not controller_ready(retained,tat=True) or not q1000_context_retained(retained,selection=selection,request_projection=summary['request_context']):raise Held('source_quote_not_retained')
            shown={Decimal(v['value']) for v in state.get('money',[]) if v['kind']=='total'}
            sub={Decimal(v['value']) for v in state.get('money',[]) if v['kind'] in {'subtotal','price per set'}}
            return shown=={Decimal(summary['totals']['grand_total'])} and sub=={Decimal(summary['totals']['subtotal'])}
        try:return await self.wait(observed,seconds=max(0,self.deadline-time.monotonic()))
        except Held as exc:
            if exc.code in {'source_context_timeout','source_phase_timeout'}:
                self.capture_diagnostic('source_rendered_total_disagree',self.last_state)
                raise Held('source_rendered_total_disagree') from None
            raise
    async def wait_quote(self,selection):
        # Magento may only activate its pricing summary after this ordinary
        # visible quote control. Give automatic source subscriptions their
        # native debounce interval first; never submit a second source quote.
        started=time.monotonic();clicked=False
        while True:
            await self.check()
            if self.quote_failure:raise Held(self.quote_failure)
            state=await self.state();await self.check()
            if ui_action_requirement(state):raise Held(ui_action_requirement(state))
            retained=self.quote_context_state(state,selection)
            if self.summary is not None and ui_ready(state) and controller_ready(retained,tat=True) and q1000_context_retained(retained,selection=selection,request_projection=self.summary['request_context']):return state
            if (not clicked and time.monotonic()-started>=.75 and ui_ready(state) and controller_ready(state,tat=True) and context_retained(state,selection=selection)
                    and not self.summary_request_observed and not self.summary_seen and not self.summary_pending and self.summary is None):
                # No await between the final Python epoch/once check and the
                # synchronous DOM recheck/action. Concurrent automatic summary
                # traffic is still admitted at most once by guarded().
                if self.stage!='quantity' or self.selection is not selection:raise Held('stale_quote_epoch')
                if self.quote_failure:raise Held(self.quote_failure)
                self.phase_check()
                result=await self.ordinary_quote_action(selection)
                await self.check()
                if type(result) is not dict or result.get('status') not in {'clicked','unavailable','context_changed'}:raise Held('source_quote_control_changed')
                self.quote_button_diagnostic=result.get('diagnostic')
                if result['status']=='context_changed':raise Held('source_quote_control_context_changed')
                if result['status']=='clicked':clicked=True;self.quote_button_clicked=True
            await asyncio.sleep(.1)
    def persist(self):save_cursor(self.path,self.cursor)
    def record(self,selection,status,evidence=None,reason=None):
        key=semantic_key(self.source,selection['child_id'],selection['run_size'])
        self.cursor['receipts'][key]={'status':status,'child_id':selection['child_id'],'quantity':selection['run_size'],'evidence':evidence,'reason_code':reason}
        self.cursor['processed_count']=len(self.cursor['receipts'])
        self.cursor['verified_count']=sum(v['status']=='verified' for v in self.cursor['receipts'].values())
        self.cursor['progress_count']=self.cursor['processed_count']+self.cursor['pipeline_retry_count']
        self.cursor['inflight']=None;self.cursor['updated_at']=_now();self.persist();self.receipts_this_call+=1
        if status=='verified':
            self.completed_this_call+=1;self.cursor['consecutive_read_retries']=0;self.persist()
    async def drain(self):
        pending=tuple(t for t in self.handlers if t is not asyncio.current_task() and t not in self.public_static_handlers and t not in self.price_metadata_handlers and t not in self.customer_metadata_handlers and t not in self.address_metadata_handlers)
        if pending:
            try:await asyncio.wait_for(asyncio.gather(*pending,return_exceptions=True),2)
            except asyncio.TimeoutError:
                for task in pending:task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
    def prepare_selection(self,entry,q):
        selection={'child_id':entry['child_id'],'run_size':q,'attributes':dict(entry['attributes'])}
        if not reserve_once(self.store.root,self.source,selection,self.cursor['key']):return None
        self.cursor['inflight']={'selection':selection,'semantic':semantic_key(self.source,entry['child_id'],q)}
        self.persist();return selection
    def time_to_yield(self):return time.monotonic()+36>=self.session_deadline
    async def reveal_quantity(self,selection,table,*,full_domain=False):
        # Presentation only. No reservation, selection/model mutation or supplier
        # request. The original full prequantity gate still follows this helper.
        if type(full_domain) is not bool:raise Held('source_quantity_expand_changed')
        clicked=False
        while True:
            await self.check()
            if (self.stage!='child' or self.selection is not selection or self.cursor.get('inflight') is not None
                    or self.summary_request_observed or self.summary_seen or self.summary_pending or self.summary is not None
                    or self.proceed_receipt is not None or not full_domain and spent(self.store.root,self.source,selection['child_id'],selection['run_size'])):raise Held('stale_quote_epoch')
            # Full-domain mode is a read/presentation action. Existing quote
            # reservations still apply in prepare_selection and normal reveal;
            # observing an already-loaded list never reopens a spent quote.
            state=await self.state();await self.check()
            if str(selection['run_size']) in state.get('available_run_sizes',[]):
                # Show More must finish rendering the whole child table, not
                # merely reveal the next requested row while later tiers are
                # still absent. A collapsed visible subset is never domain
                # completion evidence.
                if (not full_domain and not clicked) or state.get('available_run_sizes',[])==[str(row['quantity']) for row in table['rows']]:return
            if clicked:
                await asyncio.sleep(.1);continue
            if (self.tables.get(selection['child_id']) is not table or table.get('page_generation')!=self.session_id
                    or table.get('attributes')!=selection['attributes'] or type(table.get('rows')) is not list
                    or sum(type(row) is dict and row.get('quantity')==selection['run_size'] for row in table['rows'])!=1):raise Held('source_quantity_table_unretained')
            proved=all(name in self.quantity_expand_assets or type(self.public_assets.get(name)) is dict and self.public_assets[name].get('sha256')==sha
                and self.public_assets[name].get('source_static_prefix')==self.prefix for name,sha in QUANTITY_EXPAND_PUBLIC_ASSETS.items())
            result=await self.page.evaluate(quantity_expand_js(selection['attributes'],self.source,
                controller_proved=len(self.controller_modules)==len(CONTROLLER_PUBLIC_MODULES),ui_proved=len(self.ui_modules)==len(UI_PUBLIC_MODULES),assets_proved=proved,full_domain=full_domain),
                {'selection':selection,'rows':table['rows']})
            await self.check()
            if self.stage!='child' or self.selection is not selection or self.cursor.get('inflight') is not None:raise Held('stale_quote_epoch')
            self.quantity_expand_diagnostic=safe_quantity_expand_diagnostic(result)
            if self.quantity_expand_diagnostic is None:raise Held('source_quantity_expand_changed')
            if result['status']=='clicked':clicked=True
            await asyncio.sleep(.1)
    async def capture_native_domain(self,selection,table,*,expand_all=False):
        """Read domain evidence; optional expansion never selects or quotes."""
        if type(expand_all) is not bool:raise Held('source_quantity_expand_changed')
        def unknown(reason):
            return {'schema':'fourover-native-domain-observation/v1','status':'unknown','reason':reason,
                    'expanded_rendered_domain':'unknown','source_domain_complete':False}
        await self.check()
        if (self.stage!='child' or self.selection is not selection or self.cursor.get('inflight') is not None
                or self.proceed_receipt is not None or self.tables.get(selection['child_id']) is not table
                or table.get('page_generation')!=self.session_id or table.get('attributes')!=selection['attributes']):
            return unknown('table_identity_unproved')
        original_run_size=None
        if expand_all:
            original=await self.state();original_run_size=original.get('run_size')
            await self.reveal_quantity(selection,table,full_domain=True)
        try:
            raw=await self.page.evaluate(native_domain_read_js(selection['attributes'],self.source,
                controller_proved=len(self.controller_modules)==len(CONTROLLER_PUBLIC_MODULES),ui_proved=len(self.ui_modules)==len(UI_PUBLIC_MODULES)),
                {'selection':selection,'rows':table['rows']})
        except asyncio.CancelledError:raise
        except Exception:
            raw={'status':'unknown','reason':'native_domain_observation_unavailable'}
        await self.check()
        if self.stage!='child' or self.selection is not selection or self.cursor.get('inflight') is not None:
            return unknown('table_identity_unproved')
        if expand_all and type(raw) is dict and raw.get('status')=='observed' and raw.get('observed_run_size')!=original_run_size:
            raise Held('source_quantity_expansion_changed_selection')
        return safe_native_domain_observation(raw,source=self.source,selection=selection,table=table,
            document_sha256=getattr(self,'document_sha256',''),page_generation=self.session_id,
            snapshot_id=self.catalog['reference']['snapshot_id'],observed_at=time.time())
    async def prepare_quote_label(self,state):
        ui=state.get('source_ui') if type(state) is dict else None
        if type(ui) is not dict or ui.get('current_project_name_present') is not True:return
        fields=ui.get('fields') if type(ui.get('fields')) is dict else {}
        field=fields.get('project_name') if type(fields.get('project_name')) is dict else {}
        if type(field.get('visible_count')) is not int or field['visible_count']!=1:raise Held('source_project_details_required')
        try:
            await self.wait(lambda s:len(self.name_modules)==len(NAME_PUBLIC_MODULES) and ui_ready(s))
            await self.check()
            if self.stage!='child' or self.cursor.get('inflight'):raise Held('stale_quote_epoch')
            self.phase_check()
            result=await self.page.evaluate(QUOTE_LABEL_ACTION_JS,{'source':self.source,'modules_proved':True})
            await self.check()
            allowed={'filled','already_named','unproved','not_default','binding_changed','not_options','field_changed','input_unretained'}
            status=result.get('status') if type(result) is dict else None
            self.project_label_status=status if type(status) is str and status in allowed else 'unproved'
            if self.project_label_status not in {'filled','already_named'}:raise Held('source_project_details_required')
        except Held as exc:
            if exc.code in {'source_context_timeout','source_phase_timeout'}:raise Held('source_project_details_required') from None
            raise
    async def collect(self):
        for entry in self.catalog['entries']:
            if self.receipts_this_call>=self.limit or self.visited>=MAX_QUOTES or self.time_to_yield():return False
            if all(spent(self.store.root,self.source,entry['child_id'],q) for q in entry['historical_prices']):continue
            self.visited+=1
            await self.drain();self.epoch+=1;self.stage='child';self.selection=None;self.target=entry;self.quote_failure=None;self.deadline=self.cold_deadline if not self.first_target_table_ready else time.monotonic()+35
            for attribute,value in entry['attributes'].items():
                await self.check();control=self.page.locator(f'select[name="super_attribute[{attribute}]"]')
                if await control.count()!=1:raise Held('source_attribute_missing')
                diagnostic=await self.page.evaluate(ATTRIBUTE_DIAGNOSTIC_JS,{'attribute_id':attribute,'expected_value':value})
                if diagnostic.get('selected_matches') is True and diagnostic.get('expected_choice_enabled') is True:continue
                await self.page.wait_for_function('''({name,value})=>{const es=[...document.querySelectorAll('select')].filter(e=>e.name===name);return es.length===1&&!es[0].disabled&&[...es[0].options].some(o=>o.value===value&&!o.disabled)}''',arg={'name':f'super_attribute[{attribute}]','value':value},timeout=6000)
                if await control.is_visible():await self.check();await control.select_option(value)
                elif not diagnostic.get('implicit_fixed_dash'):raise Held('source_attribute_hidden')
            await self.wait(lambda s:attributes_retained(s,visible=True,selection={'child_id':entry['child_id'],'attributes':entry['attributes']}) and entry['child_id'] in self.tables,
                seconds=max(0,self.deadline-time.monotonic()) if not self.first_target_table_ready else 35)
            self.first_target_table_ready=True
            table=self.tables[entry['child_id']]
            pending=list(table['rows']);deferred_current=None
            while pending:
                row=pending.pop(0);q=row['quantity']
                if self.receipts_this_call>=self.limit or self.time_to_yield():return False
                if spent(self.store.root,self.source,entry['child_id'],q):continue
                selection={'child_id':entry['child_id'],'run_size':q,'attributes':dict(entry['attributes'])}
                await self.drain();self.epoch+=1;self.stage='child';self.selection=selection;self.summary=None;self.summary_seen=False;self.summary_request_observed=False;self.quote_button_clicked=False;self.quote_button_diagnostic=None;self.quote_failure=None;self.deadline=time.monotonic()+35
                if self.cursor['processed_count']>=MAX_DOMAIN:raise Held('cursor_limit')
                await self.check()
                try:
                    await self.reveal_quantity(selection,table)
                    before=await self.wait(lambda s:attributes_retained(s,visible=True,selection=selection)
                        and str(q) in s.get('available_run_sizes',[]) and services_off(s)
                        and not self.customer_metadata_handlers and not self.customer_module_tasks
                        and self.address_ready and not self.address_metadata_handlers and not self.address_module_tasks
                        and controller_ready(s) and ui_ready(s) and ui_action_requirement(s) in {None,'source_project_details_required'})
                    await self.prepare_quote_label(before)
                    # The normal UI event may notify source subscriptions. Retain
                    # the complete original preselection gate again before once.
                    before=await self.wait(lambda s:attributes_retained(s,visible=True,selection=selection)
                        and str(q) in s.get('available_run_sizes',[]) and services_off(s)
                        and not self.customer_metadata_handlers and not self.customer_module_tasks
                        and self.address_ready and not self.address_metadata_handlers and not self.address_module_tasks
                        and controller_ready(s) and ui_ready(s) and not ui_action_requirement(s))
                except Held as exc:
                    if exc.code in {'source_context_timeout','source_phase_timeout'} and isinstance(self.last_state,dict) and not services_off(self.last_state):
                        self.capture_diagnostic('source_service_controls_not_retained',self.last_state)
                        raise Held('source_service_controls_not_retained') from None
                    requirement=ui_action_requirement(self.last_state)
                    if requirement:self.capture_diagnostic(requirement,self.last_state);raise Held(requirement) from None
                    if exc.code in {'source_context_timeout','source_phase_timeout'} and not ui_ready(self.last_state):
                        self.capture_diagnostic('source_ui_readiness_unproven',self.last_state);raise Held('source_ui_readiness_unproven') from None
                    raise
                await self.check()
                if not services_off(before):
                    self.capture_diagnostic('source_service_controls_not_retained',before)
                    raise Held('source_service_controls_not_retained')
                if self.blocked.get('customer_dependency_unproved') or self.blocked.get('customer_query_unrecognized'):
                    self.capture_diagnostic('source_customer_dependency_unproved',before);raise Held('source_customer_dependency_unproved')
                if str(before.get('run_size',''))==str(q) and not controller_ready(before,tat=True):
                    # Calculator registration does not notify native TAT subscribers.
                    # An already-selected row can be a no-op. Defer it only for a
                    # different real unspent quote, then return immediately; never
                    # spend a dummy/repeated quantity just to initialize source UI.
                    if self.limit<2:raise Held('source_current_quantity_requires_distinct_quote_capacity')
                    if self.limit-self.receipts_this_call<2:return False
                    if deferred_current is not None or not any(candidate['quantity']!=q and not spent(self.store.root,self.source,entry['child_id'],candidate['quantity']) for candidate in pending):
                        raise Held('source_current_quantity_has_no_unspent_native_change')
                    deferred_current=row;continue
                native_domain=await self.capture_native_domain(selection,table)
                if self.prepare_selection(entry,q) is None:continue
                # Native summary is armed only after the semantic once reservation,
                # immediately before the ordinary source quantity action.
                self.stage='quantity'
                try:
                    await self.check()
                    count=await self.page.evaluate('''({quantity,source})=>{if(location.href!==source)return 0;const root=document.querySelector('.column.main')||document.querySelector('main');if(!root)return 0;const rows=[...root.querySelectorAll('li.runsizes_row')].filter(e=>e.getClientRects().length>0&&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'&&e.querySelector('.runsize')?.textContent?.trim().replace(/,/g,'')===String(quantity));if(rows.length!==1)return rows.length;rows[0].click();return 1}''',{'quantity':q,'source':self.source})
                    if count!=1:raise Held('source_quantity_unavailable')
                    state=await self.wait_quote(selection)
                    await self.drain();await self.check();state=await self.state();await self.check()
                    retained=self.quote_context_state(state,selection)
                    if self.summary['epoch']!=self.epoch or not controller_ready(retained,tat=True) or not q1000_context_retained(retained,selection=selection,request_projection=self.summary['request_context']):raise Held('source_quote_not_retained')
                    totals=self.summary['totals'];rounded=Decimal(row['price']).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
                    if Decimal(totals['subtotal'])!=rounded:raise Held('source_table_total_disagree')
                    state=await self.wait_rendered_quote(selection)
                    shown={Decimal(v['value']) for v in state.get('money',[]) if v['kind']=='total'}
                    sub={Decimal(v['value']) for v in state.get('money',[]) if v['kind'] in {'subtotal','price per set'}}
                    if shown!={Decimal(totals['grand_total'])} or sub!={Decimal(totals['subtotal'])}:raise Held('source_rendered_total_disagree')
                    production=state.get('production')
                    if not production or not retained['production'].get('visible') or self.summary['request_context']['production']!={k:production[k] for k in ('value','label')}:raise Held('source_production_not_retained')
                    historical=entry['historical_prices'].get(q)
                    receipt={'schema':'fourover-automatic-source-quote/v1','profile_id':PROFILE_ID,'association':self.catalog['association'],'selection':selection,'scope':SCOPE,
                        'current_source_table_price':row['price'],'historical_source_table_price':historical,'source_table_price_changed':historical is not None and Decimal(historical)!=Decimal(row['price']),
                        'supplier_product_total_excluding_shipping':totals['grand_total'],'totals':totals,'production':self.summary['request_context']['production'],
                        'price_response_sha256':table['sha256'],'current_price_table_evidence':table['evidence'],'request_context':self.summary['request_context'],'summary_response_sha256':self.summary['response_sha256'],'source_document_sha256':self.document_sha256,
                        'catalog_reference':self.catalog['reference'],'observed_at':self.summary['observed_at'],'quote_epoch':self.epoch,
                        'page_generation':self.session_id,'price_epoch':table['price_epoch'],'source_static_prefix':self.prefix,
                        'source_data_finalized':False,'woocommerce_import_ready':False,'shipping_verified':False}
                    # Capture was made while Product Options was still visible.
                    # Existing retained quote/Proceed guards bind it to this
                    # child and generation. It is evidence, never a standalone
                    # whole-domain or every-production-combination certificate.
                    domain_raw=_encoded(native_domain);domain_path=self.store.put_object(domain_raw,'fourover-native-domain.json')
                    receipt['native_domain_observation']=native_domain
                    receipt['native_domain_evidence']={'object_path':domain_path,'sha256':digest(domain_raw)}
                    if self.proceed_receipt is not None:
                        receipt['native_quote_transition']={'schema':'native-proceed-quote-retention/v1','basis':'visible_options_and_production_before_atomic_native_proceed',
                            'pre_proceed_context_sha256':digest(_encoded(self.proceed_receipt)),
                            'same_project_model_identity_retained':True,'native_shipping_step_retained':True,
                            'production_visible_before_proceed':True,'production_visible_at_quote':production['visible']}
                    raw=_encoded(receipt);relative=self.store.put_object(raw,'fourover-automatic-quote.json');evidence={'object_path':relative,'sha256':digest(raw)}
                    self.record(selection,'verified',evidence)
                    # The source now hides the product-options step. Persist this
                    # exact quote, then resume other unspent work on a new owned
                    # page; do not invent a back-step or claim full coverage.
                    if self.proceed_receipt is not None:return False
                    if deferred_current is not None:
                        pending.insert(0,deferred_current);deferred_current=None
                except asyncio.CancelledError:
                    self.capture_diagnostic('automatic_quote_interrupted')
                    self.record(selection,'held',reason='automatic_quote_interrupted');raise
                except Exception as exc:
                    self.record(selection,'held',reason=exc.code if isinstance(exc,Held) else 'automatic_quote_stopped')
                    if self.failure:raise Held(self.failure)
                    reason=exc.code if isinstance(exc,Held) else 'automatic_quote_stopped'
                    if reason not in {'source_rate_or_availability_stop'}:
                        if self.diagnostic is None:self.capture_diagnostic(reason)
                        raise Held(reason)
                finally:
                    self.stage='between';self.selection=None;self.epoch+=1;await self.drain()
            if deferred_current is not None:raise Held('source_current_quantity_has_no_unspent_native_change')
        return True
    def result(self,complete,reason=None):
        self.cursor['domain_exhausted']=complete;self.cursor['last_reason_code']=reason;self.cursor['updated_at']=_now();self.persist()
        progress=self.cursor['progress_count'];advanced=progress>self.initial_progress
        held_count=sum(v['status']=='held' for v in self.cursor['receipts'].values())
        closed_spent=sum(spent(self.store.root,self.source,e['child_id'],q) and semantic_key(self.source,e['child_id'],q) not in self.cursor['receipts'] for e in self.catalog['entries'] for q in e['historical_prices'])
        if complete and (held_count or closed_spent):reason=reason or 'finite_domain_has_closed_or_held_contexts'
        status=('access_required' if reason in {'source_auth_required','source_account_profile_action_required','source_project_details_required'} else 'matched_current_source_quote' if complete and self.cursor['verified_count'] and not held_count and not closed_spent
                else 'continuation_required' if advanced and (reason is None or reason in {'source_rate_or_availability_stop','automatic_session_budget'}) else 'held')
        return {'status':status,'reason_code':reason,'profile_id':PROFILE_ID,'result_reference':str(self.path.relative_to(self.store.root)),
            'progress_token':digest(self.path.read_bytes()),'progress_count':progress,'processed_count':self.cursor['processed_count'],'verified_count':self.cursor['verified_count'],
            'held_count':held_count,'closed_prior_spent_count':closed_spent,'delay_seconds':self.delay_seconds,
            'pipeline_retry_count':self.cursor['pipeline_retry_count'],'consecutive_read_retries':self.cursor['consecutive_read_retries'],
            'verified_this_call':self.completed_this_call,'domain_exhausted':complete,'diagnostic':self.diagnostic,
            'completion_basis':'fresh_and_saved_verified_scope_receipts' if self.completed_this_call else 'saved_verified_scope_receipts_no_new_quote',
            'request_counter_scope':'controlled_document_price_summary_and_public_static_xhr_only','catalog_declared_quantities':self.catalog['declared_domain_count'],
            'quarantined_or_uncollected_children':len(self.catalog['excluded_children']),'controlled_requests':dict(self.counts),
            'blocked_requests':dict(self.blocked),'supplier_requests_this_call':sum(self.counts.values())+self.customer_reads+self.address_reads,'address_metadata_requests_sent':self.address_reads,'address_modules_proved':len(self.address_modules)==len(ADDRESS_PUBLIC_MODULES),'address_lookup_ready':self.address_ready,'account_profile_action_required':self.account_profile_gate_observed,'customer_metadata_requests_sent':self.customer_reads,'customer_document_declared':self.customer_declared,'customer_public_module_declared':self.customer_module_declared,
            'shipping_requests_sent':0,'cart_order_upload_checkout_requests_sent':0,'price_fresh':self.completed_this_call>0,
            'source_data_finalized':False,'woocommerce_import_ready':False,'catalog_complete':False}
    async def run(self):
        context=None;popup_guard=None;complete=False
        try:
            await self.check_scope();await validate_url(self.source)
            # No quantity is reserved until its real quantity action is about to run.
            for entry in self.catalog['entries']:
                if any(not spent(self.store.root,self.source,entry['child_id'],q) for q in entry['historical_prices']):self.target=entry;break
            else:return self.result(True,'no_unspent_contexts')
            async with asyncio.timeout(330):
                async with browser_slot(self.check_scope,source_origin='https://4over.com',per_source_limit=self.concurrency),self.pool.page('https://4over.com',headless=False) as page:
                    self.page=page;context=page.context
                    if hasattr(page,'on'):page.on('response',self.public_response_seen)
                    async def reject_popup(route):
                        try:owner=route.request.frame.page
                        except Exception:owner=None
                        seen=set()
                        while owner is not None and owner not in seen:
                            if owner is page:self.failure='unexpected_source_popup';return await route.abort()
                            seen.add(owner);owner=await owner.opener()
                        await route.fallback()
                    popup_guard=reject_popup;await context.route('**/*',popup_guard);await page.route('**/*',self.guard);await page.add_init_script(POPUP_GUARD_JS)
                    self.begin_bootstrap();await page.goto(self.source,wait_until='domcontentloaded',timeout=30000)
                    if page.url!=self.source or not getattr(self,'document_sha256',None):raise Held('source_document_unavailable')
                    await self.wait_bootstrap()
                    complete=await self.collect()
                    await self.drain();await self.check_scope()
                    if file_identity(self.catalog['checkpoint_path'])!=self.catalog['checkpoint_identity']:raise Held('catalog_changed')
                    return self.result(complete)
        except asyncio.CancelledError:
            self.capture_diagnostic('automatic_session_interrupted')
            if self.cursor.get('inflight'):self.record(self.cursor['inflight']['selection'],'held',reason='automatic_session_interrupted')
            raise
        except Exception as exc:
            reason=self.failure or (exc.code if isinstance(exc,Held) else 'automatic_session_stopped')
            quote_started=self.cursor.get('inflight') is not None
            if quote_started:self.record(self.cursor['inflight']['selection'],'held',reason=reason)
            if (reason=='source_rate_or_availability_stop' and not quote_started and self.counts['summary']==0
                    and self.last_failed_kind in {'document','static','price','customer','address'}):
                if self.cursor['consecutive_read_retries']<3:
                    self.cursor['pipeline_retry_count']+=1;self.cursor['consecutive_read_retries']+=1
                    self.cursor['progress_count']=self.cursor['processed_count']+self.cursor['pipeline_retry_count']
                    self.cursor['last_safe_read_retry']={'request_kind':self.last_failed_kind,'quote_requests':0,'selection_started':False,'at':_now()}
                else:reason='source_read_retry_limit'
            self.capture_diagnostic(reason,error=exc)
            return self.result(False,reason)
        finally:
            self.closed=True
            if self.proceed_identity is not None:
                try:await self.proceed_identity.dispose()
                except Exception:pass
                self.proceed_identity=None
            if hasattr(getattr(self,'page',None),'remove_listener'):
                try:self.page.remove_listener('response',self.public_response_seen)
                except Exception:pass
            asset_tasks=tuple(self.public_asset_tasks)
            for task in asset_tasks:task.cancel()
            if asset_tasks:await asyncio.gather(*asset_tasks,return_exceptions=True)
            if context is not None and popup_guard is not None:
                try:await context.unroute('**/*',popup_guard)
                except Exception:pass
            pending=tuple(t for t in self.handlers if t is not asyncio.current_task())
            for task in pending:task.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)
            responses=[f['owned'] for group in (self.static,self.price_flights,self.customer_flights,self.address_flights) for f in group.values() if f['owned'] is not None]
            if responses:await asyncio.gather(*(r.dispose() for r in responses),return_exceptions=True)

async def execute_fourover_profile_quote(store,browser_pool,association,configuration,check_scope):
    """Queue-owned product batch. Never opens extra tabs or edits campaign data."""
    try:
        association=association_context(association)
        configuration=normalize_configuration(configuration)
        limit=configuration['max_quotes_per_session']
        await check_scope();catalog=await asyncio.to_thread(load_catalog,store,association,configuration.get('catalog_reference'))
        path,key=cursor_path(store,catalog);collector=FourOverChildPriceCollector(store,None)
        async with collector._locked(path,check_scope):
            await check_scope();path,cursor=load_cursor(store,catalog)
            if cursor.get('inflight'):
                old=cursor['inflight'];selection=old['selection'];semantic=old['semantic']
                cursor['receipts'][semantic]={'status':'held','child_id':selection['child_id'],'quantity':selection['run_size'],'evidence':None,'reason_code':'prior_interrupted_quote'}
                cursor['processed_count']=len(cursor['receipts']);cursor['progress_count']=cursor['processed_count']+cursor['pipeline_retry_count'];cursor['verified_count']=sum(v['status']=='verified' for v in cursor['receipts'].values());cursor['inflight']=None;save_cursor(path,cursor)
            observer=NativeObserver(store,browser_pool)
            return await observer._attempt({'catalog':catalog,'cursor_path':path,'cursor':cursor},check_scope,
                automatic_profile={'profile_id':PROFILE_ID,'max_quotes_per_session':limit})
    except Held as exc:return {'status':'needs_profile' if exc.code.startswith(('catalog_','package_family','source_family','source_url')) else 'held','reason_code':exc.code,'source_data_finalized':False,'supplier_requests_this_call':0}


def queue_handler(store,browser_pool):
    async def handler(association,configuration,check_scope):
        return await execute_fourover_profile_quote(store,browser_pool,association,configuration,check_scope)
    return handler
