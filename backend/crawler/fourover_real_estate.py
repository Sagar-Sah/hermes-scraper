"""Exact Real Estate Post package evidence, including non-child custom options."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from datetime import datetime, timezone
from contextvars import ContextVar
import html
import asyncio
import time

SOURCE = "https://4over.com/real-estate-post"
PRODUCT = "6669"
QUOTE_HTTP_EVENTS = ContextVar('real_estate_quote_http_events', default=None)
QUOTE_DEADLINE = ContextVar('real_estate_quote_deadline', default=None)
QUOTE_PARTIAL_EVIDENCE = ContextVar('real_estate_partial_evidence', default=None)
PARENT = "83548"
SIZE_VALUES = {"18x24": "544", "24x30": "958"}
STOCK_VALUES = {"pvc_3mm": "372", "aluminum_sandwich_3mm": "1000"}
STOCK_FINISH_VALUES = {"pvc_3mm": {"drill": "230", "grommets": "1033"},
                       "aluminum_sandwich_3mm": {"drill": "1030", "grommets": "608"}}
CHILDREN = {("18x24", "pvc_3mm"): "83545", ("18x24", "aluminum_sandwich_3mm"): "83536",
            ("24x30", "pvc_3mm"): "83539", ("24x30", "aluminum_sandwich_3mm"): "83542"}
OPTION_NAMES = ["product_options[super_attribute][211]", "product_options[super_attribute][197]",
                "product_options[super_attribute][204]", "product_options[super_attribute][216]",
                "product_options[super_attribute][229]", "product_options[options][892]",
                "product_options[options][893]", "product_options[turnaround_time]"]


def selection(size, stock, rounded_corners, sets):
    if (size not in SIZE_VALUES or stock not in STOCK_VALUES or type(rounded_corners) is not bool
            or type(sets) is not int or not 1 <= sets <= 200):
        raise ValueError("invalid_real_estate_post_selection")
    return {"product_id": PRODUCT, "source_url": SOURCE, "size": size, "stock": stock,
            "child_id": CHILDREN[size, stock], "hardware": "white_6ft_gothic_post",
            "rounded_corners": rounded_corners, "sets": sets, "turnaround_time": "4",
            "job_sample": False, "digital_proof": False}


def project_context(parsed):
    """Whitelist only product IDs/options and counts of serialized job slots."""
    if not isinstance(parsed, dict):
        return None
    names = ["product_id", "run_size", "custom_run_size", *OPTION_NAMES]
    safe = {name: str(parsed[name]) for name in names if name in parsed and isinstance(parsed[name], (str, int))}
    jobs = {}
    for key in parsed:
        match = re.fullmatch(r"shipments\[(\d+)\]\[jobs\]\[(\d+)\]\[uuid\]", str(key))
        if match:
            jobs.setdefault(match[1], set()).add(int(match[2]))
    safe["shipment_job_counts"] = {key: len(values) for key, values in jobs.items()}
    safe["contiguous_job_slots"] = bool(jobs) and all(values == set(range(len(values))) for values in jobs.values())
    return safe


def requested_project_context(chosen):
    return {"product_id": PARENT, "run_size": "1", "custom_run_size": "0",
        "product_options[super_attribute][211]": SIZE_VALUES[chosen["size"]],
        "product_options[super_attribute][197]": STOCK_VALUES[chosen["stock"]],
        "product_options[super_attribute][204]": "97",
        "product_options[super_attribute][216]": STOCK_FINISH_VALUES[chosen["stock"]]["drill"],
        "product_options[super_attribute][229]": STOCK_FINISH_VALUES[chosen["stock"]]["grommets"], "product_options[options][892]": "1078",
        "product_options[options][893]": "1080" if chosen["rounded_corners"] else "1079",
        "product_options[turnaround_time]": "4", "shipment_job_counts": {"0": chosen["sets"]},
        "contiguous_job_slots": True}


def _money(value):
    try:
        result = Decimal(str(value).replace(",", ""))
    except (ValueError, InvalidOperation):
        raise ValueError("real_estate_supplier_money_invalid") from None
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
        raise ValueError("real_estate_supplier_money_invalid")
    return result


def _label(value):
    return re.sub(r'[^a-z0-9]','',str(value).lower())


def _option_value(value, hardware=False):
    text=html.unescape(re.sub(r'<[^>]*>',' ',str(value)))
    if hardware:
        text=re.sub(r'\s*\+\s*\$[0-9,]+(?:\.[0-9]{2})?','',text)
    text=text.replace('×','x').replace('”','"').replace('“','"').replace('’',"'")
    return ' '.join(text.lower().split())


def visible_totals(texts):
    # The native summary's LI owns the Total label. Its nested value span
    # includes only "$amount Taxes may apply", as the verified source template
    # shows. Both exact signatures identify grand totals, never a bare price.
    found=set()
    for text in texts:
        match=(re.search(r'\bTotal\s*:?\s*\$([0-9,]+\.\d{2})',text)
               or re.fullmatch(r'\$([0-9,]+\.\d{2})\s+Taxes may apply',text))
        if match:found.add(Decimal(match.group(1).replace(',','')))
    return found


TURNAROUND_READINESS_JS = r'''() => {
  const root=document.querySelector('.column.main')||document.querySelector('main')||document;
  const visible=e=>e.getClientRects().length>0;
  const allowed=new Set(['turnaround_time','run_size','custom_run_size','qty','quantity']);
  const controls=[...root.querySelectorAll('input,select')].filter(e=>allowed.has(e.name)).slice(0,40)
    .map(e=>{const label=e.id?root.querySelector(`label[for="${CSS.escape(e.id)}"]`):null;
      return {name:e.name,id:String(e.id||'').slice(0,100),type:String(e.type||'').slice(0,30),
        value:String(e.value||'').slice(0,40),checked:!!e.checked,enabled:!e.disabled,visible:visible(e),
        label:String(label?.textContent||e.closest('label')?.textContent||'').replace(/\s+/g,' ').trim().slice(0,160)};});
  const turnaround=controls.filter(e=>e.name==='turnaround_time');
  const target=turnaround.filter(e=>e.value==='4');
  const names=[...root.querySelectorAll('input[name="project_name"]')];
  const bind=names.length===1?String(names[0].getAttribute('data-bind')||''):'';
  return {schema:'fourover-turnaround-readiness/v1',controls,
    turnaround_count:turnaround.length,visible_turnaround_count:turnaround.filter(e=>e.visible).length,
    target_count:target.length,visible_target_count:target.filter(e=>e.visible).length,
    enabled_target_count:target.filter(e=>e.enabled).length,
    project_name_binding:{count:names.length,key_up_binding:/valueUpdate\s*:\s*['"]keyup['"]/.test(bind),
      text_input_binding:/\btextInput\s*:/.test(bind),empty:names.length===1?!names[0].value:null},
    omitted:['project_name_value','purchase_order','customer_details']};
}'''


async def capture_turnaround_readiness(page, phase):
    partial=QUOTE_PARTIAL_EVIDENCE.get()
    observed=await page.evaluate(TURNAROUND_READINESS_JS)
    if partial is not None:
        partial.setdefault('turnaround_readiness',{})[phase]=observed
    return observed


def readiness_http_status(path,status):
    """Categorical public pricing route only; no query, account or object IDs."""
    known={'/print/project/summary/':'project_summary','/print/shipment/shippingRates/':'shipping_rates'}
    normalized=path.rstrip('/')+'/'
    if '/product/prices/' in path:category='product_pricing';safe_path='/product/prices/[source-product]'
    elif '/product/turnaround' in path.lower():category='turnaround';safe_path='/product/turnaround/[arguments]'
    elif normalized in known:category=known[normalized];safe_path=normalized
    else:return None
    return {'category':category,'path':safe_path,'http_status':int(status)}


PRECONFIGURATION_READY_JS = r'''() => {
  const row=document.querySelectorAll('.column.main li.runsizes_row.custom_runsize_row');
  const controls=document.querySelectorAll('.column.main li.runsizes_row.custom_runsize_row input#qty-box-qty');
  const loader=typeof window.require==='function'?window.require:null;
  const paths=['FourOver_ProductFixedSize/js/view/product/qty-box',
    'FourOver_ProductFixedSize/js/view/register-price-processor',
    'FourOver_ProductFixedSize/js/model/price-processor','FourOver_Project/js/model/selected-runsize'];
  const modules=Object.fromEntries(paths.map(path=>[path,!!loader&&typeof loader.defined==='function'&&loader.defined(path)]));
  const pricingLoginRequired=[...document.querySelectorAll('.column.main .message,.column.main .messages,.column.main [class*="price"]')]
    .filter(e=>e.getClientRects().length>0).some(e=>/(?:log\s*in|sign\s*in).{0,30}(?:view|see|get|access).{0,20}(?:price|pricing)|(?:price|pricing).{0,30}(?:log\s*in|sign\s*in|authentication).{0,15}(?:required|first)/i.test(String(e.textContent||'')));
  return {schema:'fourover-fixed-size-preconfiguration/v1',pricing_login_required:pricingLoginRequired,owned_row_count:row.length,
    owned_qty_input_count:controls.length,modules,
    ready:row.length===1&&controls.length===1&&Object.values(modules).every(Boolean),
    note:'Attached native qty template plus executed fixed-price registration, before source choices change.'};
}'''


async def wait_for_preconfiguration_readiness(page,timeout=15.0):
    deadline=QUOTE_DEADLINE.get()
    if deadline is not None:timeout=min(timeout,deadline-time.monotonic()-2)
    partial=QUOTE_PARTIAL_EVIDENCE.get()
    if timeout<=0:raise ValueError('real_estate_component_readiness_budget_exhausted')
    try:
        async with asyncio.timeout(timeout):
            while True:
                observed=await page.evaluate(PRECONFIGURATION_READY_JS)
                if partial is not None:partial['preconfiguration_readiness']=observed
                if observed.get('pricing_login_required') is True:
                    raise ValueError('real_estate_explicit_pricing_login_required')
                if observed.get('ready') is True:return observed
                await asyncio.sleep(.1)
    except TimeoutError as error:
        raise ValueError('real_estate_pricing_component_not_ready_before_configuration') from error


QUANTITY_ONE_READINESS_JS = r'''() => {
  const rows=[...document.querySelectorAll('.column.main li.runsizes_row.custom_runsize_row')];
  const inputs=rows.flatMap(row=>[...row.querySelectorAll('input#qty-box-qty')]);
  const price=selector=>rows.length===1?[...rows[0].querySelectorAll(selector)]
    .map(e=>String(e.textContent||'').replace(/\s+/g,' ').trim().slice(0,100)):[];
  return {schema:'fourover-owned-quantity-one/v1',row_count:rows.length,input_count:inputs.length,
    control:inputs.length===1?{id:inputs[0].id,name:inputs[0].name,type:inputs[0].type,value:inputs[0].value,
      min:inputs[0].min,max:inputs[0].max,valid:inputs[0].checkValidity(),enabled:!inputs[0].disabled,
      visible:inputs[0].getClientRects().length>0}:null,
    unit_price_texts:price('.custom-run-size-unitprice'),total_price_texts:price('.custom-run-size-totalprice')};
}'''


async def commit_quantity_one_before_turnaround(page,timeout=15.0):
    """Initialize the native custom-run-size model with keyboard and blur."""
    from playwright.async_api import TimeoutError as SourceUITimeout
    deadline=QUOTE_DEADLINE.get()
    if deadline is not None:timeout=min(timeout,deadline-time.monotonic()-2)
    partial=QUOTE_PARTIAL_EVIDENCE.get()
    async def capture(phase):
        value=await page.evaluate(QUANTITY_ONE_READINESS_JS)
        if partial is not None:partial.setdefault('run_size_readiness',{})[phase]=value
        return value
    def prices(texts):
        amounts=set()
        for text in texts:
            for literal in re.findall(r'\$\s*([0-9,]+\.\d{2,4})(?!\d)',text):
                amount=Decimal(literal.replace(',',''))
                if amount.is_finite() and amount>=0:amounts.add(amount)
        return amounts
    await capture('before_commit')
    if timeout<=0:raise ValueError('real_estate_quantity_readiness_budget_exhausted')
    try:
        async with asyncio.timeout(timeout):
            control=page.locator('.column.main li.runsizes_row.custom_runsize_row input#qty-box-qty')
            await control.wait_for(state='visible',timeout=max(1,int(timeout*1000)))
            if await control.count()!=1 or not await control.is_enabled():
                raise ValueError('real_estate_owned_quantity_control_ambiguous')
            await enter_set_count(control,1)
            while True:
                observed=await capture('after_commit')
                source=observed.get('control') or {}
                if (observed.get('row_count')!=1 or observed.get('input_count')!=1 or source.get('value')!='1'
                        or source.get('type')!='number' or source.get('min')!='1'
                        or source.get('valid') is not True or source.get('enabled') is not True):
                    raise ValueError('real_estate_native_quantity_one_not_retained')
                unit=prices(observed['unit_price_texts']);total=prices(observed['total_price_texts'])
                if (len(unit)==1 and len(total)==1 and next(iter(unit))>0 and next(iter(total))>0
                        and abs(next(iter(unit))-next(iter(total)))<=Decimal('0.005')):
                    receipt={**observed,'status':'native_quantity_one_and_positive_row_prices',
                        'unit_price':f'{next(iter(unit)):.4f}','row_total':f'{next(iter(total)):.2f}',
                        'price_scope':'native_print_row_only_not_complete_hardware_package'}
                    if partial is not None:partial['run_size_readiness']['price_ready']=receipt
                    return receipt
                await asyncio.sleep(.1)
    except (TimeoutError,SourceUITimeout) as error:
        await capture('not_ready')
        raise ValueError('real_estate_native_quantity_row_price_not_ready') from error


async def select_turnaround_before_shipping(page, timeout=20.0):
    """Wait for the real child dependency, then select its visible native radio."""
    from playwright.async_api import TimeoutError as SourceUITimeout
    deadline=QUOTE_DEADLINE.get()
    if deadline is not None:timeout=min(timeout,deadline-time.monotonic()-2)
    if timeout<=0:raise ValueError('real_estate_turnaround_readiness_budget_exhausted')
    await capture_turnaround_readiness(page,'before_wait')
    try:
        async with asyncio.timeout(timeout):
            control=page.locator('.column.main input[name="turnaround_time"][value="4"]')
            while True:
                if await control.count()==1 and await control.is_enabled():
                    source_id=await control.get_attribute('id')
                    label=(page.locator('.column.main label[for='+json.dumps(source_id)+']')
                           if source_id else page.locator('.column.main label[data-hermes-no-match]'))
                    label_ready=(await label.count()==1 and await label.is_visible()
                                 and ' '.join((await label.inner_text()).split())=='4 Business Days')
                    if not label_ready:
                        await asyncio.sleep(.1)
                        continue
                    action='already_selected_read_only'
                    if not await control.is_checked():
                        await label.click(timeout=3000)
                        action='native_associated_label_clicked'
                    if await control.is_checked():
                        observed=await control.evaluate('''e => {
                          const root=e.closest('.column.main');
                          const label=e.id&&root?root.querySelector(`label[for="${CSS.escape(e.id)}"]`):null;
                          return {value:e.value,label:String(label?.textContent||e.closest('label')?.textContent||e.parentElement?.textContent||'')
                            .replace(/\\s+/g,' ').trim(),retained:!!e.checked&&!e.disabled,
                            observation_phase:'before_proceed_to_shipping'};
                        }''')
                        if observed.get('label')!='4 Business Days' or observed.get('retained') is not True:
                            raise ValueError('real_estate_turnaround_label_or_retention_changed')
                        observed['selection_action']=action
                        await capture_turnaround_readiness(page,'selected_before_shipping')
                        return observed
                await asyncio.sleep(.1)
    except (TimeoutError,SourceUITimeout) as error:
        await capture_turnaround_readiness(page,'not_ready')
        raise ValueError('real_estate_turnaround_not_ready_before_shipping') from error


async def enter_set_count(control, sets):
    """Enter Sets with ordinary keyboard events, including character keyup."""
    await control.press('ControlOrMeta+A')
    await control.press_sequentially(str(sets),delay=30)
    await control.press('Tab')


async def wait_for_rendered_quote(page, responses, chosen, timeout=None):
    """Wait for this tuple's native total, without relying on subtotal CSS."""
    expected=requested_project_context(chosen)
    deadline=QUOTE_DEADLINE.get()
    timeout=30.0 if timeout is None and deadline is not None else 4.0 if timeout is None else timeout
    if deadline is not None:
        timeout=min(timeout,deadline-time.monotonic()-2.0)
    if timeout<=0:
        return False
    try:
        async with asyncio.timeout(timeout):
            while True:
                texts=await page.evaluate('''() => [...document.querySelectorAll('span,strong,b,div,li.grand-total')]
                  .filter(e=>e.getClientRects().length>0)
                  .map(e=>String(e.textContent||'').replace(/\\s+/g,' ').trim())
                  .filter(t=>t.length<80 && /\\bTotal\\s*:?\\s*\\$[0-9,]+\\.[0-9]{2}/.test(t))''')
                bound=[record for record in responses if record.get('status')==200
                       and record.get('selection_context')==expected and isinstance(record.get('totals'),dict)]
                if bound and visible_totals(texts)=={_money(bound[-1]['totals'].get('grand_total'))}:
                    return True
                await asyncio.sleep(.1)
    except (TimeoutError, ValueError):
        return False


def summarize(pilot, chosen):
    if pilot.get('source_observation_error'):
        raise ValueError(pilot['source_observation_error'])
    if (pilot.get("product_id") != PRODUCT or pilot.get("selected_child_id") != chosen["child_id"]
            or pilot.get("sets") != chosen["sets"] or pilot.get("width") is not None or pilot.get("height") is not None):
        raise ValueError("real_estate_supplier_selection_mismatch")
    if pilot.get("selected_services") != {"job_sample": False, "digital_proof": False}:
        raise ValueError("real_estate_supplier_service_scope_changed")
    observed_services=pilot.get('service_controls') or {}
    for name in ('job_sample','digital_proof'):
        observed=observed_services.get(name) or {}
        if (type(observed.get('present')) is not bool or observed.get('ambiguous') is not False
                or observed.get('checked') is not False):
            raise ValueError('real_estate_final_service_state_unverified')
    control = pilot.get("set_control") or {}
    if (control.get("value") != str(chosen["sets"]) or control.get("min") != "1" or control.get("max") != "200"
            or control.get("valid") is not True):
        raise ValueError("real_estate_set_count_not_retained")
    expected = requested_project_context(chosen)
    fields = {field.get("name"): field for field in pilot.get("source_selection", [])}
    for name, value in expected.items():
        if not name.startswith("product_options[") or name == "product_options[turnaround_time]":
            continue
        source_name = name.replace("product_options[super_attribute]", "super_attribute", 1).replace("product_options[options]", "options", 1)
        field = fields.get(source_name) or {}
        if field.get("selected") != value or field.get("disabled") is not False:
            raise ValueError("real_estate_custom_or_child_option_not_retained")
    turnaround = pilot.get("turnaround_control") or {}
    if turnaround.get("value") != "4" or turnaround.get("retained") is not True or turnaround.get("label") != "4 Business Days":
        raise ValueError("real_estate_turnaround_not_retained")
    summaries = [record for record in pilot.get("project_response_shapes", [])
                 if record.get("status") == 200 and record.get("selection_context") == expected
                 and isinstance(record.get("totals"), dict)]
    if not summaries:
        raise ValueError("real_estate_summary_not_bound_to_requested_options_and_sets")
    totals = summaries[-1]["totals"]
    applied=summaries[-1].get('product_options')
    if not isinstance(applied,list):
        raise ValueError('real_estate_server_applied_options_missing')
    server={}
    for item in applied:
        label=_label(item.get('label',''))
        if label in server:
            raise ValueError('real_estate_server_applied_options_ambiguous')
        server[label]=item.get('value','')
    for field in fields.values():
        label=_label(field.get('label',''))
        selected_label=field.get('selected_label')
        inapplicable=(chosen['stock']=='pvc_3mm' and field.get('name')=='super_attribute[216]' and field.get('selected')=='230'
                      or chosen['stock']=='aluminum_sandwich_3mm' and field.get('name')=='super_attribute[229]' and field.get('selected')=='608')
        if label not in server and inapplicable and selected_label=='-':
            # The native summary omits only these two verified no-action
            # sentinels. Required hardware, finishes and corners remain strict.
            continue
        if not label or not selected_label or label not in server:
            raise ValueError('real_estate_server_applied_options_missing')
        if _option_value(server[label],label=='hardware')!=_option_value(selected_label,label=='hardware'):
            raise ValueError('real_estate_server_applied_options_disagree')
    turn_values=[value for label,value in server.items() if label in {'turnaround','turnaroundtime'}]
    if len(turn_values)!=1 or _option_value(turn_values[0])!='4 business days':
        raise ValueError('real_estate_server_applied_turnaround_disagrees')
    # A bound destination can add source tax before any shipping service is
    # selected. Keep goods separate, while reconciling every explicit native
    # component against the displayed grand total. Never subtract an inferred
    # residual or treat a carrier-rate choice as selected shipping.
    allowed_totals = {'subtotal', 'grand_total', 'small_qty_fee', 'turnaround_time',
                      'job_sample', 'digital_proof', 'shipping', 'local_delivery_fee', 'tax', 'discount'}
    if set(totals) - allowed_totals:
        raise ValueError("real_estate_unrecognized_total_component")
    components = {key: _money(totals.get(key, 0)) for key in allowed_totals}
    subtotal = components['subtotal']; grand = components['grand_total']
    fee = components['small_qty_fee']
    if components['job_sample'] or components['digital_proof'] or components['discount']:
        raise ValueError("real_estate_unrequested_service_or_discount")
    goods = subtotal + fee + components['turnaround_time']
    if (subtotal <= 0 or goods + components['shipping'] + components['local_delivery_fee'] + components['tax'] != grand):
        raise ValueError("real_estate_total_components_disagree")
    displayed = visible_totals(pilot.get("shipping_view", {}).get("money", []))
    if displayed != {grand}:
        raise ValueError("real_estate_displayed_total_disagrees")
    per_set = {Decimal(match.group(1).replace(",", "")) for text in pilot.get("shipping_view", {}).get("money", [])
               if (match := re.fullmatch(r"Price Per Set:\s*\$([0-9,]+\.\d{2})", text))}
    per_set_status = "missing_or_placeholder"
    if len(per_set) == 1 and next(iter(per_set)) > 0:
        amount = next(iter(per_set))
        if abs(amount * chosen["sets"] - subtotal) > Decimal("0.005") * chosen["sets"] + Decimal("0.01"):
            raise ValueError("real_estate_displayed_per_set_disagrees")
        per_set_status = "matched"
    production = sorted({rate["production_estimate"] for record in pilot.get("shipping_response_shapes", [])
                         if record.get('status')==200 and record.get('selection_context')==expected
                         for rate in record.get("rates", []) if rate.get("production_estimate")})
    unbound_production=any(rate.get('production_estimate') for record in pilot.get('shipping_response_shapes',[])
                           for rate in record.get('rates',[]))
    source_content=dict(pilot.get('source_content') or {})
    source_content.setdefault('text_capture_status','legacy_unfiltered_dom_text_not_approved_copy')
    source_content.setdefault('gallery_capture_status','dom_only_not_complete_gallery_evidence')
    result = {**chosen, "status": "exact_scoped_quote", "supplier_total_excluding_shipping": f"{goods:.2f}",
            "supplier_total_excluding_shipping_and_tax": f"{goods:.2f}",
            "print_and_hardware_subtotal": f"{subtotal:.2f}", "small_quantity_fee": f"{fee:.2f}",
            "turnaround_fee": f"{components['turnaround_time']:.2f}",
            "source_grand_total": f"{grand:.2f}", "source_shipping_and_processing": f"{components['shipping']:.2f}",
            "source_handling_fee": f"{components['local_delivery_fee']:.2f}", "source_tax": f"{components['tax']:.2f}",
            "source_total_reconciliation": "explicit_native_components_and_displayed_grand_total_match",
            "effective_total_per_set": f"{(goods / chosen['sets']):.2f}",
            "displayed_per_set_status": per_set_status, "source_selection": pilot["source_selection"],
            "source_content": source_content,
            "summary_product_options": summaries[-1].get("product_options", []),
            "turnaround": {"source_value": "4", "label": "4 Business Days", "retained": True},
            "set_control": {"min": 1, "max": 200, "retained": True},
            "production_estimates": production,
            "production_detail_status": ("observed" if production else "unknown_not_captured"
                if "shipping_response_shapes" not in pilot else "unknown_unbound"
                if unbound_production else "missing_at_supplier"),
            "shipping": "supplier_reference_only_not_a_price_completion_gate",
            "scope": "Only this rendered complete post, sign, corners, services, turnaround and set count; no other price is implied."}
    if pilot.get('shipping_control_observation') is not None:
        from .fourover_shipping_observation import current_shipping_rates
        result['shipping_control_observation']=pilot['shipping_control_observation']
        result['shipping_rates']=current_shipping_rates(pilot)
    return result


class RealEstateQuoteArchive:
    def __init__(self, root):
        self.root = Path(root) / "real-estate-post-exact-quotes"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, campaign_id, chosen):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", campaign_id) or chosen != selection(chosen["size"], chosen["stock"], chosen["rounded_corners"], chosen["sets"]):
            raise ValueError("invalid_real_estate_quote_archive_scope")
        digest = hashlib.sha256(json.dumps(chosen, sort_keys=True).encode()).hexdigest()
        folder = self.root / campaign_id; folder.mkdir(mode=0o700, exist_ok=True)
        return folder / (digest + ".json")

    def get(self, campaign_id, chosen):
        path = self.path(campaign_id, chosen)
        if not path.is_file() or path.is_symlink():
            return None
        record = json.loads(path.read_text())
        if record.get("selection") != chosen or record.get("quote", {}).get("status") != "exact_scoped_quote":
            raise ValueError("real_estate_quote_archive_changed")
        return record

    @staticmethod
    def evidence(pilot):
        projected={key:pilot.get(key) for key in ('product_id','selected_child_id','sets','set_control','width','height',
                'selected_services','service_controls','source_selection','turnaround_control','source_content','project_response_shapes','source_http_events','quote_readiness','source_observation_error','turnaround_readiness','run_size_readiness','preconfiguration_readiness','source_request_statuses')}
        if pilot.get('shipping_control_observation') is not None:
            projected['shipping_control_observation']=pilot['shipping_control_observation']
        if isinstance(pilot.get('shipping_rates'),list):
            from .fourover_shipping_observation import current_shipping_rates
            projected['shipping_rates']=current_shipping_rates(pilot)
        projected['shipping_view']={'money':pilot.get('shipping_view',{}).get('money',[])}
        if 'shipping_response_shapes' in pilot:
            projected['shipping_response_shapes']=[{
                'status':record.get('status'),
                'selection_context':record.get('selection_context'),
                'rates':[{'production_estimate':rate.get('production_estimate')}
                         for rate in record.get('rates',[]) if isinstance(rate,dict)]}
                for record in pilot.get('shipping_response_shapes',[]) if isinstance(record,dict)]
        return projected

    def save(self, campaign_id, chosen, quote, pilot=None, captured_at=None, promoted_from_conflict=None):
        if quote.get("status") != "exact_scoped_quote" or any(quote.get(key) != value for key, value in chosen.items()):
            raise ValueError("real_estate_quote_archive_selection_mismatch")
        path = self.path(campaign_id, chosen)
        if captured_at is not None and datetime.fromisoformat(captured_at).tzinfo is None:
            raise ValueError('source_capture_timestamp_must_have_timezone')
        record = {"selection": chosen, "quote": quote, "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
                  "use": "historical_evidence_only_require_fresh_for_current_price"}
        if promoted_from_conflict is not None:
            if not re.fullmatch(r'[a-f0-9]{32}',promoted_from_conflict):
                raise ValueError('invalid_conflict_evidence_id')
            record['promoted_from_conflict_id']=promoted_from_conflict
        if pilot is not None:
            record['evidence']=self.evidence(pilot)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x") as handle:
                os.chmod(temporary, 0o600); json.dump(record, handle, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return record

    def promote_conflict(self,campaign_id,evidence_id):
        """Revalidate a saved observation offline without changing its source time."""
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',campaign_id)
                or not re.fullmatch(r'[a-f0-9]{32}',evidence_id)):
            raise ValueError('invalid_conflict_evidence_id')
        path=self.root/campaign_id/'conflicts'/(evidence_id+'.json')
        if path.is_symlink():
            raise ValueError('invalid_conflict_evidence_path')
        record=json.loads(path.read_text())
        if record.get('id')!=evidence_id or record.get('status')!='unverified_source_observation_no_price_completion':
            raise ValueError('invalid_conflict_evidence_record')
        chosen=record['selection']
        self.path(campaign_id,chosen)
        evidence=record['evidence']
        recovery='native_ground_selected_summary_not_ready_before_deadline'
        if evidence.get('source_observation_error')==recovery and record.get('reason')==recovery:
            # One known parser defect omitted the LI's Total label after the
            # selected shipping row expanded the native summary. Recover only
            # from its saved exact summary, template-signature total, retained
            # destination and uniquely matching native Ground amount. Original
            # conflict remains immutable and no final DOM-selection claim is
            # invented for that legacy capture.
            evidence=json.loads(json.dumps(evidence))
            evidence.pop('source_observation_error')
            quote=summarize(evidence,chosen)
            rates=quote.get('shipping_rates') or []
            if len(rates)!=1 or rates[0].get('binding',{}).get('status')!='native_choices_and_destination_retained':
                raise ValueError('native_ground_offline_recovery_unbound')
            matches=[rate for rate in rates[0]['rates'] if rate.get('available') is True
                     and rate.get('method_title')=='Ground Shipping'
                     and _money(rate.get('amount'))==_money(quote['source_shipping_and_processing'])]
            if len(matches)!=1 or _money(quote['source_shipping_and_processing'])<=0:
                raise ValueError('native_ground_offline_recovery_ambiguous')
            observation=evidence['shipping_control_observation']
            observation['selected_ground_service']={
                'status':'recovered_from_exact_summary_and_unique_native_ground_rate',
                'label':'Ground Shipping','amount':quote['source_shipping_and_processing'],
                'postcode':rates[0]['binding']['postcode'],'state':rates[0]['binding']['state'],
                'final_dom_selection_captured':False,'carrier_identity':'source_fields_only'}
            evidence['quote_readiness']={'status':'revalidated_offline_from_saved_native_total',
                                         'original_observation_error':recovery}
        quote=summarize(evidence,chosen)
        return self.save(campaign_id,chosen,quote,evidence,captured_at=record['captured_at'],
                         promoted_from_conflict=evidence_id)

    def save_conflict(self,campaign_id,chosen,pilot,reason):
        folder=self.path(campaign_id,chosen).parent/'conflicts';folder.mkdir(mode=0o700,exist_ok=True)
        evidence_id=uuid.uuid4().hex;path=folder/(evidence_id+'.json')
        record={'id':evidence_id,'selection':chosen,'reason':str(reason)[:200],
                'captured_at':datetime.now(timezone.utc).isoformat(),'evidence':self.evidence(pilot),
                'status':'unverified_source_observation_no_price_completion'}
        with path.open('x') as handle:
            os.chmod(path,0o600);json.dump(record,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
        return evidence_id
