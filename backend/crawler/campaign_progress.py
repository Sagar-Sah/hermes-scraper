"""Durable product repair inventory and public storefront evidence collection.

Diagnosis, extraction, verification and finalization are separate milestones.
Public HTML alone never closes a product or verifies supplier prices.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import time
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .pacing import retry_after_seconds
from .shopify_variants import product_json_url, project_product_json
from .soardist_account import ACCOUNT_CHECK_JS, PRODUCT_PRICE_JS, ORIGIN as SOARDIST_ORIGIN, exact_product_page, observed_price
from .itoris_options import project_itoris_dpo, project_itoris_tiers
from .tls import tls_context
from .transport import RateLimiter, validate_url


def now():
    return datetime.now(timezone.utc).isoformat()


def required_fixed_options(product):
    """Return storefront-scoped supplier choices recorded in reviewed mapping evidence."""
    fixed = {}
    for evidence in product.get('mapping_evidence') or []:
        for key, value in (evidence.get('required_fixed_options') or {}).items():
            if key in fixed and fixed[key] != value:
                raise ValueError('Conflicting required fixed supplier choices')
            fixed[key] = value
    return fixed


def _excluded(product):
    return product.get('stage') == 'excluded' or bool(product.get('excluded_at'))


def _serialized_mutation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.mutation_lock:
            return method(self, *args, **kwargs)
    return guarded


def _price_string(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('Invalid storefront price')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid storefront price') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('Invalid storefront price')
    return str(value)


def _same_page(left, right):
    def identity(value):
        parts = urlsplit(value)
        return (parts.scheme, (parts.hostname or '').removeprefix('www.'),
                parts.port or 443, parts.path.rstrip('/'), parts.query)
    try:
        return identity(left) == identity(right)
    except (ValueError, TypeError):
        return False


def _supplier_url(value):
    if value is None: return None
    if not isinstance(value, str): raise ValueError('Supplier URL must be public HTTPS')
    parts = urlsplit(value)
    host = parts.hostname or ''
    if (parts.scheme != 'https' or not host or parts.username or parts.password
            or parts.port not in (None, 443) or parts.query or parts.fragment
            or '.' not in host or host.endswith(('.local', '.localhost', '.internal'))):
        raise ValueError('Supplier URL must be public HTTPS without credentials or query parameters')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global: raise ValueError('Supplier URL must be public HTTPS')
    return value


def _storefront_prices(soup, primary, url, identity_confirmed):
    """Retain scoped retail evidence only; never execute calculator JavaScript."""
    result = {'scope': 'storefront_only', 'supplier_verified': False,
              'displayed_prices': [], 'product_offers': [], 'simple_retail': [], 'warnings': []}
    if not identity_confirmed:
        result['warnings'].append('storefront_identity_unconfirmed')
        return result
    if primary:
        selectors = '.summary > .price, .elementor-widget-wd_single_product_price .price, .wd-single-price .price'
        result['displayed_prices'] = list(dict.fromkeys(node.get_text(' ', strip=True)[:300]
                                             for node in primary.select(selectors)))[:20]
    for script in soup.select('script[type="application/ld+json"]')[:20]:
        text = script.string or script.get_text()
        if len(text) > 2_000_000:
            result['warnings'].append('product_jsonld_too_large'); continue
        try:
            queue = [json.loads(text)]
            visited = 0
            while queue and visited < 2000:
                node = queue.pop(); visited += 1
                if isinstance(node, list):
                    queue.extend(node[:1000]); continue
                if not isinstance(node, dict): continue
                if isinstance(node.get('@graph'), (dict, list)): queue.append(node['@graph'])
                types = node.get('@type', [])
                if not (types == 'Product' or isinstance(types, list) and 'Product' in types): continue
                offers = node.get('offers', [])
                offers = offers if isinstance(offers, list) else [offers]
                for offer in offers[:100]:
                    if not isinstance(offer, dict): continue
                    links = [node.get('url'), node.get('@id'), offer.get('url')]
                    links = [link for link in links if isinstance(link, str)]
                    if not any(_same_page(link, url) for link in links): continue
                    if isinstance(offer.get('url'), str) and not _same_page(offer['url'], url): continue
                    safe = {'source': 'product_jsonld', 'type': str(offer.get('@type', 'Offer'))[:40]}
                    for key in ('price', 'lowPrice', 'highPrice'):
                        if key in offer: safe[key] = _price_string(offer[key])
                    currency = offer.get('priceCurrency')
                    if isinstance(currency, str) and re.fullmatch('[A-Z]{3}', currency): safe['currency'] = currency
                    if any(key in safe for key in ('price', 'lowPrice', 'highPrice')):
                        result['product_offers'].append(safe)
        except (ValueError, TypeError, RecursionError):
            result['warnings'].append('unsupported_product_jsonld')
    # This exact public wrapper is observed in the saved storefront assets.
    # The JSON initializer is parsed alone, never evaluated or extrapolated.
    prefix = re.compile(r'''^\s*\(function\(\$\)\{\s*['"]use strict['"];\s*if\s*\(!\$\)\s*return;\s*var\s+simpleRetail\s*=\s*''')
    for script in soup.select('script#print-it-supplier-options-js:not([src])')[:10]:
        text = script.string or script.get_text()
        match = prefix.match(text)
        if not match: continue
        try:
            if len(text) > 2_000_000: raise ValueError()
            data, end = json.JSONDecoder(parse_float=Decimal).raw_decode(text[match.end():])
            if not text[match.end()+end:].lstrip().startswith(';'): raise ValueError()
            if data is None: continue
            if not isinstance(data, dict): raise ValueError()
            safe = {'source': 'simpleRetail', 'piso_retail_bands': []}
            for key in ('display_price', 'display_regular_price'):
                if key in data: safe[key] = _price_string(data[key])
            bands = data.get('piso_retail_bands', [])
            if not isinstance(bands, list) or len(bands) > 1000: raise ValueError()
            for band in bands:
                if not isinstance(band, dict): raise ValueError()
                low, high = band.get('min'), band.get('max')
                if type(low) is not int or low < 1 or (high is not None and (type(high) is not int or high < low)):
                    raise ValueError()
                safe['piso_retail_bands'].append({'min': low, 'max': high, 'unit': _price_string(band.get('unit'))})
            result['simple_retail'].append(safe)
        except (ValueError, TypeError, RecursionError):
            result['warnings'].append('unsupported_simple_retail')
    result['warnings'] = sorted(set(result['warnings']))
    return result


def inspect_storefront(html, url, expected_id):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h1.product_title")
    body = soup.find("body")
    body_ids = set(re.findall(r"\bpostid-(\d+)\b", " ".join((body or {}).get("class", []))))
    primaries = [node for node in soup.select('.product[id^="product-"]')
                 if not node.find_parent(class_=re.compile(r'^(?:related|upsells|cross-sells|products)$'))
                 and not node.find_parent(class_='product')]
    primary_ids = {node.get('id', '').removeprefix('product-') for node in primaries
                   if re.fullmatch(r'product-\d+', node.get('id', ''))}
    bound_ids = body_ids or primary_ids
    identity = bound_ids == {str(expected_id)}
    primary = next((node for node in primaries if node.get('id') == f'product-{expected_id}'), None)
    fields = []
    for node in soup.select("form.cart select, #product_calculator_display select, .pigf-builder select, .pigf-builder input"):
        label = soup.find("label", attrs={"for": node.get("id")}) if node.get("id") else None
        fields.append({"label": label.get_text(" ", strip=True) if label else node.get("aria-label") or node.get("name") or "Unlabeled control",
                       "kind": node.name if node.name == "select" else node.get("type", "text"),
                       "choices": [{"value": o.get("value"), "label": o.get_text(" ", strip=True), "disabled":o.has_attr("disabled")}
                                   for o in node.select("option") if o.has_attr("value")],
                       "min":node.get("min"), "max":node.get("max"), "step":node.get("step")})
    csv_tables = []
    for script in soup.select("script:not([src])"):
        text = script.string or script.get_text()
        match = re.match(r"\s*(?:const|let|var)\s+csvdata\s*=\s*", text)
        if not match:
            continue
        try:
            data, end = json.JSONDecoder().raw_decode(text[match.end():])
            if not re.fullmatch(r"\s*;?\s*", text[match.end()+end:]) or not isinstance(data,list):
                raise ValueError()
            csv_tables.append({"rows":len(data),"columns":list(data[0]) if data and isinstance(data[0],dict) else [],
                               "sha256":hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()})
        except (ValueError, TypeError):
            csv_tables.append({"error":"unsupported_embedded_table"})
    forms = soup.select("form.variations_form")
    variations = []
    for form in forms:
        try:
            value = json.loads(form.get("data-product_variations", "null"))
            variations.append({"product_id":form.get("data-product_id"), "rows":len(value) if isinstance(value,list) else None,
                               "dynamic":value is False})
        except ValueError:
            variations.append({"error":"invalid_variation_json"})
    markers = [name for name,selector in {"native_csv":"#product_calculator_display", "native_variations":"form.variations_form",
              "grand_format":".pigf-builder", "simple_cart":"form.cart:not(.variations_form)",
              "dtf_studio":"[data-pidt-product], .pidt-studio-launcher"}.items() if soup.select_one(selector)]
    supplier_links = sorted({urljoin(url,a['href']) for a in soup.select('a[href]')
                             if urlsplit(urljoin(url,a['href'])).hostname in {'www.b2sign.com','b2sign.com','soardistusa.com','www.soardistusa.com','4over.com','www.printograph.com'}})
    root = soup.select_one(".summary") or soup.select_one(".product")
    text = root.get_text(" ",strip=True) if root else ""
    production = [m.group(0) for m in re.finditer(r".{0,75}(?:business days?|turnaround|production time|same.day|next.day).{0,100}",text,re.I)][:12]
    return {"title":title.get_text(" ",strip=True) if title else None,"identity_confirmed":identity,
            "observed_product_ids":sorted(bound_ids),"fields":fields,"calculator_types":markers,
            "embedded_tables":csv_tables,"variation_tables":variations,"supplier_links":supplier_links,
            "production_snippets":production,"storefront_prices":_storefront_prices(soup,primary,url,identity),
            "coverage":"static_page_only","prices_verified":False}


class CampaignManager:
    def __init__(self,store):
        self.store=store
        self.root=Path(store.root)/"campaigns"
        self.root.mkdir(exist_ok=True,mode=0o700)
        self.tasks={}
        self.mutation_lock=threading.RLock()
        self.limiter=RateLimiter(store)

    def _path(self,id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}",id):
            raise ValueError("Invalid campaign identifier")
        return self.root/(id+".json")

    def _write(self,data):
        groups=data.get('completion_groups')
        if groups:
            products={item['id']:item for item in data['products']}
            index=data.get('active_group_index',0)
            while index<len(groups) and all(
                    products[pid].get('finalized_at') or _excluded(products[pid])
                    for pid in groups[index]):
                index+=1
            data['active_group_index']=index
        path=self._path(data['id']); tmp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        with tmp.open('x',encoding='utf8') as f:
            os.chmod(tmp,0o600);json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)

    def get(self,id):
        data=json.loads(self._path(id).read_text())
        if data['status']=='running' and id not in self.tasks:
            data['status']='interrupted'
        products=data['products']
        for product in products:
            if _excluded(product):
                product['stage']='excluded'
                product['reason']=product.get('exclusion_reason') or product.get('reason')
        active=[p for p in products if not _excluded(p)]
        data['total_products']=len(products)
        data['original_total_products']=len(products)
        data['active_products']=len(active)
        data['excluded_products']=len(products)-len(active)
        data['counts']={key:sum(bool(p.get(key+'_at')) for p in active) for key in ['diagnosed','extracted','verified','finalized']}
        # A complete review can promote source-backed rows directly from a
        # diagnosed product. Finalized products necessarily have extracted
        # rows even when no separate draft-extraction timestamp was recorded.
        data['counts']['extracted']=sum(bool(p.get('extracted_at') or p.get('finalized_at')) for p in active)
        data['counts']['blocked']=sum(p['stage']=='blocked' for p in active)
        data['source_pages_collected']=sum(bool(p.get('source_collection_id'))
            and p.get('source_collection_status')=='page_collected'
            and p.get('supplier_url')==(p.get('source_collection_evidence') or {}).get('requested_url') for p in active)
        data['public_variants_collected']=sum(bool(p.get('public_variant_evidence')) for p in active)
        data['account_baselines_collected']=sum(bool(p.get('account_baseline_evidence')) for p in active)
        data['fourover_default_probes_collected']=sum(bool(p.get('fourover_default_evidence')) for p in active)
        data['fourover_price_tables_collected']=sum(bool((p.get('fourover_price_table_evidence') or {}).get('complete_child_routes')) for p in active)
        data['fourover_option_maps_collected']=sum(bool((p.get('fourover_option_map_evidence') or {}).get('complete_child_mapping')) for p in active)
        data['fourover_rendered_canaries_passed']=sum((p.get('fourover_rendered_canary') or {}).get('status')=='matched' for p in active)
        data['fourover_rendered_canary_observations']=sum(len(p.get('fourover_rendered_canaries') or []) for p in active)
        data['fourover_area_drafts_collected']=sum(bool(p.get('fourover_area_rows_evidence')) for p in active)
        data['itoris_option_tables_collected']=sum(bool(p.get('itoris_option_evidence')) for p in active)
        data['itoris_quantity_tables_collected']=sum(bool(p.get('itoris_quantity_evidence')) for p in active)
        data['dps_route_price_scopes_collected']=sum(bool(p.get('dps_route_price_evidence')) for p in active)
        data['b2sign_scoped_drafts_collected']=sum(bool(p.get('b2sign_scoped_draft_evidence')) for p in active)
        data['b2sign_scoped_draft_rows']=sum((p.get('b2sign_scoped_draft_evidence') or {}).get('row_count',0) for p in active)
        data['fourover_shipping_baselines_collected']=sum(bool(p.get('fourover_shipping_baseline_evidence')) for p in active)
        groups=data.get('completion_groups') or []
        if groups:
            indexed={p['id']:p for p in products}
            active_index=data.get('active_group_index',0)
            data['group_progress']=[{
                'number':index+1,'product_ids':group,
                'product_count':len(group),
                'excluded_count':sum(_excluded(indexed[pid]) for pid in group),
                'required_count':sum(not _excluded(indexed[pid]) for pid in group),
                'finalized_count':sum(bool(indexed[pid].get('finalized_at')) and not _excluded(indexed[pid])
                                      for pid in group),
                'held_count':sum(not indexed[pid].get('finalized_at') and not _excluded(indexed[pid])
                                 for pid in group),
                'status':'complete' if index<active_index else 'active' if index==active_index else 'queued'
            } for index,group in enumerate(groups)]
            data['active_group_product_ids']=groups[active_index] if active_index<len(groups) else []
        return data

    @_serialized_mutation
    def configure_completion_groups(self,id,groups):
        """Pin an existing campaign's order; never advance past unfinished products."""
        if id in self.tasks:
            raise ValueError('Wait for active campaign analysis before grouping products')
        data=self.get(id)
        if data.get('completion_groups'):
            if data['completion_groups']==groups:
                return data
            raise ValueError('Campaign completion groups are already fixed')
        active=[p['id'] for p in data['products'] if not _excluded(p)]
        if (not isinstance(groups,list) or not groups
                or any(not isinstance(group,list) or not 1<=len(group)<=10
                       or any(not isinstance(pid,str) for pid in group) for group in groups)
                or len(groups)>100 or sorted(pid for group in groups for pid in group)!=sorted(active)):
            raise ValueError('Groups must partition every active product exactly once, at most ten per group')
        data['completion_groups']=groups
        data['active_group_index']=0
        data['updated_at']=now()
        self._write(data)
        return self.get(id)

    def require_active_group(self,id,product_id):
        data=self.get(id)
        groups=data.get('completion_groups') or []
        if groups and product_id not in data['active_group_product_ids']:
            raise ValueError('Finish the current product group before starting another')
        return data

    @_serialized_mutation
    def hold_product(self,id,product_id,reason):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if product is None or _excluded(product):
            raise ValueError('Active campaign product not found')
        if data.get('completion_groups') and product['id'] not in data['active_group_product_ids']:
            raise ValueError('Finish the current product group before holding another')
        if product.get('finalized_at'):
            raise ValueError('Finalized product cannot be held')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise ValueError('A concise hold reason is required')
        if product.get('stage')=='blocked' and product.get('reason')==reason.strip():
            return data
        product.update(stage='blocked',reason=reason.strip(),updated_at=now())
        data['updated_at']=now()
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def queue_alternate_supplier(self,id,product_id,supplier,title,supplier_url=None):
        """Queue another source for an existing storefront product without reopening its finalization."""
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if product is None or _excluded(product):
            raise ValueError('Active storefront product not found')
        if not re.fullmatch(r'[a-z0-9_-]{2,40}',supplier):
            raise ValueError('Invalid supplier key')
        if not isinstance(title,str) or not title.strip():
            raise ValueError('A source product title is required')
        if supplier_url is not None:
            parsed=urlsplit(supplier_url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Supplier URL must be a public HTTPS page')
        jobs=data.setdefault('alternate_supplier_jobs',[])
        existing=next((job for job in jobs if job['storefront_product_id']==product['id']
                       and job['supplier']==supplier),None)
        if existing:
            if supplier_url and existing.get('supplier_url') not in (None,supplier_url):
                raise ValueError('Alternate supplier URL already pinned')
            if supplier_url and not existing.get('supplier_url'):
                existing['supplier_url']=supplier_url
                existing['updated_at']=now()
                data['updated_at']=now()
                self._write(data)
            return self.get(id)
        jobs.append({'id':f"{product['id']}-{supplier}",
                     'storefront_product_id':product['id'],
                     'storefront_title':product['title'],
                     'storefront_url':product.get('storefront_url'),
                     'supplier':supplier,'source_title':title.strip(),
                     'supplier_url':supplier_url,'stage':'queued',
                     'reason':'Exact supplier product URL pending' if not supplier_url else None,
                     'queued_at':now(),'updated_at':now()})
        data['updated_at']=now()
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def queue_supplemental_product(self,id,product_id,title,storefront_url,supplier,supplier_url,reason):
        """Track a newly requested storefront product without changing completion groups."""
        data=self.get(id)
        if any(p['id']==str(product_id) for p in data['products']):
            raise ValueError('Product is already in the grouped campaign')
        if not re.fullmatch(r'[0-9]{1,12}',str(product_id)):
            raise ValueError('Invalid storefront product identifier')
        if not re.fullmatch(r'[a-z0-9_-]{2,40}',supplier):
            raise ValueError('Invalid supplier key')
        if not isinstance(title,str) or not title.strip() or not isinstance(reason,str) or not reason.strip():
            raise ValueError('Title and reason are required')
        for url in (storefront_url,supplier_url):
            parsed=urlsplit(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Product URLs must be public HTTPS pages')
        jobs=data.setdefault('supplemental_product_jobs',[])
        existing=next((j for j in jobs if j['storefront_product_id']==str(product_id)),None)
        if existing:
            if (existing['storefront_url']!=storefront_url or existing['supplier_url']!=supplier_url
                    or existing['supplier']!=supplier):
                raise ValueError('Supplemental product mapping already pinned')
            return self.get(id)
        jobs.append({'id':f'{product_id}-{supplier}-supplemental',
                     'storefront_product_id':str(product_id),'storefront_title':title.strip(),
                     'storefront_url':storefront_url,'supplier':supplier,
                     'supplier_url':supplier_url,'stage':'queued','reason':reason.strip(),
                     'queued_at':now(),'updated_at':now()})
        data['updated_at']=now()
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def complete_alternate_supplier(self,id,job_id,review_id):
        """Attach a separately reviewed supplier offer without replacing the primary product."""
        if not isinstance(review_id,str) or not re.fullmatch(r'[a-f0-9]{32}',review_id):
            raise ValueError('Invalid alternate-offer review identifier')
        data=self.get(id)
        job=next((item for item in data.get('alternate_supplier_jobs',[]) if item['id']==job_id),None)
        if job is None or not job.get('supplier_url'):
            raise ValueError('Pinned alternate supplier job required')
        if job.get('stage')=='finalized':
            if job.get('finalization_id')==review_id:return data
            raise ValueError('Alternate supplier job already finalized')
        if job.get('stage')!='queued':
            raise ValueError('Alternate supplier job is not queued')
        root=Path(self.store.root)/'finalizations'/review_id
        report_path=root/'report.json';csv_path=root/'finalized.csv'
        if (root.is_symlink() or report_path.is_symlink() or csv_path.is_symlink() or
                not report_path.is_file() or not csv_path.is_file()):
            raise ValueError('Immutable alternate-offer review unavailable')
        report_body=report_path.read_bytes();report=json.loads(report_body)
        if (report.get('id')!=review_id or report.get('product_id')!=job['storefront_product_id'] or
                report.get('source_url')!=job['supplier_url'] or
                report.get('catalog_complete') is not True or report.get('held_rows')!=0 or
                not isinstance(report.get('eligible_rows'),int) or report['eligible_rows']<=0):
            raise ValueError('Alternate-offer review does not match job and complete scope')
        with csv_path.open(newline='',encoding='utf-8-sig') as stream:
            count=sum(1 for _ in csv.DictReader(stream))
        if count!=report['eligible_rows']:
            raise ValueError('Alternate-offer row count mismatch')
        timestamp=now()
        job.update(stage='finalized',finalization_id=review_id,row_count=count,
                   finalization_evidence={'report_sha256':hashlib.sha256(report_body).hexdigest(),
                                          'source_url':job['supplier_url'],'eligible_rows':count},
                   finalized_at=timestamp,updated_at=timestamp,reason=None)
        data['updated_at']=timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_fourover_shipping_baseline(self,id,product_id,observation):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if (product is None or _excluded(product) or product.get('supplier')!='fourover'
                or observation.get('product_id')!=str(product_id)
                or observation.get('supplier_url')!=product.get('supplier_url')
                or observation.get('http_status')!=200
                or not isinstance(observation.get('shipping_selects'),list)
                or not 1<=observation.get('method_count',0)<=100
                or observation.get('final_eligible') is not False):
            raise ValueError('Bounded mapped 4over shipping observation required')
        body=json.dumps(observation,sort_keys=True,separators=(',',':')).encode()
        path=self.store.put_object(body,'fourover-shipping-baseline.json')
        product['fourover_shipping_baseline_evidence']={
            'source_url':product['supplier_url'],'object_path':path,
            'sha256':hashlib.sha256(body).hexdigest(),
            'method_count':observation['method_count'],
            'scope':'one_rendered_configuration_account_shipping_context_unresolved',
            'shipping_verified':False,'final_eligible':False,'observed_at':now()}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,'method_count':observation['method_count']}

    def fourover_shipping_baseline(self,id,product_id):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        evidence=(product or {}).get('fourover_shipping_baseline_evidence') or {}
        path=evidence.get('object_path','')
        if (not isinstance(path,str) or not re.fullmatch(
                r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.fourover-shipping-baseline\.json',path)):
            raise FileNotFoundError('4over shipping baseline not found')
        body=self.store.get_object(path)
        if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
            raise ValueError('4over shipping baseline hash mismatch')
        return json.loads(body)

    @_serialized_mutation
    def attach_dps_route_prices(self,id,product_id,report):
        """Record scoped DPS quotes without advancing extraction or verification."""
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if (product is None or _excluded(product) or product.get('supplier')!='digital_print_solutions'
                or product.get('supplier_url')!=report.get('source_url')
                or report.get('catalog_complete') is not False or report.get('final_eligible') is not False
                or not isinstance(report.get('results'),list)
                or not 1<=len(report['results'])<=200
                or report.get('planned_count')!=len(report['results'])
                or report.get('quoted_count')!=sum('price' in x for x in report['results'])
                or report.get('held_count')!=sum(x.get('status')=='held' for x in report['results'])):
            raise ValueError('Unverified mapped DPS scope and complete report required')
        pilot_id=report.get('pilot_id')
        if not isinstance(pilot_id,str) or not re.fullmatch('[a-f0-9]{32}',pilot_id):
            raise ValueError('Pinned native pilot required')
        pilot_path=Path(self.store.root)/'dps-pricing-pilots'/(pilot_id+'.json')
        try:pilot=json.loads(pilot_path.read_text())
        except (OSError,json.JSONDecodeError):raise ValueError('Pinned native pilot unavailable') from None
        if (pilot.get('status')!='completed' or pilot.get('url')!=product['supplier_url']
                or not pilot.get('source_contract_verified') or not pilot.get('observations')
                or pilot['observations'][0]['context']['product_id']!=report.get('product_id')):
            raise ValueError('Native pilot does not match mapped DPS product')
        body=json.dumps(report,sort_keys=True,separators=(',',':')).encode()
        object_path=self.store.put_object(body,'dps-route-prices.json')
        product['dps_route_price_evidence']={
            'source_url':product['supplier_url'],'native_pilot_id':pilot_id,
            'object_path':object_path,'sha256':hashlib.sha256(body).hexdigest(),
            'planned_count':report['planned_count'],'quoted_count':report['quoted_count'],
            'held_count':report['held_count'],
            'coverage':'bounded_scoped_price_quotes_only',
            'verification':'pending_rendered_case_cart_shipping_production',
            'final_eligible':False,'observed_at':now()}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,'quoted_count':report['quoted_count'],
                'counts':self.get(id)['counts']}

    def dps_route_price_snapshot(self,id,product_id):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        evidence=(product or {}).get('dps_route_price_evidence') or {}
        path=evidence.get('object_path','')
        if (not isinstance(path,str) or not re.fullmatch(
                r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.dps-route-prices\.json',path)):
            raise FileNotFoundError('DPS route price evidence not found')
        body=self.store.get_object(path)
        if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
            raise ValueError('DPS route price evidence hash mismatch')
        report=json.loads(body)
        pilot_id=evidence.get('native_pilot_id','')
        if not isinstance(pilot_id,str) or not re.fullmatch(r'[a-f0-9]{32}',pilot_id):
            raise ValueError('DPS native pilot reference missing')
        try:
            pilot=json.loads((Path(self.store.root)/'dps-pricing-pilots'/(pilot_id+'.json')).read_text())
        except (OSError,json.JSONDecodeError):
            raise ValueError('DPS native pilot unavailable') from None
        if pilot.get('url')!=product.get('supplier_url'):
            raise ValueError('DPS source mapping changed')
        from .dps_quote_contract import annotate_scoped_price_report
        return annotate_scoped_price_report(report,pilot)

    @_serialized_mutation
    def materialize_b2sign_scoped_draft(self,id,product_id,harvest_id):
        from .b2sign_drafts import build_draft
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if (product is None or _excluded(product) or product.get('supplier')!='b2sign'
                or not isinstance(harvest_id,str) or not re.fullmatch('[a-f0-9]{32}',harvest_id)):
            raise ValueError('Mapped B2Sign product and completed harvest required')
        directory=Path(self.store.root)/'b2sign-harvests'/harvest_id
        try:
            report_body=(directory/'report.json').read_bytes()
            manifest_body=(directory/'manifest.json').read_bytes()
            report=json.loads(report_body)
            manifest=json.loads(manifest_body)
            results=[json.loads(path.read_text()) for path in (directory/'results').glob('*.json')]
        except (OSError,json.JSONDecodeError):
            raise ValueError('B2Sign harvest evidence unavailable') from None
        if report.get('id')!=harvest_id or report.get('url')!=product.get('supplier_url'):
            raise ValueError('B2Sign harvest source mismatch')
        body,row_count=build_draft(manifest,report,results)
        object_path=self.store.put_object(body,'b2sign-scoped-draft.csv')
        product['b2sign_scoped_draft_evidence']={
            'source_url':product['supplier_url'],'harvest_id':harvest_id,
            'source_report_sha256':hashlib.sha256(report_body).hexdigest(),
            'source_manifest_sha256':hashlib.sha256(manifest_body).hexdigest(),
            'object_path':object_path,'sha256':hashlib.sha256(body).hexdigest(),
            'row_count':row_count,'coverage':'completed_explicit_request_scope_only',
            'verification':'pending_cart_shipping_production_and_full_catalog',
            'final_eligible':False,'observed_at':now()}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,'row_count':row_count}

    def b2sign_scoped_draft(self,id,product_id):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        evidence=(product or {}).get('b2sign_scoped_draft_evidence') or {}
        path=evidence.get('object_path','')
        if (not isinstance(path,str) or not re.fullmatch(
                r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.b2sign-scoped-draft\.csv',path)):
            raise FileNotFoundError('B2Sign scoped draft not found')
        body=self.store.get_object(path)
        if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
            raise ValueError('B2Sign scoped draft hash mismatch')
        return body

    async def collect_itoris_quantity_tables(self,id,pool,*,limit=28):
        if type(limit) is not int or not 1<=limit<=28:
            raise ValueError('Invalid quantity table limit')
        campaign=self.get(id)
        products=[(p['id'],p.get('supplier_url')) for p in campaign['products']
                  if not _excluded(p) and p.get('supplier')=='soardist'
                  and (not campaign.get('completion_groups') or p['id'] in campaign['active_group_product_ids'])]
        results=[]
        for product_id,source_url in products:
            if len(results)>=limit:break
            fresh=self.get(id)
            product=next(p for p in fresh['products'] if p['id']==product_id)
            if (product.get('itoris_quantity_evidence') or {}).get('source_url')==source_url:
                continue
            try:
                await validate_url(source_url)
                async with pool.page(SOARDIST_ORIGIN,headless=False) as page:
                    responses=[]
                    page.on('response',lambda response: responses.append(response)
                        if urlsplit(response.url).hostname=='node1.itoris.com'
                        and urlsplit(response.url).path=='/tierprice/storefront/include.js' else None)
                    await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
                    if not exact_product_page(page.url,source_url):
                        raise ValueError('Source quantity page unavailable')
                    if not responses:
                        try:await page.locator('#itier_prices tr.itier_price').first.wait_for(state='attached',timeout=3000)
                        except Exception:pass
                    selected=None
                    for response in responses:
                        if response.status!=200:continue
                        candidate_body=await response.body()
                        candidate=project_itoris_tiers(candidate_body)
                        if selected is None or candidate['tier_count']>selected[1]['tier_count']:
                            selected=(candidate_body,candidate)
                    if selected is None or selected[1]['tier_count']==0:
                        raise ValueError('No quantity price table exposed')
                    body,projection=selected
                baseline=product.get('account_baseline_evidence') or {}
                baseline_price=None
                key=baseline.get('object_path')
                if isinstance(key,str) and re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.soardist-account-baseline\.json',key):
                    path=Path(self.store.root)/key
                    if path.is_file():
                        baseline_price=json.loads(path.read_text()).get('rendered_default_unit_price')
                first_match=(Decimal(projection['tiers'][0]['source_price'])==Decimal(str(baseline_price))) if baseline_price is not None else None
                projection.update(source_url=source_url,received_body_sha256=hashlib.sha256(body).hexdigest(),
                                  first_tier_matches_account_baseline=first_match,collected_at=now())
                encoded=json.dumps(projection,sort_keys=True,separators=(',',':')).encode()
                object_path=await asyncio.to_thread(self.store.put_object,encoded,'itoris-tiers.json')
                with self.mutation_lock:
                    latest=self.get(id)
                    target=next(p for p in latest['products'] if p['id']==product_id)
                    if target.get('supplier_url')!=source_url or _excluded(target):
                        raise ValueError('Supplier mapping changed during quantity collection')
                    target['itoris_quantity_evidence']={'source_url':source_url,'object_path':object_path,
                        'sha256':hashlib.sha256(encoded).hexdigest(),'tier_count':projection['tier_count'],
                        'first_tier_matches_account_baseline':first_match,
                        'collected_at':projection['collected_at'],'final_eligible':False}
                    target['updated_at']=latest['updated_at']=now()
                    self._write(latest)
                results.append({'product_id':product_id,'status':'collected',
                                'tiers':projection['tier_count'],'first_tier_match':first_match})
            except Exception as exc:
                results.append({'product_id':product_id,'status':'held',
                                'reason':str(exc)[:100] if type(exc) is ValueError else 'quantity_response_unavailable'})
        return {'campaign_id':id,'attempted':len(results),'results':results,
                'itoris_quantity_tables_collected':self.get(id)['itoris_quantity_tables_collected']}

    async def collect_itoris_option_tables(self,id,pool,*,limit=28):
        """Capture source-priced Soardist option tables from page-loaded DPO responses."""
        if type(limit) is not int or not 1<=limit<=28:
            raise ValueError('Invalid option table limit')
        campaign=self.get(id)
        products=[(p['id'],p.get('supplier_url')) for p in campaign['products']
                  if not _excluded(p) and p.get('supplier')=='soardist'
                  and (not campaign.get('completion_groups') or p['id'] in campaign['active_group_product_ids'])]
        results=[]
        for product_id,source_url in products:
            if len(results)>=limit: break
            fresh=self.get(id)
            product=next(p for p in fresh['products'] if p['id']==product_id)
            if (product.get('itoris_option_evidence') or {}).get('source_url')==source_url:
                continue
            try:
                await validate_url(source_url)
                async with pool.page(SOARDIST_ORIGIN,headless=False) as page:
                    responses=[]
                    page.on('response',lambda response: responses.append(response)
                        if urlsplit(response.url).hostname=='node1.itoris.com'
                        and urlsplit(response.url).path=='/dpo/storefront/include.js' else None)
                    await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
                    if not exact_product_page(page.url,source_url):
                        raise ValueError('Source option response unavailable')
                    try: await page.locator('.product-single__meta select.product-custom-option').first.wait_for(state='visible',timeout=12000)
                    except Exception: pass
                    selected=None
                    for response in responses:
                        if response.status!=200:continue
                        candidate_body=await response.body()
                        candidate=project_itoris_dpo(candidate_body)
                        if selected is None or candidate['field_count']>selected[1]['field_count']:
                            selected=(candidate_body,candidate)
                    if selected is None or selected[1]['field_count']==0:
                        raise ValueError('No priced option table exposed')
                    body,projection=selected
                conflicts=[]
                for field in projection['fields']:
                    for choice in field['choices']:
                        fee=re.search(r'\+\s*\$\s*([0-9,]+(?:\.\d{2})?)',choice['label'])
                        if fee and (choice['source_price'] is None
                                    or Decimal(fee[1].replace(',',''))!=Decimal(choice['source_price'])):
                            conflicts.append({'field':field['label'],'value':choice['value']})
                projection.update(source_url=source_url,received_body_sha256=hashlib.sha256(body).hexdigest(),
                                  fee_label_conflicts=conflicts,collected_at=now())
                encoded=json.dumps(projection,sort_keys=True,separators=(',',':')).encode()
                object_path=await asyncio.to_thread(self.store.put_object,encoded,'itoris-options.json')
                with self.mutation_lock:
                    latest=self.get(id)
                    target=next(p for p in latest['products'] if p['id']==product_id)
                    if target.get('supplier_url')!=source_url or _excluded(target):
                        raise ValueError('Supplier mapping changed during option collection')
                    target['itoris_option_evidence']={'source_url':source_url,'object_path':object_path,
                        'sha256':hashlib.sha256(encoded).hexdigest(),'field_count':projection['field_count'],
                        'choice_count':projection['choice_count'],
                        'priced_choice_count':projection['priced_choice_count'],
                        'fee_label_conflict_count':len(conflicts),
                        'collected_at':projection['collected_at'],'final_eligible':False}
                    target['updated_at']=latest['updated_at']=now()
                    self._write(latest)
                results.append({'product_id':product_id,'status':'collected',
                                'fields':projection['field_count'],'choices':projection['choice_count'],
                                'fee_label_conflicts':len(conflicts)})
            except Exception as exc:
                results.append({'product_id':product_id,'status':'held',
                                'reason':str(exc)[:100] if type(exc) is ValueError else 'option_response_unavailable'})
        return {'campaign_id':id,'attempted':len(results),'results':results,
                'itoris_option_tables_collected':self.get(id)['itoris_option_tables_collected']}

    @_serialized_mutation
    def attach_fourover_default_probe(self, id, product_id, probe):
        """Save one browser-observed configuration without claiming catalog coverage."""
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if (product is None or _excluded(product) or product.get('supplier')!='fourover'
                or not isinstance(probe,dict) or probe.get('status')!='rendered_snapshot'
                or probe.get('source_url')!=product.get('supplier_url')):
            raise ValueError('Probe does not match a mapped 4over product')
        fields=(probe.get('observation') or {}).get('fields')
        routes=probe.get('price_route_shapes')
        if not isinstance(fields,list) or not isinstance(routes,list) or len(fields)>30 or len(routes)>3:
            raise ValueError('Unsupported default probe')
        observation={'schema':'hermes-fourover-default-probe/v1','source_url':product['supplier_url'],
                     'title':str((probe.get('observation') or {}).get('title') or '')[:300],
                     'selected_fields':[{'label':str(f.get('label') or '')[:160],
                                         'selected':str(f.get('selected') or '')[:100]}
                                        for f in fields if isinstance(f,dict)],
                     'price_routes':[{'path':str(r.get('path') or '')[:200],
                                      'package_prices':r.get('package_prices'),
                                      'tier_prices':r.get('tier_prices')}
                                     for r in routes if isinstance(r,dict)],
                     'coverage':'one_source_selected_configuration_only',
                     'cart_verified':False,'shipping_verified':False,
                     'production_verified':False,'final_eligible':False,'observed_at':now()}
        encoded=json.dumps(observation,sort_keys=True,separators=(',',':')).encode()
        object_path=self.store.put_object(encoded,'fourover-default-probe.json')
        tier_count=sum(len(r.get('package_prices') or [])+len(r.get('tier_prices') or [])
                       for r in observation['price_routes'])
        product['fourover_default_evidence']={'source_url':product['supplier_url'],
            'object_path':object_path,'sha256':hashlib.sha256(encoded).hexdigest(),
            'tier_count':tier_count,'observed_at':observation['observed_at'],
            'final_eligible':False}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,'tier_count':tier_count,
                'fourover_default_probes_collected':self.get(id)['fourover_default_probes_collected']}

    @_serialized_mutation
    def attach_fourover_price_tables(self, id, product_id, source_url, children, observations):
        """Persist complete child-route coverage without promoting verification status."""
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if (product is None or _excluded(product) or product.get('supplier')!='fourover'
                or source_url!=product.get('supplier_url') or not isinstance(children,list)
                or not isinstance(observations,list) or len(children)>2000):
            raise ValueError('Price tables do not match a mapped 4over product')
        child_ids={x.get('child_id') for x in children if isinstance(x,dict)}
        observed_ids={x.get('child_id') for x in observations if isinstance(x,dict)}
        if len(child_ids)!=len(children) or len(observed_ids)!=len(observations) or not observed_ids<=child_ids:
            raise ValueError('Duplicate or unexpected child price route')
        complete=observed_ids==child_ids and all(x.get('status')=='priced' for x in observations)
        evidence={'schema':'hermes-fourover-child-price-tables/v1','source_url':source_url,
                  'children':children,'observations':observations,'child_count':len(children),
                  'priced_count':sum(x.get('status')=='priced' for x in observations),
                  'coverage':'all_source_child_routes' if complete else 'partial_child_routes',
                  'cart_verified':False,'shipping_verified':False,'production_verified':False,
                  'final_eligible':False,'observed_at':now()}
        encoded=json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()
        object_path=self.store.put_object(encoded,'fourover-price-tables.json')
        product['fourover_price_table_evidence']={'source_url':source_url,'object_path':object_path,
            'sha256':hashlib.sha256(encoded).hexdigest(),'child_count':len(children),
            'priced_count':evidence['priced_count'],'complete_child_routes':complete,
            'tier_count':sum(len(x.get('package_prices') or [])+len(x.get('tier_prices') or [])
                             for x in observations if isinstance(x,dict)),
            'observed_at':evidence['observed_at'],'final_eligible':False}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,**product['fourover_price_table_evidence'],
                'fourover_price_tables_collected':self.get(id)['fourover_price_tables_collected']}

    @_serialized_mutation
    def attach_fourover_option_map(self, id, product_id, source_url, rows):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        price_evidence=(product or {}).get('fourover_price_table_evidence') or {}
        if (product is None or _excluded(product) or product.get('supplier')!='fourover'
                or source_url!=product.get('supplier_url') or not price_evidence.get('complete_child_routes')
                or not isinstance(rows,list) or len(rows)>2000):
            raise ValueError('Option map requires complete source child prices')
        path=(Path(self.store.root)/price_evidence['object_path']).resolve()
        if not path.is_relative_to(Path(self.store.root).resolve()) or not path.is_file():
            raise ValueError('Price table evidence is unavailable')
        body=path.read_bytes()
        if hashlib.sha256(body).hexdigest()!=price_evidence.get('sha256'):
            raise ValueError('Price table evidence hash mismatch')
        prices=json.loads(body)
        expected={x['child_id'] for x in prices['children']}
        mapped=[str(x.get('child_id')) for x in rows if isinstance(x,dict)]
        if len(mapped)!=len(rows) or len(set(mapped))!=len(mapped) or not set(mapped)<=expected:
            raise ValueError('Duplicate or unexpected option mapping')
        complete=set(mapped)==expected
        evidence={'schema':'hermes-fourover-option-map/v1','source_url':source_url,
                  'rows':rows,'expected_children':len(expected),'mapped_children':len(mapped),
                  'complete_child_mapping':complete,'price_table_sha256':price_evidence['sha256'],
                  'cart_verified':False,'shipping_verified':False,'production_verified':False,
                  'final_eligible':False,'observed_at':now()}
        encoded=json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()
        object_path=self.store.put_object(encoded,'fourover-option-map.json')
        product['fourover_option_map_evidence']={'source_url':source_url,'object_path':object_path,
            'sha256':hashlib.sha256(encoded).hexdigest(),'mapped_children':len(mapped),
            'expected_children':len(expected),'complete_child_mapping':complete,
            'observed_at':evidence['observed_at'],'final_eligible':False}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,**product['fourover_option_map_evidence'],
                'fourover_option_maps_collected':self.get(id)['fourover_option_maps_collected']}

    @_serialized_mutation
    def materialize_fourover_package_rows(self, id, product_id):
        from .fourover_rows import package_rows
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if product is None or _excluded(product) or product.get('supplier')!='fourover':
            raise ValueError('Mapped 4over product required')
        if product.get('verified_at') or product.get('finalized_at'):
            raise ValueError('Verified product cannot be replaced by draft rows')
        root=Path(self.store.root).resolve()
        def load_evidence(key,suffix):
            evidence=product.get(key) or {}
            relative=evidence.get('object_path','')
            if not isinstance(relative,str) or not re.fullmatch(
                    rf'objects/[a-f0-9]{{2}}/[a-f0-9]{{64}}\.{suffix}\.json',relative):
                raise ValueError('Required 4over evidence missing')
            path=(root/relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('Required 4over evidence unavailable')
            body=path.read_bytes()
            if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
                raise ValueError('4over evidence hash mismatch')
            value=json.loads(body)
            if value.get('source_url')!=product.get('supplier_url'):
                raise ValueError('4over evidence source mismatch')
            return value,evidence
        mappings,map_evidence=load_evidence('fourover_option_map_evidence','fourover-option-map')
        prices,price_evidence=load_evidence('fourover_price_table_evidence','fourover-price-tables')
        if mappings.get('price_table_sha256')!=price_evidence['sha256']:
            raise ValueError('Option mapping predates price table')
        public_evidence=product.get('source_collection_evidence') or {}
        public_relative=public_evidence.get('object_path','')
        if (product.get('source_collection_status')!='page_collected'
                or not isinstance(public_relative,str)
                or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.public-source\.json',public_relative)):
            raise ValueError('Exact public product evidence required')
        public_path=(root/public_relative).resolve()
        if not public_path.is_relative_to(root) or not public_path.is_file():
            raise ValueError('Public product evidence unavailable')
        public_body=public_path.read_bytes()
        if hashlib.sha256(public_body).hexdigest()!=public_evidence.get('sha256'):
            raise ValueError('Public product evidence hash mismatch')
        public=json.loads(public_body)
        if public.get('requested_url')!=product['supplier_url']:
            raise ValueError('Public product evidence source mismatch')
        content=(public.get('public_data') or {}).get('product_content') or {}
        images=content.get('images') or []
        image_url=images[0].get('url','') if len(images)==1 and isinstance(images[0],dict) else ''
        image_parts=urlsplit(image_url)
        if image_url and (image_parts.scheme!='https' or image_parts.hostname not in {'4over.com','www.4over.com'}
                          or image_parts.username or image_parts.password):
            raise ValueError('Public product image URL mismatch')
        turnarounds=[spec.get('values') for spec in (public.get('public_data') or {}).get('observed_specifications',[])
                     if isinstance(spec,dict) and spec.get('label')=='Turnaround Time']
        production_reference=(turnarounds[0][0] if len(turnarounds)==1
                              and isinstance(turnarounds[0],list) and len(turnarounds[0])==1
                              and isinstance(turnarounds[0][0],str) else '')
        rows,columns=package_rows(product['title'],product['supplier_url'],mappings,prices,
                                  image_url=image_url,production_reference=production_reference)
        stream=io.StringIO(newline='')
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='raise')
        writer.writeheader();writer.writerows(rows)
        encoded=stream.getvalue().encode('utf-8-sig')
        object_path=self.store.put_object(encoded,'fourover-draft.csv')
        product['fourover_rows_evidence']={'source_url':product['supplier_url'],
            'object_path':object_path,'sha256':hashlib.sha256(encoded).hexdigest(),
            'source_option_map_sha256':map_evidence['sha256'],
            'source_price_table_sha256':price_evidence['sha256'],
            'public_page_sha256':public_evidence['sha256'],
            'product_level_production_reference':production_reference,
            'image_url':image_url,
            'row_count':len(rows),'coverage':'all_finite_package_tiers',
            'verification':'pending_full_selection_availability_production_and_price',
            'final_eligible':False,'observed_at':now()}
        product['row_count']=len(rows)
        product['extracted_at']=now()
        product['stage']='extracted'
        product['reason']='Finite supplier option and quantity prices extracted; full-selection availability, production, and price checks pending'
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,'row_count':len(rows),'download_url':
                f'/api/v1/campaigns/{id}/fourover-draft/{product_id}',
                'counts':self.get(id)['counts']}

    @_serialized_mutation
    def materialize_fourover_area_rows(self,id,product_id,dimension_grid):
        from .fourover_rows import area_rows
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        if (product is None or _excluded(product) or product.get('supplier')!='fourover'
                or product.get('verified_at') or product.get('finalized_at')
                or not isinstance(dimension_grid,list) or len(dimension_grid)>2000):
            raise ValueError('Unverified mapped 4over product and bounded grid required')
        root=Path(self.store.root).resolve()
        values=[]
        for key,suffix in [('fourover_option_map_evidence','fourover-option-map'),
                           ('fourover_price_table_evidence','fourover-price-tables')]:
            evidence=product.get(key) or {}
            relative=evidence.get('object_path','')
            if not isinstance(relative,str) or not re.fullmatch(
                    rf'objects/[a-f0-9]{{2}}/[a-f0-9]{{64}}\.{suffix}\.json',relative):
                raise ValueError('Required 4over evidence missing')
            path=(root/relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('Required 4over evidence unavailable')
            body=path.read_bytes()
            if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
                raise ValueError('4over evidence hash mismatch')
            values.append((json.loads(body),evidence))
        (mapping,map_evidence),(prices,price_evidence)=values
        if mapping.get('price_table_sha256')!=price_evidence['sha256']:
            raise ValueError('Option map predates price table')
        rows,columns=area_rows(product['title'],product['supplier_url'],mapping,prices,dimension_grid)
        if len(rows)>250000:raise ValueError('Dimension grid exceeds row limit')
        stream=io.StringIO(newline='')
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='raise')
        writer.writeheader();writer.writerows(rows)
        encoded=stream.getvalue().encode('utf-8-sig')
        object_path=self.store.put_object(encoded,'fourover-area-draft.csv')
        grid_body=json.dumps({'source_url':product['supplier_url'],'dimension_grid':dimension_grid},
                             sort_keys=True,separators=(',',':')).encode()
        grid_path=self.store.put_object(grid_body,'fourover-dimension-grid.json')
        product['fourover_area_rows_evidence']={'source_url':product['supplier_url'],
            'object_path':object_path,'sha256':hashlib.sha256(encoded).hexdigest(),
            'dimension_grid_object_path':grid_path,'dimension_grid_sha256':hashlib.sha256(grid_body).hexdigest(),
            'source_option_map_sha256':map_evidence['sha256'],
            'source_price_table_sha256':price_evidence['sha256'],
            'row_count':len(rows),'coverage':'all_rendered_dimensions_at_one_set_only',
            'verification':'pending_quantity_cart_shipping_production',
            'final_eligible':False,'observed_at':now()}
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,'row_count':len(rows),
                'coverage':'all_rendered_dimensions_at_one_set_only',
                'download_url':f'/api/v1/campaigns/{id}/fourover-area-draft/{product_id}',
                'counts':self.get(id)['counts']}

    @_serialized_mutation
    def attach_fourover_rendered_canary(self,id,product_id,source_url,child_id,displayed_price,basis):
        data=self.get(id)
        product=next((p for p in data['products'] if p['id']==str(product_id)),None)
        evidence=(product or {}).get('fourover_price_table_evidence') or {}
        if (product is None or _excluded(product) or product.get('supplier')!='fourover'
                or source_url!=product.get('supplier_url') or not evidence.get('complete_child_routes')
                or not isinstance(child_id,str) or not child_id.isdecimal()
                or basis not in {'shipping_step_subtotal','price_per_set'}
                or not isinstance(displayed_price,str)
                or not re.fullmatch(r'\$[0-9,]+\.[0-9]{2}',displayed_price)):
            raise ValueError('Rendered canary is incomplete')
        root=Path(self.store.root).resolve()
        path=(root/evidence['object_path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Price table evidence unavailable')
        body=path.read_bytes()
        if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
            raise ValueError('Price table evidence hash mismatch')
        table=json.loads(body)
        child=next((x for x in table['observations'] if x['child_id']==child_id),None)
        if child is None or not child.get('package_prices'):
            raise ValueError('Rendered child has no package prices')
        actual=Decimal(displayed_price.replace('$','').replace(',',''))
        matches=[]
        for tier in child['package_prices']:
            expected=Decimal(str(tier['price'])).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
            if expected==actual: matches.append(tier['quantity'])
        result={'source_url':source_url,'child_id':child_id,'displayed_price':displayed_price,
                'display_basis':basis,
                'matched_quantities':matches,'status':'matched' if len(matches)==1 else 'held',
                'source_price_table_sha256':evidence['sha256'],
                'scope':'one_rendered_configuration_price_no_cart_or_order',
                'shipping_verified':False,'production_verified':False,'final_eligible':False,
                'observed_at':now()}
        history=product.setdefault('fourover_rendered_canaries',[])
        previous=product.get('fourover_rendered_canary')
        if previous and not any(x.get('child_id')==previous.get('child_id')
                                and x.get('displayed_price')==previous.get('displayed_price') for x in history):
            history.append(previous)
        if not any(x.get('child_id')==child_id and x.get('displayed_price')==displayed_price
                   and x.get('display_basis')==basis for x in history):
            history.append(result)
        product['fourover_rendered_canary']=result
        product['updated_at']=data['updated_at']=now()
        self._write(data)
        return {'product_id':product_id,**result,
                'fourover_rendered_canaries_passed':self.get(id)['fourover_rendered_canaries_passed']}

    async def collect_soardist_account_baselines(self, id, pool, *, limit=28):
        """Check rendered default prices in the existing signed-in browser only.

        This records a scoped observation. It never submits a cart or asserts
        that add-ons, quantity pricing, production, or shipping are complete.
        """
        if type(limit) is not int or not 1 <= limit <= 28:
            raise ValueError('Invalid account baseline limit')
        campaign = self.get(id)
        products = [(p['id'], p.get('supplier_url')) for p in campaign['products']
                    if not _excluded(p) and p.get('supplier') == 'soardist'
                    and (not campaign.get('completion_groups') or p['id'] in campaign['active_group_product_ids'])
                    and p.get('public_variant_evidence')]
        results = []
        async with pool.page(SOARDIST_ORIGIN, headless=False) as account:
            await account.goto(SOARDIST_ORIGIN + '/account', wait_until='domcontentloaded', timeout=45000)
            if not exact_product_page(account.url, SOARDIST_ORIGIN + '/account'):
                return {'campaign_id': id, 'status': 'authentication_required', 'attempted': 0, 'results': []}
            try:
                await account.wait_for_function("""() => [...document.querySelectorAll('button')]
                    .some(e => e.getClientRects().length && /^logout$/i.test((e.innerText||'').trim()))""",
                    timeout=8000)
            except Exception:
                pass
            authentication = await account.evaluate(ACCOUNT_CHECK_JS)
            if authentication.get('signed_in') is not True:
                return {'campaign_id': id, 'status': 'authentication_required', 'attempted': 0, 'results': []}

        for product_id, source_url in products:
            current = self.get(id)
            product = next(p for p in current['products'] if p['id'] == product_id)
            if (product.get('account_baseline_evidence') or {}).get('source_url') == source_url:
                continue
            if len(results) >= limit:
                break
            try:
                await validate_url(source_url)
                async with pool.page(SOARDIST_ORIGIN, headless=False) as page:
                    await page.goto(source_url, wait_until='domcontentloaded', timeout=45000)
                    if not exact_product_page(page.url, source_url):
                        raise ValueError('Product page redirected')
                    try:
                        await page.locator('.product-single__meta span.product__price').first.wait_for(state='visible', timeout=10000)
                    except Exception:
                        pass
                    raw = await page.evaluate(PRODUCT_PRICE_JS)
                price = observed_price(raw)
                if price is None:
                    raise ValueError('Rendered account price missing or ambiguous')
                snapshot = self.soardist_variant_snapshot(id, product_id)
                variants = snapshot['variants']
                selected_id = raw.get('variant_id')
                matched = [v for v in variants if str(v['id']) == selected_id] if selected_id else []
                if not matched and len(variants) == 1:
                    matched = variants
                comparison = 'variant_unbound'
                if len(matched) == 1:
                    public_price = Decimal(str(matched[0]['public_unit_price']))
                    comparison = 'matched_public_variant' if Decimal(price) == public_price else 'differs_from_public_variant'
                observation = {'schema': 'hermes-soardist-account-baseline/v1',
                               'source_url': source_url, 'title': raw.get('title', '')[:300],
                               'selected_variant_id': selected_id,
                               'rendered_default_unit_price': price, 'currency': 'USD',
                               'comparison': comparison, 'observed_at': now(),
                               'signed_in_account_confirmed': True,
                               'coverage': 'rendered_default_configuration_only',
                               'add_ons_verified': False, 'quantity_pricing_verified': False,
                               'shipping_verified': False, 'production_verified': False,
                               'final_eligible': False}
                encoded = json.dumps(observation, sort_keys=True, separators=(',', ':')).encode()
                object_path = await asyncio.to_thread(self.store.put_object, encoded, 'soardist-account-baseline.json')
                with self.mutation_lock:
                    fresh = self.get(id)
                    target = next(p for p in fresh['products'] if p['id'] == product_id)
                    if target.get('supplier_url') != source_url or _excluded(target):
                        raise ValueError('Supplier mapping changed during check')
                    target['account_baseline_evidence'] = {
                        'source_url': source_url, 'object_path': object_path,
                        'sha256': hashlib.sha256(encoded).hexdigest(), 'comparison': comparison,
                        'observed_at': observation['observed_at'], 'final_eligible': False}
                    target.pop('account_baseline_error', None)
                    target['updated_at'] = fresh['updated_at'] = now()
                    self._write(fresh)
                results.append({'product_id': product_id, 'status': 'collected', 'comparison': comparison})
            except Exception as exc:
                # Preserve a short safe reason; never persist page text or an
                # account page response with personal data.
                reason = exc.args[0] if type(exc) is ValueError and exc.args else 'account_baseline_unavailable'
                results.append({'product_id': product_id, 'status': 'held', 'reason': str(reason)[:120]})
        return {'campaign_id': id, 'status': 'completed_requested_scope',
                'attempted': len(results), 'results': results,
                'account_baselines_collected': self.get(id)['account_baselines_collected']}

    async def collect_soardist_variants(self, id):
        """Capture public Shopify variants without asserting logged-in cost or final coverage."""
        campaign = self.get(id)
        products = [(p['id'], p.get('supplier_url')) for p in campaign['products']
                    if not _excluded(p) and p.get('supplier') == 'soardist'
                    and (not campaign.get('completion_groups') or p['id'] in campaign['active_group_product_ids'])]
        completed = skipped = 0
        errors = []

        async def check():
            return None

        async with httpx.AsyncClient(verify=tls_context(), timeout=30, follow_redirects=False) as client:
            for product_id, source_url in products:
                current = self.get(id)
                product = next(p for p in current['products'] if p['id'] == product_id)
                if (product.get('public_variant_evidence') or {}).get('source_url') == source_url:
                    skipped += 1
                    continue
                try:
                    endpoint = product_json_url(source_url)
                    parsed = await validate_url(endpoint)
                    origin = f'{parsed.scheme}://{parsed.netloc}'
                    token = await self.limiter.acquire(origin, 2, 2, check)
                    started = time.monotonic(); status = 0; retry = 0
                    try:
                        response = await client.get(endpoint, headers={'Accept': 'application/json',
                            'User-Agent': 'Hermes/1.0 public variant review'})
                        status = response.status_code
                        retry = retry_after_seconds(response.headers.get('retry-after'))
                        if status != 200 or len(response.content) > 2_000_000:
                            raise ValueError('Public variant endpoint unavailable or too large')
                        projection = project_product_json(response.content, source_url)
                        body_sha = hashlib.sha256(response.content).hexdigest()
                    finally:
                        await asyncio.to_thread(self.limiter.release, origin, token,
                                                time.monotonic()-started, status, retry or 30, 2)
                    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()
                    object_path = await asyncio.to_thread(self.store.put_object, encoded, 'soardist-variants.json')
                    with self.mutation_lock:
                        fresh = self.get(id)
                        target = next(p for p in fresh['products'] if p['id'] == product_id)
                        if _excluded(target) or target.get('supplier_url') != source_url:
                            raise ValueError('Supplier mapping changed during collection')
                        target['public_variant_evidence'] = {
                            'source_url': source_url, 'json_url': endpoint, 'object_path': object_path,
                            'sha256': hashlib.sha256(encoded).hexdigest(), 'received_body_sha256': body_sha,
                            'variant_count': len(projection['variants']), 'collected_at': now(),
                            'price_basis': projection['price_basis'], 'final_eligible': False}
                        target.pop('public_variant_error', None)
                        target['updated_at'] = fresh['updated_at'] = now()
                        self._write(fresh)
                    completed += 1
                except (ValueError, httpx.HTTPError, json.JSONDecodeError) as exc:
                    reason = str(exc)[:160]
                    with self.mutation_lock:
                        fresh = self.get(id)
                        target = next(p for p in fresh['products'] if p['id'] == product_id)
                        target['public_variant_error'] = reason
                        target['updated_at'] = fresh['updated_at'] = now()
                        self._write(fresh)
                    errors.append({'product_id': product_id, 'reason': reason})
        return {'campaign_id': id, 'attempted': len(products), 'collected': completed,
                'skipped_existing': skipped, 'errors': errors,
                'public_variants_collected': self.get(id)['public_variants_collected']}

    def soardist_variant_snapshot(self, id, product_id):
        product = next((p for p in self.get(id)['products'] if p['id'] == str(product_id)), None)
        if product is None or _excluded(product):
            raise FileNotFoundError('Campaign product not found')
        evidence = product.get('public_variant_evidence') or {}
        key = evidence.get('object_path')
        if (product.get('supplier') != 'soardist' or evidence.get('source_url') != product.get('supplier_url')
                or not isinstance(key, str)
                or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.soardist-variants\.json', key)):
            raise FileNotFoundError('Public variant evidence not found')
        path = (Path(self.store.root) / key).resolve()
        if not path.is_relative_to(Path(self.store.root).resolve()) or not path.is_file():
            raise FileNotFoundError('Public variant evidence not found')
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != evidence.get('sha256'):
            raise ValueError('Public variant evidence hash mismatch')
        data = json.loads(body)
        if data.get('source_url') != product.get('supplier_url') or data.get('final_eligible') is not False:
            raise ValueError('Public variant evidence scope mismatch')
        return data

    def list(self):
        result=[]
        for p in self.root.glob('*.json'):
            data=self.get(p.stem);data.pop('products',None);result.append(data)
        return sorted(result,key=lambda x:x['updated_at'],reverse=True)

    @_serialized_mutation
    def create(self, *, name, products, id=None):
        if not products or len(products)>1000:
            raise ValueError('Campaign requires 1–1000 products')
        id=id or uuid.uuid4().hex
        if self._path(id).exists():
            raise ValueError('Campaign already exists')
        output=[]; seen=set()
        for p in products:
            pid=str(p['id']);url=p['storefront_url'];parts=urlsplit(url)
            if pid in seen or not pid.isdigit() or parts.scheme!='https' or parts.hostname not in {'printitprints.com','www.printitprints.com'} or parts.username or parts.password or parts.query or parts.fragment or parts.port not in (None,443):
                raise ValueError('Invalid or duplicate product')
            if p.get('supplier_url'): _supplier_url(p['supplier_url'])
            seen.add(pid)
            output.append({'id':pid,'title':str(p['title'])[:300],'storefront_url':url,'supplier_url':p.get('supplier_url'),
                'supplier':p.get('supplier'),'stage':'queued','reason':None,'diagnosis_id':None,'run_id':None,'product_id':None,
                'finalization_id':None,'row_count':0,'updated_at':now(),'mapping_evidence':p.get('mapping_evidence',[])})
        groups=[[p['id'] for p in output[offset:offset+10]] for offset in range(0,len(output),10)]
        data={'id':id,'name':str(name)[:300],'status':'queued','created_at':now(),'updated_at':now(),
              'products':output,'completion_groups':groups,'active_group_index':0}
        self._write(data);return self.get(id)

    @_serialized_mutation
    def exclude_products(self, id, products):
        """Retain records and evidence while removing an explicit user scope."""
        if id in self.tasks: raise ValueError('Wait for active campaign analysis before excluding products')
        if not isinstance(products,list) or not 1 <= len(products) <= 1000:
            raise ValueError('Exclusions require a bounded product list')
        data=self.get(id); indexed={p['id']:p for p in data['products']}
        updates=[]; seen=set()
        for item in products:
            if not isinstance(item,dict) or set(item)!={'id','reason'}:
                raise ValueError('Each exclusion requires only a product id and reason')
            pid=item['id']; reason=item['reason']
            if not isinstance(pid,str) or pid not in indexed or pid in seen:
                raise ValueError('Unknown or duplicate campaign product')
            if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
                raise ValueError('An explicit exclusion reason is required')
            seen.add(pid); updates.append((indexed[pid],reason.strip()))
        timestamp=now(); changed=False
        for product,reason in updates:
            if _excluded(product) and product.get('exclusion_reason')==reason: continue
            if not _excluded(product): product['stage_before_exclusion']=product['stage']
            product.setdefault('excluded_at',timestamp)
            product.update(stage='excluded',reason=reason,exclusion_reason=reason,updated_at=timestamp)
            changed=True
        if changed:
            data['updated_at']=timestamp; self._write(data)
        return self.get(id)

    @_serialized_mutation
    def update_mappings(self, id, products):
        """Update explicit supplier bindings, never extraction milestones."""
        if id in self.tasks: raise ValueError('Wait for active campaign analysis before updating mappings')
        data = self.get(id)
        indexed = {product['id']: product for product in data['products']}
        updates, seen = [], set()
        if not isinstance(products, list): raise ValueError('Mappings must be a product list')
        for product in products:
            if not isinstance(product, dict) or set(product) - {'id', 'supplier', 'supplier_url', 'mapping_evidence'}:
                raise ValueError('Only supplier mapping fields may be updated')
            pid = str(product.get('id', ''))
            if pid not in indexed or pid in seen: raise ValueError('Unknown or duplicate campaign product')
            if _excluded(indexed[pid]): raise ValueError('Excluded campaign products cannot receive mapping updates')
            seen.add(pid)
            patch = {key: value for key, value in product.items() if key != 'id'}
            if 'supplier_url' in patch: _supplier_url(patch['supplier_url'])
            if ('supplier_url' in patch and patch['supplier_url'] != indexed[pid].get('supplier_url')
                    and indexed[pid].get('finalized_at')):
                raise ValueError('A finalized product cannot be rebound to another source')
            if 'supplier' in patch and patch['supplier'] is not None and (not isinstance(patch['supplier'], str) or not patch['supplier'].strip() or len(patch['supplier']) > 120):
                raise ValueError('Invalid supplier name')
            if 'mapping_evidence' in patch and (not isinstance(patch['mapping_evidence'], list) or len(patch['mapping_evidence']) > 100):
                raise ValueError('Mapping evidence must be a bounded list')
            # Validate serializability before changing any product.
            patch = json.loads(json.dumps(patch, allow_nan=False))
            updates.append((pid, patch))
        timestamp = now()
        for pid, patch in updates:
            product = indexed[pid]
            if 'supplier_url' in patch and patch['supplier_url'] != product.get('supplier_url') and product.get('source_collection_id'):
                product.setdefault('source_collection_history', []).append({key: product[key] for key in (
                    'source_collection_id','source_collection_status','source_collection_evidence','source_collected_at') if key in product})
                for key in ('source_collection_id','source_collection_status','source_collection_evidence','source_collected_at'):
                    product.pop(key, None)
            if 'supplier_url' in patch and patch['supplier_url'] != product.get('supplier_url'):
                product.setdefault('source_mapping_history',[]).append({
                    'supplier_url':product.get('supplier_url'),
                    'supplier':product.get('supplier'),
                    'diagnosis_id':product.get('diagnosis_id'),
                    'changed_at':timestamp})
                source_keys={'diagnosis_id','diagnosis_status','diagnosis_field_count',
                    'diagnosis_warnings','diagnosis_attached_at','diagnosed_at',
                    'extracted_at','verified_at','finalized_at','public_variant_evidence',
                    'account_baseline_evidence','run_id','product_id','finalization_id',
                    'finalization_evidence'}
                for key in list(product):
                    if key in source_keys or key.startswith(('fourover_','itoris_','b2sign_','dps_')):
                        product.pop(key,None)
                product.pop('mapping_evidence',None)
                product.update(stage='queued',reason='Supplier source changed; new diagnosis required',row_count=0)
            indexed[pid].update(patch, updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_diagnosis(self, id, product_id, diagnosis_id):
        """Associate an exact supplier-page snapshot without claiming price coverage."""
        if id in self.tasks: raise ValueError('Wait for active campaign analysis before attaching evidence')
        data = self.get(id)
        product = next((item for item in data['products'] if item['id'] == str(product_id)), None)
        if product is None: raise ValueError('Unknown campaign product')
        if _excluded(product): raise ValueError('Excluded campaign products cannot receive diagnoses')
        if not isinstance(diagnosis_id, str) or not re.fullmatch('[a-f0-9]{32}', diagnosis_id):
            raise ValueError('Invalid diagnosis identifier')
        path = Path(self.store.root) / 'diagnoses' / (diagnosis_id + '.json')
        if not path.is_file(): raise ValueError('Saved diagnosis is unavailable')
        diagnosis = json.loads(path.read_text())
        if diagnosis.get('id') != diagnosis_id or diagnosis.get('url') != product.get('supplier_url'):
            raise ValueError('Diagnosis must exactly match the supplier page')
        if diagnosis.get('status') not in {'ready', 'attention', 'login_required', 'closed'}:
            raise ValueError('Diagnosis is still in progress')
        fields = diagnosis.get('fields')
        if not isinstance(fields, list): raise ValueError('Diagnosis fields are unavailable')
        timestamp = now()
        product.update(diagnosis_id=diagnosis_id, diagnosis_status=diagnosis['status'],
                       diagnosis_field_count=sum(bool(field.get('configurable')) for field in fields if isinstance(field, dict)),
                       diagnosis_warnings=diagnosis.get('warnings', []), diagnosis_attached_at=timestamp,
                       updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_complete_review(self, id, product_id, review_id):
        """Advance only an immutable review that certifies the entire mapped product."""
        from .product_finalization import same_product

        if id in self.tasks:
            raise ValueError('Wait for active campaign analysis before completing a product')
        if not isinstance(review_id, str) or not re.fullmatch(r'[a-f0-9]{32}', review_id):
            raise ValueError('Invalid finalization review identifier')
        data = self.get(id)
        product = next((item for item in data['products'] if item['id'] == str(product_id)), None)
        if product is None or _excluded(product):
            raise ValueError('Active campaign product required')
        if product.get('finalization_id') == review_id and product.get('finalized_at'):
            return self.get(id)
        if data.get('completion_groups') and str(product_id) not in data['active_group_product_ids']:
            raise ValueError('Finish the current product group before starting another')
        if product.get('finalized_at'):
            raise ValueError('A finalized product cannot be replaced without a new review workflow')
        if not product.get('supplier_url') or not product.get('diagnosis_id'):
            raise ValueError('Exact supplier mapping and diagnosis required')
        review_root = Path(self.store.root) / 'finalizations' / review_id
        report_path = review_root / 'report.json'
        csv_path = review_root / 'finalized.csv'
        if review_root.is_symlink() or report_path.is_symlink() or csv_path.is_symlink() or not report_path.is_file():
            raise ValueError('Saved finalization review unavailable')
        report_body = report_path.read_bytes()
        report = json.loads(report_body)
        if (report.get('id') != review_id or report.get('diagnosis_id') != product['diagnosis_id']
                or not same_product(report.get('source_url'), product['supplier_url'])):
            raise ValueError('Review does not match this supplier product and diagnosis')
        if report.get('pricing_model') == 'source_live_quote':
            raise ValueError('Live-quote products require a separate runtime completion review')
        eligible = report.get('eligible_rows')
        if (report.get('catalog_complete') is not True or not isinstance(eligible, int)
                or isinstance(eligible, bool) or eligible <= 0
                or report.get('held_rows') != 0 or not csv_path.is_file()):
            raise ValueError('Review does not establish complete eligible product rows')
        examined = report.get('examined_rows')
        components = [report.get(key) for key in (
            'eligible_rows', 'invalid_rows_removed', 'hidden_choice_rows_omitted')]
        if (not isinstance(examined, int) or isinstance(examined, bool)
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                       for value in components)
                or examined != sum(components)):
            raise ValueError('Review row accounting is inconsistent')
        with csv_path.open(newline='', encoding='utf-8-sig') as stream:
            csv_rows = sum(1 for _ in csv.DictReader(stream))
        if csv_rows != report['eligible_rows']:
            raise ValueError('Finalized CSV does not match review row count')
        timestamp = now()
        product.update(stage='finalized', extracted_at=product.get('extracted_at') or timestamp,
                       verified_at=timestamp, finalized_at=timestamp,
                       finalization_id=review_id, row_count=csv_rows,
                       finalization_evidence={'report_sha256': hashlib.sha256(report_body).hexdigest(),
                                              'source_url': report['source_url'],
                                              'eligible_rows': csv_rows, 'catalog_complete': True,
                                              'pricing_model': report.get('pricing_model')},
                       reason=('Source-backed parametric options, quantity scope, and price review passed'
                               if report.get('source')=='b2sign_parametric_tent' else
                               'Complete supplier selection, production, and price review passed'),
                       updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_complete_reviews_batch(self, id, reviews):
        """Preflight every complete review, then promote the group in one write."""
        from .product_finalization import same_product

        if id in self.tasks:
            raise ValueError('Wait for active campaign analysis before completing products')
        if not isinstance(reviews, list) or not reviews:
            raise ValueError('At least one complete review is required')
        data = self.get(id)
        indexed = {item['id']: item for item in data['products']}
        pending = []
        seen = set()
        for entry in reviews:
            if not isinstance(entry, dict):
                raise ValueError('Invalid batch review entry')
            product_id, review_id = entry.get('product_id'), entry.get('review_id')
            if not isinstance(product_id, str) or product_id in seen:
                raise ValueError('Each campaign product may appear once in a completion batch')
            seen.add(product_id)
            if not isinstance(review_id, str) or not re.fullmatch(r'[a-f0-9]{32}', review_id):
                raise ValueError('Invalid finalization review identifier')
            product = indexed.get(product_id)
            if product is None or _excluded(product):
                raise ValueError('Active campaign product required')
            if product.get('finalization_id') == review_id and product.get('finalized_at'):
                continue
            if data.get('completion_groups') and product_id not in data['active_group_product_ids']:
                raise ValueError('Finish the current product group before starting another')
            if product.get('finalized_at'):
                raise ValueError('A finalized product cannot be replaced without a new review workflow')
            if not product.get('supplier_url') or not product.get('diagnosis_id'):
                raise ValueError('Exact supplier mapping and diagnosis required')
            review_root = Path(self.store.root) / 'finalizations' / review_id
            report_path, csv_path = review_root / 'report.json', review_root / 'finalized.csv'
            if (review_root.is_symlink() or report_path.is_symlink() or csv_path.is_symlink()
                    or not report_path.is_file()):
                raise ValueError('Saved finalization review unavailable')
            report_body = report_path.read_bytes()
            report = json.loads(report_body)
            if (report.get('id') != review_id or report.get('diagnosis_id') != product['diagnosis_id']
                    or not same_product(report.get('source_url'), product['supplier_url'])):
                raise ValueError('Review does not match this supplier product and diagnosis')
            if report.get('pricing_model') == 'source_live_quote':
                raise ValueError('Live-quote products require a separate runtime completion review')
            eligible = report.get('eligible_rows')
            if (report.get('catalog_complete') is not True or not isinstance(eligible, int)
                    or isinstance(eligible, bool) or eligible <= 0
                    or report.get('held_rows') != 0 or not csv_path.is_file()):
                raise ValueError('Review does not establish complete eligible product rows')
            examined = report.get('examined_rows')
            components = [report.get(key) for key in (
                'eligible_rows', 'invalid_rows_removed', 'hidden_choice_rows_omitted')]
            if (not isinstance(examined, int) or isinstance(examined, bool)
                    or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                           for value in components) or examined != sum(components)):
                raise ValueError('Review row accounting is inconsistent')
            with csv_path.open(newline='', encoding='utf-8-sig') as stream:
                csv_rows = sum(1 for _ in csv.DictReader(stream))
            if csv_rows != eligible:
                raise ValueError('Finalized CSV does not match review row count')
            pending.append((product, review_id, report_body, report, csv_rows))
        if not pending:
            return self.get(id)
        timestamp = now()
        for product, review_id, report_body, report, csv_rows in pending:
            product.update(stage='finalized', extracted_at=product.get('extracted_at') or timestamp,
                           verified_at=timestamp, finalized_at=timestamp,
                           finalization_id=review_id, row_count=csv_rows,
                           finalization_evidence={'report_sha256': hashlib.sha256(report_body).hexdigest(),
                                                  'source_url': report['source_url'],
                                                  'eligible_rows': csv_rows, 'catalog_complete': True,
                                                  'pricing_model': report.get('pricing_model')},
                           reason=('Source-backed parametric options, quantity scope, and price review passed'
                                   if report.get('source')=='b2sign_parametric_tent' else
                                   'Complete supplier selection, production, and price review passed'),
                           updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    def _validate_b2sign_runtime_review(self, product, review_id):
        """Recompute dynamic source coverage; a finite CSV cannot prove this domain."""
        from .b2sign_catalog import PINNED_BUNDLE_SHA256, parse_public_product
        from .b2sign_harvest import prepare_candidate
        from .campaign_sources import SourceCollector
        from .product_finalization import same_product

        root = Path(self.store.root)
        path = root / 'finalizations' / review_id / 'report.json'
        if path.is_symlink() or not path.is_file():
            raise ValueError('Saved runtime review unavailable')
        body = path.read_bytes()
        report = json.loads(body)
        if (report.get('schema') != 'hermes-b2sign-runtime-review/v1'
                or report.get('id') != review_id
                or report.get('product_id') != product['id']
                or report.get('diagnosis_id') != product.get('diagnosis_id')
                or report.get('source_collection_id') != product.get('source_collection_id')
                or not same_product(report.get('source_url'), product.get('supplier_url'))
                or report.get('pricing_model') != 'source_live_quote'
                or report.get('fulfillment_mode') != 'dropship'
                or report.get('source_data_complete') is not True
                or report.get('static_rows_claimed') is not False):
            raise ValueError('Runtime review does not match the source product or pricing model')
        collection = SourceCollector(self.store).get(product['source_collection_id'], include_data=True)
        pages = [page for page in collection['pages']
                 if same_product(page.get('url'), product['supplier_url'])
                 and page.get('status') == 'completed'
                 and page.get('public_data', {}).get('item')]
        if len(pages) != 1 or pages[0]['public_data']['item'].get('saleable') is not True:
            raise ValueError('Source product is not presently evidenced as saleable')
        item = pages[0]['public_data']['item']
        catalog = parse_public_product(item, source_url=product['supplier_url'],
                                       source_bundle_sha256=PINNED_BUNDLE_SHA256)
        fixed_options = required_fixed_options(product)
        if report.get('fixed_options', {}) != fixed_options:
            raise ValueError('Runtime review does not preserve storefront-fixed source choices')
        if (report.get('source_contract_sha256') != catalog['contract_sha256']
                or any(gap.get('reason') != 'open_or_dynamic_size' for gap in catalog.get('gaps', []))):
            raise ValueError('Runtime source contract has unsupported gaps or changed')
        source_images = sorted({image['s1000'] for image in item.get('images', [])
                                if isinstance(image, dict) and image.get('s1000')})
        if not source_images or sorted(row.get('url') for row in report.get('images', [])) != source_images:
            raise ValueError('Runtime review images do not cover the source gallery')
        for image in report['images']:
            sha = image.get('sha256')
            if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha):
                raise ValueError('Invalid source image digest')
            saved = root / 'b2sign-images' / (sha + '.image')
            if saved.is_symlink() or not saved.is_file() or saved.stat().st_size != image.get('bytes'):
                raise ValueError('Source image is not durably saved')
            if hashlib.sha256(saved.read_bytes()).hexdigest() != sha:
                raise ValueError('Source image checksum mismatch')

        choices = {(field['key'], choice['value']) for field in catalog['dimensions']
                   if field.get('visible') and not field.get('disabled')
                   and field.get('domain_kind') == 'finite_options'
                   for choice in field.get('choices', [])
                   if choice.get('visible') and not choice.get('disabled') and choice.get('label')
                   and (field['key'] not in fixed_options or fixed_options[field['key']] == choice['value'])}
        boundaries = {1, 100, 101, 200, 201, 9999}
        covered_choices, covered_boundaries = set(), set()
        native_size_bound = False
        refs = report.get('canary_result_refs')
        if not isinstance(refs, list) or not 1 <= len(refs) <= 5000 or len(set(refs)) != len(refs):
            raise ValueError('Runtime review must reference distinct saved source quotes')
        for ref in refs:
            if not isinstance(ref, str) or not re.fullmatch(
                    r'b2sign-harvests/[a-f0-9]{32}/results/[a-f0-9]{64}\.json', ref):
                raise ValueError('Invalid source quote reference')
            saved = root / ref
            if saved.is_symlink() or not saved.is_file():
                raise ValueError('Source quote evidence is missing')
            row = json.loads(saved.read_text())
            candidate = row.get('candidate') or {}
            if (row.get('status') != 'quoted' or not row.get('quote')
                    or not same_product(candidate.get('source_url'), product['supplier_url'])
                    or candidate.get('contract_sha256') != catalog['contract_sha256']):
                raise ValueError('Source quote evidence is not complete and scoped')
            scoped = dict(candidate, runtime_context={'_shipping': 'dropship'})
            prepare_candidate(catalog, scoped)
            values = candidate.get('values') or {}
            if any(values.get(key) != value for key, value in fixed_options.items()):
                raise ValueError('Source quote violates storefront-fixed supplier choices')
            for key in ('holespunch', 'grommet'):
                if str(values.get(key, '')).startswith('custom') and not values.get(key + '_CUSTOM_VALUE'):
                    raise ValueError('Custom source choice lacks nested geometry')
            covered_choices.update((key, value) for key, value in choices if values.get(key) == value)
            covered_boundaries.add(candidate.get('quantity'))
            native_size_bound |= row.get('native_size_bound') is True
        if choices - covered_choices or boundaries - covered_boundaries:
            raise ValueError('Runtime review leaves source choices or quantity boundaries unverified')
        if (any(field.get('domain_kind') == 'open_or_dynamic_size' for field in catalog['dimensions'])
                and not native_size_bound):
            raise ValueError('Open size lacks a native-bound exact quote')
        return body, report, len(refs)

    @_serialized_mutation
    def attach_b2sign_runtime_reviews_batch(self, id, reviews):
        """Finalize source-complete dynamic contracts atomically, without static rows."""
        if id in self.tasks:
            raise ValueError('Wait for active campaign analysis before completing products')
        if not isinstance(reviews, list) or not reviews:
            raise ValueError('At least one runtime review is required')
        data = self.get(id)
        indexed = {item['id']: item for item in data['products']}
        pending, seen = [], set()
        for entry in reviews:
            product_id, review_id = entry.get('product_id'), entry.get('review_id')
            if (not isinstance(product_id, str) or product_id in seen
                    or not isinstance(review_id, str) or not re.fullmatch(r'[a-f0-9]{32}', review_id)):
                raise ValueError('Invalid or duplicate runtime review entry')
            seen.add(product_id)
            product = indexed.get(product_id)
            if not product or _excluded(product) or product.get('supplier') != 'b2sign':
                raise ValueError('Active B2Sign campaign product required')
            if product.get('finalization_id') == review_id and product.get('finalized_at'):
                continue
            if product.get('finalized_at') or product_id not in data['active_group_product_ids']:
                raise ValueError('Finish the current product group before starting another')
            if not product.get('diagnosis_id') or not product.get('source_collection_id'):
                raise ValueError('Exact diagnosis and source collection required')
            body, report, count = self._validate_b2sign_runtime_review(product, review_id)
            pending.append((product, review_id, body, report, count))
        if not pending:
            return self.get(id)
        timestamp = now()
        for product, review_id, body, report, count in pending:
            product.update(stage='finalized', extracted_at=product.get('extracted_at') or timestamp,
                           verified_at=timestamp, finalized_at=timestamp,
                           finalization_id=review_id, row_count=0,
                           finalization_evidence={
                               'report_sha256': hashlib.sha256(body).hexdigest(),
                               'source_url': report['source_url'],
                               'source_contract_sha256': report['source_contract_sha256'],
                               'pricing_model': 'source_live_quote',
                               'source_data_complete': True,
                               'static_rows_claimed': False,
                               'quoted_canaries': count,
                               'storefront_deployed': False,
                           },
                           reason='Source options and exact live-quote contract complete; storefront integration pending',
                           updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_gator_runtime_review(self, id, product_id, review_id):
        """Finalize source-live Gator data only after recomputing the review."""
        from .fourover_gator_runtime_review import inspect_gator_runtime

        if id in self.tasks:
            raise ValueError('Wait for active campaign analysis before completing a product')
        if product_id != '6742' or not isinstance(review_id, str) or not re.fullmatch(r'[a-f0-9]{32}', review_id):
            raise ValueError('Invalid Gator runtime review request')
        data = self.get(id)
        product = next((row for row in data['products'] if row['id'] == product_id), None)
        if not product or _excluded(product) or product.get('supplier') != 'fourover':
            raise ValueError('Active Gator campaign product required')
        if product.get('finalization_id') == review_id and product.get('finalized_at'):
            return self.get(id)
        if product.get('finalized_at') or product_id not in data.get('active_group_product_ids', []):
            raise ValueError('Finish the current product group before starting another')
        path = Path(self.store.root) / 'finalizations' / review_id / 'report.json'
        if path.is_symlink() or not path.is_file():
            raise ValueError('Saved Gator runtime review unavailable')
        body = path.read_bytes()
        report = json.loads(body)
        expected = inspect_gator_runtime(product, self.store.root, id)
        if report.get('id') != review_id or {k: v for k, v in report.items() if k != 'id'} != expected:
            raise ValueError('Gator runtime review no longer matches complete source evidence')
        timestamp = now()
        product.update(stage='finalized', extracted_at=product.get('extracted_at') or timestamp,
                       verified_at=timestamp, finalized_at=timestamp,
                       finalization_id=review_id, row_count=0,
                       finalization_evidence={
                           'report_sha256': hashlib.sha256(body).hexdigest(),
                           'source_url': report['source_url'],
                           'option_map_sha256': report['option_map_sha256'],
                           'dimension_grid_sha256': report['dimension_grid_sha256'],
                           'pricing_model': 'source_live_quote',
                           'source_data_complete': True,
                           'static_rows_claimed': False,
                           'quoted_canaries': report['canary_count'],
                           'storefront_deployed': False,
                       },
                       reason='Source options and guarded exact live-quote contract complete; storefront integration pending',
                       updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_dps_runtime_reviews_batch(self, id, reviews):
        """Promote only recomputed, source-live DPS contracts in the active group."""
        from .dps_runtime_review import inspect_dps_runtime

        if id in self.tasks:
            raise ValueError('Wait for active campaign analysis before completing products')
        if not isinstance(reviews, list) or not reviews:
            raise ValueError('At least one DPS runtime review is required')
        data = self.get(id)
        indexed = {row['id']: row for row in data['products']}
        pending, seen = [], set()
        for entry in reviews:
            product_id, review_id = entry.get('product_id'), entry.get('review_id')
            if (not isinstance(product_id, str) or product_id in seen
                    or not isinstance(review_id, str) or not re.fullmatch(r'[a-f0-9]{32}', review_id)):
                raise ValueError('Invalid or duplicate DPS runtime review')
            seen.add(product_id)
            product = indexed.get(product_id)
            if not product or _excluded(product) or product.get('supplier') != 'digital_print_solutions':
                raise ValueError('Active DPS campaign product required')
            if product.get('finalization_id') == review_id and product.get('finalized_at'):
                continue
            if product.get('finalized_at') or product_id not in data['active_group_product_ids']:
                raise ValueError('Finish the current product group before starting another')
            path = Path(self.store.root) / 'finalizations' / review_id / 'report.json'
            if path.is_symlink() or not path.is_file():
                raise ValueError('Saved DPS runtime review unavailable')
            body = path.read_bytes()
            report = json.loads(body)
            expected = inspect_dps_runtime(product, self.store.root, id)
            if report.get('id') != review_id or {k: v for k, v in report.items() if k != 'id'} != expected:
                raise ValueError('DPS runtime review no longer matches complete source evidence')
            pending.append((product, review_id, body, report))
        if not pending:
            return self.get(id)
        timestamp = now()
        for product, review_id, body, report in pending:
            product.update(stage='finalized', extracted_at=product.get('extracted_at') or timestamp,
                           verified_at=timestamp, finalized_at=timestamp,
                           finalization_id=review_id, row_count=0,
                           finalization_evidence={
                               'report_sha256': hashlib.sha256(body).hexdigest(),
                               'source_url': report['source_url'],
                               'source_script_sha256': report['source_script_sha256'],
                               'option_domain_sha256': report['option_domain_sha256'],
                               'pricing_model': 'source_live_quote',
                               'source_data_complete': True,
                               'static_rows_claimed': False,
                               'quoted_canaries': 1,
                               'storefront_deployed': False,
                           },
                           reason='Guarded native-page exact source quote and options complete; storefront integration pending',
                           updated_at=timestamp)
        data['updated_at'] = timestamp
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def attach_collection(self, id, product_id, job_id, source_url):
        """Attach a saved public page without asserting extracted/verified rows."""
        if id in self.tasks: raise ValueError('Wait for active campaign analysis before attaching evidence')
        data = self.get(id)
        product = next((item for item in data['products'] if item['id'] == str(product_id)), None)
        if product is None: raise ValueError('Unknown campaign product')
        if _excluded(product): raise ValueError('Excluded campaign products cannot receive source attachments')
        _supplier_url(source_url)
        if not source_url or product.get('supplier_url') != source_url:
            raise ValueError('Source page must exactly match the product supplier mapping')
        if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
            raise ValueError('Invalid source collection identifier')
        root = Path(self.store.root).resolve()
        job_path = (root / 'source-collections' / (job_id + '.json')).resolve()
        if not job_path.is_relative_to(root / 'source-collections') or not job_path.is_file():
            raise ValueError('Saved source collection is unavailable')
        job = json.loads(job_path.read_text())
        if job.get('id') != job_id or job.get('schema') != 'hermes-public-source-collection/v1':
            raise ValueError('Unsupported source collection')
        pages = [page for page in job.get('pages', []) if page.get('url') == source_url]
        if len(pages) != 1 or pages[0].get('status') != 'completed':
            raise ValueError('This exact supplier page has not completed collection')
        evidence = pages[0].get('evidence') or {}
        if evidence.get('status') != 200 or evidence.get('requested_url') != source_url:
            raise ValueError('Completed supplier HTTP evidence is missing')
        key = evidence.get('object_path', '')
        if not isinstance(key, str) or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.public-source\.json', key):
            raise ValueError('Invalid source evidence object')
        path = (root / key).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Source evidence is unavailable locally')
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != evidence.get('sha256'):
            raise ValueError('Source evidence hash does not match')
        saved = json.loads(body)
        if (saved.get('requested_url') != source_url or saved.get('status') != 200
                or saved.get('url') != evidence.get('url')
                or saved.get('stored_body') != 'public_projection_only_raw_envelope_not_retained'
                or saved.get('public_data', {}).get('data_scope') != 'public_page_metadata_only'):
            raise ValueError('Source evidence does not match the saved public page')
        attached = {key: evidence.get(key) for key in ('object_path','sha256','requested_url','url','status','captured_at')}
        if product.get('source_collection_id') and (product['source_collection_id'] != job_id or product.get('source_collection_evidence') != attached):
            product.setdefault('source_collection_history', []).append({key: product[key] for key in (
                'source_collection_id','source_collection_status','source_collection_evidence','source_collected_at') if key in product})
        product.update(source_collection_id=job_id,source_collection_status='page_collected',
                       source_collection_evidence=attached,source_collected_at=evidence.get('captured_at'),updated_at=now())
        data['updated_at'] = now()
        self._write(data)
        return self.get(id)

    @_serialized_mutation
    def reinspect_saved(self, id):
        """Reparse existing local HTML with zero requests or object-store fetches."""
        if id in self.tasks: raise ValueError('Wait for active campaign analysis before reinspection')
        data = self.get(id)
        root = Path(self.store.root).resolve()
        for product in data['products']:
            if _excluded(product): continue
            saved = product.get('storefront_evidence') or {}
            key = saved.get('object_path', '')
            if not key: continue
            if not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.response', key):
                raise ValueError('Invalid saved evidence path')
            path = (root / key).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Saved evidence is unavailable locally')
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != saved.get('sha256'):
                raise ValueError('Saved evidence hash does not match')
            evidence = inspect_storefront(body.decode('utf8', errors='replace'), saved['source_url'], product['id'])
            product['storefront_evidence'] = {**saved, **evidence, 'reinspected_at': now()}
            if not evidence['identity_confirmed']:
                product.pop('diagnosed_at', None)
                product.update(stage='blocked', reason='Storefront identity could not be confirmed')
            # An offline reparse neither reruns failed requests nor promotes an
            # uncompleted product to a new extraction or verification milestone.
            product['updated_at'] = now()
        data['updated_at'] = now()
        self._write(data)
        return self.get(id)

    async def analyze(self,id):
        with self.mutation_lock:
            if id in self.tasks:
                return self.get(id)
            data=self.get(id)
            data['status']='running';data.pop('stop_reason',None);data.pop('error',None);data['updated_at']=now();self._write(data)
            task=asyncio.create_task(self._analyze(data));self.tasks[id]=task
            task.add_done_callback(lambda _:self.tasks.pop(id,None))
            return self.get(id)

    async def _analyze(self,data):
        stop=None
        async def check():
            if stop: raise ValueError(stop)
        try:
            async with httpx.AsyncClient(verify=tls_context(),timeout=45,follow_redirects=False) as client:
                for p in data['products']:
                    if _excluded(p):continue
                    groups=data.get('completion_groups') or []
                    active_index=data.get('active_group_index',0)
                    if groups and (active_index>=len(groups)
                                   or p['id'] not in groups[active_index]):continue
                    if p.get('diagnosed_at') and p['stage']!='blocked':continue
                    # Only an explicit analyze invocation retries a blocked
                    # product. Preserve every preceding failure as audit data.
                    attempts=p.setdefault('analysis_attempts',[])
                    if not attempts and p['stage']=='blocked' and p.get('reason'):
                        attempts.append({'status':'blocked','reason':p['reason'],'finished_at':p.get('updated_at'),'legacy':True})
                    attempt={'id':uuid.uuid4().hex,'started_at':now(),'status':'running'}
                    attempts.append(attempt)
                    data['updated_at']=now();self._write(data)
                    url=p['storefront_url'];response=None
                    try:
                        for redirect in range(4):
                            parsed=await validate_url(url)
                            origin=f'{parsed.scheme}://{parsed.netloc}'
                            token=await self.limiter.acquire(origin,1,1,check)
                            start=time.monotonic();status=0;retry=0
                            try:
                                async with client.stream('GET',url,headers={'Accept':'text/html','User-Agent':'Hermes/0.1 product catalog review'}) as r:
                                    status=r.status_code;retry=retry_after_seconds(r.headers.get('retry-after'))
                                    if status in {301,302,303,307,308}:
                                        target=urljoin(url,r.headers.get('location',''))
                                        if urlsplit(target).hostname not in {'printitprints.com','www.printitprints.com'}:raise ValueError('Unexpected redirect')
                                        url=target;continue
                                    if status>=400:raise ValueError(f'Storefront HTTP {status}')
                                    chunks=[];length=0
                                    async for chunk in r.aiter_bytes():
                                        length+=len(chunk)
                                        if length>32*1024*1024:raise ValueError('Product page exceeds 32 MB')
                                        chunks.append(chunk)
                                    response=b''.join(chunks)
                            finally:
                                await asyncio.to_thread(self.limiter.release,origin,token,time.monotonic()-start,status,retry or 30,1)
                            break
                        if response is None:raise ValueError('Redirect limit exceeded')
                        html=response.decode('utf8',errors='replace')
                        if any(term in html[:5000].lower() for term in ['verify you are human','<title>just a moment','<title>authenticating']):raise ValueError('Storefront access challenge')
                        evidence=await asyncio.to_thread(inspect_storefront,html,url,p['id'])
                        digest=hashlib.sha256(response).hexdigest()
                        object_path=await asyncio.to_thread(self.store.put_object,response,'response')
                        p['storefront_evidence']={**evidence,'source_url':url,'sha256':digest,'object_path':object_path,'observed_at':now()}
                        if evidence['identity_confirmed']:p['diagnosed_at']=now()
                        else:p.pop('diagnosed_at',None)
                        p['stage']='diagnosed' if evidence['identity_confirmed'] else 'blocked'
                        p['reason']=('Needs supplier comparison and rendered calculator validation' if evidence['identity_confirmed'] else 'Storefront identity could not be confirmed')
                    except asyncio.CancelledError:
                        attempt.update(status='interrupted',finished_at=now())
                        raise
                    except Exception as exc:
                        p['stage']='blocked';p['reason']=str(exc)[:200]
                        if re.search(r'HTTP (?:401|403|429|5\d\d)',str(exc)) or 'challenge' in str(exc):
                            stop=p['reason']
                    attempt.update(status=p['stage'],reason=p.get('reason'),finished_at=now())
                    p['updated_at']=now();data['updated_at']=now();self._write(data)
                    if stop:data['stop_reason']=stop;break
            data['status']='needs_work'
        except asyncio.CancelledError:
            data['status']='interrupted'
            raise
        except Exception as exc:
            data['status']='needs_work';data['error']=type(exc).__name__
        finally:
            data['updated_at']=now();self._write(data)

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
