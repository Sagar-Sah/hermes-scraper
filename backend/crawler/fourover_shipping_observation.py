"""Passive public shipping UI structure; no address-book values or mutations."""
import asyncio
from decimal import Decimal, InvalidOperation
import time
import re
from playwright.async_api import TimeoutError as SourceUITimeout


CURRENT_DESTINATION_JS = r'''() => {
  const visible=e=>e.getClientRects().length>0;
  const roots=[...document.querySelectorAll('.ship-from-address')].filter(visible);
  const codes=new Set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split(' '));
  const rows=roots.map(root=>{
    const postcodes=[...root.querySelectorAll('[data-bind="html: address.postcode"]')].filter(visible)
      .map(e=>String(e.textContent||'').trim()).filter(value=>/^\d{5}(?:-\d{4})?$/.test(value));
    let states=[...root.querySelectorAll('[data-bind*="getRegionCode"]')].filter(visible)
      .map(e=>String(e.textContent||'').trim().match(/([A-Z]{2})\s*$/)?.[1]).filter(value=>codes.has(value));
    if(!states.length&&postcodes.length===1){
      const text=String(root.textContent||'').replace(/\s+/g,' ');
      const match=text.match(new RegExp('([A-Z]{2})\\s*'+postcodes[0]));
      if(match&&codes.has(match[1]))states=[match[1]];
    }
    return {postcodes,states:[...new Set(states)]};
  });
  return rows.length===1&&rows[0].postcodes.length===1&&rows[0].states.length===1
    ?{status:'bound_postal_and_state',postcode:rows[0].postcodes[0],state:rows[0].states[0]}
    :{status:'postal_state_binding_missing_or_ambiguous'};
}'''


def forbidden_observation_write(method, path):
    return method.upper() in {'POST','PUT','PATCH','DELETE'} and (
        path.rstrip('/').endswith('/project/mine/address')
        or '/customer/address/' in path or '/checkout/cart/' in path or '/sales/order/' in path)


async def select_existing_benchmark_destination(page, postcode, state, timeout=12.0):
    """Use the ordinary existing-record selector, never a New Address action."""
    from .fourover_real_estate import QUOTE_DEADLINE
    if not re.fullmatch(r'\d{5}',postcode) or not re.fullmatch(r'[A-Z]{2}',state):
        raise ValueError('invalid_benchmark_destination')
    deadline=QUOTE_DEADLINE.get()
    if deadline is not None:timeout=min(timeout,deadline-time.monotonic()-2)
    if timeout<=0:raise ValueError('benchmark_destination_time_budget_exhausted')
    def retained(value):
        return value.get('status')=='bound_postal_and_state' and value.get('postcode','').split('-')[0]==postcode and value.get('state')==state
    try:
        async with asyncio.timeout(timeout):
            current=await page.evaluate(CURRENT_DESTINATION_JS)
            if retained(current):
                return {**current,'selection':'current_destination_retained','address_record_written':False,'account_default_changed':False}
            lookup=page.locator('.ship-from-address a.address-lookup[data-bind*="showAddressListPopup"]')
            if await lookup.count()!=1:raise ValueError('benchmark_address_lookup_missing_or_ambiguous')
            await lookup.click(timeout=3000)
            marker=re.compile(r'hermes|rate\s*test|shipping\s*test|quote\s*test|calibration|benchmark',re.I)
            postal=page.locator('[data-bind*="postcode"]').filter(has_text=re.compile(r'^\s*'+postcode+r'(?:-\d{4})?\s*$'))
            cards=page.locator('li.project-address-item:visible').filter(has_text=marker).filter(has=postal)
            await cards.first.wait_for(state='visible',timeout=4000)
            if await cards.count()!=1:raise ValueError('benchmark_destination_missing_or_ambiguous')
            select=cards.locator('[data-bind*="selectShippingAddress"]')
            if await select.count()==1:await select.click(timeout=3000)
            elif 'selectShippingAddress' in (await cards.get_attribute('data-bind') or ''):await cards.click(timeout=3000)
            else:raise ValueError('benchmark_destination_selection_control_unverified')
            dialog=page.locator('.modal-popup._show').filter(has=page.locator('li.project-address-item'))
            if await dialog.count()!=1:raise ValueError('benchmark_address_dialog_missing_or_ambiguous')
            apply=dialog.get_by_role('button',name=re.compile(r'^\s*SHIP HERE\s*$',re.I))
            if await apply.count()!=1:raise ValueError('benchmark_existing_address_apply_control_missing')
            await apply.click(timeout=3000)
            while True:
                selected=await page.evaluate(CURRENT_DESTINATION_JS)
                if retained(selected):
                    return {**selected,'selection':'existing_labeled_benchmark','benchmark_marker_verified':True,
                        'address_record_written':False,'account_default_changed':False}
                await asyncio.sleep(.1)
    except (TimeoutError, SourceUITimeout) as error:
        raise ValueError('benchmark_destination_not_retained_before_deadline') from error


SHIPPING_RATE_CHOICES_JS = r'''() => [...document.querySelectorAll('select[data-bind*="shipment.shippingMethod"]')]
  .map(e=>({visible:e.getClientRects().length>0,choices:[...e.options].filter(o=>!o.disabled)
    .map(o=>{const code=String(o.value||'').match(/^[a-z0-9]+_(\d{1,4})$/i);
      const amount=String(o.textContent||'').match(/\$\s*([0-9,]+\.\d{2})/);
      return code&&amount?{method_code:code[1],cents:Math.round(Number(amount[1].replace(/,/g,''))*100)}:null;})
    .filter(Boolean)}))'''


async def wait_for_shipping_hydration(page, responses, timeout=8.0):
    """Bounded wait for native response/KO options before capturing the view."""
    from .fourover_real_estate import QUOTE_DEADLINE
    deadline=QUOTE_DEADLINE.get()
    if deadline is not None:timeout=min(timeout,deadline-time.monotonic()-2)
    snapshot={'status':'not_hydrated_before_deadline','selector_count':0,'native_count':0,'choices':[]}
    if timeout<=0:return snapshot
    try:
        async with asyncio.timeout(timeout):
            while True:
                rows=[record for record in responses if record.get('status')==200 and record.get('rates')]
                rates=rows[-1]['rates'] if rows else []
                expected={}
                for rate in rates:
                    if rate.get('available') is not True or rate.get('method_code') is None:continue
                    try:amount=Decimal(str(rate.get('amount')))
                    except InvalidOperation:continue
                    if amount.is_finite() and amount>=0 and amount.as_tuple().exponent>=-2:
                        code=str(rate['method_code'])
                        if code in expected:expected={};break
                        expected[code]=int(amount*100)
                selectors=await page.evaluate(SHIPPING_RATE_CHOICES_JS)
                choices=selectors[0].get('choices',[]) if len(selectors)==1 else []
                observed={item['method_code']:item['cents'] for item in choices}
                snapshot={'status':'not_hydrated_before_deadline','selector_count':len(selectors),
                    'visible_selector_count':sum(item.get('visible') is True for item in selectors),
                    'native_count':len(expected),'choices':choices,
                    'binding_scope':'attached_native_shipping_method_options; not a destination or carrier-identity proof'}
                if expected and len(choices)==len(observed) and observed==expected:
                    return {**snapshot,'status':'matched'}
                await asyncio.sleep(.1)
    except TimeoutError:
        return snapshot


NATIVE_GROUND_OPTIONS_JS = r'''() => [...document.querySelectorAll('select[data-bind*="shipment.shippingMethod"]')]
  .map(e=>({visible:e.getClientRects().length>0,value:String(e.value||''),choices:[...e.options]
    .filter(o=>!o.disabled).map(o=>({value:String(o.value||''),label:String(o.textContent||'').replace(/\s+/g,' ').trim()}))}))'''


async def select_native_ground_shipping(page, shipping_responses, project_responses, chosen, timeout=8.0):
    """Normal native service choice; no inferred carrier, cart or order action."""
    from .fourover_real_estate import QUOTE_DEADLINE, requested_project_context, _money, wait_for_rendered_quote
    deadline=QUOTE_DEADLINE.get()
    if deadline is not None:timeout=min(timeout,deadline-time.monotonic()-2)
    if timeout<=0:raise ValueError('native_ground_selection_time_budget_exhausted')
    try:
        async with asyncio.timeout(timeout):
            destination=await page.evaluate(CURRENT_DESTINATION_JS)
            if destination.get('status')!='bound_postal_and_state':
                raise ValueError('native_ground_destination_unbound')
            hydrated=await wait_for_shipping_hydration(page,shipping_responses,timeout=timeout)
            if hydrated.get('status')!='matched':raise ValueError('native_ground_rates_not_hydrated')
            rows=await page.evaluate(NATIVE_GROUND_OPTIONS_JS)
            if len(rows)!=1 or rows[0].get('visible') is not True:
                raise ValueError('native_ground_selector_missing_or_ambiguous')
            choices=[]
            for item in rows[0].get('choices',[]):
                match=re.fullmatch(r'Ground Shipping\s+\$([0-9,]+\.\d{2})',item.get('label',''),re.I)
                if match and re.fullmatch(r'[a-z0-9]+_\d{1,4}',item.get('value',''),re.I):
                    choices.append((item,_money(match.group(1))))
            if len(choices)!=1:raise ValueError('native_ground_choice_missing_or_ambiguous')
            choice,amount=choices[0]
            selector=page.locator('select[data-bind*="shipment.shippingMethod"]')
            await selector.select_option(choice['value'],timeout=3000)
            expected=requested_project_context(chosen)
            while True:
                retained=await page.evaluate(NATIVE_GROUND_OPTIONS_JS)
                current=await page.evaluate(CURRENT_DESTINATION_JS)
                if current!=destination:raise ValueError('native_ground_destination_changed')
                if len(retained)!=1 or retained[0].get('value')!=choice['value']:
                    raise ValueError('native_ground_service_not_retained')
                bound=[record for record in project_responses if record.get('status')==200
                       and record.get('selection_context')==expected and isinstance(record.get('totals'),dict)]
                totals=bound[-1]['totals'] if bound else {}
                if (_money(totals.get('shipping',0))==amount
                        and await wait_for_rendered_quote(page,project_responses,chosen,timeout=.2)):
                    return {'status':'native_ground_selected_summary_and_total_matched',
                        'native_option_value':choice['value'],'label':'Ground Shipping','amount':f'{amount:.2f}',
                        'postcode':destination['postcode'],'state':destination['state'],
                        'carrier_identity':'source_fields_only_not_inferred_from_method_code',
                        'cart_or_order_action':False,'address_record_written':False,'account_default_changed':False}
                await asyncio.sleep(.1)
    except (TimeoutError,SourceUITimeout) as error:
        raise ValueError('native_ground_selected_summary_not_ready_before_deadline') from error

SHIPPING_CONTROL_OBSERVATION_JS = r'''nativeLabels => {
  const root=document.querySelector('.column.main')||document.querySelector('main');
  if(!root)return {schema:'fourover-shipping-controls/v1',status:'main_root_missing'};
  const visible=e=>e.getClientRects().length>0;
  const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
  const identity=v=>{
    let value=clean(v).replace(/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/ig,'[uuid]')
      .replace(/\b[a-f0-9]{24,}\b/ig,'[opaque]');
    value=value.replace(/address|customer|account|shipment|project/i.test(value)?/\d+/g:/\d{4,}/g,'[number]');
    return value.slice(0,300);
  };
  const safeLabels=['Shipping & Sets','Shipping and Sets','Shipping','Estimate Shipping','Shipping Estimate',
    'Calculate Shipping','Get Shipping Rates','Get Rates','Update Shipping','Shipping Address','Ship To',
    'Change Address','Edit Address','Select Address','Add Address','New Address','Address Book','Address Book Lookup','Address',
    'New Dropship Address','Ship Here','Dropship Here','Production Facility',
    'Destination','Delivery','Delivery Address','Delivery Method','Shipping Method','Shipping Service','Shipping Options',
    'Country','State','State/Province','Province','ZIP','ZIP Code','Postal Code','Postcode','City',
    'Sets','Quantity','Continue','Back','Next','Apply','Save','Cancel','Close','Edit','Select',
    'Proceed to Shipping','Add to Cart','Proceed to Checkout'];
  const publicWords=new Set('shipping sets and estimate calculate get update address ship to change edit select add new book destination delivery method service options country state province zip code postal postcode city quantity continue back next apply save cancel close proceed cart checkout enter a this for future use in your the my residential business default'.split(' '));
  const label=v=>{
    const text=clean(v).replace(/\s*[:*]\s*$/,'');
    const exact=safeLabels.find(item=>item.toLowerCase()===text.toLowerCase());
    if(exact)return exact;
    const words=text.toLowerCase().replace(/[^a-z /-]/g,'').split(/[ /-]+/).filter(Boolean);
    return text.length<=100 && words.length>0 && words.every(word=>publicWords.has(word)) && !/\d|@/.test(text)?text:null;
  };
  const labelsFor=e=>{
    const byFor=e.id?root.querySelector(`label[for="${CSS.escape(e.id)}"]`):null;
    return label(byFor?.textContent)||label(e.closest('label')?.textContent)
      ||label(e.closest('.field')?.querySelector('label')?.textContent)||label(e.getAttribute('aria-label'));
  };
  const binding=v=>identity(String(v||'').replace(/(['"])(.*?)\1/g,"'[literal]'"));
  const privateControl=e=>/first.?name|last.?name|email|phone|telephone|street|address.?line|company/i.test([e.name,e.id].join(' '));
  const info=e=>({tag:e.tagName.toLowerCase(),id:identity(e.id),name:identity(e.name),
    class_name:identity(e.className),role:identity(e.getAttribute('role')),data_bind:binding(e.getAttribute('data-bind'))});
  const controls=[...root.querySelectorAll('input,select,textarea,button,[role="button"],[role="radio"]')]
    .filter(e=>visible(e)&&!privateControl(e)).slice(0,160).map(e=>{
      const out={...info(e),type:identity(e.type),label:labelsFor(e)||label(e.textContent),
        disabled:!!e.disabled,value_kind:'redacted',choice_count:e.tagName==='SELECT'?e.options.length:undefined,
        checked:['checkbox','radio'].includes(e.type)?!!e.checked:undefined};
      const semantic=clean([e.name,e.id,out.label].join(' '));
      if(/zip|postal|postcode/i.test(semantic)&&/^\d{5}(?:-\d{4})?$/.test(String(e.value||''))){out.value_kind='destination_postal';out.value=e.value;}
      else if(/\b(?:region|state|province)\b/i.test(semantic)&&/^[A-Z]{2}$/.test(String(e.value||''))){out.value_kind='destination_state';out.value=e.value;}
      else if(/country/i.test(semantic)&&/^[A-Z]{2}$/.test(String(e.value||''))){out.value_kind='destination_country';out.value=e.value;}
      else if((/shipping.?method|shipping.?rate/i.test(semantic)||/shipment\.shippingMethod/.test(e.getAttribute('data-bind')||''))&&/^[a-z0-9]+_\d{1,4}$/i.test(String(e.value||''))){out.value_kind='shipping_service_code';out.value=e.value;}
      return out;
    });
  const actions=[...root.querySelectorAll('a,button,[role="button"]')].filter(visible).slice(0,100)
    .map(e=>({...info(e),label:label(e.textContent)||label(e.getAttribute('aria-label')),disabled:!!e.disabled}));
  const headings=[...root.querySelectorAll('h1,h2,h3,h4,h5,h6,legend')].filter(visible)
    .map(e=>({...info(e),label:label(e.textContent)})).filter(e=>e.label).slice(0,40);
  const serviceLabels=[...new Set((Array.isArray(nativeLabels)?nativeLabels:[]).filter(value=>typeof value==='string'
    &&value.length<=100&&/ground|shipping|delivery|day|air|ups|fedex|usps/i.test(value)
    &&/^(?:(?:UPS|FedEx|USPS|Ground|Shipping|Day|Service|Air|A\.M\.|Next|by|Early|Standard|AM|PM|noon|nd|st|rd|th|[0-9]{1,2})[ .-]*)+$/i.test(value)
    &&!/@|\d{3}[- ]\d{3}[- ]\d{4}/.test(value)))];
  const cards=[];
  for(const e of root.querySelectorAll('button,label,li,[role="radio"],[class*="rate"],[class*="shipping"]')){
    if(cards.length>=60)break;if(!visible(e))continue;
    const text=clean(e.textContent);if(text.length>800)continue;
    const normalized=text.toLowerCase().replace(/[^a-z0-9]/g,'');
    const services=serviceLabels.filter(item=>normalized.includes(item.toLowerCase().replace(/[^a-z0-9]/g,''))
      ||/ground/i.test(item)&&/\bground\b/i.test(text));
    const amounts=[...new Set(text.match(/\$\s*[0-9,]+\.\d{2}/g)||[])];
    if(services.length&&amounts.length)cards.push({...info(e),service_labels:services,amounts,
      selected:e.getAttribute('aria-checked')==='true'||e.getAttribute('aria-selected')==='true'||!!e.checked});
  }
  const assetUrls=[...performance.getEntriesByType('resource').map(item=>item.name),
    ...[...document.querySelectorAll('script[src]')].map(e=>e.src)];
  const publicAssets=[];
  for(const raw of assetUrls){try{
    const url=new URL(raw,location.href);
    if(url.protocol==='https:'&&['4over.com','www.4over.com'].includes(url.hostname)
      &&/^\/static\/version\d+\/frontend\/FourOver\/default\/en_US\/.+\.js$/.test(url.pathname))
      publicAssets.push(url.origin+url.pathname);
  }catch(_){}}
  const logos=[...root.querySelectorAll('img[alt]')].filter(visible).map(e=>{
    const alt=clean(e.alt);if(!/^(?:UPS|FedEx|USPS|DHL)(?:\s+(?:logo|shipping|carrier))*$/i.test(alt))return null;
    return {...info(e),alt};
  }).filter(Boolean).slice(0,20);
  const displayedPostcodes=[...root.querySelectorAll('.ship-from-address [data-bind="html: address.postcode"]')]
    .filter(visible).map(e=>clean(e.textContent)).filter(value=>/^\d{5}(?:-\d{4})?$/.test(value));
  const facilities=[];
  for(const e of root.querySelectorAll('span,strong,b,dt,dd,div,p,li')){
    if(facilities.length>=10||!visible(e))continue;
    let text=clean(e.textContent);
    if(/^Production Facility\s*:?$/i.test(text)){
      const sibling=e.nextElementSibling;
      if(sibling?.tagName==='DD'){
        // Only the DD adjacent to the exact facility heading is eligible.
        // Its city/state precedes the supplier's explanatory tooltip copy.
        const native=clean(sibling.textContent);
        const city=native.match(/^([A-Za-z][A-Za-z .'-]{1,60}),\s*([A-Z]{2})(?:\s*Production Facility\b|$)/);
        if(city){facilities.push({label:'Production Facility',city:city[1].trim(),state:city[2]});continue;}
        text='Production Facility '+native;
      }else text=clean(e.parentElement?.textContent);
    }
    if(text.length>160)continue;
    const match=text.match(/^Production Facility\s*:?\s*([A-Za-z][A-Za-z .'-]{1,60}),\s*([A-Z]{2})$/i);
    if(match)facilities.push({label:'Production Facility',city:match[1].trim(),state:match[2].toUpperCase()});
  }
  return {schema:'fourover-shipping-controls/v1',status:'passive_observation',
    controls,actions,headings,rate_cards:cards,carrier_logos:logos,public_js_assets:[...new Set(publicAssets)].slice(0,160),
    bound_postal_displays:displayedPostcodes.map(value=>({scope:'visible_ship_from_address_postcode',postcode:value,
      binding:'html: address.postcode',destination_role_requires_shipping_type_confirmation:true})),
    production_facilities:[...new Map(facilities.map(item=>[item.city+'|'+item.state,item])).values()],
    limits:{address_values_returned:false,addressbook_ids_returned:false,customer_details_returned:false,
      mutations_performed:false,native_destination_changed:false},
    note:'Unrecognized labels and all non-postal/state/country/service values are redacted. Rate cards contain only native service labels and currency amounts.'};
}'''


def observation_labels(shipping_responses):
    """Supplier response service titles only; never inspect an address body."""
    return sorted({rate['method_title'] for record in shipping_responses
                   for rate in record.get('rates', []) if isinstance(rate.get('method_title'), str)})


def current_shipping_rates(pilot):
    """Export the final native response, never an earlier default destination.

    A rate is usable for destination calibration only when its cents match the
    retained selector and the observer explicitly retained postal/state values.
    Carrier identity remains exactly what the source returned.
    """
    responses=pilot.get('shipping_rates') or pilot.get('shipping_response_shapes',[])
    records=[record for record in responses if isinstance(record,dict)]
    if not records:return []
    record=records[-1]
    rates=[{key:rate.get(key) for key in ('method_code','method_title','amount','available',
            'shipping_time','production_estimate','carrier_code','carrier_title','carrier_name','carrier') if key in rate}
           for rate in record.get('rates',[]) if isinstance(rate,dict)] if record.get('status')==200 else []
    observation=pilot.get('shipping_control_observation') or {}
    hydration=observation.get('rate_hydration') or {}
    destination=observation.get('current_destination') or {}
    benchmark=observation.get('benchmark_destination') or {}
    expected={}
    try:
        for rate in rates:
            if rate.get('available') is not True:continue
            code=str(rate['method_code']);amount=Decimal(str(rate['amount']))
            if (not amount.is_finite() or amount<0 or amount.as_tuple().exponent < -2 or code in expected):
                raise ValueError()
            expected[code]=int(amount*100)
    except (KeyError,InvalidOperation,ValueError):expected={}
    choices=hydration.get('choices',[])
    observed={row.get('method_code'):row.get('cents') for row in choices if isinstance(row,dict)}
    postal=destination.get('postcode','');state=destination.get('state','')
    retained=(destination.get('status')=='bound_postal_and_state'
              and re.fullmatch(r'\d{5}(?:-\d{4})?',postal) is not None
              and re.fullmatch(r'[A-Z]{2}',state) is not None)
    if benchmark:
        retained=retained and benchmark.get('status')=='bound_postal_and_state' and all(
            benchmark.get(key)==destination.get(key) for key in ('postcode','state'))
    matched=(record.get('status')==200 and hydration.get('status')=='matched'
             and expected and len(choices)==len(observed) and observed==expected and retained)
    binding={'status':'native_choices_and_destination_retained' if matched else 'unknown_not_reconciled',
             'configuration_scope':'same_guarded_pilot_selection', 'carrier_identity':'source_fields_only'}
    if matched:binding.update(postcode=postal,state=state)
    return [{'status':record.get('status'),'rates':rates,'binding':binding}]


def passive_pilot_projection(pilot):
    """Observation mode intentionally omits legacy full shipping input snapshots."""
    from .fourover_real_estate import RealEstateQuoteArchive
    projected=RealEstateQuoteArchive.evidence(pilot)
    projected['shipping_control_observation']=pilot.get('shipping_control_observation')
    projected['shipping_rates']=current_shipping_rates(pilot)
    projected['shipping_rate_binding']=pilot.get('shipping_rate_binding')
    return projected
