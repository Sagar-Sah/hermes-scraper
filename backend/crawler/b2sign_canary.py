"""Plan independent UI canaries without reusing quote-fetch results as proof.

The pure planner performs no browser/source requests. It retains every finite option
value requirement, boundary interactions, explicit quantities and production
modes. Missing scopes remain uncovered; a small boundary sample is never called
full coverage. Saved quotes are comparison references, not passing canaries.
"""

from __future__ import annotations

import copy
import itertools
import json
from decimal import Decimal, InvalidOperation

from .b2sign_catalog import CatalogError, enumerate_catalog, _digest
from .b2sign_harvest import source_without_production_field, quote_only_candidates


def _key(values, quantity, turnaround, catalog):
    return _digest({"url": catalog["url"], "source_product_id": catalog["source_product_id"],
                    "contract_sha256": catalog["contract_sha256"], "values": values,
                    "quantity": quantity, "turnaround": turnaround,
                    "email_proof": False, "shipping": ""})


def _value_token(field, value):
    return "value:" + _digest([field, value])


def _amount(value):
    try:
        if isinstance(value, bool) or value is None:
            return None
        amount = Decimal(str(value))
        return format(amount, "f") if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _references(catalog, results):
    if isinstance(results, dict):
        results = results.get("results", [])
    grouped, rejected = {}, 0
    for result in results:
        candidate = result.get("candidate") or {}
        request = result.get("request") or {}
        job = request.get("job") or {}
        subjobs = job.get("subjobs") or []
        expected_values = dict(candidate.get("values") or {}, _qty=str(candidate.get("quantity")))
        if (result.get("status") != "quoted" or candidate.get("source_url") != catalog["url"]
                or candidate.get("source_product_id") != catalog["source_product_id"]
                or candidate.get("contract_sha256") != catalog["contract_sha256"]
                or candidate.get("email_proof") is not False or candidate.get("shipping") != ""
                or not isinstance(candidate.get("values"), dict) or type(candidate.get("quantity")) is not int
                or not (isinstance(candidate.get("turnaround"), str)
                        or candidate.get("turnaround") is None and source_without_production_field(catalog))
                or request.get("configuration_bound") is not True or request.get("id") != catalog["contract"]["item_id"]
                or request.get("values") != expected_values or job.get("turnaround") != candidate["turnaround"]
                or job.get("shipping", "") != "" or len(subjobs) != 1
                or not isinstance(subjobs[0], dict) or str(subjobs[0].get("quantity")) != str(candidate["quantity"])
                or (result.get("quote") or {}).get("quantity") != candidate["quantity"]):
            rejected += 1
            continue
        subtotal = _amount((result.get("quote") or {}).get("subtotal"))
        if subtotal is None:
            rejected += 1
            continue
        key = _key(candidate["values"], candidate["quantity"], candidate["turnaround"], catalog)
        grouped.setdefault(key, []).append({"candidate_id": candidate.get("candidate_id") or result.get("candidate_id"),
                                            "context_id": result.get("context_id"), "attempt_id": result.get("attempt_id"),
                                            "subtotal": subtotal, "captured_at": result.get("captured_at")})
    return grouped, rejected


def prepare_canaries(catalog, harvest_results, *, quantities, context=None,
                     explicit_sizes=None, scope_candidates=None, max_canaries=None, max_candidate_pool=100000):
    """Return a deterministic greedy full-value coverage plan, never test results.

    ``quantities`` must be an explicit approved scope. Pole's219 preset sizes
    therefore impose a219-case lower bound before interactions or missing
    production modes are considered. An optional budget is a limit, not proof
    that fewer cases suffice. No reference price is copied across configurations.
    """
    if max_canaries is not None and (type(max_canaries) is not int or max_canaries <= 0):
        raise CatalogError("positive_canary_budget_required")
    if type(max_candidate_pool) is not int or max_candidate_pool <= 0:
        raise CatalogError("positive_candidate_pool_limit_required")
    references, rejected_references = _references(catalog, harvest_results)
    allowed_keys = None if scope_candidates is None else {
        _key(row['values'], row['quantity'], row['turnaround'], catalog)
        for row in scope_candidates}
    iterator = enumerate_catalog(catalog, quantities=quantities, context=context,
                                 explicit_sizes=explicit_sizes, max_candidates=max_candidate_pool)
    candidates = [row for row in iterator
                  if allowed_keys is None or _key(row['values'], row['quantity'], row['turnaround'], catalog) in allowed_keys]
    enumeration = iterator.summary()
    if not candidates and source_without_production_field(catalog):
        candidates = [dict(row, visible={}) for row in quote_only_candidates(catalog, quantities)
                      if allowed_keys is None or _key(row['values'], row['quantity'], row['turnaround'], catalog) in allowed_keys]
        enumeration = {**enumeration, 'quote_only_count': len(candidates),
                       'production_status': 'not_exposed_no_lead_time_claim'}
    fields = {field["key"]: field for field in catalog["dimensions"]}
    requirements, dimensions = {}, {}

    def require(token, **description):
        requirements.setdefault(token, {"id": token, **description})

    def field_label(field):
        return field.get("label") or " / ".join(dict.fromkeys(choice["label"] for choice in field.get("choices", []) if choice.get("label") and choice.get("visible") is not False)) or field["key"]

    # Every nominal source option that is not explicitly hidden/disabled is
    # required. When source constraints leave no admissible case, that fact is
    # reported explicitly instead of disappearing from the coverage claim.
    for field in catalog["dimensions"]:
        if field.get("visible") is False or field.get("disabled") is True or not field.get("request_value"):
            continue
        values = []
        if field["domain_kind"] == "finite_options":
            values = [(choice["value"], choice["label"]) for choice in field["choices"]
                      if choice.get("visible") is not False and choice.get("disabled") is not True]
        elif field["domain_kind"] == "finite_size_presets":
            axes = sorted(field.get("axes", []), key=lambda axis: axis["key"] == "height")
            for pieces in itertools.product(*(axis["values"] for axis in axes)):
                raw = "x".join(map(str, pieces))
                values.append((raw, ' x '.join(str(value) + '"' for value in pieces)))
        elif field['domain_kind'] == 'open_or_dynamic_size' and explicit_sizes and field['key'] in explicit_sizes:
            values = [(size, size.replace('x', ' x ') + '"') for size in explicit_sizes[field['key']]]
        unique = {}
        for value, label in values:
            unique.setdefault(_digest(value), (value, label))
        dimensions[field["key"]] = len(unique)
        for value, label in unique.values():
            require(_value_token(field["key"], value), kind="option_value", field=field["key"],
                    field_label=field_label(field), value=value, label=label)

    for quantity in quantities:
        require("quantity:" + str(quantity), kind="quantity", quantity=quantity)

    # Mode IDs are source-owned. Clock-gated rush modes and modes outside the
    # requested quantity scope remain uncovered until a live eligibility frame
    # or an explicitly expanded quantity scope supplies an admissible case.
    for rule in catalog.get("production_rules") or []:
        for option in rule.get("options", []):
            if option.get("visible") is False or option.get("disabled") is True:
                continue
            if isinstance(option.get("key"), str):
                require("production:" + option["key"], kind="production", value=option["key"], label=option.get("label") or option["key"])

    # Multi-value non-size choices are interaction branches (Pole package and
    # windslit, other products' material/finishing choices). Fixed static fields
    # still get value coverage but do not multiply these checks.
    branches = [field["key"] for field in catalog["dimensions"]
                if field["domain_kind"] == "finite_options" and dimensions.get(field["key"], 0) > 1]
    size_boundaries = {}
    for field in catalog["dimensions"]:
        if field["domain_kind"] != "finite_size_presets":
            continue
        axes = sorted(field.get("axes", []), key=lambda axis: axis["key"] == "height")
        if len(axes) == 2:
            # Each offered width at the lowest/highest offered height. Full
            # value requirements above still retain every interior size.
            heights = axes[1]["values"]
            size_boundaries[field["key"]] = {str(width) + "x" + str(height)
                for width in axes[0]["values"] for height in (heights[0], heights[-1])}
        elif len(axes) == 1:
            values = axes[0]["values"]
            size_boundaries[field["key"]] = {str(values[0]), str(values[-1])}

    pool = {}
    for candidate in candidates:
        values, visible = candidate["values"], candidate["visible"]
        key = _key(values, candidate["quantity"], candidate["turnaround"], catalog)
        if key in pool:
            continue
        covered = {"quantity:" + str(candidate["quantity"])}
        if candidate['turnaround'] is not None:
            covered.add("production:" + candidate["turnaround"])
        for field, value in values.items():
            if visible.get(field) is True:
                covered.add(_value_token(field, value))
        active_branches = [(field, values[field]) for field in branches if visible.get(field) is True and field in values]
        for left, right in itertools.combinations(active_branches, 2):
            token = "pair:" + _digest([left, right])
            require(token, kind="observed_valid_option_pair", values=dict([left, right]))
            covered.add(token)
        # Exercise quantities in every known visible package/finishing branch.
        if active_branches:
            token = "quantity_branch:" + _digest([active_branches, candidate["quantity"]])
            require(token, kind="quantity_in_option_branch", values=dict(active_branches), quantity=candidate["quantity"])
            covered.add(token)
        for field, boundary_values in size_boundaries.items():
            if visible.get(field) is True and values.get(field) in boundary_values:
                token = "size_boundary:" + _digest([field, values[field], active_branches])
                require(token, kind="size_boundary_in_option_branch", field=field, value=values[field], values=dict(active_branches))
                covered.add(token)
        refs = references.get(key, [])
        amounts = {Decimal(ref["subtotal"]) for ref in refs}
        reference_status = "conflicting_saved_quotes" if len(amounts) > 1 else "matching_saved_quote" if refs else "no_matching_saved_quote"
        pool[key] = {"id": key, "candidate": copy.deepcopy(candidate), "covers": covered,
                     "reference_status": reference_status, "reference_quotes": refs,
                     "expected_subtotal": format(next(iter(amounts)), "f") if len(amounts) == 1 else None}

    remaining = set(requirements)
    selected = []
    # Explicit deterministic tie-breaking prefers an exact saved reference,
    # then a lower quantity, but never sacrifices additional coverage for it.
    while remaining and pool and (max_canaries is None or len(selected) < max_canaries):
        best_key = max(pool, key=lambda key: (len(pool[key]["covers"] & remaining),
            pool[key]["reference_status"] == "matching_saved_quote", -pool[key]["candidate"]["quantity"], key))
        best = pool.pop(best_key)
        newly_covered = best["covers"] & remaining
        if not newly_covered:
            break
        remaining -= newly_covered
        best["covers"] = sorted(best["covers"] & set(requirements))
        best["new_requirements_covered"] = sorted(newly_covered)
        best.update(status="planned", verification_status="not_run", independent_method="native_UI_change_fresh_quote_and_settled_DOM",
                    price_passed=False, final_eligible=False)
        selected.append(best)

    possible = set().union(*(case["covers"] for case in pool.values())) if pool else set()
    uncovered = []
    for key in sorted(remaining):
        uncovered.append({**requirements[key], "reason": "canary_budget_exhausted" if key in possible else "no_admissible_case_in_explicit_scope"})
    value_requirements = {key for key, requirement in requirements.items() if requirement["kind"] == "option_value"}
    lower_bound = max(dimensions.values(), default=0)
    return {"schema": 1, "source_url": catalog["url"], "title": catalog["title"],
            "source_product_id": catalog["source_product_id"], "contract_sha256": catalog["contract_sha256"],
            "coverage_policy": "all_finite_option_values_plus_boundaries", "explicit_quantities": list(quantities),
            "candidate_pool_count": len(candidates), "selected_count": len(selected), "minimum_value_coverage_cases": lower_bound,
            "dimension_value_counts": dimensions, "required_count": len(requirements),
            "covered_by_plan_count": len(requirements) - len(remaining), "option_values_covered_by_plan": not bool(value_requirements & remaining),
            "plan_scope_complete": not remaining and not enumeration["gaps"], "requirements": list(requirements.values()),
            "uncovered_requirements": uncovered, "enumeration": enumeration, "cases": selected,
            "rejected_reference_count": rejected_references,
            "warnings": ["Every planned case requires fresh native UI evidence; saved quote-fetch rows do not count as passing canaries.",
                         f"Full option-value coverage requires at least {lower_bound} cases in this source domain.",
                         "Quantities outside the explicit scope and unavailable runtime production modes remain unresolved."],
            "canaries_passed": 0, "prices_verified": False, "catalog_complete": False, "final_eligible": False}

# The runner deliberately uses native UI events and observes the site's own
# requests. It never invokes the quote endpoint through a synthetic fetch.
import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .b2sign_catalog import PINNED_BUNDLE_SHA256, parse_public_product, evaluate_condition, derived_request_values
from .b2sign_harvest import (B2SignHarvestManager, HarvestError, SharedQuoteGate,
                             display_only_contract_change,
                             SERVICE_STATE_JS, prepare_candidate,
                             validate_observed_request, _now, _hash)
from .b2sign_pilot import (PUBLIC_ITEM_INIT_JS, PUBLIC_ITEM_JS, PUBLIC_ITEM_LATCH_JS,
                           QUOTE_DOM_JS, PilotError, sanitize_quote, quote_schema,
                           safe_dom_diagnostics)
from .b2sign_diagnosis import inspect_b2sign
from .transport import validate_url


FIELD_UI_JS = r'''({key,label})=>{
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
 const clean=s=>(s||'').replace(/\s+/g,' ').trim();
 const selectedLabel=button=>clean(button.querySelector('span[class*="_option-label_"]')?.textContent||button.innerText);
 const root=[...document.querySelectorAll('div[class*="_instant-quote_"]')].find(visible);
 if(!root)return null;
 const size=root.querySelector('[id="attr-'+key+'"][class*="_size-wrapper_"]');
 if(size&&visible(size)){
  const axes=[...size.querySelectorAll('div[class*="_input-wrapper_"]')].filter(visible);
  const labels=axes.map(a=>clean(a.querySelector('.MuiInput-startDecorator')?.textContent));
  const inputs=axes.map(a=>a.querySelector('input'));
  if(axes.length===2&&labels.join('|')==='Width|Height'&&inputs.every(e=>e&&visible(e))){
   size.setAttribute('data-hermes-canary-field',key);
   return {kind:'size',value:inputs.map(e=>e.value).join('x'),disabled:inputs.some(e=>e.disabled)};
  }
  const dimensionRows=['Width','Height'].map(name=>[...size.querySelectorAll('div[class*="_attribute_"]')]
   .find(e=>clean(e.querySelector(':scope > label')?.textContent)===name));
  if(dimensionRows.every(Boolean)&&dimensionRows.every(row=>{
   const parts=[...row.querySelectorAll('div[class*="_input-wrapper_"]')].filter(visible);
   return parts.length===2&&parts[0].className.includes('_feet_')&&parts[1].className.includes('_inches_')&&
    parts.every(part=>part.querySelector('input')&&visible(part.querySelector('input')));
  })){
   size.setAttribute('data-hermes-canary-field',key);
   const numbers=dimensionRows.map(row=>[...row.querySelectorAll('div[class*="_input-wrapper_"]')].map(part=>part.querySelector('input')));
   return {kind:'size_feet_inches',value:numbers.map(parts=>String(Number(parts[0].value)*12+Number(parts[1].value))).join('x'),
    disabled:numbers.flat().some(e=>e.disabled)};
  }
  const selects=[...size.querySelectorAll('button[role=combobox],button.MuiSelect-button')].filter(visible);
  if(selects.length===1){
   const wrap=selects[0].closest('div[class*="_attribute_"]');
   if(!wrap)return null;
   wrap.setAttribute('data-hermes-canary-field',key);
   return {kind:'custom',label:selectedLabel(selects[0]),disabled:selects[0].disabled,
    controls:selects[0].getAttribute('aria-controls')};
  }
 }
 let matches=[...root.querySelectorAll('div[class*="_attribute_"]')].filter(e=>e.id==='attr-'+key&&visible(e));
 if(!matches.length)matches=[...root.querySelectorAll('div[class*="_attribute_"]')].filter(e=>visible(e)&&clean(e.querySelector(':scope > label')?.innerText)===clean(label));
 if(matches.length!==1)return null;const wrap=matches[0];wrap.setAttribute('data-hermes-canary-field',key);
 if(clean(label)==='Size'){
  const axes=[...wrap.querySelectorAll('div[class*="_input-wrapper_"]')].filter(visible);
  const labels=axes.map(a=>clean(a.querySelector('.MuiInput-startDecorator')?.textContent));
  const inputs=axes.map(a=>a.querySelector('input'));
  if(axes.length===2&&labels.join('|')==='Width|Height'&&inputs.every(e=>e&&visible(e)))
   return {kind:'size',value:inputs.map(e=>e.value).join('x'),disabled:inputs.some(e=>e.disabled)};
  const buttons=[...wrap.querySelectorAll('button[role=combobox]')].filter(visible);
  const selectLabels=buttons.map(button=>clean(button.parentElement?.querySelector('.MuiSelect-startDecorator')?.textContent));
  const selected=buttons.map(button=>button.parentElement?.querySelector('input[aria-hidden="true"]'));
  if(buttons.length===2&&selectLabels.every((name,index)=>name.endsWith(index?'Height':'Width'))&&selected.every(Boolean))
   return {kind:'size_selects',value:selected.map(input=>input.value).join('x'),disabled:buttons.some(button=>button.disabled)};
 }
 const buttons=[...wrap.querySelectorAll('button[role=combobox],button.MuiSelect-button')].filter(visible);
 const natives=[...wrap.querySelectorAll('select')].filter(visible);
 const items=[...wrap.querySelectorAll('div[class*="_component_"][class*="_button_"] > div[class*="_item_"]')].filter(visible);
 if(buttons.length===1)return {kind:'custom',label:selectedLabel(buttons[0]),disabled:buttons[0].disabled,controls:buttons[0].getAttribute('aria-controls')};
 if(natives.length===1)return {kind:'native',value:natives[0].value,disabled:natives[0].disabled};
 if(items.length)return {kind:'buttons',labels:items.map(e=>clean(e.innerText)),label:items.filter(e=>e.className.includes('_active_')).map(e=>clean(e.innerText)).join(''),disabled:false};
 return {kind:'static',text:clean(wrap.innerText),disabled:false};
}'''


def native_request_projection(payload, catalog):
    """Safe immutable request evidence, permitting ordinary intermediate UI states.

    Native UI requests need not carry the URL-initializer-only `_qty` value.
    If it is present it must agree with the actual job quantity. This difference
    is recorded, never manufactured in persisted source evidence.
    """
    try:
        jobs = json.loads(payload['jobs']) if isinstance(payload.get('jobs'), str) else payload.get('jobs')
        if not isinstance(jobs, list) or len(jobs) != 1:
            raise ValueError()
        subs = jobs[0].get('subjobs')
        if not isinstance(subs, list) or len(subs) != 1:
            raise ValueError()
        raw_qty = subs[0].get('quantity')
        if isinstance(raw_qty, bool) or not re.fullmatch(r'[1-9]\d{0,3}', str(raw_qty)):
            raise ValueError()
        quantity = int(raw_qty)
        values = payload.get('values')
        if not isinstance(values, dict):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HarvestError('native_quote_shape_invalid') from None
    attrs = {field['key']: field for field in catalog['contract']['attributes'] if field.get('component') not in {'divider', 'price_break'}}
    derived = {binding['key']: binding for binding in catalog['contract'].get('derived_request_fields', [])}
    if set(values) - (set(attrs) | set(derived) | {'_qty'}):
        raise HarvestError('native_quote_unknown_attribute')
    if '_qty' in values and str(values['_qty']) != str(quantity):
        raise HarvestError('initializer_quantity_shadows_live_quantity')
    for key, value in values.items():
        if key == '_qty':
            continue
        if type(value) not in (str, int, float) or len(str(value)) > 200:
            raise HarvestError('native_quote_attribute_invalid')
        if key in derived:
            allowed = {action['change'][0]['value'] for action in derived[key]['actions']}
            if value not in allowed:
                raise HarvestError('native_quote_derived_invalid')
            continue
        field = attrs[key]
        if field.get('component') == 'size':
            axes = sorted(field.get('options', []), key=lambda option: option.get('key') == 'height')
            parts = str(value).split('x')
            if value == '':
                continue  # Source's native preselection is an intermediate state.
            if value in {axis.get('key') for axis in axes}:
                continue  # The one-axis widget can emit its axis key before a height is chosen.
            # UI transitions can send a numeric dimension before the source's
            # option checks settle. Final case binding still demands the exact
            # source-enumerated value and a matching rendered control.
            if len(parts) not in (1, 2) or any(not re.fullmatch(r'\d+(?:\.\d+)?', part) for part in parts):
                raise HarvestError('native_quote_size_invalid')
        elif value != '' and not any(option.get('key') == value for option in field.get('options', [])):
            raise HarvestError('native_quote_option_invalid')
    turnaround = jobs[0].get('turnaround')
    modes = {option.get('key') for rule in (catalog.get('production_rules') or []) for option in rule.get('options', [])}
    if turnaround not in modes | {None, ''}:
        raise HarvestError('native_quote_production_unknown')
    # Reuse the strict job/address/proof exclusion guard, with a temporary
    # normalized representation solely for validation. Save actual values below.
    normalized = copy.deepcopy(payload)
    normalized['values'] = dict(values, _qty=str(quantity))
    candidate = {'values': {key: value for key, value in values.items() if key != '_qty'},
                 'quantity': quantity, 'turnaround': turnaround}
    safe = validate_observed_request(normalized, catalog, candidate)
    safe['values'] = copy.deepcopy(values)
    derived_matches = False
    if set(attrs) <= set(values):
        try:
            expected_derived = derived_request_values(catalog, values)
            derived_matches = all(values.get(key) == expected for key, expected in expected_derived.items())
        except CatalogError:
            pass
    safe['configuration_bound'] = (set(values) - {'_qty'} == set(attrs) | set(derived)
                                   and derived_matches and
                                   (turnaround not in (None, '') or
                                    turnaround is None and source_without_production_field(catalog)))
    safe['quantity_representation'] = 'job_and_initializer' if '_qty' in values else 'job_only'
    return safe, quantity


def intermediate_size_request(catalog, request):
    values = (request or {}).get('values') or {}
    return any(values.get(field['key']) in {axis.get('key') for axis in field.get('options', [])} | {''}
               for field in catalog['contract']['attributes'] if field.get('component') == 'size')


def request_matches_case(request, candidate):
    values = {key: value for key, value in request.get('values', {}).items() if key != '_qty'}
    job = request.get('job') or {}
    subjobs = job.get('subjobs') or []
    return (request.get('configuration_bound') is True and values == candidate['values']
            and job.get('turnaround') == candidate['turnaround'] and job.get('shipping', '') == ''
            and len(subjobs) == 1 and str(subjobs[0].get('quantity')) == str(candidate['quantity']))


class B2SignCanaryManager(B2SignHarvestManager):
    """Checkpointed independent UI observations; no catalog-wide upgrade.

    Fresh page per case bounds hidden-state history. The ordinary source page
    starts without `_qty`; actual controls generate reactive quote requests.
    Cases without an exact saved reference can reconcile UI/server evidence but
    cannot verify harvested pricing at that different quantity.
    """
    def __init__(self, store, browser_pool, *, timeout_seconds=35, quote_gate=None):
        super().__init__(store, browser_pool, requests_per_second=1, max_active_products=1,
                         bootstrap_timeout=timeout_seconds, quote_gate=quote_gate)
        self.root = Path(store.root) / 'b2sign-canaries'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    async def start(self, catalog, plan, diagnosis=None):
        if (not isinstance(plan, dict) or any(plan.get(key) != catalog.get(other) for key, other in
                [('source_url', 'url'), ('source_product_id', 'source_product_id'), ('contract_sha256', 'contract_sha256')])):
            raise HarvestError('canary_plan_scope_mismatch')
        cases, seen = [], set()
        for original in plan.get('cases', []):
            candidate = prepare_candidate(catalog, original.get('candidate') or {})
            if not (isinstance(candidate.get('turnaround'), str)
                    or candidate.get('turnaround') is None and source_without_production_field(catalog)):
                raise HarvestError('canary_production_scope_unsupported')
            identifier = _key(candidate['values'], candidate['quantity'], candidate['turnaround'], catalog)
            if original.get('id') != identifier or identifier in seen:
                raise HarvestError('canary_case_identity_mismatch')
            seen.add(identifier)
            expected = _amount(original.get('expected_subtotal'))
            refs = original.get('reference_quotes') or []
            if expected is not None and (original.get('reference_status') != 'matching_saved_quote' or not refs
                    or any(_amount(ref.get('subtotal')) is None or Decimal(str(ref['subtotal'])) != Decimal(expected) for ref in refs)):
                raise HarvestError('canary_reference_inconsistent')
            cases.append({'id': identifier, 'candidate': candidate, 'expected_subtotal': expected,
                          'reference_status': original.get('reference_status'), 'reference_quotes': copy.deepcopy(refs),
                          'covers': copy.deepcopy(original.get('covers', []))})
        if not cases or len(cases) > 10000:
            raise HarvestError('empty_or_excessive_canary_manifest')
        policy_binding = None
        if diagnosis is not None:
            selection = diagnosis.get('selection') or {}
            proof = [f for f in selection.get('fields', []) if f.get('label') == 'Email Proof']
            if (diagnosis.get('supplier_id') != 'b2sign' or diagnosis.get('url') != catalog['url']
                    or selection.get('source_url') != catalog['url'] or selection.get('revision') != diagnosis.get('revision')
                    or len(proof) != 1 or proof[0].get('include') is not False
                    or proof[0].get('fixed_values') != [] or proof[0].get('selected_values') != []
                    or any(f.get('configurable') and f.get('label') != 'Email Proof' and f.get('include') is not True for f in selection.get('fields', []))):
                raise HarvestError('saved_policy_requires_exact_unchecked_proof_scope')
            policy_binding = {'diagnosis_id': diagnosis['id'], 'revision': diagnosis['revision'], 'selection_sha256': _hash(selection)}
        identifier = uuid.uuid4().hex
        directory = self._path(identifier)
        directory.mkdir(mode=0o700)
        for name in ('results', 'attempts'):
            (directory / name).mkdir(mode=0o700)
        self._write(directory / 'manifest.json', {'catalog': copy.deepcopy(catalog), 'cases': cases})
        report = {'id': identifier, 'supplier_id': 'b2sign', 'url': catalog['url'], 'title': catalog['title'],
                  'status': 'opening', 'requested_count': len(cases), 'completed_count': 0, 'row_count': 0,
                  'matched_reference_count': 0, 'dom_server_matched_count': 0, 'mismatch_count': 0, 'held_count': 0,
                  'quote_requests': 0, 'attempt_budget': len(cases) * 15 + 20, 'contexts': [], 'warnings': [],
                  'created_at': _now(), 'updated_at': _now(), 'finite_requested_set_exhausted': False,
                  'catalog_complete': False, 'prices_verified': False, 'final_eligible': False,
                  'coverage': 'independent_UI_cases_only', 'shipping_status': 'held_unselected',
                  'proof_selection': 'unchecked', 'saved_policy_binding': policy_binding,
                  'uncovered_requirements': copy.deepcopy(plan.get('uncovered_requirements', [])),
                  'required_count': plan.get('required_count'), 'successful_requirement_ids': [],
                  'verification_status': 'pending_independent_UI_canaries'}
        self._write(directory / 'report.json', report)
        await self._launch(identifier)
        return self.get(identifier)

    async def _run(self, identifier):
        directory = self._path(identifier)
        manifest = json.loads((directory / 'manifest.json').read_text())
        report = json.loads((directory / 'report.json').read_text())
        report.update(status='running', warnings=[])
        try:
            async with self.slots:
                for case in sorted(manifest['cases'], key=lambda c: (c['candidate']['quantity'], c['id'])):
                    path = directory / 'results' / (case['id'] + '.json')
                    if path.exists():
                        continue
                    context_id = uuid.uuid4().hex
                    report['contexts'].append({'id': context_id, 'case_id': case['id'], 'started_at': _now()})
                    report['active_case'] = {'id': case['id'], 'quantity': case['candidate']['quantity']}
                    report.pop('last_readiness_diagnostic', None)
                    report.pop('last_production_diagnostic', None)
                    self._write(directory / 'report.json', report)
                    async with self.pool.page('https://www.b2sign.com', headless=False) as page:
                        result = await self._observe_case(page, manifest['catalog'], case, report, directory, context_id)
                    self._write(path, result)
                    self._summarize(report, directory)
                    self._write(directory / 'report.json', report)
                    if result['status'] == 'mismatch':
                        raise HarvestError('canary_price_mismatch')
                report.update(status='attention' if report['mismatch_count'] else 'completed', finite_requested_set_exhausted=True)
                if report['mismatch_count']:
                    report['warnings'] = ['canary_price_mismatch_retained']
                    report['verification_status'] = 'sample_mismatch'
                elif report['held_count']:
                    report['verification_status'] = 'sample_incomplete'
                elif report.get('uncovered_requirements'):
                    report['verification_status'] = 'sample_matched_with_uncovered_requirements'
                else:
                    report['verification_status'] = 'sample_matched'
        except asyncio.CancelledError:
            report.update(status='closed', warnings=['canary_cancelled'])
            raise
        except (HarvestError, CatalogError, PilotError) as exc:
            report.update(status='attention', warnings=[str(exc)])
        except Exception as exc:
            report.update(status='error', warnings=['canary_browser_error'], error_class=type(exc).__name__)
        finally:
            self._summarize(report, directory)
            report['updated_at'] = _now()
            self._write(directory / 'report.json', report)

    def _summarize(self, report, directory):
        rows = [json.loads(p.read_text()) for p in (directory / 'results').glob('*.json')]
        report.update(completed_count=len(rows), row_count=len(rows),
            matched_reference_count=sum(r['status'] == 'matched_reference' for r in rows),
            dom_server_matched_count=sum(r['status'] in {'matched_reference', 'dom_server_matched_without_reference'} for r in rows),
            mismatch_count=sum(r['status'] == 'mismatch' for r in rows), held_count=sum(r['status'] == 'held' for r in rows),
            production_held_count=sum((r.get('production_verification') or {}).get('availability_verified') is False for r in rows),
            successful_requirement_ids=sorted({token for row in rows if row['status'] == 'matched_reference' for token in row.get('covers', [])}))

    async def _set_field(self, page, field, value):
        label = ' x '.join(part + '"' for part in str(value).split('x')) if field.get('component') == 'size' else None
        one_axis_size = field.get('component') == 'size' and len(field.get('options', [])) == 1
        label_pattern = re.compile('^' + re.escape(label) + (r'(?:\(|$)' if one_axis_size else '$')) if label is not None else None
        if label is None:
            matches = [o for o in field.get('options', []) if o.get('key') == value and o.get('visible') is not False]
            if len(matches) != 1:
                raise HarvestError('canary_option_label_ambiguous')
            label = re.sub(r'\s+', ' ', str(matches[0].get('label', ''))).strip()
        state = await page.evaluate(FIELD_UI_JS, {'key': field['key'], 'label': field.get('label') or ''})
        if not state or state.get('disabled'):
            raise HarvestError('canary_control_not_available')
        root = page.locator('[data-hermes-canary-field=' + json.dumps(field['key']) + ']')
        if state['kind'] in {'size', 'size_feet_inches', 'size_selects'}:
            if str(state['value']) == str(value):
                return False
            pieces = str(value).split('x')
            if len(pieces) != 2:
                raise HarvestError('canary_size_value_invalid')
            axes = root.locator('div[class*="_input-wrapper_"]')
            if state['kind'] == 'size_selects':
                buttons = root.locator('button[role=combobox]')
                if await buttons.count() != 2:
                    raise HarvestError('canary_size_axes_ambiguous')
                for index, piece in enumerate(pieces):
                    if not re.fullmatch(r'[1-9]\d*(?:\.\d+)?', piece):
                        raise HarvestError('canary_size_value_invalid')
                    button = buttons.nth(index)
                    current = await button.locator('xpath=..').locator('input[aria-hidden="true"]').input_value()
                    if current == piece:
                        continue
                    await button.click(timeout=3000)
                    controls = await button.get_attribute('aria-controls')
                    if not controls:
                        raise HarvestError('canary_popup_scope_missing')
                    options = page.locator('[id=' + json.dumps(controls) + '] [role=option]')
                    labels = await options.all_inner_texts()
                    matches = [option_index for option_index, option_label in enumerate(labels)
                               if re.match('^' + re.escape(piece) + r'"(?:\s|\(|$)', option_label)]
                    if len(matches) != 1 or await options.nth(matches[0]).get_attribute('aria-disabled') == 'true':
                        raise HarvestError('canary_size_option_ambiguous')
                    await options.nth(matches[0]).click(timeout=3000)
            elif state['kind'] == 'size_feet_inches':
                if await axes.count() != 4:
                    raise HarvestError('canary_size_axes_ambiguous')
                for index, piece in enumerate(pieces):
                    dimension = int(piece)
                    if dimension <= 0:
                        raise HarvestError('canary_size_value_invalid')
                    for offset, number in enumerate((dimension // 12, dimension % 12)):
                        axis = axes.nth(index * 2 + offset)
                        expected = '_feet_' if offset == 0 else '_inches_'
                        if expected not in await axis.get_attribute('class'):
                            raise HarvestError('canary_size_axes_ambiguous')
                        await axis.locator('input').fill(str(number), timeout=3000)
                        await axis.locator('input').blur()
            else:
                if await axes.count() != 2:
                    raise HarvestError('canary_size_axes_ambiguous')
                for index, piece in enumerate(pieces):
                    axis = axes.nth(index)
                    if (await axis.locator('.MuiInput-startDecorator').inner_text()).strip() != ('Width', 'Height')[index]:
                        raise HarvestError('canary_size_axes_ambiguous')
                    await axis.locator('input').fill(piece, timeout=3000)
                    await axis.locator('input').blur()
        elif state['kind'] == 'native':
            if str(state['value']) == str(value):
                return False
            await root.locator('select').select_option(str(value), timeout=3000)
        elif state['kind'] == 'custom':
            if state['label'] == label or one_axis_size and label_pattern.match(state['label']):
                return False
            button = root.locator('button[role=combobox],button.MuiSelect-button')
            await button.click(timeout=3000)
            controls = await button.get_attribute('aria-controls')
            if not controls:
                raise HarvestError('canary_popup_scope_missing')
            options = page.locator('[id=' + json.dumps(controls) + '] [role=option]')
            option_labels = await options.evaluate_all('''nodes=>nodes.map(node=>(
             node.querySelector('span[class*="_option-label_"]')?.textContent||node.textContent||'').trim())''')
            matches = [index for index, option_label in enumerate(option_labels)
                       if (bool(label_pattern.match(option_label)) if label_pattern else option_label == label)]
            if len(matches) != 1 or await options.nth(matches[0]).get_attribute('aria-disabled') == 'true':
                raise HarvestError('canary_popup_option_ambiguous')
            await options.nth(matches[0]).click(timeout=3000)
            await page.keyboard.press('Escape')
        elif state['kind'] == 'buttons':
            if state['label'] == label:
                return False
            items = root.locator('div[class*="_component_"][class*="_button_"] > div[class*="_item_"]').filter(has_text=re.compile('^' + re.escape(label) + '$'))
            if await items.count() != 1:
                raise HarvestError('canary_button_option_ambiguous')
            await items.click(timeout=3000)
        elif state['kind'] == 'static':
            if label not in state['text']:
                raise HarvestError('canary_static_value_mismatch')
            return False
        else:
            raise HarvestError('canary_control_kind_unsupported')
        await asyncio.sleep(.15)
        return True

    async def _observe_case(self, page, catalog, case, report, directory, context_id):
        candidate = case['candidate']
        pending, requests, captures, fatal = set(), {}, [], []
        verified, bundle, document_id, vetted = False, False, None, False
        intermediate_seen = False
        proof_observed_unchecked = False
        unsupported_since = None
        origin, path = urlsplit(catalog['url']).netloc, urlsplit(catalog['url']).path

        async def guard(route):
            nonlocal vetted
            request, parsed = route.request, urlsplit(route.request.url)
            if parsed.scheme != 'https' or parsed.netloc != origin or parsed.username or parsed.password:
                return await route.abort()
            if not vetted:
                try:
                    await validate_url(request.url)
                    vetted = True
                except Exception:
                    fatal.append('canary_origin_validation_failed')
                    return await route.abort()
            if request.method in {'GET', 'HEAD'}:
                if (parsed.path != path and not parsed.path.startswith(('/build/assets/', '/assets/', '/image/'))
                        or request.is_navigation_request() and parsed.path != path):
                    return await route.abort()
                return await route.continue_()
            if request.method != 'POST' or parsed.path != '/item/quote' or parsed.query:
                return await route.abort()
            try:
                safe, quantity = native_request_projection(request.post_data_json, catalog)
                service = await page.evaluate(SERVICE_STATE_JS)
                if not service or service['proof_checked'] or service['shipping_selected']:
                    raise HarvestError('canary_service_selection_changed')
                if report['quote_requests'] >= report['attempt_budget']:
                    raise HarvestError('attempt_budget_exhausted')
                report['quote_requests'] += 1
                attempt = {'id': uuid.uuid4().hex, 'context_id': context_id, 'case_id': case['id'],
                           'quantity': quantity, 'request': safe, 'started_at': _now(), 'status': 'request_started'}
                requests[request] = attempt
                self._write(directory / 'attempts' / (attempt['id'] + '.json'), attempt)
                self._write(directory / 'report.json', report)
                await self.gate.wait()
                return await route.continue_()
            except HarvestError as exc:
                if str(exc) == 'native_quote_unknown_attribute':
                    values = (request.post_data_json or {}).get('values') or {}
                    allowed = ({field['key'] for field in catalog['contract']['attributes']
                                if field.get('component') not in {'divider', 'price_break'}}
                               | {binding['key'] for binding in catalog['contract'].get('derived_request_fields', [])}
                               | {'_qty'})
                    report['rejected_value_keys'] = sorted(key for key in set(values) - allowed
                        if isinstance(key, str) and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]{0,63}', key)
                        and not re.search(r'auth|token|secret|pass|session|cookie|key|nonce', key, re.I))[:20]
                    self._write(directory / 'report.json', report)
                if str(exc) == 'native_quote_size_invalid':
                    raw = (request.post_data_json.get('values') or {}).get('size')
                    report['rejected_size_request'] = {
                        'type': type(raw).__name__,
                        'public_numeric_shape': raw if isinstance(raw, str) and re.fullmatch(r'[0-9.x" ]{0,40}', raw) else None,
                        'length': len(str(raw)) if raw is not None else None,
                        'sha256': hashlib.sha256(str(raw).encode()).hexdigest() if raw is not None else None,
                    }
                    self._write(directory / 'report.json', report)
                fatal.append(str(exc))
                return await route.abort()
            except Exception:
                fatal.append('canary_request_projection_failed')
                return await route.abort()

        async def receive(response):
            nonlocal bundle, intermediate_seen
            if response.url == self.bundle_url and response.status == 200:
                bundle = hashlib.sha256(await response.body()).hexdigest() == self.bundle_sha256
                if not bundle:
                    fatal.append('source_bundle_changed')
                return
            attempt = requests.pop(response.request, None)
            if attempt is None:
                return
            record = {**attempt, 'http_status': response.status, 'status': 'response_received', 'received_at': _now()}
            try:
                if response.status != 200:
                    raise HarvestError('canary_source_http_attention')
                body = await response.json()
                record['schema'] = quote_schema(body)
                record['quote'] = sanitize_quote(body, attempt['quantity'])
                # The pinned Tp component uses exactly this string test to
                # switch printable UI. No arbitrary upstream type string is
                # saved. This flag proves UI presentation, not production time.
                source_type = (body.get('props') or {}).get('quoteResult', {}).get('type')
                record['source_reported_printable'] = source_type == 'printable' if isinstance(source_type, str) else None
                captures.append(record)
            except (HarvestError, PilotError) as exc:
                record['reason'] = str(exc)
                if (str(exc) == 'quote_result_missing_or_ambiguous'
                        and intermediate_size_request(catalog, attempt.get('request'))):
                    record['status'] = 'intermediate_size_unavailable'
                    intermediate_seen = True
                else:
                    fatal.append(str(exc))
            except Exception:
                record['reason'] = 'canary_response_unreadable'
                fatal.append(record['reason'])
            self._write(directory / 'attempts' / (attempt['id'] + '.json'), record)

        def received(response):
            task = asyncio.create_task(receive(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        def failed(request):
            attempt = requests.pop(request, None)
            if attempt:
                self._write(directory / 'attempts' / (attempt['id'] + '.json'), {**attempt, 'status': 'request_failed'})
                # Native reactive controls may cancel an obsolete Inertia
                # request. Preserve that failed attempt; only a later fresh
                # fully bound response can satisfy the canary.

        async def validate_frame():
            nonlocal verified, document_id, proof_observed_unchecked, unsupported_since
            if fatal:
                raise HarvestError(fatal[0])
            latch = await page.evaluate(PUBLIC_ITEM_LATCH_JS)
            if latch:
                if document_id is not None and latch['document_id'] != document_id:
                    raise HarvestError('canary_document_changed')
                document_id = latch['document_id']
            for item in [await page.evaluate(PUBLIC_ITEM_JS), (latch or {}).get('first'), (latch or {}).get('last')]:
                if item is not None:
                    actual = parse_public_product(item, source_url=catalog['url'], source_bundle_sha256=PINNED_BUNDLE_SHA256)
                    if (actual['contract_sha256'] != catalog['contract_sha256']
                            and not display_only_contract_change(catalog, actual)):
                        raise HarvestError('live_product_contract_changed')
                    verified = True
            service = await page.evaluate(SERVICE_STATE_JS)
            dom = await page.evaluate(QUOTE_DOM_JS)
            if dom.get('login_required'):
                raise HarvestError('supplier_login_required')
            if service and (service['proof_checked'] or service['shipping_selected']):
                raise HarvestError('canary_service_selection_changed')
            if service and service['proof_count'] == 1:
                proof_observed_unchecked = True
            # Source hides Email Proof together with printable controls. A
            # same-document previously observed unchecked state plus a fresh
            # explicit nonprintable response can explain observed absence; it
            # does not establish a free service or a hidden selection default.
            nonprintable = bool(captures and captures[-1].get('source_reported_printable') is False)
            known_proof = service and (service['proof_count'] == 1 or service['proof_count'] == 0 and (
                catalog.get('email_proof_available') is False or proof_observed_unchecked and nonprintable))
            snapshot = await inspect_b2sign(page, expand=False)
            warnings = set(snapshot.get('warnings', [])) - {'field_label_from_choices'}
            if warnings:
                report['unsupported_control_warnings'] = sorted(warnings)
                if unsupported_since is None:
                    unsupported_since = time.monotonic()
                if time.monotonic() - unsupported_since >= 1.5:
                    raise HarvestError('canary_unsupported_visible_controls')
            else:
                unsupported_since = None
            return dom, snapshot, bool(known_proof)

        async def drain_native():
            deadline = time.monotonic() + self.bootstrap_timeout
            while requests or pending:
                await validate_frame()
                if time.monotonic() >= deadline:
                    raise HarvestError('canary_native_requests_did_not_settle')
                await asyncio.sleep(.1)

        await page.add_init_script(PUBLIC_ITEM_INIT_JS)
        await page.route('**/*', guard)
        page.on('response', received)
        page.on('requestfailed', failed)
        try:
            # No query initializer: `_qty` in source attributes can shadow the
            # reactive job quantity in production rules. All changes below are UI.
            await page.goto(catalog['url'], wait_until='domcontentloaded', timeout=30000)
            deadline = time.monotonic() + self.bootstrap_timeout
            while time.monotonic() < deadline:
                dom, snapshot, known_proof = await validate_frame()
                if verified and bundle and known_proof and snapshot.get('root_found') and not snapshot.get('busy') and (captures or intermediate_seen) and not requests and not pending:
                    break
                await asyncio.sleep(.1)
            else:
                raise HarvestError('canary_bootstrap_incomplete')
            action_start = _now()
            actions = []
            qty = page.locator('div[class*="_quantity_"] input:visible')
            if await qty.count() != 1:
                raise HarvestError('canary_quantity_control_ambiguous')
            if await qty.input_value() != str(candidate['quantity']):
                await qty.fill(str(candidate['quantity']))
                await qty.press('Tab')
                actions.append('quantity')
                await asyncio.sleep(.2)
                await drain_native()
            fields = catalog['contract']['attributes']
            scope = {**candidate['values'], '_qty': candidate['quantity']}
            # Source actions/visibility are evaluated in source order; hidden
            # values are not overwritten. Final native request binds them all.
            for field in fields:
                if field.get('component') in {'divider', 'price_break', 'hidden'} or evaluate_condition(scope, field.get('visible', True)) is not True:
                    continue
                report['active_field'] = field['key']
                if await self._set_field(page, field, candidate['values'][field['key']]):
                    actions.append(field['key'])
                    await drain_native()
            no_production = candidate['turnaround'] is None and source_without_production_field(catalog)
            radios = page.locator('div[class*="_turnaround_"] input[type=radio][value=' + json.dumps(candidate['turnaround']) + ']')
            production_deadline = time.monotonic() + min(8, self.bootstrap_timeout)
            production_control_present = False
            if no_production:
                dom, snapshot, _ = await validate_frame()
                turns = [f for f in snapshot.get('fields', []) if f.get('label') == 'Turnaround']
                report['last_production_diagnostic'] = {'case_id': case['id'], 'requested_value': None,
                    'target_radio_count': await radios.count(), 'visible_group_count': len(turns),
                    'dom_quantity': dom.get('quantity'), 'input_quantity': await qty.input_value(),
                    'source_availability_determined': False,
                    'production_status': 'not_exposed_no_lead_time_claim'}
                if turns or await radios.count():
                    raise HarvestError('unexpected_production_control')
            while not no_production and time.monotonic() < production_deadline:
                dom, snapshot, _ = await validate_frame()
                count = await radios.count()
                eligible = [c for c in captures if request_matches_case(c['request'], candidate) and (not actions or c['started_at'] >= action_start)]
                explicit_nonprintable = bool(eligible and eligible[-1].get('source_reported_printable') is False)
                turns = [f for f in snapshot.get('fields', []) if f.get('label') == 'Turnaround']
                report['last_production_diagnostic'] = {'case_id': case['id'], 'requested_value': candidate['turnaround'],
                    'target_radio_count': count, 'target_disabled': await radios.is_disabled() if count == 1 else None,
                    'visible_group_count': len(turns), 'source_reports_nonprintable': explicit_nonprintable,
                    'dom_quantity': dom.get('quantity'), 'input_quantity': await qty.input_value(),
                    'pending_responses': len(pending), 'outstanding_quote_requests': len(requests),
                    'source_availability_determined': False}
                if not snapshot.get('busy') and not requests and not pending:
                    if count == 1 and not await radios.is_disabled():
                        production_control_present = True
                        break
                    if count == 0 and not turns and explicit_nonprintable:
                        break
                await asyncio.sleep(.1)
            if not no_production and not production_control_present and time.monotonic() >= production_deadline:
                self._write(directory / 'report.json', report)
                raise HarvestError('canary_production_control_unresolved')
            if production_control_present and not await radios.is_checked():
                label = radios.locator('..')
                await label.click(timeout=3000)
                actions.append('turnaround')
                await asyncio.sleep(.15)
                await drain_native()
            # Source may reuse its previous quote after quantity-only changes.
            # An actual reversible attribute choice forces a fresh native read;
            # it is recorded as an action, never as an endpoint replay.
            if actions and not any(request_matches_case(c['request'], candidate) and c['started_at'] >= action_start for c in captures):
                alternatives = []
                for field in fields:
                    if field.get('component') not in {'select', 'button'} or evaluate_condition(scope, field.get('visible', True)) is not True:
                        continue
                    for option in field.get('options', []):
                        if (option.get('key') != candidate['values'].get(field['key'])
                                and evaluate_condition(scope, option.get('visible', True)) is True
                                and evaluate_condition(scope, option.get('disabled', False)) is False):
                            alternatives.append((field, option['key']))
                # An action-only request value may be absent until its source
                # field changes. Toggle that field first so the restored quote
                # carries the reviewed derived value; other selects come next.
                derived_sources = {binding['source_field'] for binding in
                                   catalog['contract'].get('derived_request_fields', [])}
                alternatives.sort(key=lambda item: (item[0]['key'] not in derived_sources,
                                                     item[0].get('component') != 'select'))
                if alternatives:
                    field, other = alternatives[0]
                    before = len(captures)
                    await self._set_field(page, field, other)
                    wait = time.monotonic() + min(8, self.bootstrap_timeout)
                    while time.monotonic() < wait and (len(captures) <= before or requests or pending):
                        await validate_frame()
                        await asyncio.sleep(.1)
                    await self._set_field(page, field, candidate['values'][field['key']])
                    actions += ['fresh_quote_toggle:' + field['key'], 'restore:' + field['key']]
            deadline, stable_key, stable_since = time.monotonic() + self.bootstrap_timeout, None, None
            while time.monotonic() < deadline:
                dom, snapshot, known_proof = await validate_frame()
                eligible = [c for c in captures if request_matches_case(c['request'], candidate) and (not actions or c['started_at'] >= action_start)]
                chosen = eligible[-1] if eligible else None
                # Hardware without attributes or production can recalculate
                # quantity entirely in the page. The source may issue no new
                # quote after the quantity input changes. In that narrow case,
                # compare the settled DOM with an independently saved exact-
                # quantity quote and the current page's source unit price.
                client_only_hardware = False
                if (chosen is None and no_production and not fields
                        and case['expected_subtotal'] is not None and actions):
                    bootstrap = next((capture for capture in reversed(captures)
                        if capture.get('quote') and capture['request'].get('configuration_bound') is True
                        and capture['request'].get('id') == catalog['contract']['item_id']
                        and capture['request'].get('values', {}) == {}
                        and capture['request']['job'].get('turnaround') is None), None)
                    if bootstrap is not None:
                        unit = Decimal(bootstrap['quote']['unit_price'])
                        if unit * candidate['quantity'] == Decimal(case['expected_subtotal']):
                            chosen = bootstrap
                            client_only_hardware = True
                turn = [f for f in snapshot.get('fields', []) if f.get('label') == 'Turnaround']
                turn_ok = len(turn) == 1 and turn[0].get('selected_values') == [candidate['turnaround']]
                no_production_bound = (no_production and not turn and chosen is not None
                    and chosen['request']['job'].get('turnaround') is None and await radios.count() == 0)
                production_absence_bound = (not turn and chosen is not None and chosen.get('source_reported_printable') is False
                    and await radios.count() == 0)
                amount = dom.get('subtotal') or ''
                money_ok = bool(re.fullmatch(r'\$\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}', amount))
                controls_match = True
                control_mismatches = []
                for field in fields:
                    if field.get('component') in {'divider', 'price_break', 'hidden'} or evaluate_condition(scope, field.get('visible', True)) is not True:
                        continue
                    state = await page.evaluate(FIELD_UI_JS, {'key': field['key'], 'label': field.get('label') or ''})
                    value = candidate['values'][field['key']]
                    expected_label = (' x '.join(part + '"' for part in str(value).split('x')) if field.get('component') == 'size' else
                        next((re.sub(r'\s+', ' ', str(o.get('label', ''))).strip() for o in field.get('options', []) if o.get('key') == value and o.get('visible') is not False), None))
                    one_axis_size = field.get('component') == 'size' and len(field.get('options', [])) == 1
                    option_label_matches = (state and expected_label is not None and
                        (state.get('label') == expected_label or one_axis_size and
                         re.match('^' + re.escape(expected_label) + r'(?:\(|$)', state.get('label', ''))))
                    if (not state or state.get('disabled') or
                            state['kind'] in {'size', 'size_feet_inches', 'size_selects'} and str(state['value']) != str(value) or
                            state['kind'] == 'native' and str(state['value']) != str(value) or
                            state['kind'] in {'custom', 'buttons'} and not option_label_matches or
                            state['kind'] == 'static' and (expected_label is None or expected_label not in state.get('text', ''))):
                        controls_match = False
                        control_mismatches.append({'key': field['key'], 'expected': str(value),
                            'expected_label': expected_label, 'observed': state})
                ready = verified and bundle and known_proof and chosen and controls_match and not pending and not requests and not snapshot.get('busy') and not unsupported_since and (turn_ok or production_absence_bound or no_production_bound) and dom.get('quantity') == str(candidate['quantity']) and money_ok
                report['last_readiness_diagnostic'] = {'case_id': case['id'], 'contract_verified': verified, 'bundle_verified': bundle,
                    'proof_observed_unchecked': known_proof, 'fresh_bound_quotes': len(eligible), 'pending_responses': len(pending),
                    'client_only_hardware_with_exact_saved_quote': client_only_hardware,
                    'outstanding_quote_requests': len(requests), 'busy': bool(snapshot.get('busy')), 'production_matches': turn_ok, 'attribute_controls_match': controls_match,
                    'attribute_control_mismatches': control_mismatches,
                    'production_absence_bound_to_nonprintable_quote': production_absence_bound,
                    'production_not_exposed_bound_to_quote': no_production_bound,
                    'dom': safe_dom_diagnostics(dom, candidate['quantity'])}
                key = _hash([dom, snapshot.get('fields'), chosen['id'] if chosen else None]) if ready else None
                if key != stable_key:
                    stable_key, stable_since = key, time.monotonic()
                if ready and stable_since is not None and time.monotonic() - stable_since >= .5:
                    subtotal = Decimal(amount.replace('$', '').replace(',', '').strip())
                    source_total = (Decimal(case['expected_subtotal']) if client_only_hardware
                                    else Decimal(chosen['quote']['subtotal']))
                    source_unit = Decimal(chosen['quote']['unit_price'])
                    unit_total = source_unit * candidate['quantity']
                    dom_match = subtotal == source_total
                    reference_match = case['expected_subtotal'] is not None and source_total == Decimal(case['expected_subtotal'])
                    status = ('mismatch' if not dom_match or case['expected_subtotal'] is not None and not reference_match else
                              'matched_reference' if reference_match else 'dom_server_matched_without_reference')
                    return {'candidate_id': case['id'], 'case_id': case['id'], 'candidate': candidate, 'context_id': context_id,
                            'status': status, 'captured_at': _now(),
                            'method': 'client_quantity_DOM_vs_saved_exact_quote_and_source_unit' if client_only_hardware else 'native_UI_change_fresh_quote_and_settled_DOM',
                            'request': chosen['request'], 'attempt_id': chosen['id'], 'quote': chosen['quote'],
                            'fresh_exact_quantity_quote_observed': not client_only_hardware,
                            'dom_subtotal': format(subtotal, 'f'), 'dom_server_match': dom_match,
                            'unit_times_quantity_match': unit_total == source_total,
                            'reference_subtotal': case['expected_subtotal'], 'reference_match': reference_match,
                            'reference_quotes': case['reference_quotes'],
                            'reference_comparison_scope': 'exact_saved_quantity_and_settled_DOM_with_current_source_unit' if client_only_hardware else 'exact_public_attribute_values_job_quantity_and_production',
                            'reference_request_representation_difference': 'saved_initializer_qty_present_native_job_only' if case['reference_quotes'] and chosen['request']['quantity_representation'] == 'job_only' else None,
                            'actions': actions, 'covers': [token for token in case['covers'] if turn_ok or not token.startswith('production:')],
                            'source_reported_printable': chosen.get('source_reported_printable'),
                            'production_verification': {'status': 'visible_selected_control' if turn_ok else 'not_exposed_no_lead_time_claim' if no_production_bound else 'source_nonprintable_control_absent',
                                'availability_verified': turn_ok, 'requested_value': candidate['turnaround'],
                                'request_value': chosen['request']['job'].get('turnaround'), 'production_days': None},
                            'proof_observation': 'observed_unchecked_then_source_nonprintable_control_absent' if production_absence_bound and proof_observed_unchecked else 'observed_unchecked_or_source_declared_absent',
                            'source_bundle_sha256': self.bundle_sha256, 'contract_sha256': catalog['contract_sha256'],
                            'verification_status': status, 'final_eligible': False, 'catalog_complete': False,
                            'pricing_independence': 'not_established', 'shipping_status': 'held_unselected'}
                await asyncio.sleep(.1)
            self._write(directory / 'report.json', report)
            raise HarvestError('canary_fresh_bound_quote_or_settled_DOM_missing')
        finally:
            page.remove_listener('response', received)
            page.remove_listener('requestfailed', failed)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
