"""Inspect the ordinary B2Sign quote panel without changing its selections.

MUI listbox choices exist only while open. One bounded pass opens each observed
combobox, reads that button's own listbox, and restores its closed state. Display
labels are explicitly surrogates, never source SKU or option identifiers.
"""

from __future__ import annotations

import asyncio


B2SIGN_SNAPSHOT_JS = r"""() => {
 const clean=(s,n=240)=>String(s||'').replace(/\s+/g,' ').trim().slice(0,n);
 const visible=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden'&&e.getClientRects().length>0;
 const text=e=>clean(e?.innerText||e?.textContent);
 const roots=[...document.querySelectorAll('div[class*="_instant-quote_"]')].filter(visible);
 const inside=e=>roots.some(r=>r.contains(e));
 const wrappers=[...document.querySelectorAll('div[class*="_attribute_"]')];
 const fields=[],customs=[],warnings=[],handledWrappers=new Set(),handledNative=new Set();
 const common=(w,label,kind)=>({label,kind,source_id:w.id||'',source_name:'',source_key:w.id||label,
   required:false,disabled:false,multiple:false,configurable:true,binding_kind:'display_label_only',
   value_origin:'display_label_surrogate',choices:[],selected_values:[]});
 const option=(label,disabled=false)=>({value:label,label,disabled,value_origin:'display_label_surrogate'});
 const staticIds=new Set(['attr-sides','attr-material','attr-print','attr-polepocket','attr-hem','attr-grommet','attr-lamination','attr-lam']);
 const sizeRoots=[...document.querySelectorAll('div[id^="attr-"][class*="_size-wrapper_"],div[class*="_attribute_"]')]
   .filter(e=>inside(e)&&visible(e)&&(e.matches('[class*="_size-wrapper_"]')||
     text(e.querySelector(':scope > label'))==='Size'&&!e.querySelector('[class*="_size-wrapper_"]')));
 const processedSizeInputs=new Set();
 for(const root of sizeRoots){
   const axes=[...root.querySelectorAll('div[class*="_input-wrapper_"]')].filter(visible);
   const labels=axes.map(a=>clean(a.querySelector('.MuiInput-startDecorator')?.textContent));
   const inputs=axes.map(a=>a.querySelector('input'));
   const inches=axes.length===2&&labels.join('|')==='Width|Height';
   const feetInches=axes.length===4&&['Width','Height'].every((name,index)=>{
     const row=[...root.querySelectorAll('div[class*="_attribute_"]')].filter(e=>text(e.querySelector(':scope > label'))===name)[0];
     return row&&row.contains(axes[index*2])&&row.contains(axes[index*2+1])&&
       axes[index*2].className.includes('_feet_')&&axes[index*2+1].className.includes('_inches_');
   });
   const selects=[...root.querySelectorAll('button[role="combobox"]')].filter(visible);
   const selectedInputs=selects.map(button=>button.parentElement?.querySelector('input[aria-hidden="true"]'));
   const selectLabels=selects.map(button=>clean(button.parentElement?.querySelector('.MuiSelect-startDecorator')?.textContent));
   const finiteSelect=selects.length===2&&selectLabels.every((label,index)=>label.endsWith(index?'Height':'Width'))&&
     selectedInputs.every(input=>input&&/^\d+(?:\.\d+)?$/.test(input.value));
   if((!inches&&!feetInches||inputs.some(e=>!e||!visible(e)))&&!finiteSelect)continue;
   const sizeInputs=finiteSelect?selectedInputs:inputs;
   if(sizeInputs.every(e=>processedSizeInputs.has(e)))continue;
   for(const input of sizeInputs)processedSizeInputs.add(input);
   const field=common(root,feetInches?'Size':text(root.querySelector('label'))||'Size','size');
   field.binding_kind='native_control';field.value_origin='native_value';
   field.source_key=root.id?root.id.slice(5):'_size';
   field.selected_values=[finiteSelect?selectedInputs.map(e=>e.value).join('x'):inches?inputs.map(e=>e.value).join('x'):
     [0,2].map(i=>String(Number(inputs[i].value)*12+Number(inputs[i+1].value))).join('x')];
   field.disabled=sizeInputs.some(e=>e.disabled);fields.push(field);
   handledWrappers.add(root);
   for(const wrapper of wrappers)if(root.contains(wrapper)||wrapper.contains(root)&&text(wrapper.querySelector(':scope > label'))==='Size')handledWrappers.add(wrapper);
 }
 for(let index=0;index<wrappers.length;index++){
   const w=wrappers[index]; if(!inside(w)||!visible(w)||handledWrappers.has(w))continue;
   const label=text(w.querySelector(':scope > label'));
   const button=w.querySelector('button[role="combobox"],button.MuiSelect-button');
   if(button&&visible(button)&&label){
     const field=common(w,label,'custom_select'); field.disabled=button.disabled||button.getAttribute('aria-disabled')==='true';
     const selected=text(button); if(selected){field.choices=[option(selected)];field.selected_values=[selected];}
     fields.push(field);handledWrappers.add(w);customs.push({wrapper_index:index,field_index:fields.length-1,selected,disabled:field.disabled});
   }else if(staticIds.has(w.id)&&label){
     const value=clean([...w.children].filter(e=>e.tagName!=='LABEL'&&visible(e)).map(text).join(' '));
     if(value){const field=common(w,label,'static');field.choices=[option(value)];field.selected_values=[value];fields.push(field);
       if(![...w.querySelectorAll('input,select,button,textarea,[role="combobox"],[role="slider"]')].some(visible))handledWrappers.add(w);}
   }else if(w.id==='attr-hardware'||w.id==='attr-tent_hardware'){
     const items=[...w.querySelectorAll('div[class*="_component_"][class*="_button_"] > div[class*="_item_"]')].filter(visible);
     if(items.length){
       const labels=items.map(text).filter(Boolean);
       const field=common(w,label||labels.join(' / '),'custom_select');
       field.choices=items.map(e=>option(text(e),e.getAttribute('aria-disabled')==='true'));
       field.selected_values=items.filter(e=>[...e.classList].some(c=>c.includes('_active_'))||e.getAttribute('aria-pressed')==='true').map(text);
       if(!label){field.warnings=['field_label_from_choices'];warnings.push('field_label_from_choices');}
       fields.push(field);handledWrappers.add(w);
     }
   }
 }
 const native=[...new Set(roots.flatMap(r=>[...r.querySelectorAll('input')]))];
 const groups=new Map();
 const nativeLabel=e=>clean([...e.labels||[]].filter(visible).map(text).join(' '))||clean(e.getAttribute('aria-label'));
 const choiceLabel=e=>{
   const label=[...e.labels||[]].find(visible);
   return text(label?.querySelector('span[class*="_option_"]'))||nativeLabel(e);
 };
 const heading=e=>{
   for(let parent=e.parentElement;parent&&inside(parent);parent=parent.parentElement){
     const candidate=[...parent.querySelectorAll(':scope > label,:scope > legend,:scope > h2,:scope > h3,:scope > h4,:scope > h5,:scope > h6,:scope > span[class*="_label_"]')].find(x=>visible(x)&&!x.contains(e)&&!x.getAttribute('for')&&!x.querySelector('input,select,button'));
     if(candidate)return text(candidate);
   }
   return '';
 };
 for(const e of native){
   if(!visible(e)&&![...e.labels||[]].some(visible))continue;
   if(e.type==='radio'){
     if(!e.name)continue;
     if(!groups.has(e.name))groups.set(e.name,[]);groups.get(e.name).push(e);
   }else if(e.type==='checkbox'){
     const label=nativeLabel(e);if(!/^email\s*proof$/i.test(label))continue;
     const field=common(e,label,'checkbox');field.source_name=e.name||'';field.source_key=e.name||e.id||label;
     field.multiple=true;field.value_origin='native_value';field.binding_kind=e.name||e.id?'native_control':'display_label_only';
     field.choices=[{value:e.value,label,disabled:e.matches(':disabled'),value_origin:'native_value'}];
     field.selected_values=e.checked?[e.value]:[];field.disabled=e.matches(':disabled');fields.push(field);handledNative.add(e);
   }else if(['number','text'].includes(e.type)){
     if(e.closest('div[class*="_jobname_"]'))continue;
     const quantity=e.closest('div[class*="_quantity_"]');
     const previous=quantity?.previousElementSibling;
     const precedingLabel=previous&&[...previous.classList].some(c=>c.includes('_label_'))&&visible(previous)?text(previous):'';
     const label=nativeLabel(e)||precedingLabel||heading(e);
     if(!/^(?:qty|quantity)\b/i.test(label))continue;
     const field=common(e,label,'number');field.source_name=e.name||'';
     field.binding_kind=e.name||e.id?'native_control':'display_label_only';field.value_origin='native_value';
     field.source_key=e.name||e.id||label;field.required=e.required;field.disabled=e.matches(':disabled');
     const numeric=s=>/^\d+(?:\.\d+)?$/.test(s||'');
     field.selected_values=numeric(e.value)?[e.value]:[];
     for(const k of ['min','max','step'])field[k]=numeric(e.getAttribute(k))?e.getAttribute(k):null;
     fields.push(field);handledNative.add(e);
   }
 }
 for(const [name,group] of groups){
   const label=heading(group[0]);if(!label||!/turnaround|shipping/i.test(label))continue;
   const field=common(group[0],label,'radio');field.source_name=name;field.source_key=name;
   field.binding_kind='native_control';field.value_origin='native_value';
   field.choices=group.map(e=>({value:e.value,label:choiceLabel(e),disabled:e.matches(':disabled'),value_origin:'native_value'}));
   field.selected_values=group.filter(e=>e.checked).map(e=>e.value);
   field.disabled=group.every(e=>e.matches(':disabled'));field.required=group.some(e=>e.required);
   fields.push(field);for(const e of group)handledNative.add(e);
 }
 // An unfamiliar attribute is a coverage gap, not an absent option. Native
 // controls handled above may live inside an attribute wrapper on other pages.
 // Do not copy arbitrary wrapper text into a guessed static product field.
 for(const w of wrappers){
   if(!inside(w)||!visible(w)||handledWrappers.has(w))continue;
   const inputs=[...w.querySelectorAll('input')].filter(e=>visible(e)||[...e.labels||[]].some(visible));
   const others=[...w.querySelectorAll('select,button,textarea,[role="combobox"],[role="slider"]')].filter(visible);
   if(!inputs.length&&!others.length&&[...w.classList].some(c=>c.includes('_size-area_')))continue;
   if(inputs.length&&!others.length&&inputs.every(e=>handledNative.has(e)))continue;
   warnings.push('unsupported_visible_attribute');break;
 }
 const title=text(document.querySelector('#item-view-title'));
 const main=document.querySelector('div[class*="_item-main_"]');
 const images=main?[...main.querySelectorAll('img')].filter(e=>visible(e)&&clean(e.alt)===title).slice(0,50).map(e=>({url:e.currentSrc||e.src||'',alt:clean(e.alt)})):[];
 const descriptionHeading=document.querySelector('h4#description');
 const paragraphs=[];
 if(descriptionHeading?.closest('div[class*="_rich-text_"]')){
   for(let e=descriptionHeading.nextElementSibling;e&&!/^H[1-6]$/.test(e.tagName);e=e.nextElementSibling){
     if(e.tagName==='P'&&visible(e))paragraphs.push(clean(e.innerText,8000));
   }
 }
 const description=clean(paragraphs.join(' '),8000);
 return {root_found:roots.length>0,title,images,description,fields,customs,warnings,
   busy:roots.some(r=>[...r.querySelectorAll('[aria-busy="true"],.MuiCircularProgress-root')].some(visible))};
}"""


LISTBOX_JS = r"""button => {
 const clean=s=>String(s||'').replace(/\s+/g,' ').trim().slice(0,240);
 const id=button.getAttribute('aria-controls');
 const list=id?document.getElementById(id):null;
 if(!list||list.getAttribute('role')!=='listbox'||!list.getClientRects().length)return null;
 return [...list.querySelectorAll('[role="option"]')].filter(e=>e.getClientRects().length>0).slice(0,500).map(e=>{
   const label=clean(e.innerText||e.textContent);
   return {value:label,label,disabled:e.getAttribute('aria-disabled')==='true'||e.hasAttribute('disabled'),value_origin:'display_label_surrogate'};
 });
}"""


async def inspect_b2sign(page, *, expand=False):
    snapshot = await page.evaluate(B2SIGN_SNAPSHOT_JS)
    if not snapshot.get('root_found') or not expand:
        return snapshot
    warnings = snapshot['warnings']
    pass_deadline = asyncio.get_running_loop().time() + 12
    for descriptor in snapshot.pop('customs', [])[:30]:
        if asyncio.get_running_loop().time() >= pass_deadline:
            warnings.append('custom_dropdown_inspection_incomplete')
            break
        if descriptor['disabled']:
            continue
        button = page.locator('div[class*="_attribute_"]').nth(descriptor['wrapper_index']).locator('button[role="combobox"],button.MuiSelect-button').first
        opened = False
        field = snapshot['fields'][descriptor['field_index']]
        try:
            already_open = await button.get_attribute('aria-expanded') == 'true'
            if not already_open:
                await button.click(timeout=1500)
                opened = True
            deadline = asyncio.get_running_loop().time() + 1.5
            choices = None
            while asyncio.get_running_loop().time() < deadline:
                choices = await button.evaluate(LISTBOX_JS)
                if choices:
                    break
                await asyncio.sleep(0.05)
            if not choices:
                warnings.append('custom_dropdown_choices_unavailable')
            elif len({choice['value'] for choice in choices}) != len(choices):
                warnings.append('custom_dropdown_labels_ambiguous')
                field['choices'] = choices
            else:
                field['choices'] = choices
        except Exception:
            warnings.append('custom_dropdown_inspection_failed')
        finally:
            if opened:
                try:
                    await page.keyboard.press('Escape')
                    if await button.get_attribute('aria-expanded') == 'true':
                        warnings.append('custom_dropdown_close_failed')
                except Exception:
                    warnings.append('custom_dropdown_close_failed')
        try:
            after = ' '.join((await button.inner_text()).split())[:240]
            if after != descriptor['selected']:
                warnings.append('custom_dropdown_selection_changed')
                field['selected_values'] = []
        except Exception:
            warnings.append('custom_dropdown_inspection_failed')
    snapshot['warnings'] = sorted(set(warnings))
    return snapshot


def merge_b2sign_snapshot(raw, b2):
    if not b2.get('root_found'):
        return raw
    # Source-specific panel owns its controls; generic main-page heuristics may
    # refer to unrelated header/search inputs and are not a second field source.
    result = dict(raw, fields=b2['fields'], busy=raw.get('busy') or b2.get('busy'))
    if b2.get('title'):
        result['title'] = b2['title']
        result['content'] = dict(raw.get('content') or {}, title=b2['title'])
    if b2.get('images'):
        result['content'] = dict(result.get('content') or {}, images=b2['images'])
    if b2.get('description'):
        result['content'] = dict(result.get('content') or {}, description=b2['description'])
    result['source_warnings'] = b2.get('warnings', [])
    return result
