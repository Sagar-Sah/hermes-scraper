"""Metadata only on the existing owned DPS account page; never navigate or quote."""
from __future__ import annotations

import json

DPS_SUPPLIER_ID = 'custom_34869860f0e69b5f3d03ae40745e15edd71f0cd79d62c9b0c2d87c45e484ac3c'
DPS_ACCOUNT_URL = 'https://www.digital-print-solutions.com/my-account'
DPS_ORIGIN = 'https://www.digital-print-solutions.com'
DPS_REGISTRY_ORIGIN = 'https://digital-print-solutions.com'
PUBLIC_PATHS = {
    '/': 'catalog', '/products': 'catalog', '/vinyl-decals': 'catalog',
    '/vinyl-decals/hi-performance': 'product',
    '/vinyl-decals/calendered-vinyl': 'product',
    '/vinyl-decals/white-reflective': 'product',
    '/vinyl-decals/clear-decals': 'product',
    '/vinyl-decals/crack-and-peel-stickers': 'product',
    '/low-tack-vinyl': 'product',
    '/vinyl-decals/3-mil-high-tack-vinyl-material': 'product',
    # Discovery-only public path allowlist: existence is not assumed and no
    # link is followed. API paths, checkout, upload and account paths are absent.
    '/cart': 'cart', '/shopping-cart': 'cart',
    '/shipping': 'shipping', '/shipping-estimate': 'shipping',
    '/shipping-calculator': 'shipping',
}
PUBLIC_LABELS = (
    'Home', 'Products', 'Shop', 'Shop Products', 'Vinyl Decals', 'Cart',
    'Shopping Cart', 'View Cart', 'Shipping', 'Estimate Shipping',
    'Shipping Estimate', 'Shipping Calculator', 'Calculate Shipping',
    'Hi-Performance', 'Calendered Vinyl Decals with Lamination',
    'Reflective Decals with Lamination', 'Clear Decals with Lamination',
    'Crack and Peel Stickers', 'Low Tack Vinyl Decals with Lamination',
    '3 mil High Tack Vinyl Decal Material',
)
CONTROL_IDS = ('txtShippingZip', 'CountryList', 'estimateShipping',
               'CustProductWidthHolder', 'CustProductHeightHolder',
               'CustProductStaticSizes', 'CustQuantityList')
CONTROL_NAMES = ('postalCode', 'country', 'width', 'height', 'quantity', 'shippingMethod')
MAX_LINKS, MAX_FORMS, MAX_CONTROLS, MAX_TOTAL_CONTROLS, MAX_SCAN = 24, 8, 16, 64, 512


def validate_dps_target(key, registered, target):
    if (key != DPS_SUPPLIER_ID or type(target) is not str or target != DPS_ACCOUNT_URL or
        not isinstance(registered, dict) or registered.get('id') != DPS_SUPPLIER_ID or
        registered.get('custom') is not True or registered.get('origin') != DPS_REGISTRY_ORIGIN):
        raise ValueError('supplier_workflow_scope_unsupported')


WORKFLOW_REFERENCES_JS = r'''({expected_url}) => {
  const expected=__URL__, origin=__ORIGIN__, paths=__PATHS__, labels=__LABELS__, ids=__IDS__, names=__NAMES__;
  const no=reason=>({observed:false,reason});
  if(expected_url!==expected||location.href!==expected)return no('supplier_workflow_url_changed');
  const visible=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&
    getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden'&&e.getClientRects().length>0;
  const pathFor=raw=>{
    if(typeof raw!=='string'||raw.length<1||raw.length>256||/[?%\\\u0000-\u0020]/.test(raw)||raw.includes('..'))return null;
    try {const u=new URL(raw,expected);if(u.origin!==origin||u.username||u.password||u.search||u.hash||!Object.hasOwn(paths,u.pathname))return null;return u.pathname;}catch(_){return null;}
  };
  const anchors=document.querySelectorAll('a[href]'), allForms=document.querySelectorAll('form');
  if(anchors.length>512||allForms.length>8)return no('supplier_workflow_metadata_limit');
  const links=[],forms=[],seen=new Set();let totalControls=0;
  for(const a of anchors) {
    if(!visible(a))continue;
    const path=pathFor(a.getAttribute('href'));if(path===null)continue;
    const rawText=a.textContent||'',text=rawText.length<=120?rawText.replace(/\s+/g,' ').trim():'';
    const label=labels.includes(text)?text:null,identity=path+'\0'+(label||'');
    if(seen.has(identity))continue;seen.add(identity);
    links.push({path,label,kind:paths[path],cart_isolation_required:['cart','shipping'].includes(paths[path])});
    if(links.length>24)return no('supplier_workflow_metadata_limit');
  }
  for(const form of allForms) {
    if(!visible(form))continue;
    const path=pathFor(form.getAttribute('action'));if(path===null)continue;
    const method=(form.getAttribute('method')||'get').toLowerCase();if(!['get','post'].includes(method))continue;
    const nodes=form.querySelectorAll('input,select,button,textarea');
    if(nodes.length>64)return no('supplier_workflow_metadata_limit');
    const controls=[];
    for(const e of nodes) {
      if(!visible(e))continue;
      const id=e.getAttribute('id')||'',name=e.getAttribute('name')||'';
      if(!ids.includes(id)&&!names.includes(name))continue;
      const tag=e.tagName.toLowerCase(),type=tag==='input'?(e.getAttribute('type')||'text').toLowerCase():tag;
      if(!['text','number','select','radio','button','submit'].includes(type))continue;
      controls.push({kind:type,id:ids.includes(id)?id:null,name:names.includes(name)?name:null,
        required:e.hasAttribute('required'),disabled:e.matches(':disabled')});
      if(controls.length>16||++totalControls>64)return no('supplier_workflow_metadata_limit');
    }
    forms.push({action_path:path,method,kind:paths[path],cart_isolation_required:['cart','shipping'].includes(paths[path]),controls});
  }
  if(location.href!==expected)return no('supplier_workflow_url_changed');
  return {observed:true,links,forms};
}'''.replace('__URL__', json.dumps(DPS_ACCOUNT_URL)).replace('__ORIGIN__', json.dumps(DPS_ORIGIN)).replace('__PATHS__', json.dumps(PUBLIC_PATHS)).replace('__LABELS__', json.dumps(PUBLIC_LABELS)).replace('__IDS__', json.dumps(CONTROL_IDS)).replace('__NAMES__', json.dumps(CONTROL_NAMES))


def project_workflow_references(raw):
    """Revalidate browser projection; unknown fields never escape to the API."""
    if not isinstance(raw, dict):
        raise ValueError('supplier_workflow_projection_invalid')
    if raw.get('observed') is not True:
        if raw == {'observed': False, 'reason': 'supplier_workflow_url_changed'}:
            raise ValueError('supplier_workflow_url_changed')
        if raw == {'observed': False, 'reason': 'supplier_workflow_metadata_limit'}:
            raise ValueError('supplier_workflow_metadata_limit')
        raise ValueError('supplier_workflow_projection_invalid')
    if set(raw) != {'observed', 'links', 'forms'} or not isinstance(raw['links'], list) or not isinstance(raw['forms'], list) or len(raw['links']) > MAX_LINKS or len(raw['forms']) > MAX_FORMS:
        raise ValueError('supplier_workflow_projection_invalid')
    links, forms, seen, total = [], [], set(), 0
    for row in raw['links']:
        if (not isinstance(row, dict) or set(row) != {'path','label','kind','cart_isolation_required'} or
            not isinstance(row['path'], str) or row['path'] not in PUBLIC_PATHS or
            row['kind'] != PUBLIC_PATHS[row['path']] or
            type(row['cart_isolation_required']) is not bool or row['cart_isolation_required'] != (row['kind'] in ('cart','shipping')) or
            row['label'] is not None and (not isinstance(row['label'], str) or row['label'] not in PUBLIC_LABELS)):
            raise ValueError('supplier_workflow_projection_invalid')
        identity = (row['path'], row['label'])
        if identity in seen: raise ValueError('supplier_workflow_projection_invalid')
        seen.add(identity)
        links.append(dict(row))
    for row in raw['forms']:
        if (not isinstance(row, dict) or set(row) != {'action_path','method','kind','cart_isolation_required','controls'} or
            not isinstance(row['action_path'], str) or row['action_path'] not in PUBLIC_PATHS or
            row['kind'] != PUBLIC_PATHS[row['action_path']] or row['method'] not in ('get','post') or
            type(row['cart_isolation_required']) is not bool or row['cart_isolation_required'] != (row['kind'] in ('cart','shipping')) or
            not isinstance(row['controls'], list) or len(row['controls']) > MAX_CONTROLS):
            raise ValueError('supplier_workflow_projection_invalid')
        controls = []
        for control in row['controls']:
            if (not isinstance(control, dict) or set(control) != {'kind','id','name','required','disabled'} or
                control['kind'] not in ('text','number','select','radio','button','submit') or
                control['id'] is not None and control['id'] not in CONTROL_IDS or
                control['name'] is not None and control['name'] not in CONTROL_NAMES or
                control['id'] is None and control['name'] is None or
                type(control['required']) is not bool or type(control['disabled']) is not bool):
                raise ValueError('supplier_workflow_projection_invalid')
            controls.append(dict(control));total += 1
            if total > MAX_TOTAL_CONTROLS: raise ValueError('supplier_workflow_projection_invalid')
        forms.append({**row,'controls':controls})
    return {'links':links,'forms':forms}
