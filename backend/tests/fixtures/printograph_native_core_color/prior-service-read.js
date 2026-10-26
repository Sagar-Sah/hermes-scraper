({source_url}) => {
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
        (k.startsWith('hid')?(!own(hid,k)||hid[k]!==v):!/^[A-Za-z][A-Za-z ]{0,63}$/.test(k))))return fail();
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
 return {observed:true,source_url,product_type:root.getAttribute('data-printproducttype'),process_header:processHeader,open:panel.classList.contains('show'),native_total,source_item_type,generic_price_context,
   groups,model_services:services,base_context:{product_id:String(selected.productId),specs,production:String(selected.productionTime)},
   selection_context:{specs,options,services,quantity:String(selected.quantity),production:String(selected.productionTime)}};
}