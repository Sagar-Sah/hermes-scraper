"""One-page diagnosis with optional passive current price-component evidence.

Only duplicate pages belong to this manager. Browser contexts/profiles belong to
BrowserSessionPool; neither credentials nor browser storage are inspected here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .transport import RateLimiter, validate_url
from .b2sign_diagnosis import inspect_b2sign, merge_b2sign_snapshot
from .dps_pilot import DPS_DOM_JS
from .price_contract import (CONTROLLER_PRICE_STATE_JS, SOURCE_CONTRACT, SPECIAL_SHAPES_SOURCE_CONTRACT,
                             SPECIAL_SHAPES_SOURCE_URL, BASE_FIELDS, OPTION_FIELDS, calculate_source_total, reviewed_source_scope)
from .printograph_price_journal import (PriceJournal, OwnedPageCDPObserver, project_context,
    SPECIAL_FAMILY, STAGGERED_FAMILY, family_for_url)
from .printograph_initial_quantity import InitialQuantityPriceJournal, read_initial_body
from .supplier_profile_executor import (SupplierProfileExecutorMixin, TableQuantityJournal,
    LiveTableAnchor, read_binding_script, executor_binding_script, executor_action_script, require as profile_require)
from .supplier_registry import SupplierRegistry
from .dps_workflow_references import (DPS_ACCOUNT_URL, WORKFLOW_REFERENCES_JS,
                                      validate_dps_target, project_workflow_references)
from .printograph_source_references import SOURCE_URLS, SOURCE_REFERENCES_JS, project_references
from .printograph_turnaround_metadata import TURNAROUND_METADATA_JS, project_turnaround_metadata


SUPPLIERS = {
    "printograph": {"name": "Printograph", "origin": "https://www.printograph.com", "entry_url": "https://www.printograph.com/user/login"},
    "b2sign": {"name": "B2Sign", "origin": "https://www.b2sign.com", "entry_url": "https://www.b2sign.com/"},
    "soardist": {"name": "Soardist", "origin": "https://soardistusa.com", "entry_url": "https://soardistusa.com/"},
    "fourover": {"name": "4over", "origin": "https://4over.com", "entry_url": "https://4over.com/customer/account/login/"},
    "printitprints": {"name": "Print It Prints", "origin": "https://printitprints.com", "entry_url": "https://printitprints.com/wp-admin/"},
}

# This script reads only rendered product content and finite/numeric controls.
# No hidden/text/password/file values, HTML, account identity, or storage APIs.
DIAGNOSIS_JS = r"""() => {
  const clean = (s, n=240) => String(s || '').replace(/\s+/g, ' ').trim().slice(0,n);
  const visible = e => !!e && !e.closest('[hidden],[aria-hidden="true"]') &&
    getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden' &&
    e.getClientRects().length > 0;
  const text = e => clean(e?.innerText || e?.textContent);
  const explicitLogout = [...document.querySelectorAll('a,button,input[type="submit"]')].some(e =>
    visible(e) && /^(?:log\s*out|sign\s*out)$/i.test(text(e) || e.getAttribute('aria-label') || e.value || ''));
  const explicitAccount = [...document.querySelectorAll('[data-authenticated="true"],[data-logged-in="true"]')]
    .some(e => visible(e) && /account|dashboard|signed in/i.test(text(e)));
  const login = [...document.querySelectorAll('input[type="password"]')].some(e => visible(e) &&
    !!e.closest('form,[role="dialog"],main'));
  const session_status = login ? 'login_required' : explicitLogout || explicitAccount ? 'signed_in' : 'unknown';
  const scopeSelectors = ['#order-form','#product_calculator_display','form.variations_form',
    '[data-pirc]','[data-pigf]','[data-piso]','.pirc-configurator','.pigf-configurator',
    '[id^="product-"]','.product .summary'];
  let roots = [...document.querySelectorAll(scopeSelectors.join(','))].filter(visible);
  if (!roots.length) roots = [document.querySelector('main') || document.querySelector('article')].filter(Boolean);
  const excluded = /(?:password|username|email|e-mail|login|log in|sign in|search|newsletter|subscribe|coupon|promo code|credit card|card number|payment|billing|upload|artwork file|captcha|csrf|token|verification code|security code|one.time|two.factor|\botp\b|\bpin\b|agree|privacy|terms)/i;
  const parentLabel = e => {
    for(let n=e,depth=0;n&&depth<3;n=n.parentElement,depth++){
      const parent=n.parentElement;if(!parent||parent.matches('body,main,form,header,footer,nav'))break;
      if(parent.querySelectorAll('select,input:not([type="hidden"]),textarea').length!==1)continue;
      const named=[...parent.children].filter(x=>x!==n&&visible(x)&&x.matches('label,.control-label,.field-label'));
      const direct=[...parent.childNodes].filter(x=>x.nodeType===3).map(x=>x.textContent).join(' ');
      const label=clean(named.length===1?named[0].textContent:direct);if(label)return label;
    }
    return '';
  };
  const localLabel = e => {
    const linked = clean([...e.labels || []].map(text).join(' '));
    if (linked) return linked;
    const labelled = clean((e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>text(document.getElementById(id))).join(' '));
    if (labelled) return labelled;
    if (e.getAttribute('aria-label')) return clean(e.getAttribute('aria-label'));
    const wrap = e.closest('.form-group,.field,.form-row,.control-group,[data-field]');
    return text(wrap?.querySelector('label')) || parentLabel(e);
  };
  const controls = [...new Set(roots.flatMap(r=>[...r.querySelectorAll('select,input[type="radio"],input[type="checkbox"],input[type="number"],input[type="text"],input:not([type]),textarea')]))];
  const nonProductText='form#commentform,.comment-form,form[action*="wp-comments-post"],.woocommerce-Reviews,.product-reviews,form.product-reviews-form,[data-role="product-review"],.contact-form,.contact-widget,[data-widget="contact"],[data-form-type="contact"],.chat-widget,[data-widget="chat"],[data-role="chat"]';
  const fields = [], groups = new Map();
  let omitted = 0;
  for (const e of controls) {
    if ((!visible(e) && !(['radio','checkbox'].includes(e.type) && [...e.labels || []].some(visible))) || e.closest('header,footer,nav,[role="search"],form[role="search"]')) continue;
    const form = e.closest('form');
    if (form?.querySelector('input[type="password"]')) continue;
    const freeText=e.tagName==='TEXTAREA'||e.tagName==='INPUT'&&e.type==='text';
    if(freeText&&(!e.required&&e.getAttribute('aria-required')!=='true'||e.disabled||e.closest(nonProductText)))continue;
    const kind = freeText ? 'required_text' : e.tagName === 'SELECT' ? 'select' : e.type;
    const ownLabel = localLabel(e);
    const container = e.closest('fieldset,.form-group,.field,.form-row,.control-group,[data-field]');
    const heading = container?.querySelector('legend,h2,h3,h4,h5,h6,.control-label,.field-label');
    const legend = visible(heading) ? text(heading) : '';
    // Printograph gives each foil-color radio a numbered name even though they
    // form one mutually exclusive customer choice.
    const foilGroup = kind === 'radio' && /^raisedFoilColor\d+$/.test(e.name || '') && !!e.closest('#order-form');
    const sourceName = foilGroup ? 'raisedFoilColor' : e.name || '';
    const label = foilGroup ? 'Foil Colors' : (kind === 'radio' || kind === 'checkbox') ? legend || ownLabel : ownLabel;
    if (!label || excluded.test([label,e.name,e.id,e.getAttribute('data-id'),form?.getAttribute('action')].join(' '))) { omitted++; continue; }
    const group = (kind === 'radio' || kind === 'checkbox') && (sourceName || e.closest('fieldset'));
    const groupKey = group ? kind + ':' + (sourceName || legend) : null;
    if (groupKey && groups.has(groupKey)) {
      const field = groups.get(groupKey);
      if (field.choices.length < 500) field.choices.push({value:e.value,label:ownLabel || clean(e.value),disabled:e.matches(':disabled')});
      if (e.checked) field.selected_values.push(e.value);
      field.required ||= !!e.required;
      field.disabled &&= e.matches(':disabled');
      continue;
    }
    if (fields.length >= 200) { omitted++; continue; }
    const field = {label,kind,choices:[],selected_values:[],required:!!e.required,disabled:e.matches(':disabled'),multiple:kind==='checkbox'||kind==='select'&&e.multiple,
      configurable:true,source_key:sourceName || e.id || e.getAttribute('data-id') || label,source_name:sourceName,source_id:e.id || '',
      ...(!sourceName&&!e.id&&e.getAttribute('data-id')?{source_data_id:e.getAttribute('data-id')}: {})};
    if (kind === 'select') {
      field.choices = [...e.options].slice(0,500).map(o=>({value:o.value,label:clean(o.label || o.textContent),disabled:!!o.disabled || !!o.closest('optgroup')?.disabled}));
      field.selected_values = [...e.selectedOptions].slice(0,500).map(o=>o.value);
      if (e.options.length > 500) omitted++;
    } else if (kind === 'required_text') {
      field.required=true;field.value_missing=e.validity?.valueMissing===true;
    } else if (kind === 'number') {
      const numeric = s => /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?$/i.test(s || '') && Number.isFinite(Number(s));
      field.selected_values = numeric(e.value) ? [e.value] : [];
      for (const key of ['min','max','step']) {
        const v=e.getAttribute(key); field[key]=(numeric(v) || key==='step' && v==='any') ? v : null;
      }
    } else {
      field.choices = [{value:e.value,label:ownLabel || clean(e.value),disabled:e.matches(':disabled')}];
      if (e.checked) field.selected_values = [e.value];
    }
    fields.push(field); if (groupKey) groups.set(groupKey,field);
  }
  const turnaround = [...document.querySelectorAll('#radioBtn a.turnAround[data-toggle="turnaround"][data-title]')].filter(visible);
  if (turnaround.length && document.querySelector('#order-form')) {
    const section = turnaround[0].closest('.form-group,.row,section');
    const heading = [...(section?.querySelectorAll('h2,h3,h4,h5,h6,label,.control-label,.field-label,.customheaderlabel') || [])].find(visible);
    const label = text(heading);
    if (label) fields.push({label,kind:'radio',source_key:'productionTime',source_name:'productionTime',source_id:'radioBtn',
      required:false,multiple:false,disabled:turnaround.every(e=>e.getAttribute('aria-disabled')==='true'||e.classList.contains('disabled')),
      selected_values:turnaround.filter(e=>e.classList.contains('active')).map(e=>e.getAttribute('data-title')),
      choices:turnaround.map(e=>({value:e.getAttribute('data-title'),label:text(e),disabled:e.getAttribute('aria-disabled')==='true'||e.classList.contains('disabled')||/\bnot\s+available\b/i.test(text(e))}))});
    else omitted++;
  }
  const product = document.querySelector('[id^="product-"],main,article');
  const titleElement = product?.querySelector('h1.product_title,h1') || document.querySelector('h1');
  const title = visible(titleElement) ? text(titleElement) : '';
  const descriptionElement = [...document.querySelectorAll('.woocommerce-product-details__short-description,#tab-description,[itemprop="description"],.product-description')].find(visible);
  const description = descriptionElement ? clean(descriptionElement.innerText,8000) : '';
  const imageElements = product ? [...product.querySelectorAll('.woocommerce-product-gallery img,[itemprop="image"],.product-image img,.product-images img')].filter(visible) : [];
  const images = imageElements.slice(0,50).map(e=>({url:e.currentSrc || e.src || '',alt:clean(e.alt)}));
  // Only product-scoped, visible supplier download links. Never inspect the
  // downloaded file or copy account, upload, search, or footer links.
  const artwork_templates = [...document.querySelectorAll('#productdownloads a[href],.product-downloads a[href],.product-details a[href],table a[href]')]
    .filter(visible).slice(0,100).map(a=>{
      const label=text(a),href=a.getAttribute('href')||'';
      let target;try{target=new URL(href,location.href);}catch(_){return null;}
      if(target.protocol!=='https:'||target.username||target.password||target.search||target.hash)return null;
      const knownWs=(target.hostname==='display-templates.s3.us-west-2.amazonaws.com'&&/^\/(?:PDF|PSB)\/[^/]+\.(?:pdf|ai|eps|zip)$/i.test(target.pathname))
        ||(target.hostname==='s3-us-west-2.amazonaws.com'&&/^\/display-templates\/(?:PDF|PSB)\/[^/]+\.(?:pdf|ai|eps|zip)$/i.test(target.pathname));
      const supplierHost=target.hostname===location.hostname;
      const knownB2=supplierHost&&/^\/downloadable\/[a-f0-9]{40}$/i.test(target.pathname);
      const namedFile=supplierHost&&/\.(?:pdf|ai|eps|zip)$/i.test(target.pathname);
      if(!knownWs&&!knownB2&&!namedFile)return null;
      if(!/template|download|artwork|dieline|die.line/i.test(label+' '+target.pathname))return null;
      const row=a.closest('tr');return{url:target.href,label:clean(label,80),choice_hint:clean(row?.innerText||'',240)};
    }).filter(Boolean).slice(0,50);
  const fileSetupHeading = [...document.querySelectorAll('h2,h3,h4,h5,h6')]
    .find(e=>visible(e)&&/^(?:file setup|artwork setup|artwork requirements)$/i.test(text(e)));
  const artwork_setup = fileSetupHeading ? clean(fileSetupHeading.parentElement?.innerText||'',3000) : '';
  const busy = [...document.querySelectorAll('#ajaxLoader,[aria-busy="true"],.loading-overlay,.loading-mask')].some(visible);
  const price_login_required = [...document.querySelectorAll('a,span,p,label,div')].some(e =>
    visible(e) && /(?:please\s+)?(?:log|sign)\s*in\s+to\s+(?:view|see|access)\s+(?:the\s+)?(?:price|prices|pricing)\b|unit price\s*:?\s*please\s+log\s*in/i.test(text(e)));
  return {title,fields,content:{title,description,images,artwork_templates,artwork_setup,source_host:location.hostname},session_status:price_login_required?'login_required':session_status,price_login_required,omitted,busy,document_ready:document.readyState!=='loading'};
}"""


_ID = re.compile(r"^[a-f0-9]{32}$")
_BLOCKED_PATH = re.compile(r"(?:^|/)(?:wp-admin|wp-login\.php|login|log-in|signin|sign-in|register|checkout|cart|shopping-cart(?:\.html)?|account|my-account|payment|orders?)(?:/|$)", re.I)
_BLOCKED_QUERY = re.compile(r"(?:password|passwd|token|secret|session|nonce|auth|key|checkout|add.?to.?cart|action|redirect|return.?url)", re.I)


def _blocked_product_path(path):
    return bool(_BLOCKED_PATH.search(path)) and not bool(re.fullmatch(r"/products/[a-zA-Z0-9_-]+/order/?", path))


# Passive projection never invokes a source price-request or coupon method.
# The one mounted totalPrice scalar read is permitted only by the exact pinned
# exact pinned arithmetic callgraphs reviewed for the closed type39/type38 families.
def _passive_script(template, family):
    return (template.replace('__PASSIVE_CONTRACT__', json.dumps(SPECIAL_SHAPES_SOURCE_CONTRACT))
            .replace('__PASSIVE_URL__', json.dumps(family.source_url))
            .replace('__PASSIVE_TYPE__', json.dumps(family.product_type))
            .replace('__PASSIVE_SPECS__', json.dumps(list(family.base_fields)))
            .replace('__PASSIVE_OPTIONS__', json.dumps(list(family.option_fields)))
            .replace('__PASSIVE_FIELDS__', json.dumps([*family.base_fields,*family.option_fields,
                'quantity','productionTime','proofType','manualProofType','designServices','fileDelivery'])))


_PASSIVE_PRICE_CAPTURE_TEMPLATE = r'''({source_contract,settled}) => {
  const expected=__PASSIVE_CONTRACT__;
  if (!source_contract || JSON.stringify(Object.keys(source_contract).sort())!==JSON.stringify(Object.keys(expected).sort()) ||
      !Object.keys(expected).every(k=>source_contract[k]===expected[k]) || location.href!==__PASSIVE_URL__)
    return {observed:false,reason:'passive_scope_unproven'};
  const element=document.querySelector('#order-form');
  if (!element || element.getAttribute('data-printproducttype')!==__PASSIVE_TYPE__ || element.getAttribute('data-currentstoretype')!=='broker')
    return {observed:false,reason:'passive_controller_scope_unproven'};
  const roots=[element._vnode?.component?.proxy,element.__vue_app__?._instance?.proxy].filter(Boolean);
  const controllers=[...new Set(roots.map(r=>r.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];
  if (controllers.length!==1) return {observed:false,reason:'passive_controller_missing'};
  const c=controllers[0],d=c.jsonObj;
  const specs=__PASSIVE_SPECS__,options=__PASSIVE_OPTIONS__;
  if (String(c.printProductType)!==__PASSIVE_TYPE__ || JSON.stringify(c.fields?.[__PASSIVE_TYPE__]?.specs)!==JSON.stringify(specs) ||
      JSON.stringify(c.fields?.[__PASSIVE_TYPE__]?.options)!==JSON.stringify(options) || !d?.userSelectedOptions || !d.appliedPrices || !d.others)
    return {observed:false,reason:'passive_controller_schema_unproven'};
  if (__PASSIVE_TYPE__ === '38' && Object.hasOwn(d.userSelectedOptions,'productSpecificationId'))
    return {observed:false,reason:'unsupported_staggered_specification_context'};
  const scalar=v=>typeof v==='string'&&v.length<=64 || typeof v==='number'&&Number.isFinite(v)&&Math.abs(v)<1e12;
  const selected={};
  for(const k of [...specs,...options,'proofType','manualProofType','designServices','fileDelivery','quantity','productId','productionTime'])
    if(Object.hasOwn(d.userSelectedOptions,k)&&scalar(d.userSelectedOptions[k]))selected[k]=d.userSelectedOptions[k];
  const prices={};
  if(scalar(d.appliedPrices.productPrice))prices.productPrice=d.appliedPrices.productPrice;
  for(const k of [...options,'proofType','designServices']) {
    const bucket=d.appliedPrices[k];if(bucket===undefined)continue;prices[k]={};
    const v=k==='proofType'?selected.manualProofType:selected[k];
    if(bucket&&typeof bucket==='object'&&Object.hasOwn(bucket,String(v))&&scalar(bucket[String(v)]))prices[k][String(v)]=bucket[String(v)];
  }
  const flags={};
  for(const [out,k]of[['is_admin','isAdmin'],['waive_fees','waiveFees']])if(typeof d.others[k]==='boolean')flags[out]=d.others[k];
  const present=v=>v!==''&&v!==null&&v!==undefined;
  if(Object.hasOwn(d.others,'itemId'))flags.has_item_id=present(d.others.itemId)&&d.others.itemId!=='null';
  if(Object.hasOwn(d.others,'editOrderItem')&&d.others.editOrderItem&&typeof d.others.editOrderItem==='object')flags.has_edit_order_item=Object.keys(d.others.editOrderItem).length>0;
  for(const [out,k]of[['has_shopping_cart_item','shoppingCartItemId'],['has_price_group','priceGroupId']])if(Object.hasOwn(d.userSelectedOptions,k))flags[out]=present(d.userSelectedOptions[k]);
  let manualControls;
  if (__PASSIVE_TYPE__ === '38') {
    if ([...document.querySelectorAll('#esCollapse')].some(e=>e.classList.contains('show')))
      return {observed:false,reason:'estimated_shipping_panel_open'};
    const visible=el=>el.getClientRects().length>0 && getComputedStyle(el).display!=='none' &&
      getComputedStyle(el).visibility!=='hidden' && !el.closest('[hidden],[aria-hidden="true"]');
    const checked=(name,value)=>{
      const values=[...element.querySelectorAll('input[type="radio"][name="'+name+'"]')].filter(e=>e.checked);
      if(values.length!==1 || String(values[0].value)!==value || values[0].matches(':disabled') || !visible(values[0])) return null;
      return {value,checked:true,enabled:true,visible:true};
    };
    const controls={proof_type:checked('proofType','100'),
      manual_proof:checked('proofTypeForManual',String(selected.manualProofType)),
      file_delivery:checked('fileDeliveryForManual','5')};
    if(Object.values(controls).every(Boolean)) manualControls=controls;
  }
  const result={observed:true,capture_kind:'controller',source_contract:expected,source_url:__PASSIVE_URL__,settled:settled===true,
    product_type:__PASSIVE_TYPE__,store_type:'broker',currency:'USD',field_specs:specs,field_options:options,
    userSelectedOptions:selected,appliedPrices:prices,other_context:flags};
  if(manualControls)result.manual_controls=manualControls;
  if (__PASSIVE_TYPE__ === '38' && typeof d.others.itemId === 'string' && d.others.itemId === 'null')
    result.base_price_query={mode:'source_literal_pair_v1',itemId:'null',cid:''};
  const total=c.totalPrice;if(scalar(total))result.controller_total=total;
  const state=c.couponData;
  if(state&&typeof state==='object'&&state.coupon&&typeof state.coupon==='object'&&!Array.isArray(state.coupon)) {
    const coupon=state.coupon,parameters={};
    for(const [out,k]of[['valid_for_specs','isCouponValidForSpecs'],['qualification_level','qualificationLevel']])if(Object.hasOwn(coupon,k)&&(scalar(coupon[k])||typeof coupon[k]==='boolean'))parameters[out]=coupon[k];
    if(coupon.status&&Object.hasOwn(coupon.status,'id')&&scalar(coupon.status.id))parameters.status_id=coupon.status.id;
    const tier=Array.isArray(coupon.amountTiers)?coupon.amountTiers[0]:null;
    if(tier&&typeof tier==='object')for(const [out,k]of[['amount_percent','amount'],['max_amount','maxAmount']])if(Object.hasOwn(tier,k)&&scalar(tier[k]))parameters[out]=tier[k];
    result.coupon={observed:true,mode:'source_state',coupon_object_observed:true,coupon_object_key_count:Object.keys(coupon).length,parameters};
    if(typeof state.isCouponSpecChanged==='boolean')result.coupon.is_coupon_spec_changed=state.isCouponSpecChanged;
    if(__PASSIVE_TYPE__ === '38')result.coupon.source_quantity=selected.quantity;
  }
  if (__PASSIVE_TYPE__ === '38') {
  const visible=e=>e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
  const totals=[...element.querySelectorAll('.totalSection')].filter(visible);
  const rows=totals.filter(e=>{
    const labels=e.querySelectorAll(':scope > .col-8 > .subTotal');
    return labels.length===1&&visible(labels[0])&&/^Subtotal\s*\(excludes shipping\)\s*:\s*$/i.test(labels[0].textContent.trim());
  });
  if(rows.length===1) {
    const row=rows[0], amounts=row.querySelectorAll(':scope > .col-4');
    const wrappers=totals.filter(e=>e!==row);
    const wrappersSafe=wrappers.every(e=>e.contains(row)&&e.matches('.alert.alert-secondary')&&
      e.querySelectorAll(':scope > .container').length===1&&
      e.querySelectorAll(':scope > .col-4,:scope > .subTotal').length===0&&
      [...e.childNodes].every(n=>n.nodeType!==3||!n.textContent.trim()));
    if(wrappersSafe&&amounts.length===1&&visible(amounts[0])) {
      const text=amounts[0].textContent.trim();
      if(text.length<=64&&/^\$(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{2})?$/.test(text))result.displayed_total=text;
    }
  }
  } else {
  const totals=[...element.querySelectorAll('.totalSection')].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
  if(totals.length===1) {
    const amount=totals[0].querySelector(':scope > .col-4');
    const text=amount?.textContent||'';
    if(text.length<=128) {const matches=[...text.matchAll(/\$[0-9,]+(?:\.[0-9]{2})?/g)];if(matches.length&&matches.length<=3)result.displayed_total=matches[matches.length-1][0];}
  }
  }
  return result;
}'''
PASSIVE_PRICE_CAPTURE_JS = _passive_script(_PASSIVE_PRICE_CAPTURE_TEMPLATE, SPECIAL_FAMILY)


# The exact pinned Vue formSelect template uses props.spec as its model key.
# An HTML name, label or generated-ID prefix is not equivalent to that key.
# Inspect only own data descriptors of the mounted VNode/ref graph; no source
# callback, computed value, accessor, or function body is called/read here.
_PASSIVE_UNNAMED_SELECT_BINDING_TEMPLATE = r'''({source_contract,source_id,selected_values,target_value}) => {
  const expected=__PASSIVE_CONTRACT__, allowed=__PASSIVE_FIELDS__;
  const no=()=>({observed:false,reason:'passive_native_model_binding_unproven'});
  if(!source_contract||JSON.stringify(Object.keys(source_contract).sort())!==JSON.stringify(Object.keys(expected).sort())||
     !Object.keys(expected).every(k=>source_contract[k]===expected[k])||location.href!==__PASSIVE_URL__||
     typeof source_id!=='string'||!(/^[A-Za-z][A-Za-z0-9_-]{0,127}$/).test(source_id)||
     !Array.isArray(selected_values)||selected_values.length!==1||typeof selected_values[0]!=='string'||
     typeof target_value!=='string'||!(/^[0-9]{1,12}$/).test(target_value))return no();
  const root=document.querySelector('#order-form');
  if(!root||root.getAttribute('data-printproducttype')!==__PASSIVE_TYPE__||root.getAttribute('data-currentstoretype')!=='broker')return no();
  const matches=root.querySelectorAll('select[id="'+source_id+'"]');
  if(matches.length!==1)return no();
  const el=matches[0];
  if(el.name!==''||el.multiple||el.matches(':disabled')||!el.getClientRects().length||
     getComputedStyle(el).visibility==='hidden'||el.closest('[hidden],[aria-hidden="true"]'))return no();
  const retained=[...el.selectedOptions].map(x=>x.value);
  if(retained.length!==1||retained[0]!==selected_values[0])return no();
  const targets=[...el.options].filter(x=>x.value===target_value);
  if(targets.length!==1||targets[0].disabled||targets[0].closest('optgroup')?.disabled||retained[0]===target_value)return no();
  const own=(obj,key)=>{
    if(!obj||typeof obj!=='object')return undefined;
    const d=Object.getOwnPropertyDescriptor(obj,key);
    if(d===undefined)return undefined;
    if(!Object.hasOwn(d,'value'))throw Error('accessor');
    return d.value;
  };
  try {
    const start=own(root,'_vnode');if(!start)return no();
    const queue=[start],seen=new Set(),bindings=[];
    for(let cursor=0;cursor<queue.length;cursor++) {
      if(cursor>=2048||queue.length>2048)return no();
      const node=queue[cursor];if(!node||typeof node!=='object'||seen.has(node))continue;
      seen.add(node);
      if(own(node,'el')===el) {
        const ref=own(node,'ref'),owner=own(ref,'i'),refs=own(owner,'refs'),type=own(owner,'type'),props=own(owner,'props');
        const spec=own(props,'spec'),vprops=own(node,'props'),data=own(owner,'data'),unique=own(data,'uniqueId');
        if(own(node,'type')!=='select'||own(ref,'r')!=='currentInput'||own(type,'name')!=='formSelect'||
           own(refs,'currentInput')!==el||typeof spec!=='string'||!allowed.includes(spec)||
           typeof unique!=='string'||!(/^[a-z0-9]{1,9}$/).test(unique)||source_id!==spec+'_'+unique||
           own(vprops,'id')!==source_id||typeof own(vprops,'onUpdate:modelValue')!=='function')return no();
        bindings.push(spec);
      }
      const component=own(node,'component');if(component){const tree=own(component,'subTree');if(tree)queue.push(tree);}
      const children=own(node,'children');
      if(Array.isArray(children)) {
        if(children.length>2048)return no();
        for(let index=0;index<children.length;index++) {
          const child=own(children,String(index));if(child&&typeof child==='object')queue.push(child);
        }
      }
    }
    if(bindings.length!==1)return no();
    return {observed:true,field:bindings[0],binding_kind:'pinned_vue_form_select_spec',retained_value:retained[0]};
  } catch(_) {return no();}
}'''
PASSIVE_UNNAMED_SELECT_BINDING_JS = _passive_script(_PASSIVE_UNNAMED_SELECT_BINDING_TEMPLATE, SPECIAL_FAMILY)



# Event identities/counts only. Never read edit, notes, upload or input values.
_PASSIVE_PRICE_FENCE_TEMPLATE = r'''({key,action,source_id=null,epoch=null,notify_key=null}) => {
  if(location.href!==__PASSIVE_URL__)return{ok:false};
  const root=document.querySelector('#order-form');if(!root)return{ok:false};
  if(action==='install') {
    if(Object.hasOwn(window,key))return{ok:false};
    const state={epoch:null,expected:null,input:0,change:0,unexpected:false,notify:notify_key,root,nativeRadioTarget:null,nativeRadioNext:null};
    state.handler=e=>{
      if(!root.contains(e.target)||!(e.target instanceof Element))return;
      const tag=e.target.tagName,type=e.target.getAttribute('type');
      if(tag!=='SELECT'&&!(tag==='INPUT'&&(type==='radio'||type==='number')))return;
      const nativeRadio=__PASSIVE_TYPE__==='38'&&state.nativeRadioTarget!==null;
      if(state.epoch===null||e.target.id!==state.expected||(e.isTrusted&&!nativeRadio))state.unexpected=true;
      else if(nativeRadio&&(e.target!==state.nativeRadioTarget||tag!=='INPUT'||type!=='radio'||
              state.nativeRadioNext!==e.type))state.unexpected=true;
      else {state[e.type]++;if(state[e.type]>1)state.unexpected=true;
        if(nativeRadio)state.nativeRadioNext=e.type==='input'?'change':'done';}
      if(state.unexpected&&typeof window[state.notify]==='function') {try{window[state.notify]('unattributed_control_change').catch(()=>{});}catch(_){}}
    };
    document.addEventListener('input',state.handler,true);document.addEventListener('change',state.handler,true);
    Object.defineProperty(window,key,{value:state,configurable:true});return{ok:true};
  }
  const s=window[key];if(!s)return{ok:false};
  if(action==='arm') {if(s.epoch!==null||s.unexpected)return{ok:false};s.epoch=epoch;s.expected=source_id;s.input=0;s.change=0;}
  const result={ok:true,unexpected:s.unexpected,input:s.input,change:s.change,epoch:s.epoch};
  if(action==='disarm'){s.epoch=null;s.expected=null;}
  if(action==='remove'){document.removeEventListener('input',s.handler,true);document.removeEventListener('change',s.handler,true);delete window[key];if(s.notify)delete window[s.notify];}
  return result;
}'''
PASSIVE_PRICE_FENCE_JS = _passive_script(_PASSIVE_PRICE_FENCE_TEMPLATE, SPECIAL_FAMILY)



# Same two native DOM events as select_option, with one atomic final guard.
# A separate guard/read followed by awaited select_option would race panel changes.
STAGGERED_SELECT_ACTION_JS = r"""(e, {value,source_id,retained_values,fence_key,epoch}) => {
 const visible=x=>x.getClientRects().length>0 && getComputedStyle(x).display!=='none' &&
   getComputedStyle(x).visibility!=='hidden' && !x.closest('[hidden],[aria-hidden="true"]');
 const shippingOpen=()=>[...document.querySelectorAll('#esCollapse')].some(x=>x.classList.contains('show'));
 const roots=[...document.querySelectorAll('#order-form')],root=roots.length===1?roots[0]:null;
 const fence=typeof fence_key==='string'&&/^__hermesPassivePriceFence_[a-f0-9]{32}$/.test(fence_key)?window[fence_key]:null;
 if(!fence || fence.root!==root || fence.epoch!==epoch || fence.expected!==source_id || fence.unexpected!==false ||
    fence.input!==0 || fence.change!==0 || fence.nativeRadioTarget!==null || fence.nativeRadioNext!==null)throw Error('native selection epoch changed');
 if(location.href!==__PASSIVE_URL__ || !root || root.getAttribute('data-printproducttype')!=='38' ||
    root.getAttribute('data-currentstoretype')!=='broker' || !root.contains(e) ||
    e.tagName!=='SELECT' || e.id!==source_id || root.querySelectorAll('select[id="'+source_id+'"]').length!==1 ||
    e.multiple || e.matches(':disabled') || !visible(e) || shippingOpen() ||
    !Array.isArray(retained_values) || retained_values.length!==1 ||
    JSON.stringify([...e.selectedOptions].map(x=>x.value))!==JSON.stringify(retained_values) ||
    retained_values[0]===value)throw Error('native select state changed');
 const targets=[...e.options].filter(x=>x.value===value);
 if(targets.length!==1 || targets[0].disabled || targets[0].closest('optgroup')?.disabled)
   throw Error('native select target unavailable');
 e.selectedIndex=[...e.options].indexOf(targets[0]);
 e.dispatchEvent(new Event('input',{bubbles:true}));
 if(shippingOpen())throw Error('native select shipping state changed');
 e.dispatchEvent(new Event('change',{bubbles:true}));
 return true;
}"""
STAGGERED_SELECT_ACTION_JS = _passive_script(STAGGERED_SELECT_ACTION_JS, STAGGERED_FAMILY)


# Source-pinned radio names map to exactly one model field. No callbacks read.
STAGGERED_RADIO_BINDING_JS = r"""({source_contract,name,value,selected_values}) => {
 const no=()=>({observed:false,reason:'passive_radio_binding_unproven'});
 const expected=__PASSIVE_CONTRACT__, policies={proofType:['proofType',['100']],
  proofTypeForManual:['manualProofType',['1','36','37','38']],fileDeliveryForManual:['fileDelivery',['5']]};
 if(location.href!==__PASSIVE_URL__ || !source_contract ||
    JSON.stringify(Object.keys(source_contract).sort())!==JSON.stringify(Object.keys(expected).sort()) ||
    !Object.keys(expected).every(k=>expected[k]===source_contract[k]) || !Object.hasOwn(policies,name) ||
    !policies[name][1].includes(value) || !Array.isArray(selected_values) || selected_values.length>1 ||
    !selected_values.every(x=>typeof x==='string'))return no();
 const root=document.querySelector('#order-form');
 if(!root || root.getAttribute('data-printproducttype')!=='38' || root.getAttribute('data-currentstoretype')!=='broker' ||
    [...document.querySelectorAll('#esCollapse')].some(e=>e.classList.contains('show')))return no();
 const controls=[...root.querySelectorAll('input[type="radio"][name="'+name+'"]')],
  retained=controls.filter(e=>e.checked).map(e=>e.value), targets=controls.filter(e=>e.value===value);
 if(JSON.stringify(retained)!==JSON.stringify(selected_values) || targets.length!==1 || targets[0].checked)return no();
 const el=targets[0];
 if(el.matches(':disabled') || !el.getClientRects().length || getComputedStyle(el).display==='none' ||
    getComputedStyle(el).visibility==='hidden' || el.closest('[hidden],[aria-hidden="true"]') ||
    !(/^[A-Za-z][A-Za-z0-9_-]{0,127}$/).test(el.id) || root.querySelectorAll('[id="'+el.id+'"]').length!==1)return no();
 return {observed:true,field:policies[name][0],source_id:el.id,value,binding_kind:'pinned_type38_manual_radio'};
}"""
STAGGERED_RADIO_BINDING_JS = _passive_script(STAGGERED_RADIO_BINDING_JS, STAGGERED_FAMILY)


# Final native action repeats the source-pinned identity after every awaited
# binding/fence/locator check. Locator identity alone does not bind name/value.
STAGGERED_RADIO_ACTION_JS = r"""(e, {source_id,name,value,retained_values,fence_key,epoch}) => {
 const policies={proofType:['100'],proofTypeForManual:['1','36','37','38'],fileDeliveryForManual:['5']};
 const roots=[...document.querySelectorAll('#order-form')];
 const fence=typeof fence_key==='string'&&/^__hermesPassivePriceFence_[a-f0-9]{32}$/.test(fence_key)?window[fence_key]:null;
 if(location.href!==__PASSIVE_URL__ || roots.length!==1 || typeof name!=='string' ||
    !Object.hasOwn(policies,name) || !policies[name].includes(value) ||
    typeof source_id!=='string' || !(/^[A-Za-z][A-Za-z0-9_-]{0,127}$/).test(source_id) ||
    !Array.isArray(retained_values) || retained_values.length>1 || !retained_values.every(x=>typeof x==='string') ||
    !fence || fence.root!==roots[0] || fence.epoch!==epoch || fence.expected!==source_id || fence.unexpected!==false ||
    fence.input!==0 || fence.change!==0 || fence.nativeRadioTarget!==null || fence.nativeRadioNext!==null)throw Error('native radio identity changed');
 const root=roots[0];
 if(root.getAttribute('data-printproducttype')!=='38' || root.getAttribute('data-currentstoretype')!=='broker' ||
    e.tagName!=='INPUT' || e.type!=='radio' || e.id!==source_id || e.name!==name || e.value!==value ||
    !root.contains(e) || e.closest('#order-form')!==root ||
    root.querySelectorAll('input[id="'+source_id+'"]').length!==1 ||
    e.checked || e.matches(':disabled') || !e.getClientRects().length ||
    getComputedStyle(e).display==='none' || getComputedStyle(e).visibility==='hidden' ||
    e.closest('[hidden],[aria-hidden="true"]') ||
    [...document.querySelectorAll('#esCollapse')].some(x=>x.classList.contains('show')))
   throw Error('native radio state changed');
 const group=[...root.querySelectorAll('input[type="radio"][name="'+name+'"]')],
  targets=group.filter(x=>x.value===value),retained=group.filter(x=>x.checked).map(x=>x.value);
 if(targets.length!==1 || targets[0]!==e || JSON.stringify(retained)!==JSON.stringify(retained_values))
   throw Error('native radio group changed');
 // Browser-native click() produces trusted input/change default actions. Admit
 // those only during this synchronous exact-node input -> change window.
 fence.nativeRadioTarget=e;fence.nativeRadioNext='input';
 try {
   e.click();
   if(fence.unexpected || fence.input!==1 || fence.change!==1 ||
      fence.nativeRadioNext!=='done' || !e.checked)throw Error('native radio transition incomplete');
 } finally {fence.nativeRadioTarget=null;fence.nativeRadioNext=null;}
}"""
STAGGERED_RADIO_ACTION_JS = _passive_script(STAGGERED_RADIO_ACTION_JS, STAGGERED_FAMILY)


class _ManagedPassivePriceObserver(OwnedPageCDPObserver):
    """Same owned target; unexpected later native price traffic invalidates reuse."""
    async def _body(self,identifier):
        initial=(isinstance(self.journal,InitialQuantityPriceJournal)
                 and self.journal.is_initial_request(identifier))
        if not initial:
            return await super()._body(identifier)
        return await read_initial_body(self,identifier,getattr(self,'_initial_context_provider',None))

    def _request(self,event):
        epoch=self.journal._epochs.get(self.journal._current)
        request=event.get('request',{})
        if (epoch is not None and epoch.finished and not self.journal._invalid
                and event.get('frameId')==self.journal._frame and event.get('loaderId')==self.journal._loader
                and request.get('method')=='GET' and event.get('type') in {'XHR','Fetch'}
                and self.journal._candidate(request.get('url'))):
            self.journal.invalidate('unattributed_control_change')
        super()._request(event)


def _public_url(url):
    """Queries/fragments may contain session artifacts; never persist them."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


STAGGERED_GALLERY_PREFIX = 'https://static.printograph.com/tl/products/generic/images/staggered-cut-flyers/'
STAGGERED_GALLERY_FILENAMES = frozenset(['staggered_cut_flyers.jpg', 'staggered_flyers_100lb_gloss_book.jpg', 'staggered_flyers_100lb_gloss_cover.jpg', 'staggered_flyers_100lb_recycled_matte.jpg', 'staggered_flyers_6x9x.jpg', 'staggered_flyers_70lb.jpg', 'staggered_flyers_8.5x11x.jpg', 'staggered_flyers_80lb_matte_book.jpg', 'staggered_flyers_full_color_both_sides.jpg', 'staggered_flyers_full_color_front_blank_back.jpg', 'staggered_flyers_half_inch_6x9_3pages.jpg', 'staggered_flyers_half_inch_6x9_4pages.jpg', 'staggered_flyers_half_inch_6x9_5pages.jpg', 'staggered_flyers_half_inch_6x9_6pages.jpg', 'staggered_flyers_half_inch_6x9_7pages.jpg', 'staggered_flyers_half_inch_6x9_8pages.jpg', 'staggered_flyers_half_inch_8_5x11_3pages.jpg', 'staggered_flyers_half_inch_8_5x11_4pages.jpg', 'staggered_flyers_half_inch_8_5x11_5pages.jpg', 'staggered_flyers_half_inch_8_5x11_6pages.jpg', 'staggered_flyers_half_inch_8_5x11_7pages.jpg', 'staggered_flyers_half_inch_8_5x11_8pages.jpg', 'staggered_flyers_main_view_1.jpg', 'staggered_flyers_main_view_2.jpg', 'staggered_flyers_main_view_3.png', 'staggered_flyers_main_view_4.png', 'staggered_flyers_quarter_inch_6x9_3pages.jpg', 'staggered_flyers_quarter_inch_6x9_4pages.jpg', 'staggered_flyers_quarter_inch_6x9_5pages.jpg', 'staggered_flyers_quarter_inch_6x9_6pages.jpg', 'staggered_flyers_quarter_inch_6x9_7pages.jpg', 'staggered_flyers_quarter_inch_6x9_8pages.jpg', 'staggered_flyers_quarter_inch_8_5x11_3pages.jpg', 'staggered_flyers_quarter_inch_8_5x11_4pages.jpg', 'staggered_flyers_quarter_inch_8_5x11_5pages.jpg', 'staggered_flyers_quarter_inch_8_5x11_6pages.jpg', 'staggered_flyers_quarter_inch_8_5x11_7pages.jpg', 'staggered_flyers_quarter_inch_8_5x11_8pages.jpg'])
STAGGERED_GALLERY_PAPER_PREFIX = 'https://static.printograph.com/tl/products/generic/images/common/'
STAGGERED_GALLERY_PAPER_FILENAMES = frozenset(['staggered_flyers_100lb_gloss_book.jpg', 'staggered_flyers_100lb_gloss_cover.jpg', 'staggered_flyers_100lb_recycled_matte.jpg', 'staggered_flyers_70lb.jpg', 'staggered_flyers_80lb_matte_book.jpg'])
STAGGERED_GALLERY_JS = '() => {\n const source="https://www.printograph.com/products/staggered-cut-flyers/order",prefix="https://static.printograph.com/tl/products/generic/images/staggered-cut-flyers/",names=new Set(["staggered_cut_flyers.jpg", "staggered_flyers_100lb_gloss_book.jpg", "staggered_flyers_100lb_gloss_cover.jpg", "staggered_flyers_100lb_recycled_matte.jpg", "staggered_flyers_6x9x.jpg", "staggered_flyers_70lb.jpg", "staggered_flyers_8.5x11x.jpg", "staggered_flyers_80lb_matte_book.jpg", "staggered_flyers_full_color_both_sides.jpg", "staggered_flyers_full_color_front_blank_back.jpg", "staggered_flyers_half_inch_6x9_3pages.jpg", "staggered_flyers_half_inch_6x9_4pages.jpg", "staggered_flyers_half_inch_6x9_5pages.jpg", "staggered_flyers_half_inch_6x9_6pages.jpg", "staggered_flyers_half_inch_6x9_7pages.jpg", "staggered_flyers_half_inch_6x9_8pages.jpg", "staggered_flyers_half_inch_8_5x11_3pages.jpg", "staggered_flyers_half_inch_8_5x11_4pages.jpg", "staggered_flyers_half_inch_8_5x11_5pages.jpg", "staggered_flyers_half_inch_8_5x11_6pages.jpg", "staggered_flyers_half_inch_8_5x11_7pages.jpg", "staggered_flyers_half_inch_8_5x11_8pages.jpg", "staggered_flyers_main_view_1.jpg", "staggered_flyers_main_view_2.jpg", "staggered_flyers_main_view_3.png", "staggered_flyers_main_view_4.png", "staggered_flyers_quarter_inch_6x9_3pages.jpg", "staggered_flyers_quarter_inch_6x9_4pages.jpg", "staggered_flyers_quarter_inch_6x9_5pages.jpg", "staggered_flyers_quarter_inch_6x9_6pages.jpg", "staggered_flyers_quarter_inch_6x9_7pages.jpg", "staggered_flyers_quarter_inch_6x9_8pages.jpg", "staggered_flyers_quarter_inch_8_5x11_3pages.jpg", "staggered_flyers_quarter_inch_8_5x11_4pages.jpg", "staggered_flyers_quarter_inch_8_5x11_5pages.jpg", "staggered_flyers_quarter_inch_8_5x11_6pages.jpg", "staggered_flyers_quarter_inch_8_5x11_7pages.jpg", "staggered_flyers_quarter_inch_8_5x11_8pages.jpg"]),paperPrefix="https://static.printograph.com/tl/products/generic/images/common/",paperNames=new Set(["staggered_flyers_100lb_gloss_book.jpg", "staggered_flyers_100lb_gloss_cover.jpg", "staggered_flyers_100lb_recycled_matte.jpg", "staggered_flyers_70lb.jpg", "staggered_flyers_80lb_matte_book.jpg"]);\n const no=reason=>({observed:false,reason});\n if(location.href!==source)return no(\'gallery_source_changed\');\n const forms=[...document.querySelectorAll(\'#order-form\')];\n if(forms.length!==1||forms[0].getAttribute(\'data-printproducttype\')!==\'38\'||forms[0].getAttribute(\'data-currentstoretype\')!==\'broker\')return no(\'gallery_form_scope_changed\');\n if(forms[0].getAttribute(\'data-imagepath\')!==prefix.replace(\'https:\',\'\')||forms[0].getAttribute(\'data-paperpath\')!==paperPrefix.replace(\'https:\',\'\'))return no(\'gallery_asset_roots_changed\');\n const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!==\'hidden\'&&getComputedStyle(e).display!==\'none\';\n const galleries=[...forms[0].querySelectorAll(\'.gallery-b\')].filter(visible);\n if(galleries.length!==1)return no(\'gallery_root_not_unique\');\n const elements=[...galleries[0].querySelectorAll(\'#main-gallery-b-img img,.center-b img\')];\n if(elements.length>100)return no(\'gallery_element_limit\');\n const seen=new Set(),images=[];let unmatched=0;\n for(const e of elements){\n   let matched=false;const candidates=[e.currentSrc,e.src,e.getAttribute(\'data-src\'),e.getAttribute(\'data-lazy\')];\n   for(const value of candidates){\n     if(typeof value!==\'string\'||!((value.startsWith(prefix)&&names.has(value.slice(prefix.length))&&!paperNames.has(value.slice(prefix.length)))||(value.startsWith(paperPrefix)&&paperNames.has(value.slice(paperPrefix.length)))))continue;\n     if(!seen.has(value)){seen.add(value);images.push({url:value,alt:\'Staggered Cut Flyers\'});}matched=true;break;\n   }\n   if(!matched)unmatched++;\n }\n if(images.length>30)return no(\'gallery_image_limit\');\n return {observed:true,source_url:source,images,image_element_count:elements.length,\n   gallery_count:1,unmatched_image_elements:unmatched,source_fetches_initiated:0,scope:\'current_rendered_product_gallery_only\'};\n}'


def project_staggered_gallery(observed):
    """Project only actual current-gallery URLs; never manufacture missing URLs."""
    if (type(observed) is not dict or observed.get('observed') is not True
            or observed.get('source_url')!=STAGGERED_FAMILY.source_url
            or observed.get('scope')!='current_rendered_product_gallery_only'
            or type(observed.get('source_fetches_initiated')) is not int or observed['source_fetches_initiated']!=0
            or type(observed.get('gallery_count')) is not int or observed['gallery_count']!=1
            or type(observed.get('image_element_count')) is not int or not 0<=observed['image_element_count']<=100
            or type(observed.get('unmatched_image_elements')) is not int or not 0<=observed['unmatched_image_elements']<=observed['image_element_count']
            or type(observed.get('images')) is not list or len(observed['images'])>30):
        raise ValueError('staggered_gallery_scope_unproven')
    images=[];seen=set()
    for item in observed['images']:
        if type(item) is not dict or set(item)!={'url','alt'} or type(item.get('url')) is not str:
            raise ValueError('staggered_gallery_image_unproven')
        url=item['url']
        try:
            parsed=urlsplit(url)
            if (parsed.scheme!='https' or parsed.netloc!='static.printograph.com' or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or len(url)>512 or url!=_public_url(url)
                    or not ((url.startswith(STAGGERED_GALLERY_PREFIX)
                        and url[len(STAGGERED_GALLERY_PREFIX):] in STAGGERED_GALLERY_FILENAMES-STAGGERED_GALLERY_PAPER_FILENAMES)
                        or (url.startswith(STAGGERED_GALLERY_PAPER_PREFIX)
                        and url[len(STAGGERED_GALLERY_PAPER_PREFIX):] in STAGGERED_GALLERY_PAPER_FILENAMES))):
                raise ValueError('staggered_gallery_image_unproven')
        except ValueError:raise ValueError('staggered_gallery_image_unproven') from None
        if url not in seen:
            seen.add(url);images.append({'url':url,'alt':'Staggered Cut Flyers'})
    return images


def _supplier_for(url):
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return next((key for key, item in SUPPLIERS.items() if urlsplit(item["origin"]).hostname.removeprefix("www.") == host), None)


def _printograph_bundle_key(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "static.printograph.com":
        return None
    if parts.path.endswith("/orderForm.js"):
        return "order_form_sha256"
    if parts.path.endswith("/chunk-common.js"):
        return "common_sha256"
    return None


def _loaded_bundle_contract(observed):
    """Only one actually loaded hash per required public script can match."""
    return {key: next(iter(values)) for key, values in observed.items()
            if key in SOURCE_CONTRACT and len(values) == 1}


def _safe_controller_diagnostics(value):
    """Allow only bounded schema identifiers from an unsupported controller."""
    if not isinstance(value, dict):
        return None
    def identifier(item, pattern):
        return item if isinstance(item, str) and re.fullmatch(pattern, item) else None
    names = value.get("option_names")
    safe_names = []
    for item in names[:64] if isinstance(names, list) else []:
        name = identifier(item, r"[A-Za-z][A-Za-z0-9]{0,63}")
        if name is not None and name not in safe_names:
            safe_names.append(name)
    return {"declared_type": identifier(value.get("declared_type"), r"[0-9]{1,16}"),
            "controller_type": identifier(value.get("controller_type"), r"[0-9]{1,16}"),
            "store_type": identifier(value.get("store_type"), r"[A-Za-z0-9]{1,32}"),
            "option_names": safe_names}


def _is_dps(url):
    return (urlsplit(url).hostname or "").removeprefix("www.") == "digital-print-solutions.com"


def merge_dps_snapshot(raw, calculator):
    """Project the visible DPS calculator into the same reviewable field model.

    This is a current-page diagnosis only. Redirecting choices and conditional
    option trees still require the native calculator before any harvest.
    """
    raw = dict(raw)
    fields = list(raw.get("fields") or [])
    seen = {field.get("source_id") for field in fields}
    for key, label in (("width", "Width"), ("height", "Height"), ("quantity", "Quantity")):
        control = calculator.get(key)
        if not isinstance(control, dict) or control.get("kind") not in {"select", "number"}:
            continue
        source_id = control.get("id")
        if source_id in seen:
            continue
        field = {"label": label, "kind": control["kind"], "source_key": source_id,
                 "source_name": "", "source_id": source_id, "choices": [],
                 "selected_values": [str(control["value"])] if control.get("value") not in (None, "") else [],
                 "required": True, "disabled": bool(control.get("disabled")), "multiple": False,
                 "configurable": True}
        if control["kind"] == "select":
            field["choices"] = [{"value": str(choice.get("value", "")),
                                  "label": choice.get("label", ""), "disabled": bool(choice.get("disabled"))}
                                 for choice in control.get("choices", [])]
        else:
            field.update(min=control.get("minimum"), max=control.get("maximum"), step=control.get("step"))
        fields.append(field)
        seen.add(source_id)
    for option in calculator.get("options") or []:
        source_id = "dps-option-" + str(option.get("id", ""))
        if source_id in seen or not option.get("label"):
            continue
        choices = [{"value": str(choice.get("value", "")), "label": choice.get("label", ""),
                    "disabled": bool(choice.get("disabled") or choice.get("redirects"))}
                   for choice in option.get("choices", [])]
        fields.append({"label": option["label"], "kind": "select", "source_key": source_id,
                       "source_name": "", "source_id": source_id, "choices": choices,
                       "selected_values": [str(option["value"])] if option.get("value") not in (None, "") else [],
                       "required": True, "disabled": bool(option.get("disabled")), "multiple": False,
                       "configurable": True})
        seen.add(source_id)
    raw["fields"] = fields
    if calculator.get("login_prompt"):
        raw["price_login_required"] = True
    return raw


def _safe_error(exc):
    code = getattr(exc, "code", "")
    if code in {"invalid_url", "network_error", "browser_profile_in_use", "browser_unavailable"}:
        return code
    if isinstance(exc, asyncio.TimeoutError) or type(exc).__name__ == "TimeoutError":
        return "page_timeout"
    return "page_unavailable"


def normalize_snapshot(raw):
    """Bound the public projection; generated source IDs never become labels."""
    def clean(value, limit=240):
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    fields, keys, unsupported = [], set(), False
    for field in raw.get("fields", [])[:200]:
        if field.get("kind") not in {"select", "custom_select", "static", "radio", "checkbox", "number", "required_text"}:
            continue
        label = clean(field.get("label"))
        if not label:
            continue
        raw_values = [field.get("source_name", ""), field.get("source_id", ""), field.get("source_data_id", ""), field.get("source_key", "")] + [c.get("value", "") for c in field.get("choices", [])[:500]] + list(field.get("selected_values", [])[:500])
        if any(not isinstance(v, str) or len(v) > 2048 for v in raw_values):
            unsupported = True
            continue
        source = field.get("source_key", "")
        key = hashlib.sha256(f"{field['kind']}\0{source}\0{label}".encode()).hexdigest()[:20]
        if key in keys:
            key = hashlib.sha256(f"{key}\0{len(fields)}".encode()).hexdigest()[:20]
        keys.add(key)
        choices = [{"value": c.get("value", ""), "label": clean(c.get("label")), "disabled": bool(c.get("disabled"))} for c in field.get("choices", [])[:500]]
        selected = list(field.get("selected_values", [])[:500])
        out = {"key": key, "label": label, "kind": field["kind"], "choices": choices,
               "selected_values": selected, "required": bool(field.get("required")),
               "disabled": bool(field.get("disabled")), "multiple": field.get("multiple") is True or field["kind"] == "checkbox", "configurable": True,
               "source_name": field.get("source_name", ""), "source_id": field.get("source_id", "")}
        if not out["source_name"] and not out["source_id"] and field.get("source_data_id"):
            out["source_data_id"] = field["source_data_id"]
        for metadata, allowed in (("binding_kind", {"display_label_only", "native_control"}),
                                  ("value_origin", {"display_label_surrogate", "native_value"})):
            if field.get(metadata) in allowed:
                out[metadata] = field[metadata]
        if field.get("warnings") == ["field_label_from_choices"]:
            out["warnings"] = ["field_label_from_choices"]
        if out["kind"] == "required_text":
            out.update(choices=[], selected_values=[], required=True)
        elif out["kind"] == "number":
            def numeric(value):
                return isinstance(value, str) and len(value) < 80 and bool(re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", value)) and abs(float(value)) != float("inf")
            out["selected_values"] = [v for v in selected if numeric(v)]
            for attr in ("min", "max", "step"):
                value = field.get(attr)
                out[attr] = value if numeric(value) or attr == "step" and value == "any" else None
        else:
            values = {choice["value"] for choice in choices}
            out["selected_values"] = [v for v in selected if v in values]
        fields.append(out)
    content = raw.get("content") or {}
    title = clean(raw.get("title"))
    images = []
    for item in content.get("images", [])[:50]:
        try:
            url = urlsplit(str(item.get("url", "")))
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                continue
            images.append({"url": _public_url(url.geturl()), "alt": clean(item.get("alt"))})
        except ValueError:
            continue
    source_host = content.get('source_host')
    templates = []
    for item in content.get('artwork_templates', [])[:50]:
        try:
            url = urlsplit(str(item.get('url', '')))
            if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
                    or len(url.geturl()) > 500 or url.hostname not in {source_host, 'display-templates.s3.us-west-2.amazonaws.com', 's3-us-west-2.amazonaws.com'}
                    or url.hostname == 's3-us-west-2.amazonaws.com' and not url.path.startswith(('/display-templates/PDF/', '/display-templates/PSB/'))):
                continue
            templates.append({'url': _public_url(url.geturl()), 'label': clean(item.get('label'), 80),
                              'choice_hint': clean(item.get('choice_hint'), 240)})
        except (ValueError, AttributeError):
            continue
    artwork_setup = clean(content.get('artwork_setup'), 3000)
    explicit_to_size = bool(re.search(r'\b(?:artwork\s+)?built\s+to\s+(?:the\s+)?(?:ordered|final)\s+size\b|\bprint\s+to\s+(?:the\s+)?(?:ordered|final)\s+size\b', artwork_setup, re.I))
    explicit_no_bleed = bool(re.search(r'\b(?:do\s+not\s+include|no)\s+(?:crop\s+marks\s+or\s+)?bleeds?\b', artwork_setup, re.I))
    template_status = ('source_links_observed' if templates else
                       'to_size_no_bleed_exception' if explicit_to_size and explicit_no_bleed else 'unresolved')
    content = {"title": title, "description": clean(content.get("description"), 8000), "images": images,
               "artwork_templates": templates,
               "artwork_setup": artwork_setup,
               "artwork_template_status": template_status}
    for key, label in (("title", "Product title"), ("description", "Description"), ("images", "Images")):
        if content[key]:
            fields.append({"key": f"content_{key}", "label": label, "kind": "content", "choices": [], "selected_values": [], "required": False, "disabled": False, "multiple": False, "configurable": False})
    status = raw.get("session_status")
    warnings = ["diagnosis_limits_reached"] if raw.get("omitted") or unsupported else []
    warnings += ["required_free_text_control:" + field["label"] for field in fields if field["kind"] == "required_text"]
    if raw.get('price_login_required') is True:
        warnings.append('supplier_price_login_required')
    warnings += [w for w in raw.get("source_warnings", []) if w in {
        "field_label_from_choices", "custom_dropdown_choices_unavailable", "custom_dropdown_labels_ambiguous",
        "custom_dropdown_inspection_failed", "custom_dropdown_close_failed", "custom_dropdown_selection_changed",
        "custom_dropdown_inspection_incomplete", "unsupported_visible_attribute"}]
    return {"title": title, "fields": fields, "content": content,
            "pricing_access": "login_required" if raw.get('price_login_required') is True else "not_established",
            "session_status": status if status in {"signed_in", "login_required", "unknown"} else "unknown",
            "warnings": warnings}


class ProductDiagnosisManager(SupplierProfileExecutorMixin):
    def __init__(self, store, browser_pool):
        self.store, self.pool = store, browser_pool
        self.registry = SupplierRegistry(store.root, SUPPLIERS)
        self.pool.origin_resolver = self.registry.canonical_origin
        self.root = Path(store.root) / "diagnoses"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._records, self._pages, self._leases, self._tasks = {}, {}, {}, {}
        self._choice_limiter = RateLimiter(store)
        self._supplier_sessions = {}
        self._page_hosts = {}
        self._bundle_hashes, self._bundle_tasks = {}, {}
        self._passive_journals, self._passive_observers, self._passive_fences = {}, {}, {}
        self._passive_scope_checks = {}
        self._supplier_profile_owners, self._supplier_profile_locks = {}, {}
        self._locks = {}
        self._lock = asyncio.Lock()
        self._diagnosis_slots = asyncio.Semaphore(3)
        self._closed = False

    def _path(self, diagnosis_id):
        if not _ID.fullmatch(str(diagnosis_id)):
            raise ValueError("invalid_diagnosis_id")
        return self.root / f"{diagnosis_id}.json"

    def _persist(self, record):
        target = self._path(record["id"])
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as stream:
                os.chmod(temp, 0o600)
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def get(self, diagnosis_id):
        path = self._path(diagnosis_id)
        if diagnosis_id not in self._records:
            if not path.exists():
                raise KeyError("diagnosis_not_found")
            record = json.loads(path.read_text())
            record["status"] = "closed"
            record["session_status"] = "closed"
            record["warnings"] = list(dict.fromkeys(record.get("warnings", []) + ["page_not_open"]))
            self._records[diagnosis_id] = record
        return copy.deepcopy(self._records[diagnosis_id])

    def suppliers(self):
        return [{"origin": item["origin"], "entry_url": item["entry_url"],
                 "website_url": item["website_url"], "custom": item["custom"],
                 **copy.deepcopy(self._supplier_sessions.get(key) or {"id": key, "supplier_id": key, "name": item["name"], "url": item["entry_url"], "status": "not_open", "session_status": "not_open"})}
                for key, item in self.registry.items().items()]

    async def add_supplier(self, name, website_url, login_url=None):
        if self._closed:
            raise ValueError("diagnosis_manager_closed")
        item = await self.registry.add(name, website_url, login_url)
        return next(card for card in self.suppliers() if card["id"] == item["id"])

    async def remove_supplier(self, key):
        if self._closed:
            raise ValueError("diagnosis_manager_closed")
        item = self.registry.get(key)
        if not item["custom"]:
            raise ValueError("builtin_supplier")
        await self.registry.remove(key)
        owner = f"supplier:{key}"
        async with self._locks.setdefault(owner, asyncio.Lock()):
            await self._release(owner)
        self._supplier_sessions.pop(key, None)

    def list(self):
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        records = []
        for path in paths:
            try:
                records.append(self.get(path.stem))
            except (ValueError, KeyError, OSError):
                continue
        return records

    async def _validated_target(self, url, *, login=False):
        try:
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.fragment or not parts.hostname or parts.username or parts.password:
                raise ValueError("invalid_product_url")
            from urllib.parse import parse_qsl
            if not login and (_blocked_product_path(parts.path) or any(_BLOCKED_QUERY.search(k) for k, _ in parse_qsl(parts.query))):
                raise ValueError("product_page_required")
            await validate_url(url)
        except ValueError:
            raise ValueError("invalid_product_url") from None
        supplier = self.registry.supplier_for(url)
        origin = self.registry.get(supplier)["origin"] if supplier else f"https://{parts.hostname}"
        host = urlsplit(origin).hostname
        hosts = {host, host.removeprefix("www.")} if host.startswith("www.") else {host, f"www.{host}"}
        # Only vet the counterpart when it is actually requested; unrelated
        # source/CDN hosts still undergo the same public-address validation.
        return origin, hosts, supplier

    async def _new_page(self, owner, origin, hosts):
        lease = self.pool.page(origin, headless=False)
        page = await lease.__aenter__()
        if self._closed:
            await lease.__aexit__(None, None, None)
            raise ValueError("diagnosis_manager_closed")
        self._pages[owner], self._leases[owner] = page, lease
        self._page_hosts[owner] = hosts
        vetted = set()

        def closed():
            if self._pages.get(owner) is not page:
                return
            if owner in self._records:
                record = self._records[owner]
                record.update(status="closed", session_status="closed")
                self._persist(record)
            elif owner.startswith("supplier:"):
                supplier = self._supplier_sessions.get(owner.split(":", 1)[1])
                if supplier:
                    supplier.update(status="closed", session_status="closed")
        page.on("close", closed)
        if origin == SUPPLIERS["printograph"]["origin"] and owner in self._records:
            bundle_hashes = self._bundle_hashes[owner] = {}
            bundle_tasks = self._bundle_tasks[owner] = set()

            async def hash_loaded_bundle(response):
                key = None
                try:
                    key = _printograph_bundle_key(response.url)
                    if key and response.status == 200:
                        digest = hashlib.sha256(await response.body()).hexdigest()
                        bundle_hashes.setdefault(key, set()).add(digest)
                    elif key and self._records.get(owner, {}).get('observe_printograph_prices') is True:
                        bundle_hashes.setdefault(key, set()).add('unproven_loaded_bundle')
                except Exception:
                    if key and self._records.get(owner, {}).get('observe_printograph_prices') is True:
                        bundle_hashes.setdefault(key, set()).add('unproven_loaded_bundle')

            def queue_bundle_hash(response):
                if _printograph_bundle_key(response.url) is None:
                    return
                task = asyncio.create_task(hash_loaded_bundle(response))
                bundle_tasks.add(task)
                task.add_done_callback(bundle_tasks.discard)

            page.on("response", queue_bundle_hash)

        async def guard(route):
            request = route.request
            try:
                p = urlsplit(request.url)
                if p.scheme not in {"http", "https"}:
                    await route.abort()
                    return
                if request.is_navigation_request() and request.frame == page.main_frame:
                    if p.scheme != "https" or p.hostname not in hosts:
                        await route.abort()
                        return
                authority = (p.scheme, p.netloc)
                if authority not in vetted:
                    await validate_url(request.url)
                    vetted.add(authority)
                await route.continue_()
            except Exception:
                await route.abort()

        await page.route("**/*", guard)
        return page

    async def open_supplier(self, key, *, target_url=None):
        item = self.registry.get(key)
        if target_url is not None:
            validate_dps_target(key, item, target_url)
        entry_url = target_url if target_url is not None else item['entry_url']
        async with self._locks.setdefault(f"supplier:{key}", asyncio.Lock()):
            if self._closed:
                raise ValueError("diagnosis_manager_closed")
            owner = f"supplier:{key}"
            page = self._pages.get(owner)
            try:
                if page is None or page.is_closed():
                    await self._release(owner)
                    origin, hosts, _ = await self._validated_target(entry_url, login=True)
                    page = await self._new_page(owner, origin, hosts)
                    await page.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
                elif target_url is not None and page.url != target_url:
                    await self._validated_target(target_url, login=True)
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                await page.bring_to_front()
                return await self.check_supplier(key)
            except Exception as exc:
                self._supplier_sessions[key] = {"id": key, "supplier_id": key, "name": item["name"], "url": item["entry_url"], "origin": item["origin"], "entry_url": item["entry_url"], "website_url": item["website_url"], "custom": item["custom"], "status": "attention", "session_status": "unknown", "warnings": [_safe_error(exc)]}
                return copy.deepcopy(self._supplier_sessions[key])

    async def check_supplier(self, key):
        item = self.registry.get(key)
        page = self._pages.get(f"supplier:{key}")
        result = {"id": key, "supplier_id": key, "name": item["name"], "url": item["entry_url"], "origin": item["origin"], "entry_url": item["entry_url"], "website_url": item["website_url"], "custom": item["custom"]}
        if page is None or page.is_closed():
            result.update(status="closed" if key in self._supplier_sessions else "not_open", session_status="closed" if key in self._supplier_sessions else "not_open")
        elif self.registry.supplier_for(page.url) != key:
            result.update(status="attention", session_status="unknown", warnings=["unexpected_source_origin"])
        else:
            try:
                raw = await page.evaluate(DIAGNOSIS_JS)
                status = normalize_snapshot(raw)["session_status"]
                result.update(status=status, session_status=status, url=_public_url(page.url))
            except Exception as exc:
                result.update(status="attention", session_status="unknown", warnings=[_safe_error(exc)])
        self._supplier_sessions[key] = result
        return copy.deepcopy(result)

    async def read_supplier_workflow_references(self, key, expected_url):
        """Read metadata from an existing owned DPS account page, never open it."""
        item = self.registry.get(key)
        validate_dps_target(key, item, expected_url)
        owner = f'supplier:{key}'
        async with self._locks.setdefault(owner, asyncio.Lock()):
            if self._closed:
                raise ValueError('diagnosis_manager_closed')
            page = self._pages.get(owner)
            if page is None or page.is_closed():
                raise ValueError('supplier_workflow_owned_page_required')
            if self._supplier_sessions.get(key, {}).get('session_status') != 'signed_in':
                raise ValueError('supplier_workflow_signed_in_check_required')
            if page.url != expected_url or self.registry.supplier_for(page.url) != key:
                raise ValueError('supplier_workflow_url_changed')
            try:
                raw = await page.evaluate(WORKFLOW_REFERENCES_JS, {'expected_url': expected_url})
            except Exception:
                raise ValueError('supplier_workflow_projection_failed') from None
            if page.is_closed() or page.url != expected_url or self._pages.get(owner) is not page:
                raise ValueError('supplier_workflow_url_changed')
            projected = project_workflow_references(raw)
            return {'schema':'supplier-workflow-references/v1','supplier_id':key,'source_url':DPS_ACCOUNT_URL,
                    'ownership':'existing_supplier_session_page','session_check':'previously_signed_in_not_rechecked',
                    **projected,'cart_workflow_requires_isolation':True,'navigation_performed':False,
                    'source_requests_initiated':0,'source_functions_invoked':False,'page_state_mutated':False,
                    'price_or_shipping_quote_collected':False,'checkout_verified':False}


    async def start_staggered_passive(self, *, check_scope, focus=False):
        """Internal campaign-gated entry; the ordinary diagnosis API cannot opt in."""
        if not callable(check_scope):
            raise ValueError('passive_campaign_scope_required')
        await check_scope()
        return await self.start(STAGGERED_FAMILY.source_url, focus=focus,
                                observe_printograph_prices=True, _scope_check=check_scope)

    async def start(self, url, *, focus=False, observe_printograph_prices=False, _scope_check=None):
        if type(focus) is not bool:
            raise ValueError("invalid_diagnosis_focus")
        if type(observe_printograph_prices) is not bool or (observe_printograph_prices and
                url != SPECIAL_SHAPES_SOURCE_URL and not (url == STAGGERED_FAMILY.source_url and callable(_scope_check))):
            raise ValueError("passive_price_source_unsupported")
        if self._closed:
            raise ValueError("diagnosis_manager_closed")
        origin, hosts, supplier = await self._validated_target(url)
        diagnosis_id = uuid.uuid4().hex
        record = {"id": diagnosis_id, "url": _public_url(url), "title": "", "status": "opening", "revision": 0,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "fields": [], "content": {}, "warnings": [], "coverage": "current_page_only",
                  "supplier_id": supplier, "session_status": "unknown", "selection": None}
        if observe_printograph_prices:
            record["observe_printograph_prices"] = True
            record["passive_price_observation"] = {"status":"requested", "source_data_finalized":False}
        async with self._lock:
            self._records[diagnosis_id] = record
            if observe_printograph_prices and url == STAGGERED_FAMILY.source_url:
                self._passive_scope_checks[diagnosis_id] = _scope_check
                record['passive_campaign_product_id'] = '17792'
            self._persist(record)
            self._tasks[diagnosis_id] = asyncio.create_task(self._open_diagnosis(diagnosis_id, url, origin, hosts, focus))
        return copy.deepcopy(record)

    async def _open_diagnosis(self, diagnosis_id, url, origin, hosts, focus):
        async with self._diagnosis_slots:
            await self._open_diagnosis_in_slot(diagnosis_id, url, origin, hosts, focus)

    async def _open_diagnosis_in_slot(self, diagnosis_id, url, origin, hosts, focus):
        phase='creating_page'
        try:
            page = await self._new_page(diagnosis_id, origin, hosts)
            phase='navigating'
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if focus:
                await page.bring_to_front()
            async with self._lock:
                self._records[diagnosis_id]["status"] = "analyzing"
                self._persist(self._records[diagnosis_id])
            phase='settling'
            settled = await self._settle_initial(page)
            phase='refreshing'
            await self.refresh(diagnosis_id)
            if self._records[diagnosis_id].get("observe_printograph_prices"):
                async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
                    phase='attaching_observer'
                    try:
                        await self._ensure_passive_observer_locked(diagnosis_id)
                    except ValueError:
                        record=self._records[diagnosis_id]
                        record["passive_price_observation"]={"status":"held","reason":"passive_observer_not_ready","source_data_finalized":False}
                        self._persist(record)
            if not settled:
                async with self._lock:
                    record = self._records[diagnosis_id]
                    record["warnings"] = list(dict.fromkeys(record["warnings"] + ["initial_snapshot_not_settled"]))
                    self._persist(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                record = self._records[diagnosis_id]
                record.update(status="attention", warnings=[_safe_error(exc)],opening_failure_phase=phase)
                self._persist(record)

    async def _settle_initial(self, page):
        """Wait only for local rendered state; never click, reload, or fetch.

        A stable snapshot is not proof of a complete dependent option domain.
        Empty/simple product pages time out with an explicit warning and remain
        available for the user's immediate Refresh action.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0
        previous = None
        placeholder = re.compile(r"^(?:[-–—]+|select(?:\s|$)|please select|choose(?:\s|$)|loading(?:\s|$))", re.I)
        while not page.is_closed():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                raw = await asyncio.wait_for(self._snapshot(page), remaining)
            except asyncio.TimeoutError:
                return False
            snapshot = normalize_snapshot(raw)
            controls = [f for f in snapshot["fields"] if f["configurable"]]
            usable = any(
                field["kind"] == "number" or any(
                    not choice["disabled"] and not placeholder.search(choice["label"])
                    for choice in field["choices"]
                ) for field in controls
            )
            ready = raw.get("document_ready") is True and not raw.get("busy") and (
                snapshot["session_status"] == "login_required" or usable
            )
            signature = json.dumps({"fields": snapshot["fields"], "title": snapshot["title"],
                                    "session_status": snapshot["session_status"]}, sort_keys=True)
            if ready and signature == previous:
                return True
            previous = signature if ready else None
            await asyncio.sleep(min(0.3, max(0, deadline - loop.time())))
        return False

    async def _snapshot(self, page, *, expand=False):
        raw = await page.evaluate(DIAGNOSIS_JS)
        if _supplier_for(page.url) == "b2sign":
            raw = merge_b2sign_snapshot(raw, await inspect_b2sign(page, expand=expand))
        elif _is_dps(page.url):
            raw = merge_dps_snapshot(raw, await page.evaluate(DPS_DOM_JS))
        return raw

    async def refresh(self, diagnosis_id):
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            return await self._refresh_locked(diagnosis_id)

    async def _refresh_locked(self, diagnosis_id):
        page = self._pages.get(diagnosis_id)
        record = self._records[diagnosis_id]
        if page is None or page.is_closed():
            async with self._lock:
                record.update(status="closed", session_status="closed")
                record["warnings"] = list(dict.fromkeys(record.get("warnings", []) + ["page_not_open"]))
                self._persist(record)
            return self.get(diagnosis_id)
        try:
            hosts = self._page_hosts[diagnosis_id]
            current = urlsplit(page.url)
            if current.scheme != "https" or current.hostname not in hosts:
                raise ValueError("unexpected_source_origin")
            raw = normalize_snapshot(await self._snapshot(page, expand=True))
            raw["url"] = _public_url(page.url)
            raw["status"] = "login_required" if raw["session_status"] == "login_required" else "ready"
            if _blocked_product_path(current.path) and raw["session_status"] != "login_required":
                raw.update(status="attention", fields=[], content={}, warnings=["product_page_required"])
            if not any(f["configurable"] for f in raw["fields"]) and raw["status"] != "login_required":
                raw["warnings"].append("no_visible_configurable_controls")
                if _supplier_for(page.url) == "b2sign" and raw["status"] != "login_required":
                    raw["status"] = "attention"
            if any(w.startswith('custom_dropdown_') or w == 'unsupported_visible_attribute' for w in raw["warnings"]):
                raw["status"] = "attention"
            # A refreshed login page stays inspectable, but never claims a
            # verified product, price, or complete option domain.
            async with self._lock:
                changed = any(record.get(k) != raw[k] for k in ("url", "title", "fields", "content", "session_status"))
                if changed:
                    record["revision"] += 1
                    if record.get("selection") is not None:
                        record["selection"] = None
                        raw["warnings"].append("selection_cleared_after_page_change")
                record.update(raw)
                self._persist(record)
        except Exception as exc:
            async with self._lock:
                record.update(status="attention", warnings=[_safe_error(exc)])
                self._persist(record)
        return self.get(diagnosis_id)


    async def _check_staggered_scope_locked(self, diagnosis_id):
        check=self._passive_scope_checks.get(diagnosis_id)
        if not callable(check):raise ValueError('passive_campaign_scope_required')
        await check()
        page=self._pages.get(diagnosis_id)
        if page is None or page.is_closed() or page.url!=STAGGERED_FAMILY.source_url:
            raise ValueError('passive_owned_page_unavailable')
        snapshot=await page.evaluate(DIAGNOSIS_JS)
        if snapshot.get('session_status')!='signed_in':raise ValueError('passive_signed_in_required')


    async def _passive_capture_locked(self, diagnosis_id):
        page=self._pages.get(diagnosis_id)
        family=family_for_url(self._records[diagnosis_id]['url'])
        if page is None or page.is_closed() or page.url!=family.source_url:
            raise ValueError('passive_owned_page_unavailable')
        if family is STAGGERED_FAMILY:
            await self._check_staggered_scope_locked(diagnosis_id)
        tasks=tuple(self._bundle_tasks.get(diagnosis_id,()))
        if tasks:
            _,pending=await asyncio.wait(tasks,timeout=5)
            if pending:raise ValueError('passive_bundle_not_ready')
        contract=_loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))
        if contract!=SPECIAL_SHAPES_SOURCE_CONTRACT:
            raise ValueError('passive_bundle_unverified')
        return await page.evaluate(_passive_script(_PASSIVE_PRICE_CAPTURE_TEMPLATE,family),{'source_contract':contract,'settled':True})

    async def _initial_quantity_capture_locked(self, diagnosis_id):
        """Same bounded scalar projection, without reading shipping UI fields.

        First quantity may automatically reveal estimated shipping. Its UI is
        unrelated to the subtotal; this projection reads no address or shipping
        controls. The ordinary/default capture's presentation guard is unchanged.
        """
        # Same campaign/account proof as the ordinary capture, without walking
        # every product/estimated-shipping control around each response body.
        check=self._passive_scope_checks.get(diagnosis_id)
        record=self._records.get(diagnosis_id,{})
        if (not callable(check) or record.get('passive_campaign_product_id')!='17792'
                or record.get('observe_printograph_prices') is not True or record.get('status')!='ready'):
            raise ValueError('passive_campaign_scope_required')
        await check()
        page=self._pages.get(diagnosis_id)
        if page is None or page.is_closed() or page.url!=STAGGERED_FAMILY.source_url:
            raise ValueError('passive_owned_page_unavailable')
        signed_in=await page.evaluate(r'''()=>{
          const visible=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&e.getClientRects().length>0&&
            getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
          const text=e=>String(e.innerText||e.textContent||e.getAttribute('aria-label')||'').trim();
          const login=[...document.querySelectorAll('input[type="password"]')].slice(0,20).some(e=>visible(e)&&!!e.closest('form,[role="dialog"],main'));
          const logout=[...document.querySelectorAll('a,button,input[type="submit"]')].slice(0,500).some(e=>visible(e)&&/^(?:log\s*out|sign\s*out)$/i.test(text(e)||e.value||''));
          const account=[...document.querySelectorAll('[data-authenticated="true"],[data-logged-in="true"]')].slice(0,200).some(e=>visible(e)&&/account|dashboard|signed in/i.test(text(e)));
          return !login&&(logout||account);
        }''')
        if signed_in is not True:raise ValueError('passive_signed_in_required')
        page=self._pages[diagnosis_id]
        tasks=tuple(self._bundle_tasks.get(diagnosis_id,()))
        if tasks:
            _,pending=await asyncio.wait(tasks,timeout=5)
            if pending:raise ValueError('passive_bundle_not_ready')
        contract=_loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))
        if contract!=SPECIAL_SHAPES_SOURCE_CONTRACT:raise ValueError('passive_bundle_unverified')
        guard="""    if ([...document.querySelectorAll('#esCollapse')].some(e=>e.classList.contains('show')))
      return {observed:false,reason:'estimated_shipping_panel_open'};
"""
        if _PASSIVE_PRICE_CAPTURE_TEMPLATE.count(guard)!=1:raise ValueError('initial_capture_template_unproven')
        script=_PASSIVE_PRICE_CAPTURE_TEMPLATE.replace(guard,'')
        return await page.evaluate(_passive_script(script,STAGGERED_FAMILY),
                                   {'source_contract':contract,'settled':True})

    async def _disclose_initial_quantity_locked(self, diagnosis_id, epoch):
        journal=self._passive_journals[diagnosis_id]
        if not isinstance(journal,InitialQuantityPriceJournal) or not journal.is_initial_epoch(epoch):
            raise ValueError('initial_quantity_scope_unproven')
        capture=await self._initial_quantity_capture_locked(diagnosis_id)
        journal.start_initial_disclosure(capture)
        page=self._pages[diagnosis_id]
        fence_key=self._passive_fences[diagnosis_id]
        async def state(target=None):
            result=await page.evaluate(r'''({key,epoch,target})=>{
              const root=document.querySelector('#order-form'),s=window[key];
              if(location.href!=='https://www.printograph.com/products/staggered-cut-flyers/order'||
                 !root||root.getAttribute('data-printproducttype')!=='38'||
                 root.getAttribute('data-currentstoretype')!=='broker'||!s||s.root!==root||
                 s.epoch!==epoch||s.input!==1||s.change!==1||s.unexpected!==false)return{ok:false};
              const panels=root.querySelectorAll('#collapseOne'),buttons=root.querySelectorAll('button[aria-controls="collapseOne"]');
              if(panels.length!==1||buttons.length!==1)return{ok:false};
              const b=buttons[0],visible=e=>e.getClientRects().length>0&&getComputedStyle(e).display!=='none'&&
                getComputedStyle(e).visibility!=='hidden'&&!e.closest('[hidden],[aria-hidden="true"]');
              if(b.type!=='button'||b.disabled||!visible(b)||(target&&target!==b))return{ok:false};
              return{ok:true,open:panels[0].classList.contains('show')};
            }''',{'key':fence_key,'epoch':epoch,'target':target})
            if result.get('ok') is not True:raise ValueError('initial_quantity_disclosure_unproven')
            return result
        try:
            if (await state()).get('open') is not True:
                # Ordinary actual header click only; never invoke a source callback.
                button=await page.query_selector('#order-form button[aria-controls="collapseOne"]')
                if button is None:raise ValueError('initial_quantity_header_changed')
                # Final checks and native DOM click share one synchronous turn.
                acted=await button.evaluate(r'''(e,{key,epoch})=>{
                  const roots=[...document.querySelectorAll('#order-form')],root=roots.length===1?roots[0]:null,s=window[key];
                  const panels=root?.querySelectorAll('#collapseOne'),buttons=root?.querySelectorAll('button[aria-controls="collapseOne"]');
                  const visible=x=>x.getClientRects().length>0&&getComputedStyle(x).display!=='none'&&
                    getComputedStyle(x).visibility!=='hidden'&&!x.closest('[hidden],[aria-hidden="true"]');
                  if(location.href!=='https://www.printograph.com/products/staggered-cut-flyers/order'||!root||
                     root.getAttribute('data-printproducttype')!=='38'||root.getAttribute('data-currentstoretype')!=='broker'||
                     !s||s.root!==root||s.epoch!==epoch||s.input!==1||s.change!==1||s.unexpected!==false||
                     !e.isConnected||e.tagName!=='BUTTON'||e.getAttribute('type')!=='button'||e.disabled||!visible(e)||
                     !panels||panels.length!==1||panels[0].classList.contains('show')||!buttons||buttons.length!==1||buttons[0]!==e)
                    throw Error('initial header state changed');
                  HTMLElement.prototype.click.call(e);
                  return true;
                }''',{'key':fence_key,'epoch':epoch})
                if acted is not True:raise ValueError('initial_quantity_header_changed')
            if not await self._settle_initial(page) or (await state()).get('open') is not True:
                raise ValueError('initial_quantity_disclosure_not_retained')
            journal.validate_initial_progress(await self._initial_quantity_capture_locked(diagnosis_id))
        except asyncio.CancelledError:
            journal.invalidate('cancelled')
            raise
        except Exception:
            journal._hold_initial('initial_quantity_disclosure_failed')
            raise ValueError('initial_quantity_disclosure_failed') from None

    async def _ensure_passive_observer_locked(self, diagnosis_id):
        record=self._records[diagnosis_id]
        family=family_for_url(record['url'])
        if record.get('observe_printograph_prices') is not True:
            raise ValueError('passive_price_source_unsupported')
        if record['status']!='ready':raise ValueError('diagnosis_not_ready')
        existing=self._passive_journals.get(diagnosis_id)
        if existing is not None:
            if existing._invalid:raise ValueError('passive_journal_not_live')
            return existing
        observer=None
        try:
            # Partial base/productId fields stay explicit; no prospective identity is invented.
            capture=await self._passive_capture_locked(diagnosis_id)
            project_context(capture,complete=False,family=family)
            page=self._pages[diagnosis_id]
            if family is STAGGERED_FAMILY:
                # Recheck the existing authenticated active-17792 campaign guard
                # immediately before enabling its literal-only observer variant.
                await self._check_staggered_scope_locked(diagnosis_id)
                if record.get('passive_campaign_product_id') != '17792':
                    raise ValueError('passive_campaign_scope_required')
                journal_class=(TableQuantityJournal if diagnosis_id in self._supplier_profile_owners else InitialQuantityPriceJournal)
                journal=journal_class(diagnosis_id,family=family,allow_staggered_literal_base=True,
                    enable_initial_quantity=True)
            else:
                journal=PriceJournal(diagnosis_id,family=family)
            observer=_ManagedPassivePriceObserver(page,journal)
            if isinstance(journal,InitialQuantityPriceJournal):
                observer._initial_context_provider=lambda:self._initial_quantity_capture_locked(diagnosis_id)
            await observer.attach(SPECIAL_SHAPES_SOURCE_CONTRACT)
            fence='__hermesPassivePriceFence_'+diagnosis_id
            def changed(source,reason):
                if source.get('page') is page and source.get('frame') is page.main_frame and reason=='unattributed_control_change':
                    journal.invalidate('unattributed_control_change')
                    record['passive_price_observation']={'status':'held','reason':'unattributed_control_change','source_data_finalized':False}
                    try:self._persist(record)
                    except Exception:pass
            await page.expose_binding(fence+'_notify',changed)
            if (await page.evaluate(_passive_script(_PASSIVE_PRICE_FENCE_TEMPLATE,family),{'key':fence,'action':'install','notify_key':fence+'_notify'})).get('ok') is not True:
                raise ValueError('passive_fence_unavailable')
            self._passive_journals[diagnosis_id]=journal
            self._passive_observers[diagnosis_id]=observer
            self._passive_fences[diagnosis_id]=fence
            record['passive_price_observation']={'status':'observing','epochs':0,'source_data_finalized':False}
            self._persist(record)
            return journal
        except asyncio.CancelledError:
            if observer is not None:await observer.close()
            raise
        except Exception:
            if observer is not None:await observer.close()
            raise ValueError('passive_observer_not_ready') from None

    async def _begin_passive_choice_locked(self, diagnosis_id, revision, field, value):
        journal=self._passive_journals[diagnosis_id]
        capture=(await self._initial_quantity_capture_locked(diagnosis_id) if diagnosis_id in self._supplier_profile_owners
                 else await self._passive_capture_locked(diagnosis_id))
        family=journal.family
        source_name=field.get('source_name')
        if source_name == '' and field.get('kind') == 'select':
            binding_script=_passive_script(_PASSIVE_UNNAMED_SELECT_BINDING_TEMPLATE,family)
            if diagnosis_id in self._supplier_profile_owners:binding_script=executor_binding_script(binding_script)
            binding=await self._pages[diagnosis_id].evaluate(binding_script,
                {'source_contract':capture.get('source_contract'),'source_id':field.get('source_id'),
                 'selected_values':field.get('selected_values'),'target_value':value})
            if (not isinstance(binding,dict) or binding.get('observed') is not True or
                binding.get('binding_kind') != 'pinned_vue_form_select_spec' or
                str(capture.get('userSelectedOptions',{}).get(binding.get('field'))) != binding.get('retained_value')):
                raise ValueError('passive_native_model_binding_unproven')
            source_name=binding.get('field')
        target_id=field['source_id']
        if field.get('kind')=='radio':
            if family is not STAGGERED_FAMILY:raise ValueError('passive_radio_not_supported')
            binding=await self._pages[diagnosis_id].evaluate(
                executor_action_script(STAGGERED_RADIO_BINDING_JS) if diagnosis_id in self._supplier_profile_owners else STAGGERED_RADIO_BINDING_JS,
                {'source_contract':capture.get('source_contract'),'name':source_name,'value':value,
                 'selected_values':field.get('selected_values')})
            if (not isinstance(binding,dict) or binding.get('observed') is not True or
                binding.get('binding_kind')!='pinned_type38_manual_radio'):
                raise ValueError('passive_radio_binding_unproven')
            source_name=binding['field'];target_id=binding['source_id']
            field['_passive_target_id']=target_id
        if source_name not in set(family.keys)-{'productId'}:
            raise ValueError('passive_observed_field_unmapped')
        epoch=journal.begin_epoch(capture,revision,source_name,value)
        fence=self._passive_fences[diagnosis_id]
        result=await self._pages[diagnosis_id].evaluate(_passive_script(_PASSIVE_PRICE_FENCE_TEMPLATE,family),
                   {'key':fence,'action':'arm','source_id':target_id,'epoch':epoch})
        if result.get('ok') is not True or result.get('unexpected') is not False:
            await self._hold_passive_choice_locked(diagnosis_id,'unattributed_control_change')
            raise ValueError('passive_control_change_unattributed')
        return epoch

    async def _hold_passive_choice_locked(self, diagnosis_id, reason):
        journal=self._passive_journals.get(diagnosis_id)
        if journal is not None:
            if journal._invalid:
                reason=journal._invalid
            else:
                journal.invalidate(reason)
            try:journal.persist(Path(self.store.root))
            except Exception:reason='passive_checkpoint_failed'
        record=self._records[diagnosis_id]
        record['passive_price_observation']={'status':'held','reason':reason,'source_data_finalized':False}
        try:self._persist(record)
        except Exception:pass

    async def _finish_passive_choice_locked(self, diagnosis_id, revision, epoch, field_key, value):
        journal=self._passive_journals[diagnosis_id]
        observer=self._passive_observers[diagnosis_id]
        try:
            settled=await self._settle_initial(self._pages[diagnosis_id])
            await observer.drain()
            initial=isinstance(journal,InitialQuantityPriceJournal) and journal.is_initial_epoch(epoch)
            if initial:
                await self._disclose_initial_quantity_locked(diagnosis_id,epoch)
            result=await self._refresh_locked(diagnosis_id)
            field=next((f for f in result.get('fields',[]) if f.get('key')==field_key),{})
            selected=field.get('selected_values',[])
            if (selected!=[value] if field.get('kind')=='radio' else value not in selected):
                raise ValueError('observed_choice_not_retained')
            fence=await self._pages[diagnosis_id].evaluate(_passive_script(_PASSIVE_PRICE_FENCE_TEMPLATE,journal.family),
                {'key':self._passive_fences[diagnosis_id],'action':'disarm'})
            if (not settled or fence.get('ok') is not True or fence.get('unexpected') is not False
                    or fence.get('epoch')!=epoch or fence.get('input')!=1 or fence.get('change')!=1):
                journal.invalidate('unattributed_control_change')
            after=(await self._initial_quantity_capture_locked(diagnosis_id) if initial or diagnosis_id in self._supplier_profile_owners else
                   await self._passive_capture_locked(diagnosis_id))
            observation=journal.finalize_epoch(epoch,after,result['revision'])
            if set(observation['reasons']) & {'native_requests_inflight','request_budget_exceeded','fresh_tables_conflict','product_identity_conflict','option_identity_conflict','current_component_response_failed'}:
                journal.invalidate('adapter_error')
            journal.persist(Path(self.store.root))
            record=self._records[diagnosis_id]
            record['passive_price_observation']={'status':observation['status'],'epoch':epoch,
                'reasons':observation['reasons'],'source_data_finalized':False}
            self._persist(record)
            return self.get(diagnosis_id)
        except asyncio.CancelledError:
            await self._hold_passive_choice_locked(diagnosis_id,'cancelled');raise
        except Exception:
            await self._hold_passive_choice_locked(diagnosis_id,'adapter_error')
            raise ValueError('passive_observation_held') from None

    async def _resolve_supplier_profile_field(self, diagnosis_id, expected_revision, semantic, value):
        """Resolve native model identity; labels/generated ID prefixes grant none."""
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            optional_coating=semantic=='coating_optional'
            if optional_coating:semantic='coating'
            owner=self._supplier_profile_owners.get(diagnosis_id)
            record=self._records.get(diagnosis_id)
            profile_require(owner is not None and record is not None and record.get('status')=='ready'
                and record.get('revision')==expected_revision, 'supplier_profile_not_live')
            await self._check_staggered_scope_locked(diagnosis_id)
            capture=await self._initial_quantity_capture_locked(diagnosis_id)
            fields=copy.deepcopy(record.get('fields',[]))
            names={'proofType':'proofType','manualProofType':'proofTypeForManual',
                   'fileDelivery':'fileDeliveryForManual','productionTime':'productionTime'}
            matches=[]
            if semantic in names:
                matches=[field for field in fields if field.get('kind')=='radio'
                         and field.get('source_name')==names[semantic]]
            elif semantic in {'size','paper','color','quantity','coating'}:
                script=read_binding_script(_passive_script(_PASSIVE_UNNAMED_SELECT_BINDING_TEMPLATE,STAGGERED_FAMILY))
                for field in fields:
                    if field.get('kind')!='select' or field.get('source_name')!='' or field.get('disabled'):
                        continue
                    targets=([value] if value is not None else
                        [choice['value'] for choice in field.get('choices',[]) if not choice.get('disabled')
                         and isinstance(choice.get('value'),str) and (re.fullmatch(r'[1-9][0-9]{0,11}',choice['value'])
                             or semantic=='coating' and choice['value']=='')][:1])
                    if not targets:continue
                    if not any(choice.get('value')==targets[0] and not choice.get('disabled') for choice in field.get('choices',[])):
                        continue
                    binding=await self._pages[diagnosis_id].evaluate(script,{'source_contract':capture.get('source_contract'),
                        'source_id':field.get('source_id'),'selected_values':field.get('selected_values'),'target_value':targets[0]})
                    if (isinstance(binding,dict) and binding.get('observed') is True
                            and binding.get('field')==semantic and binding.get('binding_kind')=='pinned_vue_form_select_spec'
                            and str(capture.get('userSelectedOptions',{}).get(semantic))==binding.get('retained_value')):
                        matches.append(field)
            if not matches and optional_coating:
                selected=capture.get('userSelectedOptions',{})
                profile_require(type(selected) is dict and 'coating' in selected and selected['coating']=='',
                    'source_coating_absence_unproven')
                await self._check_staggered_scope_locked(diagnosis_id)
                profile_require(self._records.get(diagnosis_id) is record and record.get('revision')==expected_revision
                    and self._supplier_profile_owners.get(diagnosis_id) is owner, 'supplier_profile_not_live')
                return None
            profile_require(len(matches)==1, 'source_control_unproven')
            field=matches[0]
            profile_require(field.get('disabled') is False and (value is None or any(
                choice.get('value')==value and not choice.get('disabled') for choice in field.get('choices',[]))),
                'source_choice_unavailable')
            await self._check_staggered_scope_locked(diagnosis_id)
            profile_require(self._records.get(diagnosis_id) is record and record.get('revision')==expected_revision
                and self._supplier_profile_owners.get(diagnosis_id) is owner, 'supplier_profile_not_live')
            field['source_key']=semantic
            owner['binding'][semantic]=field
            return copy.deepcopy(field)

    async def _profile_read_quote(self, diagnosis_id, configuration, check_scope):
        """Read exact live tables/current production; never issue a request."""
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record=self._records.get(diagnosis_id);owner=self._supplier_profile_owners.get(diagnosis_id)
            journal=self._passive_journals.get(diagnosis_id);page=self._pages.get(diagnosis_id)
            profile_require(owner is not None and isinstance(journal,TableQuantityJournal)
                and record is not None and record.get('status')=='ready', 'supplier_profile_not_live')
            revision=record['revision'];generation=journal.page_generation
            def guard():
                profile_require(self._records.get(diagnosis_id) is record and record.get('revision')==revision
                    and record.get('status')=='ready' and self._pages.get(diagnosis_id) is page
                    and page is not None and not page.is_closed() and page.url==STAGGERED_FAMILY.source_url
                    and self._supplier_profile_owners.get(diagnosis_id) is owner
                    and self._passive_journals.get(diagnosis_id) is journal and not journal._invalid
                    and journal.page_generation==generation and not self._bundle_tasks.get(diagnosis_id)
                    and _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))==SPECIAL_SHAPES_SOURCE_CONTRACT,
                    'supplier_profile_not_live')
            guard();await check_scope();guard()
            profile_require(await self._settle_initial(page), 'source_not_settled')
            await self._passive_observers[diagnosis_id].drain();guard()
            capture=await self._initial_quantity_capture_locked(diagnosis_id);guard()
            context=project_context(capture,family=STAGGERED_FAMILY)
            profile_require(all(context['selected'].get(k)==v for k,v in configuration.items())
                and all(context['selected'].get(k)==v for k,v in {'proofType':'100','fileDelivery':'5','designServices':'42'}.items()),
                'requested_configuration_not_retained')
            anchor=owner.get('anchor')
            if anchor is None or anchor.nonquantity!=context['nonquantity_hash']:
                anchor=LiveTableAnchor.create(journal,capture,revision)
                owner['anchor']=anchor
            stamp=(journal._order,journal._current)
            metadata=await self._read_staggered_turnaround_metadata_locked(diagnosis_id,revision);guard()
            final_capture=await self._initial_quantity_capture_locked(diagnosis_id);guard()
            profile_require(project_context(final_capture,family=STAGGERED_FAMILY)['context_hash']==context['context_hash']
                and (journal._order,journal._current)==stamp, 'source_context_changed_during_quote')
            await check_scope();guard()
            quote=anchor.quote(final_capture,revision,metadata)
            # Export field identities are the actual source-bound snapshot keys.
            production=[field for field in record['fields'] if field.get('source_name')=='productionTime']
            profile_require(len(production)==1, 'production_control_unproven')
            owner['binding']['productionTime']={**copy.deepcopy(production[0]),'source_key':'productionTime'}
            bindings={}
            for semantic,bound in owner['binding'].items():
                current=[field for field in record['fields'] if field.get('key')==bound.get('key')
                         and field.get('source_id')==bound.get('source_id')]
                profile_require(len(current)==1, 'export_control_binding_changed')
                bindings[semantic]={**copy.deepcopy(current[0]),'source_key':semantic}
            from .supplier_profile_executor import normal_export_bundle
            quote['normal_export']=normal_export_bundle(quote,bindings,record)
            return quote

    async def read_passive_price_evidence(self, diagnosis_id, expected_revision):
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id,asyncio.Lock()):
            record=self._records[diagnosis_id]
            if type(expected_revision) is not int or record['revision']!=expected_revision:
                raise ValueError('diagnosis_revision_conflict')
            if record.get('observe_printograph_prices') is not True:
                raise ValueError('passive_price_observation_not_enabled')
            journal=self._passive_journals.get(diagnosis_id)
            if journal is None:
                return {'status':'unavailable','reason':'no_live_passive_journal','diagnosis_id':diagnosis_id,
                        'revision':expected_revision,'source_data_finalized':False,'woocommerce_import_ready':False}
            page=self._pages.get(diagnosis_id)
            live=(page is not None and not page.is_closed()
                  and page.url==journal.family.source_url and not journal._invalid
                  and not self._bundle_tasks.get(diagnosis_id)
                  and _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))==SPECIAL_SHAPES_SOURCE_CONTRACT)
            if live and journal.family is STAGGERED_FAMILY:
                try:await self._check_staggered_scope_locked(diagnosis_id)
                except ValueError:live=False
            evidence=copy.deepcopy(journal.snapshot())
            # This reads cached evidence only. Do not extend TTL or imply a new DOM/source check.
            for observation in evidence['epochs']:
                expired=False
                revision_matches=observation.get('revision_after')==expected_revision
                for component in observation.get('components',[]):
                    completed=component.get('response_completed_at_monotonic')
                    if type(completed) not in (int,float):continue
                    age=journal.clock()-completed
                    component['ttl_remaining_seconds']=max(0,300-age) if age>=0 else 0
                    if not live or not revision_matches or not 0<=age<=300:
                        component['status']='historical_only';expired=True
                if not live or not revision_matches or expired:
                    observation['status']='held'
                    reason=('observation_not_live' if not live else
                            'observation_revision_not_current' if not revision_matches else 'table_ttl_expired')
                    observation['reasons']=sorted(set(observation.get('reasons',[]))|{reason})
            latest=evidence['epochs'][-1] if evidence['epochs'] else None
            current=(live and latest is not None and latest.get('revision_after')==expected_revision
                     and latest.get('status')=='matched_fresh_observed_components')
            return {'status':'observed' if current else 'held','diagnosis_id':diagnosis_id,'revision':expected_revision,
                    'evidence':evidence,'historical_only':not current,'current_selection_revalidated':False,
                    'source_requests_initiated':0,'source_data_finalized':False,'woocommerce_import_ready':False,'checkout_verified':False}


    async def configure_coroplast(self, diagnosis_id, expected_revision, selections):
        """Observe one explicitly selected branch on the retained 6383 page.

        No price/proceed button, customer field or address-book action is used.
        The ordinary native changes share the campaign origin limiter.
        """
        from .fourover_coroplast import SOURCE, configure_and_observe, selection_from_diagnosis, forbidden_write
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if type(expected_revision) is not int or record['revision'] != expected_revision:
                raise ValueError('diagnosis_revision_conflict')
            selection_from_diagnosis(record, selections)
            if page is None or page.is_closed() or page.url != SOURCE:
                raise ValueError('coroplast_owned_page_unavailable')

            async def check():
                if (self._closed or page.is_closed() or self._pages.get(diagnosis_id) is not page
                        or self._records.get(diagnosis_id) is not record
                        or record['revision'] != expected_revision or page.url != SOURCE):
                    raise ValueError('coroplast_owned_page_changed')

            statuses = []
            blocked_writes = []
            async def guard_source_write(route):
                if forbidden_write(route.request.method, route.request.url):
                    blocked_writes.append(True)
                    await route.abort()
                else:
                    await route.continue_()
            def response_seen(response):
                if (urlsplit(response.url).hostname in {'4over.com','www.4over.com'}
                        and response.request.resource_type in {'xhr','fetch'}
                        and response.status in {401,403,429,503}):
                    statuses.append(response.status)
            token = await self._choice_limiter.acquire('https://4over.com', 1, 1, check)
            started = time.monotonic(); status = 500
            page.on('response', response_seen)
            try:
                await page.route('**/*', guard_source_write)
                async with asyncio.timeout(25):
                    result = await configure_and_observe(page, copy.deepcopy(record), selections, check)
                    await check()
                    if blocked_writes:
                        raise ValueError('coroplast_forbidden_source_write_blocked')
                    if statuses:
                        status = statuses[-1]
                        raise ValueError('coroplast_supplier_backoff_required' if status in {429,503}
                                         else 'coroplast_supplier_pricing_access_unverified')
                    # Read only; _refresh_locked does not click 4over controls.
                    refreshed = await self._refresh_locked(diagnosis_id)
                    if refreshed.get('status') != 'ready' or refreshed.get('url') != SOURCE:
                        raise ValueError('coroplast_diagnosis_not_retained')
                    result['diagnosis_revision_after'] = refreshed['revision']
                    result['diagnosis'] = refreshed
                    record['coroplast_observation'] = copy.deepcopy({k:v for k,v in result.items() if k!='diagnosis'})
                    self._persist(record)
                    status = 200
                    return result
            finally:
                page.remove_listener('response', response_seen)
                try:
                    await page.unroute('**/*', guard_source_write)
                finally:
                    if statuses: status = statuses[-1]
                    await asyncio.to_thread(self._choice_limiter.release, 'https://4over.com', token,
                                            time.monotonic()-started, status, 30, 1)

    async def select_observed_choice(self, diagnosis_id, expected_revision, field_key, value):
        """Change one currently offered native dropdown choice in the open page.

        This is a bounded diagnosis action, not a pricing walk or a claim that
        the returned price covers other selections. Its source-triggered calls
        share the persisted origin limiter with the campaign workers.
        """
        self.get(diagnosis_id)
        if not isinstance(field_key, str) or not isinstance(value, str):
            raise ValueError("invalid_observed_choice")
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready" or page is None or page.is_closed():
                raise ValueError("diagnosis_not_ready")
            if record["supplier_id"] != "printograph":
                raise ValueError("observed_choice_supplier_unsupported")
            current = urlsplit(page.url)
            if current.scheme != "https" or current.hostname not in self._page_hosts[diagnosis_id]:
                raise ValueError("unexpected_source_origin")
            fields = [f for f in record["fields"] if f.get("key") == field_key]
            if len(fields) != 1 or fields[0].get("kind") not in {"select", "radio"}:
                raise ValueError("observed_choice_control_required")
            field = fields[0]
            if record.get("observe_printograph_prices") is True and record["url"]==STAGGERED_FAMILY.source_url:
                field=dict(field)
            if field.get("disabled"):
                raise ValueError("observed_choice_control_disabled")
            source_id = field.get("source_id", "")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", source_id):
                raise ValueError("observed_choice_control_id_invalid")
            choices = [c for c in field.get("choices", []) if c.get("value") == value and not c.get("disabled")]
            if not choices or value in field.get("selected_values", []):
                raise ValueError("choice_not_newly_available")
            origin = f"{current.scheme}://{current.netloc}"

            async def check():
                if self._closed or page.is_closed():
                    raise ValueError("diagnosis_not_ready")

            passive = record.get("observe_printograph_prices") is True
            if passive and field['kind']!='select' and not (record['url']==STAGGERED_FAMILY.source_url and field['kind']=='radio'):
                raise ValueError('passive_radio_not_supported')
            if passive:
                await self._ensure_passive_observer_locked(diagnosis_id)
            token = await self._choice_limiter.acquire(origin, 1, 1, check)
            epoch = None
            started = time.monotonic()
            status = 500
            try:
                if passive:
                    epoch = await self._begin_passive_choice_locked(diagnosis_id, expected_revision, field, value)
                if diagnosis_id in self._supplier_profile_owners:
                    await self._supplier_profile_owners[diagnosis_id]['check_scope']()
                    profile_require(self._records.get(diagnosis_id) is record and record.get('revision')==expected_revision
                        and self._pages.get(diagnosis_id) is page and not page.is_closed(), 'supplier_profile_not_live')
                # The ID is taken from the saved rendered control, not from
                # caller input, and restricted above to a CSS-safe alphabet.
                if passive and record['url']==STAGGERED_FAMILY.source_url:
                    journal=self._passive_journals[diagnosis_id]
                    if (journal._invalid or journal._current!=epoch or self._bundle_tasks.get(diagnosis_id) or
                        _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))!=SPECIAL_SHAPES_SOURCE_CONTRACT):
                        raise ValueError('passive_native_action_context_changed')
                if field["kind"] == "select":
                    control=page.locator(f'select[id="{source_id}"]')
                    if passive and record['url']==STAGGERED_FAMILY.source_url:
                        await control.evaluate(executor_action_script(STAGGERED_SELECT_ACTION_JS) if diagnosis_id in self._supplier_profile_owners else STAGGERED_SELECT_ACTION_JS,
                            {'value':value,'source_id':source_id,'retained_values':field.get('selected_values'),
                             'fence_key':self._passive_fences[diagnosis_id],'epoch':epoch})
                    else:
                        await control.select_option(value, timeout=10000)
                else:
                    name = field.get("source_name", "")
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", name) or not re.fullmatch(r"[0-9]{1,12}", value):
                        raise ValueError("observed_radio_identity_invalid")
                    selector = (f'#order-form input[type="radio"][name^="raisedFoilColor"][value="{value}"]'
                                if name == "raisedFoilColor" else
                                f'#order-form input[type="radio"][name="{name}"][value="{value}"]')
                    if passive:
                        selector=f'#order-form input[type="radio"][id="{field["_passive_target_id"]}"]'
                    radio = page.locator(selector)
                    if await radio.count() != 1 or not await radio.is_enabled():
                        raise ValueError("observed_radio_unavailable")
                    if passive:
                        journal=self._passive_journals[diagnosis_id]
                        if (journal._invalid or journal._current!=epoch or self._bundle_tasks.get(diagnosis_id) or
                            _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))!=SPECIAL_SHAPES_SOURCE_CONTRACT):
                            raise ValueError('passive_native_action_context_changed')
                        await radio.evaluate(executor_action_script(STAGGERED_RADIO_ACTION_JS) if diagnosis_id in self._supplier_profile_owners else STAGGERED_RADIO_ACTION_JS,
                            {'source_id':field['_passive_target_id'],'name':name,'value':value,
                             'retained_values':field.get('selected_values'),
                             'fence_key':self._passive_fences[diagnosis_id],'epoch':epoch})
                    else:
                        await radio.evaluate('(e) => e.click()')
                # Printograph first displays the generic turnaround and then
                # recalculates finish-dependent text. Wait for that update.
                await asyncio.sleep(1.5)
                status = 200
            except BaseException as error:
                if passive:
                    await self._hold_passive_choice_locked(diagnosis_id, "cancelled" if isinstance(error,asyncio.CancelledError) else "adapter_error")
                if isinstance(error,asyncio.CancelledError):
                    raise
                if passive:
                    raise ValueError("passive_native_choice_failed") from None
                raise
            finally:
                await asyncio.to_thread(self._choice_limiter.release, origin, token,
                                        time.monotonic() - started, status, 30, 1)
            if passive:
                return await self._finish_passive_choice_locked(diagnosis_id, expected_revision, epoch, field_key, value)
        result = await self.refresh(diagnosis_id)
        selected = next((f.get("selected_values", []) for f in result.get("fields", [])
                         if f.get("key") == field_key), [])
        if (selected != [value] if field["kind"] == "radio" else value not in selected):
            raise ValueError("observed_choice_not_retained")
        return result

    async def read_scoped_price(self, diagnosis_id, expected_revision):
        """Read the current Printograph calculator without making a source call.

        The historical client bundle is not proven to be the loaded bundle by
        diagnosis alone, so this observation cannot certify a product or quote.
        """
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready" or page is None or page.is_closed():
                raise ValueError("diagnosis_not_ready")
            current = urlsplit(page.url)
            if record["supplier_id"] != "printograph" or current.scheme != "https" or current.hostname not in self._page_hosts[diagnosis_id]:
                raise ValueError("unexpected_source_origin")
            settled = await self._settle_initial(page)
            # Finish hashing responses already observed by this diagnosis. This
            # does not fetch scripts or initiate any additional source request.
            pending = tuple(self._bundle_tasks.get(diagnosis_id, ()))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            loaded_contract = _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id, {}))
            bundle_metadata = {"source_bundle_verified": reviewed_source_scope(loaded_contract) is not None,
                               "loaded_contract": loaded_contract}
            capture = await page.evaluate(CONTROLLER_PRICE_STATE_JS,
                                          {"source_contract": loaded_contract, "settled": settled})
            if not capture.get("observed"):
                result = {"status": "unavailable", "reason": capture.get("reason", "controller_not_observed"),
                          "diagnosis_id": diagnosis_id, "revision": expected_revision, **bundle_metadata}
                if result["reason"] == "controller_product_mismatch":
                    metadata = _safe_controller_diagnostics(capture.get("diagnostic_metadata"))
                    if metadata is not None:
                        result["diagnostic_metadata"] = metadata
                if record["url"] == STAGGERED_FAMILY.source_url:
                    result["turnaround_metadata"] = await self._read_staggered_turnaround_metadata_locked(diagnosis_id, expected_revision)
                return result
            displayed = await page.evaluate("""() => {
              const root=document.querySelector('#order-form');
              const text=root?.innerText || '';
              const match=text.match(/Subtotal\\s*\\(excludes shipping\\)\\s*:\\s*(\\$[0-9,]+(?:\\.[0-9]{2})?)/i);
              return match ? match[1] : null;
            }""")
            if displayed is not None:
                capture["displayed_total"] = displayed
            reconciliation = calculate_source_total(capture)
            result = {"status": "observed", "scope": "current_page_only", "diagnosis_id": diagnosis_id,
                    "revision": expected_revision, **bundle_metadata,
                    "capture": capture, "reconciliation": reconciliation}
            if record["url"] == STAGGERED_FAMILY.source_url:
                result["turnaround_metadata"] = await self._read_staggered_turnaround_metadata_locked(diagnosis_id, expected_revision)
            return result

    async def _read_staggered_turnaround_metadata_locked(self, diagnosis_id, expected_revision):
        """Read source-owned current cache metadata; caller holds the diagnosis lock."""
        record = self._records[diagnosis_id]
        page = self._pages.get(diagnosis_id)

        def guard():
            if (type(expected_revision) is not int or self._closed
                    or self._records.get(diagnosis_id) is not record or self._pages.get(diagnosis_id) is not page
                    or record.get('revision') != expected_revision or record.get('status') != 'ready'
                    or record.get('supplier_id') != 'printograph' or record.get('url') != STAGGERED_FAMILY.source_url
                    or record.get('passive_campaign_product_id') != '17792' or record.get('observe_printograph_prices') is not True
                    or page is None or page.is_closed() or page.url != STAGGERED_FAMILY.source_url
                    or self._bundle_tasks.get(diagnosis_id)
                    or _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id, {})) != SPECIAL_SHAPES_SOURCE_CONTRACT):
                raise ValueError('staggered_turnaround_scope_unproven')

        guard()
        await self._check_staggered_scope_locked(diagnosis_id)
        guard()
        observed = await page.evaluate(TURNAROUND_METADATA_JS, {'source_contract': dict(SPECIAL_SHAPES_SOURCE_CONTRACT)})
        guard()
        await self._check_staggered_scope_locked(diagnosis_id)
        guard()
        metadata = project_turnaround_metadata(observed)
        return {'diagnosis_id': diagnosis_id, 'revision': expected_revision, 'source_url': record['url'],
                'product_id': '17792', 'scope': 'current_selected_turnaround_cache_metadata_only',
                'source_bundle_verified': True, 'source_fetches_initiated': 0,
                'diagnosis_or_selection_changed': False, **metadata}

    async def read_source_references(self, diagnosis_id, expected_revision, source_url):
        """Read only safe source reference attributes from the owned current page.

        This does not fetch, refresh, select controls, read prices or change the
        diagnosis. Exact mapped URL and revision bind both sides of evaluation.
        """
        if type(expected_revision) is not int:
            raise ValueError("diagnosis_revision_conflict")
        if not isinstance(source_url, str) or source_url not in SOURCE_URLS:
            raise ValueError("diagnosis_source_url_unsupported")
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)

            def check_current():
                if record["revision"] != expected_revision:
                    raise ValueError("diagnosis_revision_conflict")
                if self._closed or record["status"] != "ready" or page is None or page.is_closed():
                    raise ValueError("diagnosis_not_ready")
                if (record["supplier_id"] != "printograph" or record["url"] != source_url
                        or page.url != source_url):
                    raise ValueError("diagnosis_source_url_mismatch")

            check_current()
            raw = await page.evaluate(SOURCE_REFERENCES_JS, {"source_url": source_url})
            check_current()
            result = project_references(raw)
            # Finish only responses already observed by this page. Never fetch
            # a static asset or cancel a shared source response on timeout.
            pending = tuple(self._bundle_tasks.get(diagnosis_id, ()))
            if pending:
                _, unfinished = await asyncio.wait(pending, timeout=2)
                if unfinished:
                    raise ValueError("source_reference_hashes_pending")
            check_current()
            observed = self._bundle_hashes.get(diagnosis_id, {})
            if any(len(observed.get(key, ())) > 1 for key in SOURCE_CONTRACT):
                raise ValueError("source_script_hash_ambiguous")
            loaded = _loaded_bundle_contract(observed)
            loaded = {key: value for key, value in loaded.items()
                      if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)}
            return {"schema": "printograph-current-source-references/v1", "status": "observed",
                    "diagnosis_id": diagnosis_id, "revision": expected_revision, "source_url": source_url,
                    **result, "loaded_contract": loaded, "source_bundle_verified": reviewed_source_scope(loaded) is not None,
                    "static_asset_hash_comparison_ready": not result["script_roles_missing"] and set(loaded) == set(SOURCE_CONTRACT),
                    "source_fetches_initiated": 0, "page_contents_emitted": False,
                    "account_or_input_fields_emitted": False, "asset_bodies_returned": False,
                    "static_asset_fetch_authorized": False, "price_verified": False,
                    "diagnosis_or_selection_changed": False}

    async def read_observed_gallery(self, diagnosis_id, expected_revision):
        """Read source-gallery image URLs from the current product page only."""
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready" or page is None or page.is_closed():
                raise ValueError("diagnosis_not_ready")
            current = urlsplit(page.url)
            if record.get('supplier_id')=='printograph':
                return await self._read_staggered_gallery_locked(diagnosis_id,expected_revision)
            if (record["supplier_id"] != "fourover" or current.scheme != "https"
                    or current.hostname not in self._page_hosts[diagnosis_id]):
                raise ValueError("unexpected_source_origin")
            observed = await page.evaluate("""() => {
              const scope=document.querySelector('#maincontent') || document.querySelector('main');
              const imgs=[...(scope?.querySelectorAll('img') || [])];
              return imgs.slice(0,100).map(e=>({
                alt:String(e.alt||'').replace(/\\s+/g,' ').trim().slice(0,180),
                src:e.currentSrc || e.getAttribute('data-src') || e.src || '',
                gallery:!!e.closest('.fotorama,.gallery-placeholder,[data-gallery-role],.product.media')
              })).filter(e=>e.gallery && e.src);
            }""")
            images = []
            for image in observed:
                parsed = urlsplit(image["src"])
                if (parsed.scheme == "https" and parsed.hostname
                        and (parsed.hostname == "4over.com" or parsed.hostname.endswith(".4over.com"))):
                    item = {"url": image["src"], "alt": image["alt"]}
                    if item not in images:
                        images.append(item)
            return {"diagnosis_id": diagnosis_id, "revision": expected_revision,
                    "source_url": record["url"], "images": images[:30],
                    "scope": "current_rendered_product_gallery_only"}

    async def _read_staggered_gallery_locked(self, diagnosis_id, expected_revision):
        """Source-readonly17792 gallery; caller already holds the diagnosis lock."""
        record=self._records[diagnosis_id];page=self._pages.get(diagnosis_id)
        def guard():
            if (record.get('revision')!=expected_revision or record.get('status')!='ready'
                    or record.get('supplier_id')!='printograph' or record.get('url')!=STAGGERED_FAMILY.source_url
                    or record.get('passive_campaign_product_id')!='17792' or record.get('observe_printograph_prices') is not True
                    or page is None or page.is_closed() or page.url!=STAGGERED_FAMILY.source_url
                    or self._bundle_tasks.get(diagnosis_id)
                    or _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id,{}))!=SPECIAL_SHAPES_SOURCE_CONTRACT):
                raise ValueError('staggered_gallery_scope_unproven')
        guard();await self._check_staggered_scope_locked(diagnosis_id);guard()
        observed=await page.evaluate(STAGGERED_GALLERY_JS)
        guard();await self._check_staggered_scope_locked(diagnosis_id);guard()
        images=project_staggered_gallery(observed)
        return {'diagnosis_id':diagnosis_id,'revision':expected_revision,'source_url':record['url'],
                'product_id':'17792','images':images,'scope':'current_rendered_product_gallery_only',
                'image_element_count':observed['image_element_count'],'unmatched_image_elements':observed['unmatched_image_elements'],
                'source_gallery_complete':False,
                'source_bundle_verified':True,'source_fetches_initiated':0,
                'diagnosis_or_selection_changed':False,'price_verified':False,'source_data_finalized':False}

    async def save_selection(self, diagnosis_id, expected_revision, selection):
        self.get(diagnosis_id)
        # The API validates selection policy. This boundary still requires a
        # finite JSON object and an exact revision under the same commit lock.
        if not isinstance(selection, dict):
            raise ValueError("invalid_selection")
        payload = json.dumps(selection, allow_nan=False)
        if len(payload) > 65536:
            raise ValueError("selection_too_large")
        async with self._lock:
            record = self._records[diagnosis_id]
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready":
                raise ValueError("diagnosis_not_ready")
            record["selection"] = json.loads(payload)
            self._persist(record)
            return copy.deepcopy(record)

    async def _release(self, owner):
        self._supplier_profile_owners.pop(owner, None)
        observer=self._passive_observers.pop(owner,None)
        if observer is not None:
            await observer.close()
        page=self._pages.get(owner)
        fence=self._passive_fences.pop(owner,None)
        if fence and page is not None and not page.is_closed():
            try:await page.evaluate(_passive_script(_PASSIVE_PRICE_FENCE_TEMPLATE,
                self._passive_journals[owner].family),{"key":fence,"action":"remove"})
            except Exception:pass
        self._passive_journals.pop(owner,None)
        self._passive_scope_checks.pop(owner,None)
        self._pages.pop(owner, None)
        self._page_hosts.pop(owner, None)
        tasks = self._bundle_tasks.pop(owner, set())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bundle_hashes.pop(owner, None)
        lease = self._leases.pop(owner, None)
        if lease is not None:
            try:
                await lease.__aexit__(None, None, None)
            except Exception:
                pass

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for owner in list(self._leases):
            await self._release(owner)
        async with self._lock:
            for record in self._records.values():
                record.update(status="closed", session_status="closed")
                self._persist(record)
            for result in self._supplier_sessions.values():
                result.update(status="closed", session_status="closed")
