"""Opt-in native service policy and causal DOM-quote evidence, without formulas.

Only ordinary observed controls are clicked. No source callbacks, request
replays, hidden field writes, uploaded files, carts or orders are used.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import urlsplit

SCHEMA = "printograph-native-services/v3"
SERVICE_KEYS = ("proofType", "proofTypeForManual", "fileDeliveryForManual",
                "fileDeliveryForIop", "designServices", "fileDeliveryForDs")
MODEL_KEYS = ("proofType", "manualProofType", "fileDelivery", "designServices")
PDF_SERVICE_POLICY = "manual_pdf_proof_v1"
GENERIC_REFERENCE_FLAG_SCOPES = {
    "https://www.printograph.com/products/cardboards/order": "10016",
    "https://www.printograph.com/products/acrylic-boards/order": "10019",
}
PDF_SOURCE_URLS = frozenset({
    "https://www.printograph.com/products/cardboards/order",
    "https://www.printograph.com/products/acrylic-boards/order",
})
# Current common.turnaroundParam's explicit source whitelist; these are request
# identities, not a family production formula or an inferred price domain.
TURNAROUND_FIELDS = frozenset({"shape","size","material","page","pocket","paper","finish","folding","color","coverPaper","panel","tray"})


class ModernHeld(ValueError):
    def __init__(self, reason, details=None):
        self.reason = reason
        self.details = None
        fields = {"schema", "group_key", "offered_count", "visible_count", "enabled_count", "checked_count", "policy_match_count", "eligible_match_count"}
        if (type(details) is dict and set(details) == fields
                and details.get("schema") == "printograph-safe-service-choice-diagnostic/v1"
                and details.get("group_key") in SERVICE_KEYS
                and all(type(details[k]) is int and 0 <= details[k] <= 100 for k in fields - {"schema", "group_key"})):
            self.details = deepcopy(details)
        super().__init__(reason)


def validate_service_policy(policy, source_url):
    if policy is None:
        return None
    if type(policy) is not str or type(source_url) is not str or policy != PDF_SERVICE_POLICY or source_url not in PDF_SOURCE_URLS:
        raise ModernHeld("modern_service_policy_scope_unproven")
    return policy


def bind_service_policy(raw, policy, source_url):
    """Attach trusted Run intent, never infer a policy from a source default."""
    validate_service_policy(policy, source_url)
    if policy is None:
        return raw
    if isinstance(raw, dict) and raw.get("observed") is not True:
        return raw
    if not isinstance(raw, dict) or raw.get("source_url") != source_url:
        raise ModernHeld("modern_service_policy_scope_unproven")
    result = deepcopy(raw)
    result["service_policy"] = policy
    return result


SERVICE_READ_JS = r'''({source_url}) => {
 const no=reason=>({observed:false,reason});
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden'&&
   getComputedStyle(e).display!=='none'&&!e.closest('[hidden],[aria-hidden="true"]');
 const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
 // Native proof labels contain a source-rendered money badge after e.label.
 // Strip only that known, money-only direct badge for policy classification;
 // retain the FULL label below for the final atomic action identity.
 const policyLabel=(e,name,label)=>{
   if(name!=='proofTypeForManual')return label;
   const labels=[...e.labels||[]];if(labels.length!==1)return label;
   const badges=[...labels[0].querySelectorAll('span.badge.badge-danger')];
   if(badges.length!==1)return label;
   const badge=badges[0],money='(?:0|[1-9][0-9]{0,11})(?:\\.[0-9]{1,6})?';
   if(badge.parentElement!==labels[0]||badge.classList.length!==2||badge.querySelectorAll('*').length>4||
      [...badge.querySelectorAll('*')].some(n=>n.tagName!=='SPAN')||
      !(new RegExp('^\\$'+money+'(?:\\s+\\$'+money+')?$')).test(clean(badge.textContent)))return label;
   const clone=labels[0].cloneNode(true);clone.querySelector('span.badge.badge-danger').remove();return clean(clone.textContent).slice(0,500);
 };
 const safe=v=>typeof v==='string'&&/^(?:[0-9]+(?:\.[0-9]+)?|)$/.test(v)&&v.length<=32 ||
   typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<1e12;
 const keys=['proofType','proofTypeForManual','fileDeliveryForManual','fileDeliveryForIop','designServices','fileDeliveryForDs'];
 if(location.href!==source_url||!/^https:\/\/(?:www\.)?printograph\.com\/products\/[a-zA-Z0-9_-]+\/order\/?$/.test(source_url))return no('modern_source_scope_changed');
 const roots=[...document.querySelectorAll('#order-form')],root=roots.length===1?roots[0]:null;
 if(!root||!/^\d+$/.test(root.getAttribute('data-printproducttype')||'')||root.getAttribute('data-currentstoretype')!=='broker')return no('modern_product_scope_unproven');
 if([...document.querySelectorAll('#esCollapse')].some(e=>e.classList.contains('show')))return no('shipping_panel_open');
 const panels=[...root.querySelectorAll('#collapseOne')],buttons=[...root.querySelectorAll('button[aria-controls="collapseOne"]')];
 const headerText=buttons.length===1?clean(buttons[0].textContent).toLowerCase():'';
 const processHeader={schema:'printograph-process-header-diagnostic/v1',panel_count:Math.min(panels.length,3),button_count:Math.min(buttons.length,3),
   button_type:buttons.length===1?(['button','submit','reset'].includes(buttons[0].type)?buttons[0].type:'other'):'unavailable',
   enabled:buttons.length===1?!buttons[0].disabled:null,visible:buttons.length===1?visible(buttons[0]):null,
   text_class:buttons.length!==1?'unavailable':headerText==='process'?'process':headerText==='process type'?'process_type':'other'};
 if(panels.length!==1||buttons.length!==1||buttons[0].type!=='button'||buttons[0].disabled||!visible(buttons[0])||
    !/^Process(?: Type)?$/i.test(clean(buttons[0].textContent)))return {observed:false,reason:'modern_process_header_unproven',process_header:processHeader};
 const panel=panels[0],groups=[];
 for(const name of keys){
   const radios=[...root.querySelectorAll('input[type="radio"][name="'+name+'"]')];
   if(radios.length>100||radios.some(e=>!panel.contains(e)))return no('modern_service_group_unproven');
   const choices=radios.map(e=>{const label=clean([...e.labels||[]].map(l=>l.textContent).join(' ')).slice(0,500);return {id:e.id,name,value:e.value,label,policy_label:policyLabel(e,name,label),
     checked:e.checked,disabled:e.matches(':disabled'),visible:visible(e)||[...e.labels||[]].some(visible)}});
   if(choices.some(e=>!e.id||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(e.id)||!safe(e.value)||!e.label)||new Set(choices.map(e=>e.id)).size!==choices.length)
     return no('modern_service_group_unproven');
   groups.push({key:name,control_count:choices.length,choices});
 }
 if([...panel.querySelectorAll('input,select,textarea,button,a')].some(e=>visible(e)&&!e.matches(':disabled')&&
   (e.tagName==='INPUT'&&['radio','checkbox'].includes(e.type)&&!keys.includes(e.name)||
    e.tagName==='SELECT'||e.tagName==='TEXTAREA'||e.tagName==='INPUT'&&['text','number','file'].includes(e.type)||
    e.tagName==='BUTTON'&&e.type==='submit'||e.tagName==='A'&&e.hasAttribute('href'))))return no('unknown_modern_service_control');
 const proxies=[root._vnode?.component?.proxy,root.__vue_app__?._instance?.proxy].filter(Boolean);
 const controllers=[...new Set(proxies.map(p=>p.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];
 if(controllers.length!==1)return no('modern_controller_missing');
 const c=controllers[0],selected=c.jsonObj?.userSelectedOptions,others=c.jsonObj?.others;
 if(!selected||!others||String(c.printProductType)!==root.getAttribute('data-printproducttype'))return no('modern_controller_schema_unproven');
 const own=(o,k)=>Object.hasOwn(o,k),empty=v=>v!==null&&typeof v==='object'&&!Array.isArray(v)&&[Object.prototype,null].includes(Object.getPrototypeOf(v))&&Object.keys(v).length===0&&Object.getOwnPropertySymbols(v).length===0;
 if(!own(others,'isAdmin')||others.isAdmin!==false||!own(others,'waiveFees')||others.waiveFees!==false||
    !own(others,'itemId')||others.itemId!=='null'||!own(others,'editOrderItem')||!empty(others.editOrderItem)||
    ['shoppingCartItemId','priceGroupId'].some(k=>!own(selected,k)||!['',null,undefined].includes(selected[k])))return no('private_or_special_context');
 const fields=c.fields?.[String(c.printProductType)];
 if(!fields||!Array.isArray(fields.specs)||!Array.isArray(fields.options)||fields.specs.length>30||fields.options.length>30)return no('modern_field_schema_unproven');
 const names=[...fields.specs,...fields.options];
 if(new Set(names).size!==names.length||names.some(k=>typeof k!=='string'||!/^[A-Za-z][A-Za-z0-9]{0,63}$/.test(k)||/account|address|name|instructions|file|zip|country|billing|shipping|token|coupon|password/i.test(k)))return no('unsupported_product_field');
 const specs={},options={},services={};
 for(const [bucket,list]of[[specs,fields.specs],[options,fields.options],[services,['proofType','manualProofType','fileDelivery','designServices']]]){
   for(const key of list){if(!own(selected,key)||!safe(selected[key]))return no('modern_selected_context_incomplete');bucket[key]=String(selected[key]);}
 }
 for(const key of ['productId','quantity','productionTime'])if(!own(selected,key)||!safe(selected[key])||!Number(selected[key]))return no('modern_selected_context_incomplete');
 const aliases={proofType:'proofType',proofTypeForManual:'manualProofType',fileDeliveryForManual:'fileDelivery',designServices:'designServices'};
 for(const [name,key]of Object.entries(aliases)){
   const group=groups.find(g=>g.key===name),chosen=group.choices.filter(e=>e.checked);
   if(chosen.length>1)return no('modern_service_selection_ambiguous');
   if(chosen.length&&String(chosen[0].value)!==services[key])return no('modern_model_dom_service_mismatch');
 }
 // Read-only native monetary state. Missing/transitional totals do not block
 // safe service discovery, but can never qualify a quote below.
 const money=v=>(typeof v==='string'&&/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(v)&&v.length<=32)||
   typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=100000000;
 let native_total={observed:false};
 try{const total=c.totalPrice,base=c.jsonObj.appliedPrices?.productPrice;
   if(money(total)&&money(base))native_total={observed:true,controller_total:String(total),applied_product_price:String(base)};
 }catch{}
 // Exact source getStatelessParam relationship. Unknown blankProducts is not
 // treated as an ordinary product; membership uses the native strict type.
 let source_item_type={observed:false};
 const blanks=others.blankProducts;
 if(Array.isArray(blanks)&&blanks.length<=500&&blanks.every(v=>(typeof v==='string'&&/^[1-9][0-9]{0,11}$/.test(v))||typeof v==='number'&&Number.isSafeInteger(v)&&v>0&&v<1e12)){
   const blank=Array.prototype.indexOf.call(blanks,c.printProductType)!==-1;
   source_item_type={observed:true,is_blank_product:blank,value:blank?'13':services.fileDelivery};
 }
 // Native turnaroundComponent.nextDay uses the public root store and raw
 // rush range. Its Cardboards label is an alias for 2 Business Days, never
 // permission to infer one day or replace the retained numeric API range.
 let native_nextday_presentation={observed:false};
 const nativeStore=c.$root?.$props?.store;
 if(source_url==='https://www.printograph.com/products/cardboards/order'&&String(c.printProductType)==='10016'&&
    (nativeStore===101||nativeStore==='101')&&c.jsonObj.turnaroundRange?.rush==='2 Business Days')
   native_nextday_presentation={observed:true,store:'101',rush:'2 Business Days'};
 // A bounded read-only projection of the source's generic rigid-material
 // request builder. No source callback is invoked and no request is created.
 // Unsupported compound/array/special mappings stay explicitly unobserved.
 const generic_price_context=(()=>{try{
   const fail=()=>({observed:false}),t=c.printProductType,sc=c.schema,ap=c.jsonObj.appliedPrices;
   const member=(key)=>Array.isArray(others[key])&&others[key].length<=500&&others[key].every(safe)&&others[key].indexOf(t)!==-1;
   if(!Array.isArray(others.getGenericProductsPriceFromApiList)||!others.getGenericProductsPriceFromApiList.includes(t)||
      !['subItemProducts','embroideryProducts','genericOrientation'].every(k=>Array.isArray(others[k])&&others[k].length<=500&&others[k].every(safe))||
      member('subItemProducts')||member('embroideryProducts')||others.redirectFromMediaClip!==false||source_item_type.is_blank_product!==false||
      String(selected.proofType)!=='100'||fields.options.length||!fields.specs.includes('material')||!fields.specs.includes('thickness'))return fail();
   const mappings=fields.other;
   if(!mappings||typeof mappings!=='object'||Array.isArray(mappings)||![Object.prototype,null].includes(Object.getPrototypeOf(mappings))||Object.keys(mappings).length>60||Object.getOwnPropertySymbols(mappings).length)return fail();
   const hid={hidHoleDrillingId:'materialHoleDrilling',hidSideId:'color',hidSizeId:'size',hidWidthId:'width',hidHeightId:'height',
     hidDesignServiceId:'designServices',hidMaterialColorId:'materialColor',hidCoreColorId:'coreColor',hidMaterialId:'material',
     hidShapeId:'shape',hidAccessoriesId:'accessories',hidThicknessId:'thickness',hidOrientationId:'orientation'};
   if(mappings.hidWidthId!=='width'||mappings.hidHeightId!=='height'||mappings.hidSizeId!=='size'||
      Object.entries(mappings).some(([k,v])=>typeof v!=='string'||!/^[A-Za-z][A-Za-z0-9]{0,63}$/.test(v)||
        /account|address|instructions|file|zip|country|billing|shipping|token|coupon|password|project/i.test(k+' '+v)||
        (k.startsWith('hid')?(!own(hid,k)||(hid[k]!==v&&!(String(t)==='10019'&&k==='hidCoreColorId'&&v==='materialColor'))):!/^[A-Za-z][A-Za-z ]{0,63}$/.test(k))))return fail();
   const list=(key)=>Array.isArray(sc?.[key]?.list)&&sc[key].list.length<=10000?sc[key].list:null;
   const sizes=list('size'),quantities=list('quantity');
   if(!sizes||!quantities||!quantities.length||!own(selected,'orientation')||!safe(selected.orientation)||!own(selected,'paper')||!safe(selected.paper))return fail();
   const matching=sizes.filter(v=>v&&[v.id,v.idv].some(id=>id!==undefined&&String(id)===String(selected.size)));
   const positive=v=>safe(v)&&Number(v)>0;
   if(matching.length!==1||!positive(matching[0].width)||!positive(matching[0].height))return fail();
   const dims=String(selected.orientation)==='2'?{width:String(matching[0].height),height:String(matching[0].width)}:
     {width:String(matching[0].width),height:String(matching[0].height)};
   const qs=quantities.map(v=>v?.quantity);
   if(qs.some(v=>!positive(v)||!/^\d+$/.test(String(v)))||new Set(qs.map(String)).size!==qs.length||!qs.map(String).includes(String(selected.quantity)))return fail();
   const specOther=Object.create(null);
   for(const [key,field]of Object.entries(mappings)){
     if(key==='hidWidthId'||key==='hidHeightId'){specOther[key]=dims[field];continue;}
     if(key.startsWith('hid')){
       if(!own(selected,field)||!safe(selected[field]))return fail();
       specOther[key]=key==='hidSideId'&&String(selected[field])==='3'?'2':selected[field]?String(selected[field]):'';continue;
     }
     const choices=list(field);if(!choices||key==='Center Split')continue;
     const matches=choices.filter(v=>v&&String(v.id)===String(selected[field]));
     if(matches.length>1)return fail();if(!matches.length)continue;
     const label=matches[0].label;
     if(typeof label!=='string'||label.length>500||/[\x00-\x1f\x7f]/.test(label))return fail();
     const shape=String(selected.shape),orientation=field==='orientation';
     if(!orientation||!['1','4','9'].includes(shape))specOther[key]=label;
     if(orientation){
       if(member('genericOrientation')){
         if(['15','5'].includes(shape)||String(t)==='10009')specOther[key]=label==='Horizontal'?'Landscape':'Portrait';
         else if(shape!==''&&!['15','5'].includes(shape))delete specOther[key];
       }else{
         const tall=others.defaultSizeOptionCategories?.Tall;
         if(!Array.isArray(tall)||tall.length>10000||tall.some(v=>!safe(v)))return fail();
         if(!(tall.map(String).includes(String(selected.size))&&shape!=='24')&&!['10024','10025'].includes(String(t))&&shape!==''&&!['15','5'].includes(shape))delete specOther[key];
       }
     }
   }
   const printProduct={productType:String(t),size:String(selected.size),color:selected.color?(String(selected.color)==='2'?'3':String(selected.color)):'1',paper:String(selected.paper)};
   if(selected.shape!=='')printProduct.shape=String(selected.shape);
   // These special generic builders are outside the declared scalar rigid
   // mapping above; do not silently apply their alternate specification rules.
   if(['10006','10008','10018','10023'].includes(String(t)))return fail();
   const other={paper:printProduct.paper,color:printProduct.color};
   for(const [key,value]of Object.entries(specOther))if(key.startsWith('hid')){const k=key.slice(3);other[k[0].toLowerCase()+k.slice(1)]=value;}
   const turnOptions=[];
   if(!ap||!money(ap.holeDrillingPrice))return fail();
   if(Number(ap.holeDrillingPrice)!==0){if(!positive(selected.materialHoleDrilling))return fail();turnOptions.push(String(selected.materialHoleDrilling));}
   if(String(selected.centerSplit)==='2')turnOptions.push(String(selected.orientation)==='1'?'119':'120');
   for(const key of ['standType','frame'])if(own(selected,key)&&selected[key]!==''&&Number(selected[key])!==0){
     if(!safe(selected[key])||!ap[key]||!own(ap[key],String(selected[key]))||!money(ap[key][selected[key]]))return fail();
     if(Number(ap[key][selected[key]])!==0)turnOptions.push(String(selected[key]));
   }
   if(!positive(selected.manualProofType))return fail();turnOptions.push(String(selected.manualProofType));
   if(new Set(turnOptions).size!==turnOptions.length)return fail();
   return {observed:true,properties:{size:String(selected.size),printProductType:String(t),turnaround:String(selected.productionTime),quantity:'1-'+String(qs[qs.length-1]),other},
     turnaround:{printProduct,other:specOther,options:turnOptions},quantity_choices:qs.map(String),dimensions:dims,
     binding:{product_id:String(selected.productId),specs,options,services,production:String(selected.productionTime)}};
 }catch{return {observed:false};}})();
 return {observed:true,source_url,product_type:root.getAttribute('data-printproducttype'),process_header:processHeader,open:panel.classList.contains('show'),native_total,source_item_type,native_nextday_presentation,generic_price_context,
   groups,model_services:services,base_context:{product_id:String(selected.productId),specs,production:String(selected.productionTime)},
   selection_context:{specs,options,services,quantity:String(selected.quantity),production:String(selected.productionTime)}};
}'''

SERVICE_ACTION_JS = r'''({source_url,action,target}) => {
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none'&&!e.closest('[hidden],[aria-hidden="true"]');
 const roots=[...document.querySelectorAll('#order-form')],root=roots.length===1?roots[0]:null;
 if(location.href!==source_url||!root||root.getAttribute('data-currentstoretype')!=='broker'||[...document.querySelectorAll('#esCollapse')].some(e=>e.classList.contains('show')))throw Error('modern native scope changed');
 const panels=[...root.querySelectorAll('#collapseOne')],buttons=[...root.querySelectorAll('button[aria-controls="collapseOne"]')];
 if(panels.length!==1||buttons.length!==1)throw Error('modern process identity changed');
 if(action==='open'){
   const b=buttons[0];if(b.type!=='button'||b.disabled||!visible(b)||!/^process(?: type)?$/i.test(b.textContent.replace(/\s+/g,' ').trim()))throw Error('modern header changed');
   if(!panels[0].classList.contains('show'))HTMLElement.prototype.click.call(b);return true;
 }
 if(action!=='radio'||!target||!['proofType','proofTypeForManual','fileDeliveryForManual','designServices'].includes(target.name)||!panels[0].classList.contains('show'))throw Error('modern radio scope changed');
 const nodes=[...root.querySelectorAll('input[type="radio"]')].filter(e=>e.id===target.id),e=nodes[0];
 if(nodes.length!==1||!panels[0].contains(e)||e.name!==target.name||e.value!==target.value||e.matches(':disabled')||!visible(e)&&![...e.labels||[]].some(visible)||
    [...e.labels||[]].map(l=>l.textContent).join(' ').replace(/\s+/g,' ').trim()!==target.label)throw Error('modern radio identity changed');
 const checked=[...root.querySelectorAll('input[type="radio"]')].filter(x=>x.name===target.name&&x.checked).map(x=>x.value);
 if(JSON.stringify(checked)!==JSON.stringify(target.retained))throw Error('modern radio retention changed');
 if(!e.checked)HTMLElement.prototype.click.call(e);return true;
}'''

# The same read-only normal-context guard runs synchronously in the final action
# evaluation. Awaited scope/check calls cannot open an identity race before click.
SERVICE_ACTION_JS = SERVICE_ACTION_JS.replace(
    " const visible=",
    " const scope=(" + SERVICE_READ_JS + ")({source_url});"
    "if(scope.observed!==true)throw Error('modern atomic scope changed');"
    "if(action==='radio'&&(!target||JSON.stringify(scope.base_context)!==JSON.stringify(target.expected_base_context)||"
    "JSON.stringify(scope.selection_context)!==JSON.stringify(target.expected_selection_context)))throw Error('modern atomic context changed');\n const visible=",
    1,
)


def service_signature(raw):
    unknown = {"version": 3, "schema": SCHEMA, "status": "unknown", "groups": {}, "model_services": {}}
    if not isinstance(raw, dict) or raw.get("observed") is not True: return unknown
    groups = {}
    entries = raw.get("groups")
    if not isinstance(entries, list) or len(entries) != len(SERVICE_KEYS): return unknown
    for entry in entries:
        if not isinstance(entry, dict): return unknown
        key = entry.get("key")
        if key not in SERVICE_KEYS or key in groups: return unknown
        choices = entry.get("choices")
        if not isinstance(choices, list) or len(choices)>100 or type(entry.get("control_count")) is not int or entry["control_count"] != len(choices): return unknown
        if any(not isinstance(c,dict) or not isinstance(c.get("value"),str) or not re.fullmatch(r"[0-9]{1,12}",c["value"]) or type(c.get("checked")) is not bool for c in choices): return unknown
        selected = [str(c["value"]) for c in choices if c.get("checked") is True]
        state = "absent" if not choices else "selected" if len(selected) == 1 else "ambiguous"
        groups[key] = {"status": state, "values": selected}
    model = raw.get("model_services")
    if set(groups) != set(SERVICE_KEYS) or not isinstance(model, dict) or set(model) != set(MODEL_KEYS): return unknown
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,12}", v) for v in model.values()): return unknown
    result = {"version": 3, "schema": SCHEMA, "status": "ambiguous" if any(g["status"] == "ambiguous" for g in groups.values()) else "observed", "groups": groups, "model_services": deepcopy(model)}
    if raw.get("service_policy") is not None:
        try: validate_service_policy(raw["service_policy"], raw.get("source_url"))
        except ModernHeld: return unknown
        result["service_policy"] = raw["service_policy"]
    return result


def modern_bound(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 3 or value.get("schema") != SCHEMA or value.get("status") != "observed": return False
    if set(value.get("groups", {})) != set(SERVICE_KEYS) or set(value.get("model_services", {})) != set(MODEL_KEYS): return False
    if value.get("service_policy") is not None:
        if (value["service_policy"] != PDF_SERVICE_POLICY or value["model_services"].get("proofType") != "100"
                or value["model_services"].get("manualProofType") != "1"
                or not isinstance(value["groups"].get("proofType"), dict)
                or not isinstance(value["groups"].get("proofTypeForManual"), dict)
                or value["groups"].get("proofType", {}).get("values") != ["100"]
                or value["groups"].get("proofTypeForManual", {}).get("values") != ["1"]): return False
    return all(isinstance(g, dict) and g.get("status") in {"selected", "absent"} and
               isinstance(g.get("values"), list) and len(g["values"]) == (1 if g["status"] == "selected" else 0) and
               all(isinstance(v, str) and re.fullmatch(r"[0-9]{1,12}", v) for v in g["values"])
               for g in value["groups"].values()) and all(isinstance(v,str) and re.fullmatch(r"[0-9]{1,12}",v) for v in value["model_services"].values())


_INTENTS = (("proofType", r"manually processed(?:\s.*)?"),
            ("proofTypeForManual", r"no proof(?:\s*(?:\([^)]*\)|required))?"),
            ("fileDeliveryForManual", r"upload (?:files )?now(?:\s*\([^)]*\))?"),
            ("designServices", r"(?:no design(?: services?)?|none)(?:\s*\([^)]*\))?"))


def _intents(raw):
    policy = raw.get("service_policy")
    validate_service_policy(policy, raw.get("source_url"))
    if policy is None:
        return _INTENTS
    # Source manual-proof schema ID1 is exactly PDF Proof. This is a separate
    # requested scope, never an automatic substitute for unavailable No Proof.
    return tuple((name, r"pdf proof" if name == "proofTypeForManual" else pattern)
                 for name, pattern in _INTENTS)


def _policy_label(choice):
    value = choice.get("policy_label", choice.get("label"))
    return value if isinstance(value, str) and len(value) <= 500 else ""


def _service_choice_details(choices, name, pattern):
    # Only bounded counters and the known service key leave the page failure.
    offered = [c for c in choices if isinstance(c, dict)][:100]
    match = [c for c in offered if re.fullmatch(pattern, _policy_label(c), re.I)]
    return {"schema": "printograph-safe-service-choice-diagnostic/v1", "group_key": name,
            "offered_count": len(offered), "visible_count": sum(c.get("visible") is True for c in offered),
            "enabled_count": sum(c.get("disabled") is False for c in offered),
            "checked_count": sum(c.get("checked") is True for c in offered), "policy_match_count": len(match),
            "eligible_match_count": sum(c.get("visible") is True and c.get("disabled") is False for c in match)}


def policy_available(raw):
    if not isinstance(raw,dict) or not isinstance(raw.get("groups"),list):return False
    groups={g.get("key"):g for g in raw["groups"] if isinstance(g,dict)}
    try: intents = _intents(raw)
    except ModernHeld: return False
    for name,pattern in intents:
        choices=groups.get(name,{}).get("choices")
        if not isinstance(choices,list):return False
        if not choices and name=="designServices":continue
        selected=[c for c in choices if isinstance(c,dict) and c.get("checked") is True]
        if len(selected)!=1 or selected[0].get("disabled") is not False or selected[0].get("visible") is not True or re.fullmatch(pattern,_policy_label(selected[0]),re.I) is None:return False
        if raw.get("service_policy") == PDF_SERVICE_POLICY and name == "proofTypeForManual" and selected[0].get("value") != "1": return False
    return True


async def prepare_services(page, source_url, check, mark_action, settle, *, service_policy=None):
    """Choose only source-offered safe labels; values are learned, never constants."""
    validate_service_policy(service_policy, source_url)
    async def read():
        await check()
        raw = await page.evaluate(SERVICE_READ_JS, {"source_url": source_url})
        if raw.get("observed") is not True: raise ModernHeld(raw.get("reason", "modern_service_unavailable"))
        return bind_service_policy(raw, service_policy, source_url)
    raw = await read()
    if not raw["open"]:
        await mark_action()
        await page.evaluate(SERVICE_ACTION_JS, {"source_url": source_url, "action": "open", "target": None})
        await settle(); raw = await read()
    policy = {}
    for name, pattern in _intents(raw):
        raw = await read(); group = next(g for g in raw["groups"] if g["key"] == name)
        if not group["choices"] and name == "designServices":
            # No offered design radio is structural evidence. Its exact native
            # model value remains in the signature; no default ID is invented.
            continue
        candidates = [c for c in group["choices"] if not c["disabled"] and c["visible"] and re.fullmatch(pattern, _policy_label(c), re.I)]
        if len(candidates) != 1: raise ModernHeld("modern_safe_service_choice_unavailable", _service_choice_details(group["choices"], name, pattern))
        if service_policy == PDF_SERVICE_POLICY and name == "proofTypeForManual" and candidates[0]["value"] != "1":
            raise ModernHeld("modern_pdf_proof_identity_unproven")
        target = candidates[0]; policy[name] = target["value"]
        if not target["checked"]:
            await mark_action(); await check()
            await page.evaluate(SERVICE_ACTION_JS, {"source_url": source_url, "action": "radio", "target": {
                **target, "retained": [c["value"] for c in group["choices"] if c["checked"]],
                "expected_base_context": raw["base_context"], "expected_selection_context": raw["selection_context"]}})
            await settle()
    raw = await read()
    for name, value in policy.items():
        chosen = [c for g in raw["groups"] if g["key"] == name for c in g["choices"] if c["checked"]]
        if len(chosen)!=1 or chosen[0]["value"]!=value or chosen[0].get("disabled") is not False or chosen[0].get("visible") is not True: raise ModernHeld("modern_service_policy_reset")
    if not modern_bound(service_signature(raw)): raise ModernHeld("modern_service_identity_unbound")
    return raw, policy


def _key(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _prices(data):
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 10000: raise ModernHeld("modern_price_schema_unproven")
    result = {}
    for row in rows:
        if not isinstance(row, dict): raise ModernHeld("modern_price_schema_unproven")
        q, amount = row.get("quantity"), row.get("markupPrice")
        if isinstance(q, bool) or not re.fullmatch(r"[1-9][0-9]{0,7}", str(q)) or isinstance(amount,bool) or len(str(amount))>32: raise ModernHeld("modern_price_schema_unproven")
        try: amount = Decimal(str(amount))
        except InvalidOperation: raise ModernHeld("modern_price_schema_unproven") from None
        if not amount.is_finite() or amount < 0 or amount > 100000000: raise ModernHeld("modern_price_schema_unproven")
        if str(q) in result and result[str(q)] != str(amount): raise ModernHeld("modern_price_schema_unproven")
        result[str(q)] = str(amount)
    return result


def _generic_prices(data, maximum=None):
    """Actual generic API rows; unavailable quantities never become prices."""
    rows = data.get("items") if type(data) is dict else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000: raise ModernHeld("modern_generic_price_schema_unproven")
    result = {}
    for row in rows:
        if type(row) is not dict or type(row.get("available")) is not bool: raise ModernHeld("modern_generic_price_schema_unproven")
        q = row.get("quantity")
        if type(q) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,7}", str(q)) or str(q) in result or maximum is not None and int(q)>maximum: raise ModernHeld("modern_generic_price_schema_unproven")
        def money(value, reference=False):
            if value is None and (reference or row["available"] is False): return None
            if type(value) not in (str, int, float, Decimal) or len(str(value)) > 32: raise ModernHeld("modern_generic_price_schema_unproven")
            try: amount = Decimal(str(value))
            except InvalidOperation: raise ModernHeld("modern_generic_price_schema_unproven") from None
            if not amount.is_finite() or amount < 0 or amount > 100000000: raise ModernHeld("modern_generic_price_schema_unproven")
            return str(amount)
        result[str(q)] = {"product_price": money(row.get("productPrice")), "subtotal": money(row.get("subtotal"), reference=True), "available": row["available"]}
    return result


def _generic_context(raw):
    """Full source-projected identity, including dimensions and native services."""
    try:
        value = raw["generic_price_context"]
        if type(value) is not dict or value.get("observed") is not True: raise ValueError()
        binding = {"product_id": raw["base_context"]["product_id"], **{k: raw["selection_context"][k] for k in ("specs", "options", "services", "production")}}
        if value.get("binding") != binding or not isinstance(value.get("properties"), dict) or not isinstance(value.get("turnaround"), dict): raise ValueError()
        choices = value.get("quantity_choices")
        if not isinstance(choices, list) or not 1 <= len(choices) <= 10000 or len(set(choices)) != len(choices) or any(type(q) is not str or not re.fullmatch(r"[1-9][0-9]{0,7}", q) for q in choices) or raw["selection_context"]["quantity"] not in choices or int(raw["selection_context"]["quantity"])>int(choices[-1]): raise ValueError()
        props = value["properties"]
        if set(props) != {"size", "printProductType", "turnaround", "quantity", "other"} or type(props["other"]) is not dict or props["size"] != binding["specs"]["size"] or props["printProductType"] != raw["product_type"] or props["turnaround"] != binding["production"] or props["quantity"] != "1-" + choices[-1]: raise ValueError()
        dims=value.get("dimensions")
        if type(dims) is not dict or set(dims)!={"width","height"} or any(type(v) is not str or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",v) or not Decimal(v).is_finite() or not 0<Decimal(v)<1000000 for v in dims.values()): raise ValueError()
        if props["other"].get("widthId") != dims["width"] or props["other"].get("heightId") != dims["height"]: raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, InvalidOperation): raise ModernHeld("modern_generic_context_unproven") from None


def _native_json(body, reason):
    if not isinstance(body, str) or not 0 < len(body) <= 16384: raise ModernHeld(reason)
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise ValueError()
            out[key] = value
        return out
    def constant(value): raise ValueError()
    try: return json.loads(body, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, TypeError, RecursionError): raise ModernHeld(reason) from None


def _scalar_tree(value, depth=0):
    """Native JSON number/string IDs can differ in type, never in value."""
    if depth > 6: raise ValueError()
    if type(value) is dict:
        if len(value)>100 or any(type(k) is not str or len(k)>64 or k in {"__proto__","constructor","prototype"} for k in value): raise ValueError()
        return {k:_scalar_tree(v, depth+1) for k,v in value.items()}
    if type(value) is list:
        if len(value)>32: raise ValueError()
        return [_scalar_tree(v, depth+1) for v in value]
    if type(value) not in (str,int,float) or len(str(value))>500 or any(ord(c)<32 or ord(c)==127 for c in str(value)): raise ValueError()
    if type(value) in (int,float) and not Decimal(str(value)).is_finite(): raise ValueError()
    return str(value)


def _generic_price_request(body, raw):
    value = _native_json(body, "modern_generic_price_request_unproven")
    context = _generic_context(raw)
    try:
        if type(value) is not dict or set(value)!={"properties"} or _scalar_tree(value["properties"]) != _scalar_tree(context["properties"]): raise ValueError()
        return hashlib.sha256(_key(value).encode()).hexdigest(), _key(context)
    except (TypeError, ValueError, InvalidOperation): raise ModernHeld("modern_generic_price_request_unproven") from None


def _production_identity(raw):
    if raw.get("generic_price_context",{}).get("observed") is not True:
        return raw["selection_context"]
    # Native turnaroundParam requests both Regular/Rush and does not include
    # the selected production ID. Reuse only this owned page's exact response
    # across that one native mode change; current price remains mode-bound.
    context = deepcopy(_generic_context(raw))
    selection = deepcopy(raw["selection_context"])
    del selection["production"]
    del context["binding"]["production"]
    del context["properties"]["turnaround"]
    return {"selection": selection, "generic_context": context}


def _production(data):
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20: raise ModernHeld("modern_production_schema_unproven")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "range"} or type(row["id"]) is not int or row["id"] <= 0: raise ModernHeld("modern_production_schema_unproven")
        value = row["range"]
        if not isinstance(value,dict) or not {"min","max"} <= set(value) or any(v is not None for k,v in value.items() if k not in {"min","max"}): raise ModernHeld("modern_production_schema_unproven")
        minimum, maximum = value["min"], value["max"]
        if type(minimum) is not int or not 0<=minimum<=365 or maximum is not None and (type(maximum) is not int or not minimum<=maximum<=365): raise ModernHeld("modern_production_schema_unproven")
        if str(row["id"]) in result: raise ModernHeld("modern_production_schema_unproven")
        result[str(row["id"])] = {"min": minimum, "max": maximum}
    return result


def _turnaround_request(body, raw):
    # The current public native builder emits only these product fields. Never
    # persist the raw body, unknown keys or values; no request is manufactured.
    if not isinstance(body,str) or not 0<len(body)<=16384: raise ModernHeld("modern_turnaround_request_unproven")
    try:
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out:raise ValueError()
                out[k]=v
            return out
        value=json.loads(body,object_pairs_hook=unique)
        if type(value) is not dict or set(value)!={"quantity","specifications","productOptions","itemType"}:raise ValueError()
        integer=lambda v: type(v) in (str,int) and re.fullmatch(r"[1-9][0-9]{0,11}",str(v)) is not None
        item_type=raw.get("source_item_type")
        if type(item_type) is not dict or item_type.get("observed") is not True or type(item_type.get("is_blank_product")) is not bool:raise ValueError()
        expected_item_type="13" if item_type["is_blank_product"] else raw["selection_context"]["services"]["fileDelivery"]
        if item_type.get("value")!=expected_item_type or not integer(value["itemType"]) or str(value["itemType"])!=expected_item_type:raise ValueError()
        if not integer(value["quantity"]) or str(value["quantity"])!=raw["selection_context"]["quantity"]:raise ValueError()
        specs=value["specifications"]
        if type(specs) is not dict or set(specs)!={"printProduct","other"} or type(specs["other"]) is not dict:raise ValueError()
        if raw.get("generic_price_context",{}).get("observed") is True:
            generic = _generic_context(raw)["turnaround"]
            expected = {**generic["printProduct"], "turnarounds": ["1","3"]}
            if (_scalar_tree(specs["printProduct"]) != _scalar_tree(expected) or _scalar_tree(specs["other"]) != _scalar_tree(generic["other"]) or
                _scalar_tree(value["productOptions"]) != _scalar_tree(generic["options"]) or raw["selection_context"]["production"] not in {"1","3"}): raise ValueError()
            return hashlib.sha256(_key(value).encode()).hexdigest()
        if specs["other"]!={}:raise ValueError()
        product=specs["printProduct"];known=raw["selection_context"]["specs"]
        if type(product) is not dict or not {"productType","turnarounds"}<=set(product) or set(product)-{"productType","turnarounds"}-(set(known)&TURNAROUND_FIELDS):raise ValueError()
        if str(product["productType"])!=str(raw["product_type"]) or not set(known)&TURNAROUND_FIELDS<=set(product):raise ValueError()
        if any(not integer(v) or str(v)!=known[k] for k,v in product.items() if k not in {"productType","turnarounds"}):raise ValueError()
        ids=product["turnarounds"]
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(not integer(v) for v in ids) or len({str(v) for v in ids})!=len(ids) or raw["selection_context"]["production"] not in [str(v) for v in ids]:raise ValueError()
        options=value["productOptions"];selected=raw["selection_context"]
        # Native common.updateOptions deliberately omits the presentation-only
        # paperFinish field. It remains in full selection/context identity.
        required={str(v) for k,v in selected["options"].items() if k!="paperFinish" and str(v) not in {"","0"}}|{selected["services"]["manualProofType"]}
        allowed=required|{selected["services"]["designServices"]}
        if not isinstance(options,list) or not 1<=len(options)<=32 or any(not integer(v) or str(v) not in allowed for v in options) or not required<={str(v) for v in options} or len({str(v) for v in options})!=len(options):raise ValueError()
        return hashlib.sha256(_key(value).encode()).hexdigest()
    except (ValueError,KeyError,TypeError,RecursionError):raise ModernHeld("modern_turnaround_request_unproven") from None


class FreshQuoteGate:
    """Only this owned page's successful native price and production receipts.

    Price tables cover quantities only within their exact retained base context;
    no price arithmetic is performed. Production binds the entire current native
    selection. Old/foreign responses invalidate rather than overwrite freshness.
    """
    def __init__(self, *, service_policy=None, source_url=None):
        validate_service_policy(service_policy, source_url)
        self.service_policy = service_policy; self.source_url = source_url
        self.prices = {}; self.generic_prices = {}; self.production = {}; self.invalid = None; self.order = 0

    def _policy_scope(self, raw):
        if self.service_policy is None:
            return not isinstance(raw, dict) or raw.get("service_policy") is None
        return (isinstance(raw, dict) and raw.get("service_policy") == self.service_policy
                and raw.get("source_url") == self.source_url)

    def observe(self, url, status, data, before, after, evidence_id, generation, current_generation, epoch, current_epoch, method="GET", request_body=None):
        path = urlsplit(url).path
        price = re.fullmatch(r"/service/rest/v1/products/([1-9][0-9]*)/prices", path)
        generic_price = path == "/service/rest/v1/product/prices"
        turnaround = path == "/service/rest/v1/rules/turnarounds"
        if not (price or generic_price or turnaround): return
        self.order += 1
        if (not isinstance(before,dict) or not isinstance(after,dict) or before.get("observed") is not True or after.get("observed") is not True or not modern_bound(service_signature(after))):
            if self.prices or self.generic_prices or self.production:self.invalid = "modern_unbound_response_after_evidence"
            return
        if not self._policy_scope(before) or not self._policy_scope(after):
            self.invalid = "modern_service_policy_scope_unproven"; return
        if (generation != current_generation or epoch != current_epoch or not isinstance(before,dict) or not isinstance(after,dict) or
            urlsplit(url).scheme != "https" or urlsplit(url).netloc != urlsplit(after.get("source_url", "")).netloc or
            before.get("source_url") != after.get("source_url") or
            not isinstance(evidence_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", evidence_id) or
            before.get("observed") is not True or after.get("observed") is not True or
            before.get("base_context") != after.get("base_context") or
            before.get("selection_context") != after.get("selection_context") or status != 200 or
            ((generic_price or turnaround and after.get("generic_price_context",{}).get("observed") is True) and before.get("generic_price_context") != after.get("generic_price_context")) or
            not modern_bound(service_signature(after))):
            self.invalid = "modern_foreign_or_changed_response"; return
        try:
            if generic_price:
                if method != "POST" or urlsplit(url).query != "itemId=null&cid=" or urlsplit(url).fragment: raise ModernHeld("modern_generic_price_request_unproven")
                request_hash, identity = _generic_price_request(request_body, after)
                maximum = int(after["generic_price_context"]["quantity_choices"][-1])
                self.generic_prices[identity] = {"rows": _generic_prices(data, maximum), "evidence_id": evidence_id, "order": self.order, "request_sha256": request_hash}
            elif price:
                if method != "GET" or urlsplit(url).query not in {"", "itemId=null&cid="}:raise ModernHeld("modern_price_request_unproven")
                if price.group(1) != after["base_context"]["product_id"]: raise ModernHeld("modern_foreign_product_price")
                self.prices[_key(after["base_context"])] = {"rows": _prices(data), "evidence_id": evidence_id, "order": self.order}
            else:
                if method != "POST" or urlsplit(url).query:raise ModernHeld("modern_turnaround_request_unproven")
                request_hash = _turnaround_request(request_body, after)
                self.production[_key(_production_identity(after))] = {"rows": _production(data), "evidence_id": evidence_id, "order": self.order, "request_sha256": request_hash}
        except (KeyError, TypeError, ValueError) as exc:
            self.invalid = exc.reason if isinstance(exc,ModernHeld) else "modern_response_schema_unproven"

    def verify(self, raw, production, controls=None, displayed_subtotal=None):
        if not self._policy_scope(raw): raise ModernHeld("modern_service_policy_scope_unproven")
        if self.invalid: raise ModernHeld(self.invalid)
        if not modern_bound(service_signature(raw)): raise ModernHeld("modern_service_identity_unbound")
        if not policy_available(raw):raise ModernHeld("modern_service_policy_unavailable")
        if controls is not None:
            def key(value): return re.sub(r"[^a-z0-9]", "", str(value).lower())
            selected = {**raw["selection_context"]["specs"], **raw["selection_context"]["options"],
                        "quantity": raw["selection_context"]["quantity"], "productionTime": raw["selection_context"]["production"]}
            normalized = {key(k):str(v) for k,v in selected.items()}
            if any(key(c.get("key")) not in normalized or str(c.get("value")) != normalized[key(c.get("key"))] for c in controls):
                raise ModernHeld("modern_model_dom_product_mismatch")
        generic = raw.get("generic_price_context",{}).get("observed") is True
        base = self.generic_prices.get(_key(_generic_context(raw))) if generic else self.prices.get(_key(raw["base_context"]))
        current = self.production.get(_key(_production_identity(raw)))
        q = raw["selection_context"]["quantity"]
        if not base or q not in base["rows"]: raise ModernHeld("modern_current_price_evidence_missing")
        native_quantity_enabled = False
        if generic:
            # The pinned native generic table consumer applies productPrice and
            # itemOptionsPrice without consulting available. That API boolean
            # is reference metadata, not stock or checkout proof. Only the two
            # source-verified URL/type pairs may quote a false-flag row, and only
            # when the ordinary visible enabled quantity control retains it.
            quantity_controls = [c for c in (controls or []) if c.get("key") == "quantity"]
            if len(quantity_controls) == 1:
                control = quantity_controls[0]
                choices = control.get("choices")
                matches = [c for c in choices if isinstance(c, dict) and str(c.get("value")) == q] if isinstance(choices, list) else []
                native_quantity_enabled = (control.get("kind") == "select" and str(control.get("value")) == q and
                    len(matches) == 1 and matches[0].get("disabled") is False and matches[0].get("placeholder") is False)
            if base["rows"][q]["available"] is False and (GENERIC_REFERENCE_FLAG_SCOPES.get(raw.get("source_url")) != raw.get("product_type") or not native_quantity_enabled):
                raise ModernHeld("modern_quantity_unavailable")
        total=raw.get("native_total")
        try:
            if type(total) is not dict or total.get("observed") is not True:raise ValueError()
            def amount(value):
                if type(value) not in (str,int,float,Decimal) or isinstance(value,bool):raise ValueError()
                value=Decimal(str(value))
                if not value.is_finite() or value<0 or value>100000000:raise ValueError()
                return value
            native=amount(total["controller_total"]);applied=amount(total["applied_product_price"])
            shown=amount(displayed_subtotal)
            expected_base = base["rows"][q]["product_price"] if generic else base["rows"][q]
            if native<=0 or shown!=native or native!=native.quantize(Decimal(".01")) or applied!=amount(expected_base):raise ValueError()
        except (KeyError,ValueError,InvalidOperation):raise ModernHeld("modern_native_subtotal_unproven") from None
        if not current or not isinstance(production, dict): raise ModernHeld("modern_current_production_evidence_missing")
        if str(production.get("value")) != raw["selection_context"]["production"]: raise ModernHeld("modern_rendered_production_stale")
        source_range = current["rows"].get(str(production.get("value")))
        label = str(production.get("selected_label", ""))
        match = re.fullmatch(r"(?:Regular|Rush|Standard)?\s*([0-9]{1,3})(?:\s*[-–]\s*([0-9]{1,3}))?\s*(?:Business\s+)?Days?", label, re.I)
        numeric_label_matches = bool(match and {"min": int(match[1]), "max": int(match[2]) if match[2] else None} == source_range)
        presentation = raw.get("native_nextday_presentation")
        nextday_alias_matches = (raw.get("source_url") == "https://www.printograph.com/products/cardboards/order" and
            raw.get("product_type") == "10016" and str(production.get("value")) == "3" and
            label == "Rush Following Business Day" and source_range == {"min": 2, "max": None} and
            type(presentation) is dict and presentation.get("observed") is True and
            presentation == {"observed": True, "store": "101", "rush": "2 Business Days"})
        if not source_range or not (numeric_label_matches or nextday_alias_matches):
            raise ModernHeld("modern_rendered_production_stale")
        return {"schema": "printograph-native-dom-quote-freshness/v1", "price_evidence_id": base["evidence_id"],
                **({"service_policy": self.service_policy} if self.service_policy is not None else {}),
                **({"price_request_sha256": base["request_sha256"], "native_price_schema": "generic-product-price/v1", "source_api_available_flag": base["rows"][q]["available"], "stock_availability_verified": False, "native_enabled_quantity_observed": native_quantity_enabled, "api_subtotal_reference_only": True} if generic else {}),
                "production_evidence_id": current["evidence_id"], "production_request_sha256": current["request_sha256"], "price_basis": "source_displayed_total",
                "quantity_in_owned_price_table": True, "production": source_range,
                **({"production_label_basis": "source_native_nextday_alias_numeric_range_retained"} if nextday_alias_matches else {}),
                "native_computed_subtotal_matches_display": True, "current_quantity_applied_base_matches_table": True,
                "component_arithmetic_verified": False, "source_data_finalized": False}
