"""Bounded DPS public price-route observations tied to a native browser pilot.

A quote is a scoped observation, not proof that every size, option combination,
production choice, or quantity is available. No account headers are replayed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from .dps_pilot import SCRIPT_SHA256, sanitize_price, number
from .pacing import retry_after_seconds
from .tls import tls_context
from .transport import RateLimiter, validate_url

API_ORIGIN = 'https://api.digital-print-solutions.com'


def _code(groups):
    return 'C'.join(number(g['id'],integer=True,positive=True)+'E'+number(g['value'],integer=True,positive=True)
                    for g in groups)


def _selected(control, value):
    return any(not choice.get('disabled') and Decimal(number(choice['value'],positive=True))==Decimal(str(value))
               for choice in control.get('choices') or [])


def plan_cases(pilot, quantities, dimension_pairs, vary_one_option=True):
    if (pilot.get('status')!='completed' or pilot.get('source_script_sha256')!=SCRIPT_SHA256
            or pilot.get('source_contract_verified') is not True or not pilot.get('observations')):
        raise ValueError('Completed pinned native DPS pilot required')
    if (not isinstance(quantities,list) or not 1<=len(quantities)<=100
            or any(type(q) is not int or not 1<=q<=25000 for q in quantities)
            or len(set(quantities))!=len(quantities)
            or not isinstance(dimension_pairs,list) or len(dimension_pairs)>50
            or type(vary_one_option) is not bool):
        raise ValueError('Invalid bounded DPS probe scope')
    baseline=pilot['observations'][0]
    context=baseline['context']; controls=baseline['controls']
    if (not isinstance(context.get('product_id'),str) or not context['product_id'].isdecimal()
            or any(key not in controls for key in ('width','height','quantity','options'))
            or not isinstance(controls['options'],list)
            or _code(controls['options'])!=context['options']):
        raise ValueError('Native calculator context incomplete')
    width_domain=controls['width']; height_domain=controls['height']
    for item in dimension_pairs:
        if (not isinstance(item,list) or len(item)!=2 or
                not _selected(width_domain,item[0]) or not _selected(height_domain,item[1])):
            raise ValueError('Dimension outside rendered calculator domain')
    qcontrol=controls['quantity']
    for q in quantities:
        if qcontrol['kind']=='select':
            if not _selected(qcontrol,q):raise ValueError('Quantity outside rendered domain')
        elif qcontrol['kind']=='number':
            low=Decimal(number(qcontrol['minimum'],integer=True,positive=True))
            high=Decimal(number(qcontrol['maximum'],integer=True,positive=True))
            step=Decimal(number(qcontrol.get('step') or '1',integer=True,positive=True))
            if not low<=q<=high or (Decimal(q)-low)%step:raise ValueError('Quantity outside rendered domain')
        else:raise ValueError('Quantity control unsupported')
    cases=[];seen=set()
    def add(width,height,quantity,options,scope,field=None,choice=None):
        request={'width':number(width,positive=True),'height':number(height,positive=True),
                 'quantity':number(quantity,integer=True,positive=True),
                 'turnaroundId':number(context['turnaroundId'],integer=True),'options':options}
        key=json.dumps(request,sort_keys=True,separators=(',',':'))
        if key not in seen:
            seen.add(key);cases.append({'request':request,'scope':scope,'field':field,'choice':choice,
                                        'key':hashlib.sha256(key.encode()).hexdigest()})
    # Recheck the native configuration first. A changed public price invalidates
    # the whole probe before it can collect prices for unrendered configurations.
    add(context['width'],context['height'],context['quantity'],context['options'],'native_baseline')
    for q in quantities:add(context['width'],context['height'],q,context['options'],'quantity')
    for width,height in dimension_pairs:add(width,height,quantities[0],context['options'],'dimension')
    if vary_one_option:
        for i,group in enumerate(controls['options']):
            for choice in group['choices']:
                if (choice['value']==group['value'] or choice.get('disabled')
                        or choice.get('redirects') or not choice['value']):continue
                changed=[dict(x) for x in controls['options']]
                changed[i]['value']=choice['value']
                add(context['width'],context['height'],quantities[0],_code(changed),
                    'one_option_change',group['label'],choice['label'])
    if len(cases)>200:raise ValueError('DPS probe exceeds request budget')
    return cases


async def harvest_cases(store,pilot,cases):
    if not cases or len(cases)>200:raise ValueError('Bounded cases required')
    baseline=pilot['observations'][0]
    product_id=baseline['context']['product_id']
    url=f'{API_ORIGIN}/pricing/{product_id}'
    await validate_url(url)
    limiter=RateLimiter(store)
    async def active():return None
    results=[]
    expected={k:baseline['context'][k] for k in ('width','height','quantity','turnaroundId','options')}
    if cases[0]['request']!=expected:
        raise ValueError('Native baseline must be the first price request')
    async with httpx.AsyncClient(verify=tls_context(),timeout=20,follow_redirects=False) as client:
        for case in cases:
            last_error=None
            completed=False
            for attempt in range(4):
                token=await limiter.acquire(API_ORIGIN,5,4,active)
                started=time.monotonic();status=0;retry_after=0
                try:
                    response=await client.get(url,params=case['request'],headers={'Accept':'application/json'})
                    status=response.status_code
                    if status in {429,503}:
                        retry_after=retry_after_seconds(response.headers.get('Retry-After')) or 2
                        last_error='source_rate_limited';continue
                    if status!=200 or len(response.content)>200000:
                        last_error='source_price_unavailable';break
                    if urlsplit(str(response.url)).hostname!='api.digital-print-solutions.com':
                        last_error='source_route_changed';break
                    price=sanitize_price(response.json())
                    if case['request']==expected:
                        if price['price']!=baseline['price']['price']:
                            raise ValueError('Public route differs from native browser price')
                    record={'key':case['key'],'request':case['request'],'scope':case['scope'],
                            'field':case['field'],'choice':case['choice'],'price':price,
                            'body_sha256':hashlib.sha256(response.content).hexdigest(),
                            'verification':'source_route_quote_pending_rendered_case_check'}
                    object_path=store.put_object(json.dumps(record,sort_keys=True,separators=(',',':')).encode(),
                                                 'dps-scoped-price.json')
                    results.append({'key':case['key'],'object_path':object_path,**record})
                    completed=True
                    break
                except (httpx.HTTPError,ValueError,TypeError,KeyError) as exc:
                    if isinstance(exc,ValueError) and str(exc)=='Public route differs from native browser price':raise
                    last_error='source_price_unreadable'
                finally:
                    await asyncio.to_thread(limiter.release,API_ORIGIN,token,time.monotonic()-started,
                                            status,retry_after,5)
                if status not in {429,503}:
                    break
                await asyncio.sleep(min(2**attempt,5))
            if not completed:
                results.append({'key':case['key'],'request':case['request'],'status':'held','reason':last_error})
                if case['request']==expected:
                    raise ValueError('Native baseline route unavailable')
    return results
