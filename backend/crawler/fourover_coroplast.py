"""Bounded native observations for the confirmed 4mm Coroplast product.

The initial adapter deliberately separates option discovery from pricing. A
configured page proves only its observed branch; it does not prove every
dimension/finish combination or turn area-tier anchors into supplier quotes.
The caller owns the diagnosis lock, retained page and shared origin limiter.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

SOURCE = "https://4over.com/4coro-coroplast-signs"
PRODUCT = "6383"
SCHEMA = "hermes-fourover-coroplast-native-observation/v1"
_NAME = re.compile(r"(?:dynamic_size\[(?:width|height)\]|super_attribute\[[0-9]{1,8}\]|options\[[0-9]{1,8}\])\Z")


def forbidden_write(method, url):
    """Deny transactional/address endpoints even if a native change emits one."""
    if str(method).upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return False
    path = urlsplit(url).path.lower().rstrip('/')
    return (path.endswith('/project/mine/address') or any(part in path+'/' for part in
            ('/customer/address/', '/checkout/', '/sales/order/', '/carts/', '/guest-carts/')))

# Product controls only. Text inputs, customer/project labels, address books,
# cookies and hidden application state are intentionally excluded.
OBSERVE_JS = r"""() => {
 const clean=(v,n=160)=>String(v||'').replace(/\s+/g,' ').trim().slice(0,n);
 const visible=e=>!!e&&!e.closest('[hidden],[aria-hidden="true"]')&&!!e.getClientRects().length
   &&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
 const roots=[...document.querySelectorAll('.column.main')];
 if(roots.length!==1)return {status:'unavailable',reason:'product_owner_ambiguous'};
 const root=roots[0];
 const label=e=>clean([...(e.labels||[])].map(n=>n.textContent).join(' '))||clean(e.closest('.field')?.querySelector('label')?.textContent);
 const recognized=n=>/^(?:dynamic_size\[(?:width|height)\]|super_attribute\[\d{1,8}\]|options\[\d{1,8}\])$/.test(n||'');
 const selects=[...root.querySelectorAll('select')].filter(e=>recognized(e.name)&&visible(e)).map(e=>({
   name:e.name,id:clean(e.id),label:label(e),selected:e.value,disabled:e.matches(':disabled'),required:e.required,
   valid:e.checkValidity(),choices:[...e.options].slice(0,501).map(o=>({value:o.value,label:clean(o.textContent),disabled:o.disabled}))}));
 const numbers=[...root.querySelectorAll('input[type="number"]')].filter(e=>visible(e)
   &&(/^(?:run_size|qty|custom_run_size|sets|number_of_sets)$/.test(e.name||'')||/^qty-box-qty(?:-\d+)?$/.test(e.id||'')))
   .map(e=>({name:clean(e.name),id:clean(e.id),label:label(e),value:e.value,min:e.getAttribute('min'),max:e.getAttribute('max'),
     step:e.getAttribute('step'),disabled:e.matches(':disabled'),valid:e.checkValidity()}));
 const quantities=[...root.querySelectorAll('li.runsizes_row')].slice(0,1001).map(e=>({
   custom:e.classList.contains('custom_runsize_row'),visible:visible(e),selected:e.classList.contains('selected'),
   quantity:clean(e.querySelector('.runsize')?.textContent),
   unit_price:clean(e.querySelector('.custom-run-size-unitprice')?.textContent),
   total_price:clean(e.querySelector('.custom-run-size-totalprice')?.textContent)}));
 const expansion=[...root.querySelectorAll('a,button')].filter(e=>visible(e)&&/^(?:see|show|view)\s+(?:more|all)(?:\s+quantities)?$/i.test(clean(e.textContent)))
   .map(e=>({tag:e.tagName.toLowerCase(),id:clean(e.id),label:clean(e.textContent)}));
 const production=[...root.querySelectorAll('input[name="turnaround_time"]')].map(e=>({value:e.value,
   label:label(e),checked:e.checked,disabled:e.matches(':disabled'),visible:visible(e),label_visible:[...(e.labels||[])].some(visible)}));
 const services=['job_sample','digital_proof'].map(name=>{const es=[...root.querySelectorAll('input')].filter(e=>e.name===name);
   return {name,count:es.length,checked:es.length===1?es[0].checked:null};});
 const assets=[...document.querySelectorAll('script[src]')].map(e=>{try{const u=new URL(e.src);return u.origin==='https://4over.com'
   &&u.pathname.startsWith('/static/')&&!u.search&&!u.hash?u.href:null}catch{return null}}).filter(Boolean).slice(0,100);
 return {status:'observed',source_url:location.href,selects,numbers,quantity_rows:quantities,quantity_expansion_controls:expansion,
   production,services,public_script_urls:[...new Set(assets)],
   truncation:{selects:selects.length>100,choices:selects.some(s=>s.choices.length>500),quantity_rows:quantities.length>1000}};
}"""


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def selection_from_diagnosis(diagnosis, selections):
    """Accept native selects observed in this exact diagnosis; no caller CSS."""
    if (not isinstance(diagnosis, dict) or diagnosis.get("url") != SOURCE
            or diagnosis.get("supplier_id") != "fourover" or diagnosis.get("status") != "ready"):
        raise ValueError("coroplast_diagnosis_scope_invalid")
    if not isinstance(selections, dict) or not 1 <= len(selections) <= 12:
        raise ValueError("coroplast_selection_invalid")
    result = []
    for name, value in selections.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(value, str) or not value:
            raise ValueError("coroplast_selection_invalid")
        matches = [field for field in diagnosis.get("fields", []) if field.get("source_name") == name]
        if len(matches) != 1 or matches[0].get("kind") != "select" or matches[0].get("disabled") is not False:
            raise ValueError("coroplast_control_not_observed")
        field = matches[0]
        choices = [choice for choice in field.get("choices", []) if choice.get("value") == value and choice.get("disabled") is False]
        if len(choices) != 1:
            raise ValueError("coroplast_choice_not_observed")
        result.append({"name": name, "value": value, "source_id": field.get("source_id", ""), "label": field.get("label", "")})
    return result


def retained_selection(observation, selections):
    if observation.get("status") != "observed" or observation.get("source_url") != SOURCE:
        raise ValueError("coroplast_source_changed")
    if any(observation.get("truncation", {}).values()):
        raise ValueError("coroplast_controls_truncated")
    for chosen in selections:
        fields = [field for field in observation.get("selects", []) if field.get("name") == chosen["name"]]
        if len(fields) != 1:
            raise ValueError("coroplast_control_ambiguous")
        field = fields[0]
        if (field.get("selected") != chosen["value"] or field.get("disabled") is not False
                or field.get("valid") is not True
                or chosen.get("source_id") and field.get("id") != chosen["source_id"]):
            raise ValueError("coroplast_selection_not_retained")
        matches = [choice for choice in field.get("choices", []) if choice.get("value") == chosen["value"] and choice.get("disabled") is False]
        if len(matches) != 1:
            raise ValueError("coroplast_choice_changed")
    return observation


def observation_receipt(observation, selections, *, diagnosis_id, revision):
    retained_selection(observation, selections)
    return {"schema": SCHEMA, "source_url": SOURCE, "product_id": PRODUCT,
            "diagnosis_id": diagnosis_id, "diagnosis_revision": revision,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "status": "observed_current_branch_only", "requested_selection": selections,
            "observation": observation, "observation_sha256": hashlib.sha256(_encoded(observation)).hexdigest(),
            "coverage": {"options": "current_branch_only", "quantity_expansion": "unknown",
                         "custom_quantity_domain": "observed_controls_only", "supplier_prices": "unknown",
                         "production": "current_branch_only", "other_branches": "unknown"},
            "source_data_finalized": False, "can_trigger_auto_correction": False}


async def configure_and_observe(page, diagnosis, selections, check_scope, *, timeout=18):
    """Normal native select changes, bounded discovery, no quote/proceed click.

    Must run under the caller's diagnosis lock and shared supplier limiter.
    Each newly exposed branch needs a refreshed diagnosis before it can be
    selected; absent choices are never synthesized from a Cartesian product.
    """
    chosen = selection_from_diagnosis(diagnosis, selections)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 25:
        raise ValueError("coroplast_observation_budget_invalid")
    deadline = time.monotonic() + timeout
    last_action = None
    for index, item in enumerate(chosen):
        await check_scope()
        if page.url != SOURCE:
            raise ValueError("coroplast_source_changed")
        control = page.locator('.column.main select[name="' + item["name"] + '"]')
        if await control.count() != 1 or not await control.is_visible() or not await control.is_enabled():
            raise ValueError("coroplast_control_unavailable")
        # select_option dispatches the browser's ordinary input/change events.
        # Read current DOM again before acting: a prior choice may reset or
        # disable a dependent field even though the diagnosis offered it.
        current = await page.evaluate(OBSERVE_JS)
        field = next((field for field in current.get("selects", []) if field["name"] == item["name"]), None)
        if field is None or not any(choice["value"] == item["value"] and not choice["disabled"] for choice in field["choices"]):
            raise ValueError("coroplast_choice_changed")
        if field["selected"] != item["value"]:
            # The shared permit owns the entire observation. Space its native
            # choice actions as well, rather than sending a burst of source
            # pricing requests beneath one operation-level limiter permit.
            while last_action is not None and time.monotonic()-last_action < 1:
                await check_scope()
                if time.monotonic() >= deadline:
                    raise ValueError("coroplast_observation_timeout")
                await asyncio.sleep(min(.1, 1-(time.monotonic()-last_action)))
            await control.select_option(item["value"], timeout=max(1, int((deadline-time.monotonic())*1000)))
            last_action = time.monotonic()
        # Two stable DOM observations permit conditional controls to mount;
        # they certify no completion, network price or unseen future branch.
        last = None
        stable = 0
        while time.monotonic() < deadline:
            await check_scope()
            observed = await page.evaluate(OBSERVE_JS)
            retained_selection(observed, chosen[:index+1])
            fingerprint = hashlib.sha256(_encoded(observed)).hexdigest()
            stable = stable+1 if fingerprint == last else 0
            if stable >= 2:
                break
            last = fingerprint
            await asyncio.sleep(.15)
        else:
            raise ValueError("coroplast_observation_timeout")
    observed = await page.evaluate(OBSERVE_JS)
    return observation_receipt(observed, chosen, diagnosis_id=diagnosis["id"], revision=diagnosis["revision"])
