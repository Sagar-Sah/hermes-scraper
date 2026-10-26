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
