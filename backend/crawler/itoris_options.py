"""Bounded projection of the supplier-loaded Itoris product option HTML."""
from __future__ import annotations

import re
import json
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup


def _clean(value, limit=200):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _money(value):
    if value is None:
        return None
    raw=str(value).strip()
    if not re.fullmatch(r'\d{1,8}(?:\.\d{1,4})?',raw):
        return None
    try: amount=Decimal(raw)
    except InvalidOperation: return None
    return str(amount) if amount.is_finite() and amount>=0 else None


def project_itoris_pricing_policy(body):
    """Read the supplier's embedded DPO configuration without inferring totals."""
    if not isinstance(body,(bytes,bytearray)) or len(body)>2_000_000:
        raise ValueError('Itoris response unavailable or too large')
    source=body.decode('utf8','replace')
    marker='window.dpoOptions.initialize('
    start=source.find(marker)
    if start<0:
        return {'configuration_found':False,'absolute_pricing':None,'choices':[],
                'pricing_semantics_verified':False}
    try:
        config,end=json.JSONDecoder().raw_decode(source[start+len(marker):].lstrip())
    except (ValueError,TypeError):
        raise ValueError('Itoris pricing configuration is malformed') from None
    if not isinstance(config,dict):
        raise ValueError('Itoris pricing configuration is not an object')
    choices=[]
    def visit(value):
        if isinstance(value,list):
            for entry in value: visit(entry)
        elif isinstance(value,dict):
            if 'option_type_id' in value:
                choices.append({
                    'option_type_id':str(value['option_type_id']),
                    'option_id':str(value['option_id']) if value.get('option_id') is not None else None,
                    'title':_clean(value.get('title'),160),
                    'price':_money(value.get('price')),
                    'price_type':_clean(value.get('price_type'),40) or None,
                    'onetime':value.get('onetime') if value.get('onetime') in (0,1,False,True) else None,
                    'use_qty':value.get('use_qty') if value.get('use_qty') in (0,1,False,True) else None,
                })
            for nested in value.values(): visit(nested)
    visit(config)
    # Itoris renders each field's JSON separately from initialize(...); the
    # initialize object carries global flags but often contains no choices.
    for match in re.finditer(r'"items"\s*:\s*\[',source):
        try:
            items,_=json.JSONDecoder().raw_decode(source[match.start()+match.group().find('['):])
        except (ValueError,TypeError):
            continue
        visit(items)
    by_id={}
    for choice in choices:
        previous=by_id.setdefault(choice['option_type_id'],choice)
        if previous!=choice:
            raise ValueError('Itoris pricing configuration has conflicting choice IDs')
    return {'configuration_found':True,
            'absolute_pricing':_clean(config.get('absolute_pricing'),10) or None,
            'choices':list(by_id.values()),'pricing_semantics_verified':False}


def project_itoris_dpo(body):
    if not isinstance(body,(bytes,bytearray)) or len(body)>2_000_000:
        raise ValueError('Itoris response unavailable or too large')
    soup=BeautifulSoup(body,'html.parser')
    fields=[]
    selects=soup.select('select.product-custom-option')
    if len(selects)>200:
        raise ValueError('Itoris select count exceeds review limit')
    for select in selects:
        options=select.find_all('option',recursive=False)
        if len(options)>1000:
            raise ValueError('Itoris option count exceeds review limit')
        identifier=_clean(select.get('id'),100)
        label=soup.find('label',attrs={'for':identifier}) if identifier else None
        if label is None:
            label=select.find_previous('label')
        choices=[]
        for option in options:
            value=_clean(option.get('value'),100)
            if not value: continue
            choices.append({'value':value,'label':_clean(option.get_text(' ',strip=True)),
                            'source_price':_money(option.get('price')),
                            'source_price_type':_clean(option.get('price_type'),40) or None,
                            'disabled':option.has_attr('disabled')})
        fields.append({'id':identifier,'label':_clean(label.get_text(' ',strip=True)) if label else '',
                       'choices':choices})
    return {'schema':'hermes-itoris-dpo-options/v1','fields':fields,
            'field_count':len(fields),'choice_count':sum(len(f['choices']) for f in fields),
            'priced_choice_count':sum(c['source_price'] not in (None,'0','0.00') for f in fields for c in f['choices']),
            'pricing_policy':project_itoris_pricing_policy(body),
            'conditional_availability_verified':False,'quantity_effects_verified':False,
            'final_eligible':False}


def project_itoris_tiers(body):
    if not isinstance(body,(bytes,bytearray)) or len(body)>2_000_000:
        raise ValueError('Itoris tier response unavailable or too large')
    soup=BeautifulSoup(body,'html.parser')
    rows=soup.select('#itier_prices tr.itier_price')
    if len(rows)>1000:
        raise ValueError('Itoris tier count exceeds review limit')
    tiers=[]
    for row in rows:
        minimum=row.get('qty')
        if not isinstance(minimum,str) or not re.fullmatch(r'[1-9]\d{0,8}',minimum):
            raise ValueError('Itoris quantity tier unresolved')
        price=_money(row.get('price'))
        if price is None:
            raise ValueError('Itoris quantity price unresolved')
        label=_clean((row.select_one('.column_quantity_range') or row).get_text(' ',strip=True),100)
        tiers.append({'minimum_quantity':int(minimum),'source_price':price,
                      'range_label':label,'discount':_money(row.get('discount'))})
    if len({r['minimum_quantity'] for r in tiers})!=len(tiers) or tiers!=sorted(tiers,key=lambda r:r['minimum_quantity']):
        raise ValueError('Itoris quantity tiers ambiguous')
    return {'schema':'hermes-itoris-quantity-tiers/v1','tiers':tiers,'tier_count':len(tiers),
            'price_semantics':'supplier_tier_display_price_unconfirmed_cart_basis',
            'add_ons_verified':False,'shipping_verified':False,'production_verified':False,
            'final_eligible':False}
