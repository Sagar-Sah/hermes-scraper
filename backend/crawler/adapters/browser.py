"""Ordinary browser extraction from visible product controls and displayed quotes.

No API credentials are read from the browser. Network responses are observations
of normal page operation, not requests replayed with harvested authorization.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlparse

from ..config import settings
from ..commerce import unit_price
from ..service_context import canonical_service_context
from ..browser_stability import SettledSnapshot, supports_stable_reuse
from ..printograph_modern_browser import (
    SCHEMA as MODERN_SERVICE_SCHEMA, SERVICE_KEYS as MODERN_SERVICE_KEYS,
    ModernHeld, FreshQuoteGate, SERVICE_READ_JS, prepare_services, modern_bound,
    validate_service_policy, bind_service_policy,
)
from ..browser_capacity import browser_workload
from ..pacing import retry_after_seconds, source_pacing
from ..transport import RunInterrupted
from .printograph import AdapterError

# Keep the shared context-manager interface used by title discovery. This limits
# active product pages only; every page still uses the database source limiter.
_BROWSER_LOCK = asyncio.Semaphore(settings.browser_product_concurrency)


class InactivityDeadline:
    """Only new source progress extends a wait; repeating busy state does not."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.expires_at = time.monotonic() + seconds
        self.seen = set()

    def observe(self, key):
        if key not in self.seen:
            self.seen.add(key)
            self.expires_at = time.monotonic() + self.seconds

    @property
    def expired(self):
        return time.monotonic() >= self.expires_at


@asynccontextmanager
async def browser_slot(check, *, source_origin=None, per_source_limit=None):
    """Bound active pages and keep queued paused/cancelled jobs responsive."""
    async with browser_workload.slot(
        _BROWSER_LOCK,
        check,
        source_origin=source_origin,
        per_source_limit=per_source_limit,
    ):
        yield


ALIASES = {
    "stock": "paper",
    "paperstock": "paper",
    "colorspec": "color",
    "colors": "color",
    "runsize": "quantity",
    "qty": "quantity",
    "production": "productionTime",
    "productiontime": "productionTime",
    "turnaround": "productionTime",
    "turnaroundtime": "productionTime",
    "blankenvelopes": "blankEnvelopes",
    "windposition": "windPosition",
    "roundedcorners": "roundedCorners",
}


def field_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return ALIASES.get(normalized, normalized)


def is_navigation_cancellation(
    reason, request_generation, current_generation, *, bootstrap=False, navigating=False
):
    return "ERR_ABORTED" in str(reason) and (
        bootstrap or navigating or request_generation != current_generation
    )


def selected_matches(control: dict, requested) -> bool:
    values = requested if isinstance(requested, list) else [requested]
    actual = {
        str(control.get("value", "")).casefold(),
        str(control.get("selected_label", "")).casefold(),
    }
    return any(str(v).strip().casefold() in actual for v in values)


def public_control_text(value) -> str:
    """Bound product-control diagnostics and omit URL queries/fragments."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    text = re.sub(
        r"https?://[^\s]+",
        lambda match: urlparse(match.group())._replace(query="", fragment="").geturl(),
        text,
    )
    return text[:160]


def filter_choices(control: dict, requested=None) -> list[dict]:
    choices = [
        x
        for x in control.get("choices", [])
        if not x.get("placeholder") and not x.get("disabled")
    ]
    if control["kind"] == "number" and requested is not None:
        values = requested if isinstance(requested, list) else [requested]
        choices = []
        for value in values:
            try:
                number = Decimal(str(value).replace(",", ""))
            except InvalidOperation as exc:
                raise AdapterError(
                    "invalid_selection", f"Non-numeric selection for {control['label']}"
                ) from exc
            if not number.is_finite():
                raise AdapterError(
                    "invalid_selection", "Numeric selections must be finite"
                )
            for limit, compare in (
                ("min", lambda a, b: a < b),
                ("max", lambda a, b: a > b),
            ):
                if control.get(limit) not in (None, "") and compare(
                    number, Decimal(control[limit])
                ):
                    raise AdapterError(
                        "invalid_selection",
                        f"Selection outside {control['label']} {limit}",
                    )
            choices.append(
                {
                    "value": format(number, "f"),
                    "label": format(number, "f"),
                    "selector": control["selector"],
                }
            )
        return choices
    if requested is None:
        return choices
    values = {
        str(x).strip().casefold()
        for x in (requested if isinstance(requested, list) else [requested])
    }
    return [
        x
        for x in choices
        if str(x["value"]).strip().casefold() in values
        or x["label"].strip().casefold() in values
    ]


SNAPSHOT_JS = r"""({priceSelector,modernServices=false}) => {
 // These caches live for one synchronous observation only. They avoid repeated
 // layout/style/ancestor scans without retaining stale controls between reads.
 const visibility = new WeakMap(), paths = new WeakMap(), siblingIndexes = new WeakMap(), labels = new WeakMap();
 const visible = e => {
   if(!e)return false;
   if(!visibility.has(e))visibility.set(e,e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden');
   return visibility.get(e);
 };
 const path = e => {
   if(paths.has(e))return paths.get(e);
   const pending=[];let prefix='';
   for(let n=e;n&&n.nodeType===1;n=n.parentElement){
     if(paths.has(n)){prefix=paths.get(n);break;}
     if(n.id){prefix='#'+CSS.escape(n.id);paths.set(n,prefix);break;}
     pending.push(n);
   }
   for(const n of pending.reverse()){
     let ix=1;const parent=n.parentElement;
     if(parent){
       if(!siblingIndexes.has(parent)){
         const indexes=new WeakMap(),counts=new Map();
         for(const child of parent.children){const count=(counts.get(child.tagName)||0)+1;counts.set(child.tagName,count);indexes.set(child,count);}
         siblingIndexes.set(parent,indexes);
       }
       ix=siblingIndexes.get(parent).get(n);
     }
     prefix=(prefix?prefix+' > ':'')+n.tagName.toLowerCase()+':nth-of-type('+ix+')';paths.set(n,prefix);
   }
   return prefix;
 };
 const clean = s => (s||'').replace(/\s+/g,' ').trim();
 const label = e => {
   if(!labels.has(e))labels.set(e,clean(e.getAttribute('aria-label') || Array.from(e.labels||[]).map(x=>x.innerText).join(' ') || e.name || e.id));
   return labels.get(e);
 };
 const excluded = /country|shipping|delivery.?address|address.?type|zip.?code|postal|billing|stateprovince|language|currency/i;
 const root=document.querySelector('#order-form') || document.querySelector('form.variations_form') || document.querySelector('main') || document.body;
 if(!root||!document.body)return {title:document.title||'',controls:[],unsupported:[],price:null,basis:null,images:[],busy:true,document_ready:false,challenge:false,login:false};
 const controls=[],unsupported=[],coverageGaps=new Set(),serviceContext=[];
 const serviceContextObserved=!!document.querySelector('#order-form[data-printproducttype]');
 // The source's collapsed service section can hide priced proof/design choices.
 // Record only these known product-service radio selections as diagnostics.
 // They remain outside traversed controls and prices; never read hidden inputs.
 if(serviceContextObserved&&!modernServices){
   for(const e of document.querySelectorAll('#collapseOne input[type="radio"][name="proofType"],#collapseOne input[type="radio"][name="fileDeliveryForIop"]')){
     if(!e.disabled&&!visible(e)&&!Array.from(e.labels||[]).some(visible))coverageGaps.add('collapsed_service_control:'+e.name);
   }
   for(const key of ['proofType','fileDeliveryForIop']){
     const group=Array.from(document.querySelectorAll('#collapseOne input[type="radio"][name="'+key+'"]'));
     if(!group.length)continue;
     const selected=group.filter(e=>e.checked).map(e=>({value:e.value,checked:true,disabled:e.disabled,
       visible:visible(e)||Array.from(e.labels||[]).some(visible),
       label:clean(Array.from(e.labels||[]).map(l=>l.textContent).join(' ')).slice(0,500)}));
     serviceContext.push({key,control_count:group.length,checked_count:selected.length,
       selection_status:selected.length===1?'selected':selected.length?'ambiguous':'unselected',selected});
   }
 }
 for(const e of root.querySelectorAll('select')) {
   if(!visible(e)||e.disabled)continue;
   const text=label(e),rawKey=(e.id||e.name||text).replace(/_[a-z0-9]{5,}$/i,'');
   if(excluded.test(text+' '+rawKey)||modernServices&&e.closest('#collapseOne'))continue;
   const choices=Array.from(e.options).map(o=>({value:o.value,label:clean(o.textContent),disabled:o.disabled,
     placeholder:o.disabled||/^\s*(?:[-–—]+|select(?:\s|$)|please select|choose(?:\s|$))/i.test(o.textContent||'')}));
   if(choices.some(o=>/^(custom|other)(?:\s|$)/i.test(o.label)))unsupported.push('custom_values:'+rawKey);
   controls.push({kind:'select',raw_key:rawKey,label:text,selector:path(e),value:e.value,
     selected_label:clean(e.selectedOptions[0]?.textContent),choices:choices.filter(o=>!/^(custom|other)(?:\s|$)/i.test(o.label))});
 }
 const groups=new Map();
 const printographRollLabels=root.getAttribute('data-printproducttype')==='36';
 // Native generic-spec radios use name/id = declared spec + source index, but
 // Vue binds every sibling to the ONE declared userSelectedOptions[spec].
 // Read the current normal controller only; this is grouping, never price proof.
 let modernRadioSpecs=[];
 if(modernServices&&document.querySelectorAll('#order-form').length===1&&root.id==='order-form'&&root.getAttribute('data-currentstoretype')==='broker'){
   const proxies=[root._vnode?.component?.proxy,root.__vue_app__?._instance?.proxy].filter(Boolean);
   const cs=[...new Set(proxies.map(p=>p.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];
   if(cs.length===1){const c=cs[0],selected=c.jsonObj?.userSelectedOptions,others=c.jsonObj?.others,fields=c.fields?.[String(c.printProductType)];
     const own=(o,k)=>Object.hasOwn(o,k),empty=v=>v!==null&&typeof v==='object'&&!Array.isArray(v)&&[Object.prototype,null].includes(Object.getPrototypeOf(v))&&Object.keys(v).length===0&&Object.getOwnPropertySymbols(v).length===0;
     if(String(c.printProductType)===root.getAttribute('data-printproducttype')&&selected&&others&&
       own(others,'isAdmin')&&others.isAdmin===false&&own(others,'waiveFees')&&others.waiveFees===false&&own(others,'itemId')&&others.itemId==='null'&&own(others,'editOrderItem')&&empty(others.editOrderItem)&&
       ['shoppingCartItemId','priceGroupId'].every(k=>own(selected,k)&&['',null,undefined].includes(selected[k]))&&
       Array.isArray(fields?.specs)&&fields.specs.length<=30&&new Set(fields.specs).size===fields.specs.length&&
       fields.specs.every(k=>typeof k==='string'&&/^[A-Za-z][A-Za-z0-9]{0,63}$/.test(k)&&!/account|address|name|instructions|file|zip|country|billing|shipping|token|coupon|password/i.test(k)))modernRadioSpecs=fields.specs;
   }
 }
 const modernRadioKey=e=>{
   if(!modernServices||e.id!==e.name)return null;
   const matches=modernRadioSpecs.filter(k=>e.name.startsWith(k)&&/^(?:0|[1-9][0-9]{0,2})$/.test(e.name.slice(k.length)));
   if(matches.length!==1)return null;
   const spec=matches[0],siblings=[...root.querySelectorAll('input[type="radio"]')].filter(n=>n.id===n.name&&n.name.startsWith(spec)&&/^(?:0|[1-9][0-9]{0,2})$/.test(n.name.slice(spec.length)));
   if(siblings.length>100||new Set(siblings.map(n=>n.id)).size!==siblings.length||new Set(siblings.map(n=>n.value)).size!==siblings.length||siblings.some(n=>!/^[1-9][0-9]{0,11}$/.test(n.value)))return null;
   return spec;
 };
 for(const e of root.querySelectorAll('input[type=radio]')) {
   const associated=Array.from(e.labels||[]).find(visible);if((!visible(e)&&!associated)||e.disabled)continue;
   if(modernServices&&e.closest('#collapseOne'))continue;
   if(!e.name){unsupported.push('unnamed_radio');continue;}
   if(excluded.test(e.name+' '+label(e)))continue;
   // This source's Vue wind selector uses distinct native names despite being
   // one mutually exclusive product field. Keep the exception product-specific.
   const groupName=modernRadioKey(e)||(printographRollLabels&&/^windPosition\d+$/.test(e.name)?'windPosition':e.name);
   if(!groups.has(groupName))groups.set(groupName,[]);groups.get(groupName).push(e);
 }
 for(const [name,group] of groups){
   const checked=group.find(e=>e.checked),first=group[0];
   const groupLabel=clean(first.closest('fieldset')?.querySelector('legend')?.textContent || (printographRollLabels&&name==='windPosition'?'Wind position':name));
   if(modernServices&&group.filter(e=>e.checked).length>1)unsupported.push('ambiguous_modern_radio_selection:'+name);
   controls.push({kind:'radio',raw_key:name.replace(/_[a-z0-9]{5,}$/i,''),label:groupLabel,selector:path(first),
     value:checked?.value??null,selected_label:checked?label(checked):'',
     choices:group.map(e=>({value:e.value,label:label(e),selector:path(e),label_selector:Array.from(e.labels||[]).find(visible)?path(Array.from(e.labels||[]).find(visible)):null}))});
 }
 // Printograph renders this ordinary visible selector outside #order-form.
 const turnaround=Array.from(document.querySelectorAll('#radioBtn a.turnAround[data-toggle="turnaround"][data-title]')).filter(visible);
 if(turnaround.length){
   const active=turnaround.find(e=>e.classList.contains('active'));
   controls.push({kind:'button_group',raw_key:'productionTime',label:'Production time',selector:path(turnaround[0]),
     value:active?.getAttribute('data-title')??null,selected_label:clean(active?.textContent),
     choices:turnaround.map(e=>({value:e.getAttribute('data-title'),label:clean(e.textContent),selector:path(e),
       disabled:e.getAttribute('aria-disabled')==='true'||e.classList.contains('disabled')||/\bnot\s+available\b/i.test(clean(e.textContent))}))});
 }
 for(const e of root.querySelectorAll('input[type=number],input[type=text]')) {
   if(!visible(e)||e.disabled||e.readOnly)continue;
   const text=label(e),rawKey=(e.id||e.name||text).replace(/_[a-z0-9]{5,}$/i,'');
   if(excluded.test(text+' '+rawKey)||!/quantity|qty|run.?size|width|height|length|diameter|page.?count/i.test(text+' '+rawKey))continue;
   controls.push({kind:'number',raw_key:rawKey,label:text,selector:path(e),value:e.value,selected_label:e.value,
      choices:e.value?[{value:e.value,label:e.value}]:[],min:e.min,max:e.max,step:e.step});
 }
 for(const e of root.querySelectorAll('input[type=checkbox],[role=combobox]:not(select),[role=slider]')) {
   if(visible(e)&&!e.disabled&&!/terms|agree|newsletter|remember|proof|guideline/i.test(label(e)))unsupported.push('unhandled_control:'+label(e));
 }
 let price=null,basis=null;
 if(priceSelector){const el=Array.from(document.querySelectorAll(priceSelector)).find(visible);if(el){price=clean(el.value||el.textContent);basis='explicit_source_selector';}}
 if(!price){const sections=Array.from(document.querySelectorAll('.totalSection')).filter(visible);
   const subtotal=sections.find(e=>/subtotal/i.test(e.querySelector('.subTotal')?.textContent||''));
   if(subtotal){price=clean(subtotal.querySelector('.col-4')?.textContent);basis=clean(subtotal.querySelector('.subTotal')?.textContent);}}
 if(!price){const el=Array.from(document.querySelectorAll('[itemprop=price]')).find(visible);
   if(el){price=clean(el.getAttribute('content')||el.textContent);basis='source_display_price';}}
 const h1=Array.from(document.querySelectorAll('h1')).find(visible);
 const productTitle=clean(h1?.textContent).toLowerCase();
 const gallery=Array.from(document.querySelectorAll('#large-gallery-img img,.woocommerce-product-gallery img,[itemprop=image]'));
 const labelledImages=Array.from(root.querySelectorAll('img')).filter(e=>productTitle&&clean(e.alt).toLowerCase()===productTitle);
 const images=Array.from(new Set([...gallery,...labelledImages].map(e=>{
   for(const source of [e.currentSrc,e.getAttribute('src'),e.getAttribute('data-src'),e.getAttribute('data-original'),e.getAttribute('content')]){
     if(!source)continue;
     try {const resolved=new URL(source,location.href);if(/^https?:$/.test(resolved.protocol)&&!/(?:^|[._\/-])(?:loader|logo|spacer|placeholder)(?:[._\/-]|$)|\/icons?\//i.test(resolved.pathname))return resolved.href;}catch{}
   }
   return null;
 }).filter(Boolean)));
 const busy=Array.from(document.querySelectorAll('#ajaxLoader,[aria-busy=true]')).some(visible);
 const fullBodyText=clean(document.body?.innerText||'');
 const bodyText=fullBodyText.slice(0,1000);
 // Some catalog pages show public descriptions but replace their entire price
 // and purchase controls with a sign-in notice. They have no password field on
 // the product URL, so waiting for controls can only end in a false timeout.
 const productText=clean(document.querySelector('article')?.innerText||document.querySelector('main')?.innerText||fullBodyText).slice(0,4000);
 const priceLoginNotice=!price && /(?:please\s+)?(?:log|sign)\s+in\s+to\s+(?:see|view|access)\s+(?:the\s+)?(?:price|pricing|prices)|(?:price|pricing|prices)\s+(?:are\s+)?(?:available|visible)\s+(?:only\s+)?(?:after|when)\s+(?:you\s+)?(?:log|sign)\s+in/i.test(productText);
 return {title:clean(h1?.textContent||document.title),controls,unsupported,coverage_gaps:Array.from(coverageGaps),service_context:serviceContext,service_context_observed:serviceContextObserved,price,basis,images,busy,
   product_type:document.querySelector('#order-form')?.getAttribute('data-printproducttype')||null,
   product_slug:document.querySelector('#order-form')?.getAttribute('data-ptype')||null,
   store_type:document.querySelector('#order-form')?.getAttribute('data-currentstoretype')||null,
   currency:document.querySelector('[itemprop=priceCurrency]')?.content||null,
   challenge:/^authenticating$/i.test(document.title)||/verify you are human|checking your browser|access denied|just a moment/i.test(document.title+' '+bodyText),
   login:(!!document.querySelector('input[type=password]') && controls.length===0)||priceLoginNotice};
}"""


def normalize_snapshot(snapshot: dict) -> dict:
    # Preserve diagnostics independently of the finite visible product controls.
    snapshot["service_context"] = deepcopy(snapshot.get("service_context", []))
    seen = set()
    for control in snapshot["controls"]:
        raw = field_key(control["raw_key"])
        labelled = field_key(control["label"])
        control["key"] = (
            labelled
            if labelled
            in set(ALIASES.values())
            | {
                "size",
                "shape",
                "finish",
                "paper",
                "color",
                "orientation",
                "quantity",
                "coating",
            }
            else raw
        )
        if control["key"] in seen:
            raise AdapterError(
                "unsupported_controls",
                f"Ambiguous visible controls named {control['key']}",
            )
        seen.add(control["key"])
    return snapshot


def observed_amount(snapshot: dict) -> Decimal:
    text = str(snapshot.get("price") or "")
    if re.search(r"[-−–—]\s*(?:[$€£¥]|[A-Z]{3})?\s*\d", text, re.I) or re.search(
        r"\(\s*(?:[A-Z]{3}\s*|[$€£¥]\s*)?[\d.,]+\s*(?:[A-Z]{3})?\s*\)", text, re.I
    ):
        raise AdapterError(
            "price_unavailable",
            "Negative or accounting-style product totals are not positive source quotes",
        )
    matches = re.findall(
        r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.])", text
    )
    if len(matches) != 1:
        raise AdapterError(
            "price_unavailable",
            "No unambiguous displayed product subtotal; provide a source price selector",
        )
    amount = Decimal(matches[0].replace(",", ""))
    if amount <= 0:
        raise AdapterError(
            "price_unavailable", "The page has not produced a positive product quote"
        )
    return amount


def make_record(
    url: str, snapshot: dict, evidence_ids: list[str], partial: set[str], metadata: dict
) -> dict:
    controls = snapshot["controls"]
    amount = observed_amount(snapshot)
    q_control = next((c for c in controls if c["key"] == "quantity"), None)
    if not q_control:
        raise AdapterError("quantity_unavailable", "No visible source quantity control")
    quantity_text = str(q_control["value"]).replace(",", "")
    if not quantity_text.isdigit() or int(quantity_text) < 1:
        quantity_text = str(q_control["selected_label"]).replace(",", "").strip()
    if not quantity_text.isdigit() or int(quantity_text) < 1:
        raise AdapterError(
            "quantity_unavailable", "Source quantity is not an exact positive integer"
        )
    quantity = int(quantity_text)
    currency = snapshot.get("currency") or metadata.get("currency")
    if (
        not currency
        and urlparse(url).hostname in {"printograph.com", "www.printograph.com"}
        and "$" in str(snapshot["price"])
    ):
        currency = "USD"
    if not currency:
        raise AdapterError("currency_required", "Supply the source currency explicitly")
    production = next((c for c in controls if c["key"] == "productionTime"), None)
    configuration = {
        c["key"]: {"value": c["value"], "label": c["selected_label"]}
        for c in controls
        if c["key"] not in {"quantity", "productionTime"}
    }
    row = {
        "record_schema_version": 2,
        "product_url": url,
        "title": snapshot["title"],
        "source_product_id": str(
            snapshot.get("product_type") or metadata.get("product_type") or ""
        ),
        "configuration": configuration,
        "quantity": quantity,
        "currency": str(currency).upper(),
        "subtotal": format(amount, "f"),
        "unit_price": unit_price(amount, quantity),
        "provenance": "direct_quote",
        "verification_status": "not_selected",
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "production": (
            {
                "value": production["value"],
                "label": production["selected_label"],
                "status": "source_displayed",
            }
            if production
            else {"status": "not_quoted"}
        ),
        "images": snapshot["images"],
        "coverage_status": "partial" if partial else "enumeration_pending",
        "coverage_reasons": sorted(partial),
        "pricing_scope": snapshot.get("basis") or "displayed_price",
        "service_context": deepcopy(snapshot.get("service_context", [])),
        "service_context_observed": snapshot.get("service_context_observed") is True,
        "pricing_context": {
            "schema_version": 2,
            "service_context": canonical_service_context(snapshot),
            "store_type": snapshot.get("store_type"),
            "source_product_type": snapshot.get("product_type"),
            "product_slug": snapshot.get("product_slug"),
        },
    }
    if snapshot.get("service_context_schema") == MODERN_SERVICE_SCHEMA:
        freshness = snapshot.get("modern_native_freshness")
        services = canonical_service_context(snapshot)
        if not modern_bound(services) or not isinstance(freshness,dict) or freshness.get("schema") != "printograph-native-dom-quote-freshness/v1":
            raise AdapterError("modern_quote_unbound", "Native services or same-selection price/production evidence is unavailable")
        row["record_schema_version"] = row["pricing_context"]["schema_version"] = 3
        row["service_context_schema"] = MODERN_SERVICE_SCHEMA
        row["modern_service_observation"] = deepcopy(snapshot["modern_service_observation"])
        row["pricing_context"]["native_freshness"] = deepcopy(freshness)
        row["source_data_finalized"] = row["woocommerce_import_ready"] = False
    return row


def pricing_table_observation(url, observed):
    """Count captured entries without treating them as validated output rows."""
    path = urlparse(url).path
    items = observed.data.get("items") if isinstance(observed.data, dict) else None
    if not (
        200 <= observed.status < 300
        and path.rstrip("/").endswith("/prices")
        and isinstance(items, list)
        and len(items) > 1
    ):
        return None
    return {
        "evidence_id": observed.response_id,
        "url_path": path,
        "tier_count": len(items),
        "count_basis": "source_table_entries",
        "published_rows": 0,
        "verification_status": "unverified",
    }


class BrowserAdapter:
    name = "browser"
    capabilities = {
        "ordinary_browser": True,
        "native_selects": True,
        "radio_groups": True,
        "dependent_controls": True,
        "source_response_capture": True,
        "direct_displayed_quotes": True,
        "cart_verification": False,
        "shipping_quotes": False,
        "bulk_tier_expansion_verified": False,
        "bulk_quantity_candidates": "opt_in_supported_printograph_presets",
    }

    async def extract(self, spec, transport, emit):
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeout
        except ImportError as exc:
            raise AdapterError(
                "browser_unavailable", "Install Playwright and its Chromium browser"
            ) from exc
        from crawler.transport import validate_url
        from crawler.browser_session import browser_page

        url = spec["url"]
        metadata = spec.get("source_metadata") or {}
        modern_requested = metadata.get("printograph_modern_services") is True
        modern_policy = metadata.get("printograph_modern_service_policy")
        try:
            if modern_policy is not None and not modern_requested: raise ModernHeld("modern_service_policy_scope_unproven")
            validate_service_policy(modern_policy, url)
        except ModernHeld as exc:
            raise AdapterError("invalid_modern_scope", exc.reason) from None
        await validate_url(url)
        source_origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        filters = {
            field_key(k): v for k, v in spec.get("selection_filters", {}).items()
        }
        limit = spec.get("max_configurations")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise AdapterError(
                "invalid_selection", "max_configurations must be a positive integer"
            )
        maximum_requests = spec.get("max_requests")
        bulk_requested = spec.get("browser_bulk_quantities") is True
        if modern_requested and (urlparse(url).hostname not in {"www.printograph.com","printograph.com"} or bulk_requested):
            raise AdapterError("invalid_modern_scope", "Modern native services require a Printograph ordinary browser walk")
        inactivity_seconds = max(
            1.0, float(os.getenv("ATLAS_BROWSER_INACTIVITY_SECONDS", "30"))
        )
        headless = os.getenv("ATLAS_BROWSER_HEADLESS", "true").lower() not in {
            "false",
            "0",
            "no",
        }
        await emit("capabilities", self.capabilities)
        shipping_requested = bool(spec.get("shipping_contexts"))
        if shipping_requested:
            await emit(
                "shipping_unavailable",
                {
                    "status": "unavailable",
                    "reason": "No browser shipping calculator contract is configured",
                },
            )
        public_profile = {}
        if hasattr(transport.store, "profiles"):
            public_profile = next(
                (
                    p
                    for p in transport.store.profiles("source", secrets=False)
                    if p.get("host", "").lower() == urlparse(url).hostname
                ),
                {},
            )
        try:
            rps_limit, concurrency_limit = source_pacing(
                public_profile, transport.store.config
            )
        except ValueError as exc:
            raise AdapterError("invalid_source_pacing", str(exc)) from exc
        async with browser_slot(
            transport.check,
            source_origin=source_origin,
            per_source_limit=concurrency_limit,
        ):
            await transport.check()
            async with browser_page(
                transport, source_origin, headless=headless
            ) as page:
                page.set_default_timeout(30_000)
                responses = deque(maxlen=64)
                fatal = []
                from ..browser_progress import CoverageNotes

                partial = CoverageNotes()
                progress_journal = getattr(transport, "browser_progress", None)
                resumed_branches = 0
                active_requests = 0
                request_count = 0
                observed_controls = set()
                observed_inactive_finishes = {}
                observed_finishing_quantities = {}
                current_selection = {}
                selection_epoch = 0
                table_captures = deque(maxlen=256)
                # Product-wide specifications are fetched once near navigation.
                # Keep a small separate window so a long option walk cannot evict
                # the evidence needed to bind square presets without a UI selector.
                specification_captures = deque(maxlen=8)
                verified_origins = {source_origin}
                navigation = {"generation": 0, "bootstrap": True, "in_progress": True}
                capture_warnings = set()
                bulk_fallback_reasons = set()
                branch_diagnostics = {}
                restore_diagnostics = {}
                route_tasks = set()
                closing = False
                source_progress = {"sequence": 0, "events": deque(maxlen=512)}
                settled_snapshot = SettledSnapshot()
                modern_gate = FreshQuoteGate(service_policy=modern_policy, source_url=url)
                modern_recoveries = set()
                modern_header_diagnostics = set()

                async def modern_read():
                    await transport.check()
                    result = bind_service_policy(await page.evaluate(SERVICE_READ_JS, {"source_url": url}), modern_policy, url)
                    diagnostic = result.get("process_header") if isinstance(result, dict) else None
                    if result.get("reason") == "modern_process_header_unproven" and isinstance(diagnostic, dict):
                        key = (navigation["generation"], json.dumps(diagnostic, sort_keys=True))
                        if key not in modern_header_diagnostics and len(modern_header_diagnostics) < 32:
                            modern_header_diagnostics.add(key)
                            await emit("modern_process_header_diagnostic", dict(diagnostic))
                    return result

                async def modern_mark_action():
                    nonlocal selection_epoch
                    await transport.check()
                    selection_epoch += 1


                def navigated(frame):
                    nonlocal modern_gate
                    if frame == page.main_frame:
                        navigation["generation"] += 1
                        modern_gate = FreshQuoteGate(service_policy=modern_policy, source_url=url)
                        responses.clear()
                        table_captures.clear()
                        specification_captures.clear()
                        observed_inactive_finishes.clear()
                        observed_finishing_quantities.clear()

                def bulk_captures():
                    captures = {
                        capture["evidence_id"]: capture
                        for capture in (*specification_captures, *table_captures)
                    }
                    return list(captures.values())

                page.on("framenavigated", navigated)

                async def capture_warning(code):
                    if code not in capture_warnings:
                        capture_warnings.add(code)
                        await emit(
                            "capture_warning",
                            {
                                "code": code,
                                "message": "A navigation request was cancelled or its network body was unavailable; visible quote evidence is recorded separately.",
                            },
                        )

                completions = {}

                def request_done(request):
                    event = completions.get(request)
                    if event:
                        event.set()

                page.on("requestfinished", request_done)
                page.on("requestfailed", request_done)
                counters = {"rows": 0, "invalid_branches": 0}
                api_path = re.compile(
                    r"/service/rest/v1/(?:products?(?:/|[?]|$)|settings/product/|rules/turnarounds)|[?&]wc-ajax=get_variation"
                )

                async def route_request_inner(route):
                    nonlocal active_requests, request_count
                    task = asyncio.current_task()
                    route_tasks.add(task)
                    request = route.request
                    parsed = urlparse(request.url)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    specification_query = parse_qsl(
                        parsed.query, keep_blank_values=True
                    )
                    specification_request = (
                        request.method == "GET"
                        and origin == source_origin
                        and parsed.path
                        == "/service/rest/v1/settings/product/specifications"
                        and len(specification_query) == 2
                        and dict(specification_query)
                        == {"requestValues": "true", "productType": "36"}
                        and not parsed.fragment
                    )
                    token = None
                    counted = False
                    started = time.monotonic()
                    status = 0
                    retry_after = 0
                    generation = navigation["generation"]
                    epoch = selection_epoch
                    requested_selection = dict(current_selection)
                    request_state = None
                    modern_before = None
                    guarded = request.resource_type in {"document", "xhr", "fetch"}
                    main_document = (
                        request.resource_type == "document"
                        and request.frame == page.main_frame
                    )
                    quote_request = main_document or (
                        origin == source_origin and bool(api_path.search(request.url))
                    )
                    response = None

                    def navigation_cancelled(reason):
                        return is_navigation_cancellation(
                            reason,
                            generation,
                            navigation["generation"],
                            bootstrap=navigation["bootstrap"],
                            navigating=navigation["in_progress"],
                        )

                    try:
                        if closing:
                            await route.abort()
                            return
                        if headless and request.resource_type in {
                            "image",
                            "media",
                            "font",
                        }:
                            await route.abort()
                            return
                        if parsed.scheme not in {"http", "https"}:
                            await route.continue_()
                            return
                        if origin not in verified_origins:
                            await validate_url(request.url)
                            verified_origins.add(origin)
                        if not guarded:
                            await route.continue_()
                            return
                        await transport.check()
                        if modern_requested and origin == source_origin and api_path.search(request.url):
                            # Read safe product/service scalars before the real
                            # native request; no callback, request replay or body write.
                            modern_before = await modern_read()
                        if bulk_requested and api_path.search(request.url):
                            try:
                                request_state = normalize_snapshot(
                                    await page.evaluate(
                                        SNAPSHOT_JS,
                                        {
                                            "priceSelector": metadata.get(
                                                "price_selector"
                                            )
                                        },
                                    )
                                )
                            except Exception:
                                # A missing/transitional document cannot bind a
                                # bulk table; the ordinary request still runs.
                                request_state = None
                        if (
                            maximum_requests is not None
                            and request_count >= maximum_requests
                        ):
                            raise AdapterError(
                                "request_limit",
                                "Explicit source request ceiling reached",
                            )
                        if request.method not in {"GET", "HEAD"} and (
                            re.search(r"/(?:cart|checkout|orders)(?:/|$)", parsed.path)
                            or re.search(
                                r"[?&]wc-ajax=(?:add_to_cart|checkout|remove_from_cart)(?:&|$)",
                                request.url,
                            )
                        ):
                            raise AdapterError(
                                "mutation_blocked",
                                "The browser attempted a cart/order mutation",
                            )
                        request_count += 1
                        if hasattr(transport, "_count"):
                            await asyncio.to_thread(transport._count, False)
                        if quote_request:
                            active_requests += 1
                            counted = True
                        token = await transport.limiter.acquire(
                            origin, rps_limit, concurrency_limit, transport.check
                        )
                        started = time.monotonic()
                        # Continue the real Chromium request. route.fetch() would
                        # issue another request through Node's separate TLS stack.
                        if not quote_request:
                            # Rate-gate auxiliary traffic, but do not make a
                            # long-lived account/telemetry poll block a quote.
                            await route.continue_()
                            return
                        complete = completions[request] = asyncio.Event()
                        await route.continue_()
                        async with asyncio.timeout(30):
                            response = await request.response()
                            if response is None:
                                reason = (
                                    request.failure
                                    or "Request ended without a response"
                                )
                                if navigation_cancelled(reason):
                                    await capture_warning(
                                        "navigation_request_cancelled"
                                    )
                                    return
                                if re.search(
                                    r"certificate|cert_authority|ssl", reason, re.I
                                ):
                                    raise AdapterError(
                                        "tls_trust_error",
                                        "Chromium could not verify the source TLS certificate; configure its trusted CA chain without disabling verification",
                                    )
                                raise AdapterError(
                                    "browser_network_error",
                                    f"Chromium request failed: {reason[:200]}",
                                )
                            status = response.status
                            headers = response.headers
                            size_limit = getattr(
                                transport.store.config,
                                "max_response_bytes",
                                16 * 1024 * 1024,
                            )
                            declared_size = headers.get("content-length", "")
                            if (
                                declared_size.isdigit()
                                and int(declared_size) > size_limit
                            ):
                                raise AdapterError(
                                    "response_too_large",
                                    "Browser source response exceeds its configured byte ceiling",
                                )
                            # Explicit request completion avoids a leaked
                            # target-closed task in this Playwright version's
                            # Response.finished(), while retaining the permit.
                            await complete.wait()
                            if request.failure:
                                if navigation_cancelled(request.failure):
                                    await capture_warning(
                                        "navigation_request_cancelled"
                                    )
                                    return
                                raise AdapterError(
                                    "browser_network_error",
                                    f"Chromium request failed: {request.failure[:200]}",
                                )
                        if (
                            origin == source_origin
                            and 200 <= status < 400
                            and (
                                api_path.search(request.url)
                                or request.resource_type == "document"
                            )
                        ):
                            source_progress["sequence"] += 1
                            source_progress["events"].append(
                                (
                                    source_progress["sequence"],
                                    (request.method, request.url),
                                )
                            )
                        retry_after = (
                            retry_after_seconds(headers.get("retry-after"))
                            if status in {429, 503}
                            else 0
                        )
                        if (
                            api_path.search(request.url)
                            and "json" in headers.get("content-type", "")
                            and generation == navigation["generation"]
                        ):
                            try:
                                body = await response.body()
                            except Exception as body_error:
                                if re.search(
                                    r"No resource with given identifier|No data found for resource|body.*evicted|Response body is unavailable",
                                    str(body_error),
                                    re.I,
                                ):
                                    body = None
                                    await capture_warning("network_body_unavailable")
                                else:
                                    raise
                            if body is not None:
                                if len(body) > size_limit:
                                    raise AdapterError(
                                        "response_too_large",
                                        "Browser source response exceeds its configured byte ceiling",
                                    )
                                observed = await transport.record_observation(
                                    request.url,
                                    status,
                                    {
                                        k: headers[k]
                                        for k in (
                                            "content-type",
                                            "etag",
                                            "last-modified",
                                        )
                                        if k in headers
                                    },
                                    body,
                                    context={
                                        "lane": "browser_source",
                                        "requested_selection": requested_selection,
                                        "navigation_generation": generation,
                                        "selection_epoch": epoch,
                                        "request_service_context": canonical_service_context(
                                            request_state or {}
                                        ),
                                        "request_controls": (
                                            {
                                                c["key"]: c["value"]
                                                for c in request_state["controls"]
                                            }
                                            if request_state
                                            else None
                                        ),
                                    },
                                    count_request=False,
                                )
                                responses.append(observed.response_id)
                                if modern_requested and origin == source_origin:
                                    modern_gate.observe(request.url,status,observed.data,
                                        modern_before,await modern_read(),observed.response_id,
                                        generation,navigation["generation"],epoch,selection_epoch,request.method,
                                        request.post_data if parsed.path in {"/service/rest/v1/rules/turnarounds", "/service/rest/v1/product/prices"} and modern_before and modern_before.get("observed") is True else None)

                                if bulk_requested and (
                                    (request_state and epoch == selection_epoch)
                                    or specification_request
                                ):
                                    capture_state = request_state or {}
                                    capture = {
                                        "url": request.url,
                                        "data": observed.data,
                                        "evidence_id": observed.response_id,
                                        "status": status,
                                        "navigation_generation": generation,
                                        "selection_epoch": epoch,
                                        "request_controls": {
                                            c["key"]: c["value"]
                                            for c in capture_state.get("controls", [])
                                        },
                                        "request_store_type": capture_state.get(
                                            "store_type"
                                        ),
                                        "request_product_type": capture_state.get(
                                            "product_type"
                                        ),
                                        "request_service_context": canonical_service_context(
                                            capture_state
                                        ),
                                    }
                                    table_captures.append(capture)
                                    # Product-wide dimensions may arrive before
                                    # the form exists. Keep unknown metadata as
                                    # unknown; the binder rejects explicit conflicts.
                                    if specification_request:
                                        specification_captures.append(capture)
                                table_event = pricing_table_observation(
                                    request.url, observed
                                )
                                if table_event:
                                    await emit("pricing_table_captured", table_event)
                        if status >= 400 and (
                            api_path.search(request.url)
                            or request.resource_type == "document"
                        ):
                            if (
                                generation != navigation["generation"]
                                and request.resource_type != "document"
                            ):
                                await capture_warning("stale_navigation_response")
                                return
                            code = (
                                "access_required"
                                if status in {401, 403}
                                else (
                                    "rate_limited"
                                    if status == 429
                                    else (
                                        "source_unavailable"
                                        if status in {500, 502, 503, 504}
                                        else "source_error"
                                    )
                                )
                            )
                            await emit(
                                "browser_request_error",
                                {
                                    "url_path": parsed.path,
                                    "resource_type": request.resource_type,
                                    "status": status,
                                    "code": code,
                                    "navigation_generation": generation,
                                    "current_generation": navigation["generation"],
                                },
                            )
                            fatal.append(
                                AdapterError(
                                    code,
                                    f"Source {request.resource_type} {parsed.path} returned HTTP {status}; refusing a potentially stale displayed quote",
                                    url_path=parsed.path,
                                    resource_type=request.resource_type,
                                    status=status,
                                )
                            )
                    except Exception as exc:
                        if (
                            not main_document and generation != navigation["generation"]
                        ) or (
                            main_document and navigation["generation"] > generation + 1
                        ):
                            await capture_warning("stale_navigation_request")
                            return
                        if navigation_cancelled(str(exc)):
                            await capture_warning("navigation_request_cancelled")
                            return
                        await emit(
                            "browser_request_error",
                            {
                                "url_path": parsed.path,
                                "resource_type": request.resource_type,
                                "status": status,
                                "code": getattr(exc, "code", type(exc).__name__),
                                "navigation_generation": generation,
                                "current_generation": navigation["generation"],
                            },
                        )
                        if getattr(exc, "code", None):
                            fatal.append(exc)
                        else:
                            # Playwright call logs may include request headers;
                            # retain the reason, never those browser credentials.
                            reason = (
                                str(exc).split("\nCall log:", 1)[0].splitlines()
                                or [type(exc).__name__]
                            )[0]
                            if re.search(
                                r"certificate|cert_authority|unable to verify|self.signed",
                                reason,
                                re.I,
                            ):
                                fatal.append(
                                    AdapterError(
                                        "tls_trust_error",
                                        "Chromium could not verify the source TLS certificate; configure its trusted CA chain without disabling verification",
                                    )
                                )
                            else:
                                fatal.append(
                                    AdapterError(
                                        "browser_network_error",
                                        f"Source {request.resource_type} {parsed.path} failed: {reason[:250]}",
                                        url_path=parsed.path,
                                        resource_type=request.resource_type,
                                    )
                                )
                        try:
                            await route.abort()
                        except Exception:
                            pass
                    finally:
                        completions.pop(request, None)
                        if counted:
                            active_requests -= 1
                        try:
                            if token:
                                await asyncio.to_thread(
                                    transport.limiter.release,
                                    origin,
                                    token,
                                    time.monotonic() - started,
                                    status,
                                    retry_after,
                                    rps_limit,
                                )
                        finally:
                            route_tasks.discard(task)

                async def route_request(route):
                    try:
                        await route_request_inner(route)
                    except RunInterrupted as exc:
                        # Playwright stores exceptions escaping event listeners
                        # and rethrows them in the next context API call, even
                        # one made by an independent DOMAIN cookie reader. A
                        # stopped product must terminate only its own route.
                        fatal.append(exc)
                        try:
                            await route.abort()
                        except Exception:
                            pass

                await page.route("**/*", route_request)

                async def navigate():
                    nonlocal selection_epoch
                    selection_epoch += 1
                    navigation["in_progress"] = True
                    try:
                        await page.goto(
                            url, wait_until="domcontentloaded", timeout=30_000
                        )
                    except Exception as exc:
                        if fatal:
                            raise fatal[0] from exc
                        reason = (
                            str(exc).split("\nCall log:", 1)[0].splitlines()
                            or [type(exc).__name__]
                        )[0]
                        raise AdapterError(
                            "browser_navigation_failed",
                            f"Source navigation failed: {reason[:350]}",
                        ) from exc

                def finishing_base_context(current):
                    values = {
                        c["key"]: str(c.get("value")) for c in current["controls"]
                    }
                    return tuple(
                        values.get(key)
                        for key in ("size", "paper", "color", "orientation")
                    )

                def finishing_recovery_context(current):
                    values = {
                        c["key"]: str(c.get("value")) for c in current["controls"]
                    }
                    return (
                        *finishing_base_context(current),
                        *(
                            values.get(key)
                            for key in ("roundedCorners", "coating", "productionTime")
                        ),
                    )

                def selected_positive_quantity(current):
                    quantity = next(
                        (c for c in current["controls"] if c["key"] == "quantity"), None
                    )
                    if quantity is None or not filter_choices(
                        quantity, quantity.get("value")
                    ):
                        return None
                    try:
                        amount = Decimal(str(quantity["value"]).replace(",", ""))
                    except InvalidOperation:
                        return None
                    return (
                        str(quantity["value"])
                        if amount.is_finite() and amount > 0
                        else None
                    )

                def remember_finishing_domain(state):
                    if str(state.get("product_type")) not in {"1", "27"}:
                        return
                    recovery_context = finishing_recovery_context(state)
                    selected_quantity = selected_positive_quantity(state)
                    for control in state["controls"]:
                        if control["key"] not in {"raisedprint", "spotcoating"}:
                            continue
                        choices = filter_choices(control)
                        if selected_quantity:
                            for choice in choices:
                                witness = (
                                    recovery_context,
                                    control["key"],
                                    str(choice["value"]),
                                )
                                quantities = observed_finishing_quantities.setdefault(
                                    witness, []
                                )
                                if selected_quantity not in quantities:
                                    quantities.append(selected_quantity)
                                    del quantities[:-8]
                            while len(observed_finishing_quantities) > 128:
                                del observed_finishing_quantities[
                                    next(iter(observed_finishing_quantities))
                                ]
                        if (
                            control["kind"] == "select"
                            and control.get("value") == ""
                            and control.get("selected_label") == "None"
                            and len(
                                [item for item in choices if item.get("value") == ""]
                            )
                            == 1
                            and any(
                                item.get("value") == "" and item.get("label") == "None"
                                for item in choices
                            )
                        ):
                            observed_inactive_finishes[control["key"]] = (
                                recovery_context
                            )
                        else:
                            observed_inactive_finishes.pop(control["key"], None)

                async def snapshot(deadline=None, progress=None):
                    deadline = deadline or InactivityDeadline(inactivity_seconds)
                    while True:
                        if progress:
                            progress()
                        await transport.check()
                        if fatal:
                            raise fatal[0]
                        try:
                            state = await page.evaluate(
                                SNAPSHOT_JS,
                                {"priceSelector": metadata.get("price_selector"), "modernServices": modern_requested},
                            )
                            break
                        except Exception as exc:
                            if not re.search(
                                r"execution context was destroyed|cannot find context|most likely because of a navigation",
                                str(exc),
                                re.I,
                            ):
                                raise
                            if deadline.expired:
                                raise AdapterError(
                                    "source_timeout",
                                    "Source kept navigating before product controls could be read",
                                ) from exc
                            await asyncio.sleep(0.2)
                    state = normalize_snapshot(state)
                    if modern_requested:
                        state["service_context_schema"] = MODERN_SERVICE_SCHEMA
                        state["modern_service_observation"] = await modern_read()
                    observed_controls.update(c["key"] for c in state["controls"])
                    partial.observe_snapshot(state)
                    if "network_body_unavailable" in capture_warnings:
                        partial.add(
                            "some_network_bodies_unavailable_dom_evidence_preserved"
                        )
                    for c in state["controls"]:
                        if c["kind"] == "number" and c["key"] not in filters:
                            partial.add("unbounded_numeric_control:" + c["key"])
                    return state

                async def settle(require_price=False, auth_grace=0, allow_reuse=True):
                    deadline = InactivityDeadline(inactivity_seconds)
                    sequence = source_progress["sequence"]

                    def progress():
                        nonlocal sequence
                        for index, key in source_progress["events"]:
                            if index > sequence:
                                deadline.observe(("request", *key))
                        sequence = source_progress["sequence"]

                    auth_until = time.monotonic() + auth_grace
                    previous = None
                    stable = 0
                    while True:
                        progress()
                        if deadline.expired:
                            break
                        state = await snapshot(deadline, progress)
                        if state["challenge"] or state["login"]:
                            if time.monotonic() < auth_until:
                                await asyncio.sleep(0.35)
                                continue
                            raise AdapterError(
                                "access_required",
                                "Complete normal source authentication in a headed browser profile; no challenge bypass is attempted",
                            )
                        signature = json.dumps(
                            {
                                "controls": state["controls"],
                                "price": state["price"],
                                "service_context": canonical_service_context(state),
                            },
                            sort_keys=True,
                        )
                        deadline.observe(("dom", signature))
                        stable = (
                            stable + 1
                            if signature == previous
                            and not active_requests
                            and not state["busy"]
                            else 0
                        )
                        previous = signature
                        actionable = any(
                            filter_choices(control)
                            or (
                                control["kind"] == "number"
                                and str(control.get("value", "")).strip()
                            )
                            for control in state["controls"]
                        )
                        discrete_domains_ready = all(
                            control["kind"] == "number" or filter_choices(control)
                            for control in state["controls"]
                        )
                        stability_token = (
                            navigation["generation"],
                            selection_epoch,
                            request_count,
                            source_progress["sequence"],
                        )
                        # DFS checkpoint/restore calls often follow a completed
                        # settle with no intervening action. Re-read the DOM, but
                        # do not spend another 700 ms proving the same state.
                        # Source activity, actions, navigation and any transient
                        # DOM/busy state invalidate this short certification.
                        reusable = settled_snapshot.matches(
                            signature,
                            stability_token,
                            idle=(
                                not active_requests
                                and not state["busy"]
                                and supports_stable_reuse(page.url, state)
                            ),
                        )
                        if (
                            (
                                stable >= 2
                                or (reusable and allow_reuse and not require_price)
                            )
                            and actionable
                            and discrete_domains_ready
                        ):
                            if require_price:
                                observed_amount(state)
                            if stable >= 2:
                                settled_snapshot.remember(signature, stability_token)
                            # Only stable effective controls establish recovery
                            # witnesses; transient dependency updates do not.
                            remember_finishing_domain(state)
                            navigation["in_progress"] = False
                            return state
                        await asyncio.sleep(0.35)
                    raise AdapterError(
                        "source_timeout",
                        f"Product controls or quote stopped making progress for {inactivity_seconds:g} seconds",
                    )

                async def choose(control, choice):
                    nonlocal selection_epoch
                    await transport.check()
                    if control["key"] in {
                        "shape",
                        "orientation",
                        "size",
                        "paper",
                        "color",
                        "finish",
                    } and str(control.get("value")) != str(choice["value"]):
                        current = await snapshot()
                        production = next(
                            (
                                c
                                for c in current["controls"]
                                if c["key"] == "productionTime"
                            ),
                            None,
                        )
                        regular = (
                            filter_choices(production, "1")
                            if production is not None
                            else []
                        )
                        if (
                            str(current.get("product_type")) in {"1", "27", "36"}
                            and production is not None
                            and str(production.get("value")) == "3"
                            and regular
                        ):
                            # A completed descendant Rush branch can remain
                            # selected when DFS changes its parent stock/size.
                            # Use the currently offered Regular control first;
                            # otherwise the page can request an invalid transient
                            # base+Rush combination before it refreshes the domain.
                            # This temporary state is never itself published.
                            desired_key, desired_value = control["key"], choice["value"]
                            current = await choose(production, regular[0])
                            if not any(
                                c["key"] == "productionTime"
                                and str(c.get("value")) == "1"
                                for c in current["controls"]
                            ):
                                raise AdapterError(
                                    "selection_reset",
                                    "The source did not retain observed Regular production before a base selection change",
                                )
                            control = next(
                                (
                                    c
                                    for c in current["controls"]
                                    if c["key"] == desired_key
                                ),
                                None,
                            )
                            available = (
                                filter_choices(control, desired_value)
                                if control
                                else []
                            )
                            if not available:
                                raise AdapterError(
                                    "selection_reset",
                                    "The requested base selection became unavailable after returning to observed Regular production",
                                    key=public_control_text(desired_key),
                                    choice_value=public_control_text(desired_value),
                                )
                            choice = available[0]
                            if str(control.get("value")) == str(desired_value):
                                return current
                    selection_epoch += 1
                    current_selection[control["key"]] = choice["value"]
                    locator = page.locator(
                        choice.get("selector") or control["selector"]
                    )
                    action_generation = navigation["generation"]
                    try:
                        if control["kind"] == "select":
                            await locator.select_option(str(choice["value"]))
                        elif control["kind"] == "radio":
                            # Styled radios can be visible with opacity zero but
                            # covered by their associated label. Use that normal
                            # label action when available; never force a click.
                            if choice.get("label_selector"):
                                await page.locator(choice["label_selector"]).click()
                            elif await locator.is_visible():
                                await locator.check()
                            else:
                                raise AdapterError(
                                    "unsupported_controls",
                                    "Radio control has no visible native input or label",
                                )
                        elif control["kind"] == "button_group":
                            await locator.click()
                        else:
                            await locator.fill(str(choice["value"]))
                            await locator.press("Tab")
                    except PlaywrightTimeout as exc:
                        if fatal:
                            raise fatal[0] from exc
                        details = {
                            "kind": public_control_text(control["kind"]),
                            "key": public_control_text(control["key"]),
                            "label": public_control_text(control["label"]),
                            "choice_value": public_control_text(choice["value"]),
                            "choice_label": public_control_text(choice.get("label")),
                            "active_source_requests": active_requests,
                        }
                        # Diagnose the live replacement control without exposing a
                        # Playwright call log, URL query, or unrelated page fields.
                        latest = None
                        try:
                            latest = normalize_snapshot(
                                await asyncio.wait_for(
                                    page.evaluate(
                                        SNAPSHOT_JS,
                                        {
                                            "priceSelector": metadata.get(
                                                "price_selector"
                                            )
                                        },
                                    ),
                                    timeout=2,
                                )
                            )
                            live = next(
                                (
                                    c
                                    for c in latest["controls"]
                                    if c["key"] == control["key"]
                                ),
                                None,
                            )
                            live_choice = (
                                next(iter(filter_choices(live, choice["value"])), None)
                                if live
                                else None
                            )
                            details.update(
                                control_present=live is not None,
                                requested_choice_available=live_choice is not None,
                                page_busy=bool(latest["busy"]),
                            )
                            if live:
                                details["selected_value"] = public_control_text(
                                    live.get("value")
                                )
                                details["selector_changed"] = (
                                    (live_choice or {}).get("selector")
                                    or live["selector"]
                                ) != (choice.get("selector") or control["selector"])
                        except Exception:
                            details["control_snapshot_available"] = False
                        if (
                            control["kind"] == "radio"
                            and control["key"] == "windPosition"
                            and latest is not None
                            and str(latest.get("product_type")) == "36"
                            and not latest.get("challenge")
                            and not latest.get("login")
                            and navigation["generation"] == action_generation
                            and details.get("selected_value") == str(choice["value"])
                            and details.get("requested_choice_available")
                            and not fatal
                        ):
                            # Some native label actions take effect while their
                            # Playwright acknowledgment times out. For this known
                            # Roll control only, let normal settling and downstream
                            # quote/bulk checks validate the effective selection.
                            # The diagnostic snapshot is never a quote or evidence
                            # of a fresh price response.
                            try:
                                recovered = await settle()
                            except AdapterError:
                                if fatal:
                                    raise fatal[0] from exc
                                recovered = None
                            live_wind = next(
                                (
                                    c
                                    for c in (recovered or {}).get("controls", [])
                                    if c["key"] == "windPosition"
                                    and c["kind"] == "radio"
                                ),
                                None,
                            )
                            if (
                                recovered is not None
                                and str(recovered.get("product_type")) == "36"
                                and not recovered.get("challenge")
                                and not recovered.get("login")
                                and not fatal
                                and navigation["generation"] == action_generation
                                and live_wind is not None
                                and str(live_wind.get("value")) == str(choice["value"])
                                and any(
                                    str(item["value"]) == str(choice["value"])
                                    for item in filter_choices(live_wind)
                                )
                            ):
                                await emit(
                                    "action_recovered",
                                    {
                                        "reason": "native_action_acknowledgment_timeout",
                                        "key": "windPosition",
                                        "choice_value": public_control_text(
                                            choice["value"]
                                        ),
                                        "published_rows": 0,
                                        "quote_validation": "pending_normal_extraction",
                                    },
                                )
                                return recovered
                        if fatal:
                            raise fatal[0] from exc
                        await emit("browser_action_error", details)
                        raise AdapterError(
                            "source_action_timeout",
                            f"Timed out selecting {details['label']} ({details['key']}, {details['kind']}): "
                            f"{details['choice_label']} [value {details['choice_value']}]; completed records are retained",
                            **details,
                        ) from exc
                    return await settle()

                def public_configuration(values):
                    return {
                        public_control_text(key): public_control_text(value)
                        for key, value in list(values.items())[:20]
                    }

                async def diagnostic(kind, totals, reason, key, details):
                    group = (reason, key)
                    if group not in totals and len(totals) >= 20:
                        group = ("other", "other")
                    totals[group] = totals.get(group, 0) + 1
                    count = totals[group]
                    # First and powers of two preserve useful progress without
                    # one database event for every failed quantity restoration.
                    if count & (count - 1) == 0:
                        await emit(
                            kind,
                            {
                                **details,
                                "reason": group[0],
                                "key": public_control_text(group[1]),
                                "occurrences": count,
                                "navigation_generation": navigation["generation"],
                            },
                        )

                async def skipped_branch(
                    reason, control, choice=None, *, prefix=None, state=None
                ):
                    await diagnostic(
                        "branch_skipped",
                        branch_diagnostics,
                        reason,
                        control["key"],
                        {
                            "reason": reason,
                            "key": public_control_text(control["key"]),
                            "label": public_control_text(control["label"]),
                            "available_choices": len(filter_choices(control)),
                            "prefix": public_configuration(prefix or {}),
                            "effective_configuration": public_configuration(
                                {
                                    c["key"]: c["value"]
                                    for c in (state or {}).get("controls", [])
                                }
                            ),
                            **(
                                {"choice_value": public_control_text(choice["value"])}
                                if choice
                                else {}
                            ),
                        },
                    )

                async def restore(prefix, reloaded=False, choice_hint=None):
                    # Vue can replace dependent options without a visible loader
                    # or an in-flight request. Use a stable domain before deciding
                    # that a requested branch has disappeared.
                    state = await settle()
                    quantity_seeded = False
                    production_seeded = False

                    def inactive_absent(current, key, desired):
                        # A source can remove its explicitly inactive finish at
                        # high tiers. Preserve that actual absence in the row;
                        # never infer absence for a paid finish or a user filter.
                        return (
                            str(current.get("product_type")) in {"1", "27"}
                            and observed_inactive_finishes.get(key)
                            == finishing_recovery_context(current)
                            and desired == ""
                            and key not in filters
                            and selected_positive_quantity(current) is not None
                            and (
                                (
                                    "quantity" in prefix
                                    and selected_positive_quantity(current)
                                    == str(prefix["quantity"])
                                )
                                or (
                                    "quantity" not in prefix
                                    and choice_hint is not None
                                    and choice_hint[0] == "quantity"
                                )
                            )
                            and any(
                                str(item["value"])
                                == selected_positive_quantity(current)
                                for control in current["controls"]
                                if control["key"] == "quantity"
                                for item in filter_choices(
                                    control, filters.get("quantity")
                                )
                            )
                            and (
                                "productionTime" not in prefix
                                or any(
                                    c["key"] == "productionTime"
                                    and str(c["value"]) == str(prefix["productionTime"])
                                    for c in current["controls"]
                                )
                            )
                            and not any(c["key"] == key for c in current["controls"])
                        )

                    async def reveal_finishing(current, key, desired):
                        nonlocal quantity_seeded, production_seeded
                        if str(current.get("product_type")) not in {
                            "1",
                            "27",
                        } or key not in {"raisedprint", "spotcoating"}:
                            return current
                        control = next(
                            (c for c in current["controls"] if c["key"] == key), None
                        )
                        if control is not None and filter_choices(control, desired):
                            return current
                        if not production_seeded and "productionTime" not in prefix:
                            production = next(
                                (
                                    c
                                    for c in current["controls"]
                                    if c["key"] == "productionTime"
                                ),
                                None,
                            )
                            regular = (
                                [
                                    item
                                    for item in filter_choices(
                                        production, filters.get("productionTime")
                                    )
                                    if str(item["value"]) == "1"
                                ]
                                if production is not None
                                else []
                            )
                            if (
                                production is not None
                                and str(production.get("value")) == "3"
                                and regular
                            ):
                                # A finished Rush branch can hide a paid finishing
                                # sibling even at its minimum quantity. Restore
                                # the offered Regular domain before looking for
                                # that sibling, unless production is already fixed
                                # by the prefix or excluded by an explicit filter.
                                production_seeded = True
                                current = await choose(production, regular[0])
                                control = next(
                                    (c for c in current["controls"] if c["key"] == key),
                                    None,
                                )
                                if control is not None and filter_choices(
                                    control, desired
                                ):
                                    return current
                        if quantity_seeded:
                            return current
                        quantity = next(
                            (c for c in current["controls"] if c["key"] == "quantity"),
                            None,
                        )
                        if quantity is None:
                            return current
                        available = []
                        witnessed = observed_finishing_quantities.get(
                            (finishing_recovery_context(current), key, str(desired)), []
                        )
                        for item in filter_choices(quantity, filters.get("quantity")):
                            try:
                                amount = Decimal(str(item["value"]).replace(",", ""))
                            except InvalidOperation:
                                continue
                            if amount.is_finite() and amount > 0:
                                available.append(
                                    (str(item["value"]) not in witnessed, amount, item)
                                )
                        if not available:
                            return current
                        quantity_seeded = True
                        baseline = min(available, key=lambda item: (item[0], item[1]))[
                            2
                        ]
                        if str(quantity["value"]) == str(baseline["value"]):
                            return current
                        # Prefer a quantity at which this exact finishing choice
                        # was observed for this base and production context. The
                        # minimum offered quantity can itself hide the finish.
                        # Restore that domain before deciding a
                        # saved prefix or its next sibling is unavailable. This
                        # transient selection is never itself a quoted row.
                        return await choose(quantity, baseline)

                    async def reset_once(reason, key):
                        await diagnostic(
                            "browser_restore_reset",
                            restore_diagnostics,
                            reason,
                            key,
                            {
                                "prefix": public_configuration(prefix),
                                "effective_configuration": public_configuration(
                                    {c["key"]: c["value"] for c in state["controls"]}
                                ),
                                "next_choice": (
                                    public_configuration(dict([choice_hint]))
                                    if choice_hint
                                    else {}
                                ),
                                "reloading": not reloaded,
                            },
                        )
                        if reloaded:
                            return None
                        await navigate()
                        await settle(auth_grace=10)
                        return await restore(
                            prefix, reloaded=True, choice_hint=choice_hint
                        )

                    for key, desired in prefix.items():
                        if inactive_absent(state, key, desired):
                            continue
                        state = await reveal_finishing(state, key, desired)
                        control = next(
                            (c for c in state["controls"] if c["key"] == key), None
                        )
                        if not control:
                            return await reset_once("control_missing", key)
                        if str(control["value"]) != str(desired):
                            available = filter_choices(control, desired)
                            if not available:
                                return await reset_once("choice_unavailable", key)
                            state = await choose(control, available[0])
                    if choice_hint is not None:
                        state = await reveal_finishing(state, *choice_hint)
                    mismatched = next(
                        (
                            key
                            for key, value in prefix.items()
                            if not inactive_absent(state, key, value)
                            and not any(
                                c["key"] == key and str(c["value"]) == str(value)
                                for c in state["controls"]
                            )
                        ),
                        None,
                    )
                    if mismatched is not None:
                        return await reset_once("selection_changed", mismatched)
                    return state

                async def record_quote(final, prefix, extra_evidence=()):
                    observation = {
                        "kind": "visible_product_quote",
                        "url": page.url,
                        "observed_at": time.time(),
                        "title": final["title"],
                        "controls": final["controls"],
                        "service_context": deepcopy(final.get("service_context", [])),
                        "service_context_observed": final.get(
                            "service_context_observed"
                        )
                        is True,
                        "subtotal_text": final["price"],
                        "basis": final["basis"],
                        "product_type": final.get("product_type"),
                        "store_type": final.get("store_type"),
                        "independent_cart_verification": False,
                        **({"service_context_schema": MODERN_SERVICE_SCHEMA,
                            "modern_service_observation": deepcopy(final["modern_service_observation"]),
                            "native_freshness": deepcopy(final["modern_native_freshness"])} if modern_requested else {}),
                    }
                    evidence = await transport.record_observation(
                        url,
                        200,
                        {"content-type": "application/json"},
                        json.dumps(observation).encode(),
                        context={"lane": "browser_dom_quote", "configuration": prefix},
                        count_request=False,
                    )
                    row = make_record(
                        url,
                        final,
                        [*responses, *extra_evidence, evidence.response_id],
                        partial,
                        metadata,
                    )
                    row["shipping"] = (
                        {
                            "status": "unavailable",
                            "reason": "Browser shipping quote contract is not configured",
                        }
                        if shipping_requested
                        else {"status": "not_requested"}
                    )
                    return row

                async def counted_row(row):
                    counters["rows"] += 1
                    await emit(
                        "progress",
                        {
                            **counters,
                            "source_requests": request_count,
                            "count_is_exact": False,
                        },
                    )
                    return row

                def branch_fingerprint(state):
                    return (
                        canonical_service_context(state),
                        tuple(
                            sorted(
                                (
                                    c["key"],
                                    str(c["value"]),
                                    (
                                        ()
                                        if c["key"] == "productionTime"
                                        else tuple(
                                            sorted(
                                                str(o["value"])
                                                for o in filter_choices(c)
                                            )
                                        )
                                    ),
                                )
                                for c in state["controls"]
                                if c["key"] != "quantity"
                            )
                        ),
                    )

                async def try_bulk_quantities(prefix, state):
                    remaining = None if limit is None else limit - counters["rows"]
                    if (
                        not bulk_requested
                        or remaining == 1
                        or str(state.get("product_type")) not in {"1", "27", "36"}
                    ):
                        return None
                    if set(filters) - {c["key"] for c in state["controls"]}:
                        return None
                    from .browser_tables import (
                        TableSchemaError,
                        bind_business_card_tables,
                        reconstruct_candidate,
                    )

                    async def fallback(reason):
                        if reason not in bulk_fallback_reasons:
                            bulk_fallback_reasons.add(reason)
                            await emit(
                                "bulk_fallback",
                                {"reason": reason, "method": "direct_browser_quotes"},
                            )
                        return None

                    try:
                        anchor = await settle(allow_reuse=False)
                        try:
                            observed_amount(anchor)
                        except AdapterError as exc:
                            if exc.code == "price_unavailable":
                                return None
                            raise
                        if any(
                            not any(
                                c["key"] == key and str(c["value"]) == str(value)
                                for c in anchor["controls"]
                            )
                            for key, value in prefix.items()
                        ):
                            raise TableSchemaError(
                                "Source reset the requested non-quantity configuration"
                            )
                        anchor["currency"] = (
                            anchor.get("currency")
                            or metadata.get("currency")
                            or (
                                "USD"
                                if "$" in str(anchor["price"])
                                and urlparse(url).hostname
                                in {"www.printograph.com", "printograph.com"}
                                else None
                            )
                        )
                        bundle = bind_business_card_tables(
                            anchor,
                            bulk_captures(),
                            navigation_generation=navigation["generation"],
                            selection_epoch=selection_epoch,
                            filters=None,
                        )
                        quantity_control = next(
                            c for c in anchor["controls"] if c["key"] == "quantity"
                        )
                        selected = {
                            int(c["value"])
                            for c in filter_choices(
                                quantity_control, filters.get("quantity")
                            )
                        }
                        quantities = [
                            q for q in bundle["allowed_quantities"] if q in selected
                        ]
                        truncated = (
                            remaining is not None and len(quantities) > remaining
                        )
                        if remaining is not None:
                            quantities = quantities[:remaining]
                        if len(quantities) <= 1:
                            return None
                        generation = navigation["generation"]
                        fingerprint = branch_fingerprint(anchor)
                        anchor_row = await record_quote(
                            anchor,
                            {c["key"]: c["value"] for c in anchor["controls"]},
                            bundle["evidence_ids"],
                        )
                        anchor_quantity = anchor_row["quantity"]

                        def candidate(quantity):
                            return reconstruct_candidate(
                                bundle["base_capture"],
                                quantity=quantity,
                                currency=anchor_row["currency"],
                                allowed_quantities=bundle["allowed_quantities"],
                                configuration=anchor_row["configuration"],
                                option_captures=bundle["option_captures"],
                                selected_option_ids=bundle["selected_option_ids"],
                                allow_additive=True,
                            )

                        if Decimal(candidate(anchor_quantity)["subtotal"]) != Decimal(
                            anchor_row["subtotal"]
                        ):
                            raise TableSchemaError(
                                "Captured tables do not match the selected anchor subtotal"
                            )
                        samples = {anchor_quantity: anchor_row}
                        boundary_quantity = (
                            quantities[-1]
                            if quantities[-1] != anchor_quantity
                            else quantities[0]
                        )
                        if boundary_quantity != anchor_quantity:
                            boundary_choice = filter_choices(
                                quantity_control, str(boundary_quantity)
                            )
                            if len(boundary_choice) != 1:
                                raise TableSchemaError(
                                    "Boundary quantity is not a unique visible selection"
                                )
                            boundary = await choose(
                                quantity_control, boundary_choice[0]
                            )
                            if not any(
                                control["key"] == "quantity"
                                and str(control["value"]) == str(boundary_quantity)
                                for control in boundary["controls"]
                            ):
                                raise TableSchemaError(
                                    "Source reset the requested boundary quantity before quote validation"
                                )
                            if (
                                navigation["generation"] != generation
                                or branch_fingerprint(boundary) != fingerprint
                            ):
                                raise TableSchemaError(
                                    "Quantity changed the configuration or its available non-quantity options"
                                )
                            boundary["currency"] = anchor_row["currency"]
                            rebound = bind_business_card_tables(
                                boundary,
                                bulk_captures(),
                                navigation_generation=generation,
                                selection_epoch=selection_epoch,
                                filters=None,
                            )
                            if (
                                rebound["source_base_product_id"]
                                != bundle["source_base_product_id"]
                                or rebound["allowed_quantities"]
                                != bundle["allowed_quantities"]
                                or rebound["base_capture"]["data"]
                                != bundle["base_capture"]["data"]
                                or [
                                    (c["url"], c["data"])
                                    for c in rebound["option_captures"]
                                ]
                                != [
                                    (c["url"], c["data"])
                                    for c in bundle["option_captures"]
                                ]
                            ):
                                raise TableSchemaError(
                                    "Source tables changed during boundary validation"
                                )
                            boundary_row = await record_quote(
                                boundary,
                                {c["key"]: c["value"] for c in boundary["controls"]},
                                bundle["evidence_ids"],
                            )
                            if boundary_row["quantity"] != boundary_quantity:
                                raise TableSchemaError(
                                    "Recorded boundary quantity differs from the requested boundary quantity"
                                )
                            if Decimal(
                                candidate(boundary_quantity)["subtotal"]
                            ) != Decimal(boundary_row["subtotal"]):
                                raise TableSchemaError(
                                    "Captured tables do not match the boundary subtotal"
                                )
                            samples[boundary_quantity] = boundary_row
                        # Validate the entire in-memory batch before publishing any
                        # calculated row. Missing interior tiers fall back safely.
                        derived = {
                            quantity: candidate(quantity)
                            for quantity in quantities
                            if quantity not in samples
                        }
                        if derived:
                            partial.add("quantity_candidates_pending_verification")
                        if truncated:
                            partial.add("configuration_limit")
                        sample_evidence = [
                            identifier
                            for row in samples.values()
                            for identifier in row["evidence_ids"]
                        ]
                        rows = []
                        for quantity in quantities:
                            if quantity in samples:
                                row = samples[quantity]
                            else:
                                row = deepcopy(anchor_row)
                                row.update(derived[quantity])
                                row["source_product_id"] = anchor_row[
                                    "source_product_id"
                                ]
                                row["production"] = {
                                    "value": bundle["binding"]["production_mode"],
                                    "status": "requires_configuration_quote",
                                }
                                row["table_derivation"]["sampled_quantities"] = sorted(
                                    samples
                                )
                                row["table_derivation"]["binding"] = deepcopy(
                                    bundle["binding"]
                                )
                            row["pricing_context"] = {
                                **anchor_row.get("pricing_context", {}),
                                "source_base_product_id": str(
                                    bundle["source_base_product_id"]
                                ),
                            }
                            row["evidence_ids"] = list(
                                dict.fromkeys(
                                    [
                                        *row["evidence_ids"],
                                        *bundle["evidence_ids"],
                                        *sample_evidence,
                                    ]
                                )
                            )
                            row["coverage_status"] = (
                                "partial" if partial else "enumeration_pending"
                            )
                            row["coverage_reasons"] = sorted(partial)
                            rows.append(row)
                        await emit(
                            "bulk_quantity_expansion",
                            {
                                "configuration_rows": len(rows),
                                "direct_quote_rows": sum(
                                    q in samples for q in quantities
                                ),
                                "pending_table_rows": len(derived),
                                "sampled_quantities": sorted(samples),
                                "base_product_id": str(
                                    bundle["source_base_product_id"]
                                ),
                                "verification_complete": False,
                            },
                        )
                        return rows
                    except TableSchemaError as exc:
                        return await fallback(str(exc))

                async def walk(prefix, *, quantity_warmed=False, size_warmed=False):
                    nonlocal resumed_branches
                    if progress_journal is None:
                        async for row in walk_live(
                            prefix,
                            quantity_warmed=quantity_warmed,
                            size_warmed=size_warmed,
                        ):
                            yield row
                        return
                    checkpoint_state = await settle()
                    if modern_requested and checkpoint_state["controls"] and all(
                        c["key"] in prefix and str(c["value"]) == str(prefix[c["key"]])
                        for c in checkpoint_state["controls"]
                    ):
                        try:
                            # Fix the observed service policy before the leaf's
                            # checkpoint frame begins, so durable rows cannot be
                            # attributed to the page's former service defaults.
                            await prepare_services(page,url,transport.check,modern_mark_action,settle,service_policy=modern_policy)
                            checkpoint_state = await settle(allow_reuse=False)
                        except ModernHeld:
                            # walk_live owns the bounded leaf recovery/hold.
                            pass
                    checkpoint_services = (
                        canonical_service_context(checkpoint_state)
                        if modern_requested or str(checkpoint_state.get("product_type"))
                        in {"1", "27", "36"}
                        else None
                    )
                    saved = progress_journal.skip(prefix, checkpoint_services)
                    if saved is not None:
                        await transport.check()
                        resumed_branches += 1
                        counters["rows"] += saved["row_count"]
                        observed_controls.update(
                            saved["details"].get("control_keys", [])
                        )
                        partial.replay(saved["details"].get("coverage_reasons", []))
                        partial.replay(["completed_branches_not_revalidated"])
                        await emit(
                            "browser_branch_resumed",
                            {
                                "prefix": prefix,
                                "durable_rows": saved["row_count"],
                                "source_revalidated": False,
                                "coverage_basis": "previously_exhausted_supported_visible_finite_branch",
                            },
                        )
                        return
                    frame = progress_journal.begin(prefix, checkpoint_services)
                    gap_generation = partial.generation
                    try:
                        async for row in walk_live(
                            prefix,
                            quantity_warmed=quantity_warmed,
                            size_warmed=size_warmed,
                        ):
                            yield row
                        # Engine has committed each browser row before requesting
                        # the next item. No marker is written on error/cancel or
                        # any enumeration gap, even a repeated sibling gap.
                        if partial.generation == gap_generation:
                            await transport.check()
                            await progress_journal.complete(
                                frame, partial.retained_notes
                            )
                    except BaseException:
                        # Parent async generators may finalize before suspended
                        # children on cancel/aclose. Abandon the whole unfinished
                        # subtree; late descendant cleanup must not mark it done.
                        progress_journal.abandon(frame)
                        raise
                    finally:
                        progress_journal.end(frame)

                async def walk_live(
                    prefix, *, quantity_warmed=False, size_warmed=False
                ):
                    if limit is not None and counters["rows"] >= limit:
                        partial.add("configuration_limit")
                        return
                    state = await restore(prefix)
                    if state is None:
                        counters["invalid_branches"] += 1
                        partial.add("source_reset_prevented_branch")
                        return
                    source_dependencies = str(state.get("product_type")) in {
                        "1",
                        "6",
                        "10016",
                        "10019",
                        "27",
                        "36",
                    }
                    quantity_deferred = bulk_requested and source_dependencies

                    async def seed_production_quantity(current):
                        """Reveal low-quantity production modes after a high-tier branch."""
                        if not quantity_deferred or "quantity" in prefix:
                            return current
                        requested_mode = filters.get("productionTime")
                        requested_modes = (
                            requested_mode
                            if isinstance(requested_mode, list)
                            else [requested_mode]
                        )
                        production = next(
                            (
                                c
                                for c in current["controls"]
                                if c["key"] == "productionTime"
                            ),
                            None,
                        )
                        if (
                            requested_mode is not None
                            and len(requested_modes) == 1
                            and production is not None
                            and selected_matches(production, requested_modes[0])
                        ):
                            # An explicitly selected single mode excludes the
                            # others, so its valid current quantity is sufficient.
                            return current
                        quantity = next(
                            (c for c in current["controls"] if c["key"] == "quantity"),
                            None,
                        )
                        if quantity is None:
                            return current
                        permitted = filter_choices(quantity, filters.get("quantity"))
                        if not permitted:
                            return current
                        try:
                            baseline = min(
                                permitted,
                                key=lambda item: Decimal(
                                    str(item["value"]).replace(",", "")
                                ),
                            )
                        except (InvalidOperation, ValueError):
                            baseline = permitted[0]
                        if str(quantity["value"]) != str(baseline["value"]):
                            return await choose(quantity, baseline)
                        return current

                    pending_controls = [
                        c for c in state["controls"] if c["key"] not in prefix
                    ]
                    # Saved source catalogs bind size IDs to orientation1/2;
                    # Roll Labels also binds the orientation domain to shape.
                    pending = (
                        next(
                            (
                                c
                                for key in ("shape", "orientation")
                                for c in pending_controls
                                if c["key"] == key
                            ),
                            None,
                        )
                        if source_dependencies
                        else None
                    )
                    pending = pending or (
                        next(
                            (c for c in pending_controls if c["key"] != "quantity"),
                            None,
                        )
                        if quantity_deferred
                        else None
                    )
                    pending = pending or next(iter(pending_controls), None)
                    if pending:
                        if (
                            source_dependencies
                            and pending["key"] == "size"
                            and pending["kind"] == "select"
                            and not size_warmed
                            and "orientation" not in prefix
                            and not any(
                                c["key"] == "orientation" for c in state["controls"]
                            )
                        ):
                            finite_sizes = filter_choices(pending)
                            if finite_sizes:
                                preferred = filter_choices(pending, filters.get("size"))
                                seed = next(iter(preferred or finite_sizes))
                                # Some source pages expose orientation only after
                                # size selection. Reveal it before locking size;
                                # otherwise vertical orientation invalidates the
                                # fixed horizontal ID and its unvisited siblings.
                                # A temporary finite preset may reveal the domain
                                # containing a requested vertical-only size.
                                await choose(pending, seed)
                                await emit(
                                    "size_dependency_warmup",
                                    {
                                        "size": public_control_text(seed["value"]),
                                        "published_rows": 0,
                                        "reason": "discover_optional_orientation_before_size",
                                    },
                                )
                                async for row in walk(
                                    prefix,
                                    quantity_warmed=quantity_warmed,
                                    size_warmed=True,
                                ):
                                    yield row
                                return
                        if pending["key"] == "productionTime" and quantity_deferred:
                            await seed_production_quantity(state)
                            state = await restore(prefix)
                            if state is None:
                                counters["invalid_branches"] += 1
                                partial.add("prefix_restore_failed")
                                await skipped_branch(
                                    "prefix_restore_failed", pending, prefix=prefix
                                )
                                return
                            pending = next(
                                (
                                    c
                                    for c in state["controls"]
                                    if c["key"] == "productionTime"
                                ),
                                None,
                            )
                            if pending is None:
                                counters["invalid_branches"] += 1
                                partial.add("dynamic_control_disappeared")
                                return
                        if pending["key"] == "quantity" and bulk_requested:
                            remaining = (
                                None if limit is None else limit - counters["rows"]
                            )
                            if (
                                not quantity_warmed
                                and remaining != 1
                                and source_dependencies
                            ):
                                permitted = filter_choices(
                                    pending, filters.get("quantity")
                                )
                                selected_quantity = any(
                                    str(choice["value"]) == str(pending["value"])
                                    for choice in filter_choices(pending)
                                )
                                try:
                                    observed_amount(state)
                                    has_quote = True
                                except AdapterError:
                                    has_quote = False
                                if permitted and (
                                    not selected_quantity or not has_quote
                                ):
                                    # Production may appear only after the first
                                    # quantity creates a quote. Do not lock quantity
                                    # into the DFS prefix before discovering those
                                    # controls, or the bulk branch is never revisited.
                                    await choose(pending, permitted[0])
                                    await emit(
                                        "bulk_quantity_warmup",
                                        {
                                            "quantity": public_control_text(
                                                permitted[0]["value"]
                                            ),
                                            "published_rows": 0,
                                            "reason": "source_quote_requires_quantity_selection",
                                        },
                                    )
                                    # One attempt for this exact prefix, including
                                    # when the source leaves its quote unavailable.
                                    async for row in walk(
                                        prefix,
                                        quantity_warmed=True,
                                        size_warmed=size_warmed,
                                    ):
                                        yield row
                                    return
                            bulk_rows = await try_bulk_quantities(prefix, state)
                            if bulk_rows is not None:
                                for row in bulk_rows:
                                    await transport.check()
                                    yield await counted_row(row)
                                return
                        choices = filter_choices(pending, filters.get(pending["key"]))
                        if not choices:
                            excluded = (
                                pending["key"] in filters
                                and filters[pending["key"]] is not None
                                and bool(filter_choices(pending))
                            )
                            await skipped_branch(
                                (
                                    "excluded_by_selection_filter"
                                    if excluded
                                    else "no_matching_choice"
                                ),
                                pending,
                                prefix=prefix,
                                state=state,
                            )
                            if excluded:
                                counters["filtered_branches"] = (
                                    counters.get("filtered_branches", 0) + 1
                                )
                            else:
                                partial.add("no_matching_choice")
                                counters["invalid_branches"] += 1
                            return
                        for choice in choices:
                            if limit is not None and counters["rows"] >= limit:
                                partial.add("configuration_limit")
                                return
                            child_prefix = {**prefix, pending["key"]: choice["value"]}
                            if (
                                progress_journal is not None
                                and str(state.get("product_type"))
                                not in {"1", "27", "36"}
                                and progress_journal.contains(child_prefix, canonical_service_context(state) if modern_requested else None)
                            ):
                                # Skip before changing the UI: otherwise a saved
                                # branch still triggers its lookup and price XHRs.
                                async for row in walk(child_prefix):
                                    yield row
                                continue
                            if pending["key"] == "productionTime" and quantity_deferred:
                                await seed_production_quantity(await settle())
                            fresh = await restore(
                                prefix, choice_hint=(pending["key"], choice["value"])
                            )
                            if fresh is None:
                                await skipped_branch(
                                    "prefix_restore_failed",
                                    pending,
                                    choice,
                                    prefix=prefix,
                                )
                                partial.add("prefix_restore_failed")
                                counters["invalid_branches"] += 1
                                # Every remaining child needs this same parent.
                                # restore() already tried one fresh navigation;
                                # retrying once per quantity repeats the identical
                                # failed rebuild. Preserve the gap and return to
                                # the ancestor, whose other branches may work.
                                return
                            control = (
                                next(
                                    (
                                        c
                                        for c in fresh["controls"]
                                        if c["key"] == pending["key"]
                                    ),
                                    None,
                                )
                                if fresh
                                else None
                            )
                            if not control:
                                await skipped_branch(
                                    "dynamic_control_disappeared",
                                    pending,
                                    choice,
                                    prefix=prefix,
                                    state=fresh,
                                )
                                partial.add("dynamic_control_disappeared")
                                counters["invalid_branches"] += 1
                                continue
                            current_choices = filter_choices(control, choice["value"])
                            if not current_choices:
                                await skipped_branch(
                                    "choice_disappeared",
                                    control,
                                    choice,
                                    prefix=prefix,
                                    state=fresh,
                                )
                                partial.add("choice_disappeared")
                                counters["invalid_branches"] += 1
                                continue
                            if str(control["value"]) != str(choice["value"]):
                                after = await choose(control, current_choices[0])
                                if not any(
                                    c["key"] == control["key"]
                                    and str(c["value"]) == str(choice["value"])
                                    for c in after["controls"]
                                ):
                                    counters["invalid_branches"] += 1
                                    partial.add("source_auto_reset")
                                    continue
                            async for row in walk(
                                {**prefix, control["key"]: choice["value"]}
                            ):
                                yield row
                        return
                    if not state["controls"]:
                        raise AdapterError(
                            "unsupported_controls",
                            "No supported product controls were visible",
                        )
                    if any(
                        key not in {c["key"] for c in state["controls"]}
                        for key in filters
                    ):
                        partial.add("requested_control_missing")
                        return
                    if modern_requested:
                        # One bounded ordinary fresh-page recovery per previously
                        # unpublished leaf. No recursion and no retry of saved rows.
                        key = json.dumps(prefix,sort_keys=True,separators=(",",":"))
                        try:
                            await prepare_services(page,url,transport.check,modern_mark_action,settle,service_policy=modern_policy)
                            final = await settle(require_price=True,allow_reuse=False)
                            production = next((c for c in final["controls"] if c["key"] == "productionTime"),None)
                            final["modern_native_freshness"] = modern_gate.verify(final["modern_service_observation"],production,final["controls"],observed_amount(final))
                        except ModernHeld as first:
                            if key in modern_recoveries:
                                partial.add(first.reason);counters["invalid_branches"] += 1;return
                            modern_recoveries.add(key)
                            await emit("modern_quote_recovery",{"reason":first.reason,"attempt":1,"maximum_attempts":1, **({"details":first.details} if first.details is not None else {})})
                            await navigate();await settle()
                            rebuilt = await restore(prefix)
                            if rebuilt is None:
                                partial.add("modern_fresh_page_restore_failed");counters["invalid_branches"] += 1;return
                            try:
                                await prepare_services(page,url,transport.check,modern_mark_action,settle,service_policy=modern_policy)
                                final = await settle(require_price=True,allow_reuse=False)
                                production = next((c for c in final["controls"] if c["key"] == "productionTime"),None)
                                final["modern_native_freshness"] = modern_gate.verify(final["modern_service_observation"],production,final["controls"],observed_amount(final))
                            except ModernHeld as final_error:
                                await emit("modern_quote_held", {"reason":final_error.reason, **({"details":final_error.details} if final_error.details is not None else {})})
                                partial.add(final_error.reason);counters["invalid_branches"] += 1;return
                    else:
                        final = await settle(require_price=True)
                    if any(
                        str(c["value"]) != str(prefix.get(c["key"]))
                        for c in final["controls"]
                    ):
                        partial.add("source_auto_reset")
                        return
                    for control in final["controls"]:
                        if control["key"] in filters and not selected_matches(
                            control, filters[control["key"]]
                        ):
                            raise AdapterError(
                                "selection_reset",
                                "The page changed a requested selection before quoting",
                            )
                    yield await counted_row(await record_quote(final, prefix))

                try:
                    await navigate()
                    try:
                        initial = await settle(auth_grace=10)
                    except AdapterError as exc:
                        if exc.code != "access_required" or headless:
                            raise
                        await emit(
                            "attention",
                            {
                                "code": "access_required",
                                "message": "Complete normal authentication in the opened browser. The run will detect completion.",
                            },
                        )
                        auth_wait = max(
                            0, float(os.getenv("ATLAS_BROWSER_AUTH_WAIT_SECONDS", "0"))
                        )
                        auth_deadline = (
                            time.monotonic() + auth_wait if auth_wait else None
                        )
                        while auth_deadline is None or time.monotonic() < auth_deadline:
                            await transport.check()
                            await asyncio.sleep(1)
                            try:
                                initial = await settle()
                                break
                            except AdapterError as auth_error:
                                if auth_error.code != "access_required":
                                    raise
                        else:
                            raise AdapterError(
                                "access_required",
                                "Authentication handoff timed out; sign in through the configured headed profile and resume",
                            )
                    await emit(
                        "discovery",
                        {
                            "title": initial["title"],
                            "controls": initial["controls"],
                            "coverage_basis": "visible source controls",
                            "count_is_exact": False,
                        },
                    )
                    navigation["bootstrap"] = False
                    async for record in walk({}):
                        yield record
                    unknown = set(filters) - observed_controls
                    if unknown:
                        raise AdapterError(
                            "invalid_selection",
                            "Requested controls were never observed",
                            fields=sorted(unknown),
                        )
                    if not counters["rows"]:
                        raise AdapterError(
                            "no_matching_configuration",
                            "No fully selected source configuration produced a quote",
                        )
                    await emit(
                        "coverage",
                        {
                            "status": (
                                "partial" if partial else "visible_domain_exhausted"
                            ),
                            **counters,
                            "reasons": sorted(partial),
                            "count_is_exact": not bool(partial),
                            "verification_complete": False,
                            "shipping_status": "not_requested",
                            "resumed_completed_branches": resumed_branches,
                            "source_revalidated_on_resume": not bool(resumed_branches),
                            "restore_failures": sum(restore_diagnostics.values()),
                            "skipped_branch_occurrences": sum(
                                branch_diagnostics.values()
                            ),
                        },
                    )
                except PlaywrightTimeout as exc:
                    raise AdapterError(
                        "source_timeout",
                        "A normal browser action timed out; completed records are retained",
                    ) from exc
                finally:
                    closing = True
                    pending_routes = list(route_tasks)
                    for task in pending_routes:
                        task.cancel()
                    if pending_routes:
                        await asyncio.gather(*pending_routes, return_exceptions=True)
                    page.remove_listener("requestfinished", request_done)
                    page.remove_listener("requestfailed", request_done)
                    page.remove_listener("framenavigated", navigated)
                    if branch_diagnostics or restore_diagnostics:
                        try:
                            await emit(
                                "browser_traversal_diagnostics",
                                {
                                    "navigation_generation": navigation["generation"],
                                    "restore_failures": [
                                        {
                                            "reason": reason,
                                            "key": public_control_text(key),
                                            "occurrences": count,
                                        }
                                        for (
                                            reason,
                                            key,
                                        ), count in restore_diagnostics.items()
                                    ],
                                    "skipped_branches": [
                                        {
                                            "reason": reason,
                                            "key": public_control_text(key),
                                            "occurrences": count,
                                        }
                                        for (
                                            reason,
                                            key,
                                        ), count in branch_diagnostics.items()
                                    ],
                                },
                            )
                        except Exception:
                            # A closed/paused run cannot accept diagnostic events;
                            # preserve the original cancellation or source failure.
                            pass


Adapter = BrowserAdapter
