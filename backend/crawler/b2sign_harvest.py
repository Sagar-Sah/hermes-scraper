"""Finite B2Sign quote reads in an owned, authenticated browser context.

One ordinary product-page bootstrap establishes current public source contract
and the actual Inertia request headers. Only allowlisted protocol headers live
in memory. Subsequent reads use the exact observed /item/quote payload through
same-origin browser fetch. Reports contain no cookies, tokens, customer props or
addresses. These are supplier quote observations, never finalized product rows.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .b2sign_catalog import (
    PINNED_BUNDLE_SHA256, CatalogError, evaluate_condition, parse_public_product,
    production_choices, derived_request_values, size_bound_holds_without_shipping, _compare, _number,
)
from .b2sign_pilot import (
    PUBLIC_ITEM_INIT_JS, PUBLIC_ITEM_JS, PUBLIC_ITEM_LATCH_JS, QUOTE_DOM_JS,
    PilotError, load_contract, quote_schema, sanitize_quote, safe_dom_diagnostics,
)
from .transport import validate_url


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# The second asset differs from the original only in order-tracking carrier
# link text. The source option, production, size, and quote code is byte-for-
# byte unchanged; see work/hermes-group7-bundle-diff-20260928.json.
COMPATIBLE_RUNTIME_BUNDLES = {
    "https://www.b2sign.com/build/assets/app-496df72e.js": PINNED_BUNDLE_SHA256,
    "https://www.b2sign.com/build/assets/app-bf29bac0.js":
        "7d22c5b5e98ed1238348e67be7b8ace2668c6c89077c5a2646ad0c657c69e459",
}


def verified_runtime_bundle(url, body):
    expected = COMPATIBLE_RUNTIME_BUNDLES.get(url)
    return bool(expected and hashlib.sha256(body).hexdigest() == expected)


class HarvestError(ValueError):
    pass


def open_size_key(catalog):
    for field in catalog.get("contract", {}).get("attributes", []):
        if field.get("component") != "size":
            continue
        axes = field.get("options", [])
        if len(axes) != 2 or not any(
            not isinstance(axis.get("component_props", {}).get("range"), list)
            or not axis.get("component_props", {}).get("range") for axis in axes
        ):
            continue
        return field.get("key")
    return None


def bootstrap_open_size_match(catalog, candidate, dom):
    """Compare native width/height controls with the requested open size.

    None means the controls have not rendered yet. A numeric supplier quote is
    not a valid native selection if the page normalized to another size.
    """
    key = open_size_key(catalog)
    if key is None:
        return True
    expected = candidate.get("values", {}).get(key)
    observed = dom.get("size_inputs")
    if not isinstance(observed, list) or len(observed) != 2:
        return None
    if not isinstance(expected, str) or len(expected.split("x")) != 2:
        return False
    numbers = [_number(part) for part in observed]
    requested = [_number(part) for part in expected.split("x")]
    return None if any(number is None for number in numbers) else numbers == requested


_CUSTOM_HOLE = re.compile(
    r"TM(0|(?:\d+(?:\.\d+)?))(?:,TC)?,BM(0|S|(?:\d+(?:\.\d+)?))(?:,BC)?,([PG])$"
)
_CUSTOM_GROMMET = re.compile(
    r"T(N|C|E\d+),B(N|C|S|E\d+),L(N|C|S|E\d+),R(N|C|S|E\d+)$"
)
_CUSTOM_POLEPOCKET = re.compile(
    r"T(0|1\.5|2|2\.5|3),B(0|1\.5|2|2\.5|3),"
    r"L(0|1\.5|2|2\.5|3),R(0|1\.5|2|2\.5|3)$"
)


def complete_custom_hole_value(field, value, scope):
    """Accept only the pinned client's complete, size-bound hole geometry.

    The source UI sends the selected ``holespunch`` custom option key and a
    separate ``holespunch_CUSTOM_VALUE``. It serializes top/bottom corner
    margins and optional center holes as ``TM1,TC,BMS,BC,P`` (or ``G`` for
    grommets). A bare custom dropdown key is never quoteable.
    """
    if field.get("key") not in {"holespunch", "grommet"} or not isinstance(value, str):
        return False
    geometry = scope.get(field["key"] + "_CUSTOM_VALUE")
    match = _CUSTOM_HOLE.fullmatch(geometry) if isinstance(geometry, str) else None
    if match is None:
        return False
    suffix = match.group(3)
    choices = [option for option in field.get("options", [])
               if option.get("key") == value and option.get("component") == "custom"
               and option.get("component_props", {}).get("type") == "hole"
               and ("G" if option.get("component_props", {}).get("is_grommet") else "P") == suffix
               and evaluate_condition(scope, option.get("visible", True)) is True
               and evaluate_condition(scope, option.get("disabled", False)) is False]
    if len(choices) != 1:
        return False
    size_keys = choices[0]["component_props"].get("size_key")
    size_keys = [size_keys] if isinstance(size_keys, str) else size_keys
    if not isinstance(size_keys, list):
        return False
    size = next((scope.get(key) for key in size_keys
                 if isinstance(scope.get(key), str)
                 and re.fullmatch(r"\d+(?:\.\d+)?x\d+(?:\.\d+)?", scope[key])), None)
    if size is None:
        return False
    width = Decimal(size.split("x", 1)[0])
    maximum_margin = width / 2 - 1
    if maximum_margin < 1:
        return False
    for margin in match.group(1, 2):
        if margin == "S":
            continue
        number = Decimal(margin)
        if number != 0 and not 1 <= number <= maximum_margin:
            return False
    return True


def complete_custom_grommet_value(field, value, scope):
    """Validate the four-edge serialization emitted by the reviewed B2 client."""
    if not isinstance(value, str):
        return False
    choices = [option for option in field.get("options", [])
               if option.get("key") == value and option.get("component") == "custom"
               and option.get("component_props", {}).get("type") == "grommet"
               and evaluate_condition(scope, option.get("visible", True)) is True
               and evaluate_condition(scope, option.get("disabled", False)) is False]
    if len(choices) != 1:
        return False
    geometry = scope.get(field["key"] + "_CUSTOM_VALUE")
    match = _CUSTOM_GROMMET.fullmatch(geometry) if isinstance(geometry, str) else None
    if match is None:
        return False
    size_keys = choices[0]["component_props"].get("size_key")
    size_keys = [size_keys] if isinstance(size_keys, str) else size_keys
    if not isinstance(size_keys, list):
        return False
    size = next((scope.get(key) for key in size_keys
                 if isinstance(scope.get(key), str)
                 and re.fullmatch(r"\d+(?:\.\d{1,2})?x\d+(?:\.\d{1,2})?", scope[key])), None)
    if size is None:
        return False
    width, height = (Decimal(part) for part in size.split("x"))
    for edge, setting in zip("TBLR", match.groups()):
        if setting.startswith("E"):
            interval = int(setting[1:])
            length = width if edge in "TB" else height
            if not 6 <= interval <= 999 or interval > length:
                return False
    return True


def complete_custom_polepocket_value(field, value, scope):
    """Require the four-edge pocket sizes serialized by the pinned client."""
    if field.get('key') != 'polepocket' or not isinstance(value, str):
        return False
    choices = [option for option in field.get('options', [])
               if option.get('key') == value and option.get('component') == 'custom'
               and option.get('component_props', {}).get('type') == 'polepocket'
               and evaluate_condition(scope, option.get('visible', True)) is True
               and evaluate_condition(scope, option.get('disabled', False)) is False]
    if len(choices) != 1:
        return False
    geometry = scope.get('polepocket_CUSTOM_VALUE')
    match = _CUSTOM_POLEPOCKET.fullmatch(geometry) if isinstance(geometry, str) else None
    if match is None:
        return False
    size_key = choices[0]['component_props'].get('size_key')
    size = scope.get(size_key) if isinstance(size_key, str) else None
    if not isinstance(size, str) or not re.fullmatch(r'\d+(?:\.\d{1,2})?x\d+(?:\.\d{1,2})?', size):
        return False
    width, height = (Decimal(part) for part in size.split('x'))
    top, bottom, left, right = (Decimal(part) for part in match.groups())
    # A pocket cannot consume the full artwork dimension. The UI permits
    # only these five discrete edge sizes, but its serializer is not a
    # source-side validity response for an undersized print.
    return top + bottom < height and left + right < width


def display_only_contract_change(saved, live):
    """Accept only the observed hide_sqft presentation flag difference."""
    if saved['contract_sha256'] == live['contract_sha256']:
        return False
    changed = [key for key in set(saved['contract']) | set(live['contract'])
               if saved['contract'].get(key) != live['contract'].get(key)]
    return (changed == ['display_metadata'] and
            set((saved['contract'].get('display_metadata') or {})) |
            set((live['contract'].get('display_metadata') or {})) == {'hide_sqft'})


def source_without_production_field(catalog):
    return (catalog.get('contract', {}).get('attributes') == []
            and catalog.get('production_rules') is None
            and catalog.get('email_proof_available') is False
            and not catalog.get('gaps'))


def quote_only_candidates(catalog, quantities):
    """Capture a native hardware quote with no manufactured production choice.

    The pinned source returns no turnaround controls when attrs.turnaround is
    absent. None preserves that absence; it is not a lead-time promise.
    """
    if not source_without_production_field(catalog):
        raise HarvestError('production_free_quote_scope_unavailable')
    return [{'source_url':catalog['url'],'source_product_id':catalog['source_product_id'],
             'contract_sha256':catalog['contract_sha256'],'values':{},'quantity':quantity,
             'turnaround':None,'production_status':'not_exposed','final_eligible':False}
            for quantity in dict.fromkeys(quantities)]


def prepare_candidate(catalog, candidate):
    """Validate exact public selector/production membership, retaining all keys."""
    if (catalog.get("schema") != 1 or catalog.get("supplier_id") != "b2sign"
            or catalog.get("contract", {}).get("source_bundle_sha256") != PINNED_BUNDLE_SHA256):
        raise HarvestError("unreviewed_catalog_contract")
    quantity = candidate.get("quantity")
    values = candidate.get("values")
    turnaround = candidate.get("turnaround")
    context = candidate.get("runtime_context", {})
    no_production = turnaround is None and source_without_production_field(catalog)
    if (type(quantity) is not int or not 1 <= quantity <= 9999
            or not isinstance(values, dict) or not (isinstance(turnaround, str) or no_production)
            or candidate.get("contract_sha256") != catalog.get("contract_sha256")
            or candidate.get("source_product_id") != catalog.get("source_product_id")
            or candidate.get("source_url") != catalog.get("url")):
        raise HarvestError("candidate_scope_mismatch")
    if set(context) - {"_time", "_business_day", "_shipping", "_channel_letter_turnaround"}:
        raise HarvestError("unsupported_runtime_context")
    fulfillment_mode = context.get("_shipping")
    if fulfillment_mode is not None and fulfillment_mode not in {"dropship", "storepickup"}:
        raise HarvestError("unsupported_fulfillment_mode")
    # Time/availability claims supplied to an offline manifest are not live
    # source-clock evidence. This first harvester accepts only unconditional
    # production availability for the supplied quantity and attribute values.
    field_rules = catalog["contract"]["attributes"]
    fields = [field for field in field_rules if field.get("component") not in {"divider", "price_break"}]
    derived = derived_request_values(catalog, values)
    custom_keys = {field["key"] + "_CUSTOM_VALUE" for field in fields
                   if field["key"] + "_CUSTOM_VALUE" in values and any(
                       option.get("key") == values.get(field["key"])
                       and option.get("component") == "custom"
                       for option in field.get("options", []))}
    if set(values) != {field["key"] for field in fields} | set(derived) | custom_keys:
        raise HarvestError("incomplete_attribute_values")
    if any(values[key] != value for key, value in derived.items()):
        raise HarvestError('derived_attribute_value_mismatch')
    if any(type(value) not in (str, int, float) or len(str(value)) > 200 for value in values.values()):
        raise HarvestError("unsupported_attribute_value")
    scope = {**values, "_qty": quantity}
    if fulfillment_mode is not None:
        scope["_shipping"] = fulfillment_mode
    for field in fields:
        value, component = values[field["key"]], field.get("component")
        visible = evaluate_condition(scope, field.get("visible", True))
        disabled = evaluate_condition(scope, field.get("disabled", False))
        if visible is None or disabled is None:
            raise HarvestError("attribute_availability_unresolved")
        if disabled:
            if value != "":
                raise HarvestError("disabled_attribute_not_empty")
            continue
        if component in {"select", "button", "hidden"}:
            options = [option for option in field.get("options", []) if option.get("key") == value
                       and evaluate_condition(scope, option.get("visible", True)) is True
                       and evaluate_condition(scope, option.get("disabled", False)) is False]
            if len(options) != 1:
                # Fi can initialize an optional, unselected hidden field to an
                # empty string when none of its options is defaulted.
                if not visible and value == "" and not any(option.get("default") for option in field.get("options", [])):
                    continue
                raise HarvestError("attribute_value_unavailable")
            # A divider is not a choice. A custom choice opens additional
            # geometry controls. The source sends its custom key together
            # with a separate serialized *_CUSTOM_VALUE; both are required.
            if options[0].get("component") == "custom":
                props = options[0].get("component_props") or {}
                complete = (complete_custom_hole_value(field, value, scope)
                            if props.get("type") == "hole" else
                            complete_custom_grommet_value(field, value, scope)
                            if props.get("type") == "grommet" else
                            complete_custom_polepocket_value(field, value, scope)
                            if props.get("type") == "polepocket" else False)
                if not complete:
                    raise HarvestError("nested_or_placeholder_option_unsupported")
            elif options[0].get("component") == "divider":
                raise HarvestError("nested_or_placeholder_option_unsupported")
        elif component == "size":
            props = field.get("component_props") or {}
            axes = sorted(field.get("options", []), key=lambda option: option.get("key") == "height")
            if props.get("plugin") or len(axes) not in (1, 2) or not isinstance(value, str):
                raise HarvestError("dynamic_size_unsupported")
            pieces = value.split("x")
            if len(pieces) != len(axes):
                raise HarvestError("size_value_unavailable")
            ranges = [axis.get("component_props", {}).get("range") for axis in axes]
            open_size = any(not isinstance(items, list) or not items for items in ranges)
            if open_size:
                # The native size control rounds each axis to hundredths on blur.
                # A finer value cannot be selected, even if the price API quotes it.
                if (len(axes) != 2 or not re.fullmatch(r"\d+(?:\.\d{1,2})?x\d+(?:\.\d{1,2})?", value)
                        or any(_number(piece) is None or _number(piece) <= 0 for piece in pieces)):
                    raise HarvestError("size_value_unavailable")
            elif any(piece not in {str(v) for v in items} for piece, items in zip(pieces, ranges)):
                raise HarvestError("size_value_unavailable")
            for bound in ("min", "max"):
                if bound in props:
                    limit = props[bound]
                    if isinstance(limit, list) and open_size:
                        fit = size_bound_holds_without_shipping(limit, scope, value, bound)
                        if fit is None:
                            raise HarvestError("conditional_size_bound_unsupported")
                    else:
                        if not isinstance(limit, str):
                            raise HarvestError("conditional_size_bound_unsupported")
                        fit = _compare(limit, value, "inside") if bound == "min" else _compare(value, limit, "inside")
                    if fit is not True:
                        raise HarvestError("size_outside_bound")
        else:
            raise HarvestError("dynamic_attribute_unsupported")
    production = production_choices(catalog, values, quantity)
    options = [option for option in production["choices"] if option["value"] == turnaround and option["available"] is True]
    if not no_production and len(options) != 1:
        raise HarvestError("production_requires_live_context_or_unavailable")
    safe = {"source_url": catalog["url"], "source_product_id": catalog["source_product_id"],
            "contract_sha256": catalog["contract_sha256"], "values": copy.deepcopy(values),
            "quantity": quantity, "turnaround": turnaround, "production_label": None if no_production else options[0]["label"],
            "email_proof": False, "shipping": ""}
    if fulfillment_mode is not None:
        safe["source_fulfillment_mode"] = fulfillment_mode
    if no_production:
        safe['production_status'] = 'not_exposed_no_lead_time_claim'
    safe["candidate_id"] = _hash(safe)
    return safe


def make_payload(catalog, candidate, job_template=None):
    job = copy.deepcopy(job_template or {"subjobs": [{"name": "", "quantity": "1"}],
        "shipping": "", "shipping_service": "03", "shipping_service_label": "Ground"})
    job["subjobs"] = [{"name": "", "quantity": str(candidate["quantity"])}]
    if candidate["turnaround"] is None:
        job.pop("turnaround", None)
    else:
        job["turnaround"] = candidate["turnaround"]
    return {"id": catalog["contract"]["item_id"], "values": dict(candidate["values"], _qty=str(candidate["quantity"])),
            "jobs": json.dumps([job], separators=(",", ":"))}


def validate_observed_request(payload, catalog, candidate, *, allow_initializing=False):
    """Return a safe request projection or reject before it leaves the page."""
    if not isinstance(payload, dict) or set(payload) != {"id", "values", "jobs"} or payload["id"] != catalog["contract"]["item_id"]:
        raise HarvestError("unexpected_quote_request")
    expected = dict(candidate["values"], _qty=str(candidate["quantity"]))
    derived = derived_request_values(catalog, candidate['values'])
    def initializing_derived(key, value):
        # Native initialization may leave the action-only target absent/empty.
        # It may pass startup, but can never become a bound quote or saved row.
        return allow_initializing and key in derived and expected[key] == derived[key] and value == ''
    if not isinstance(payload["values"], dict) or any(key not in expected or
            (value != expected[key] and not initializing_derived(key,value)) for key, value in payload["values"].items()):
        raise HarvestError("request_values_mismatch")
    if not allow_initializing and payload["values"] != expected:
        raise HarvestError("request_values_incomplete")
    try:
        jobs = json.loads(payload["jobs"]) if isinstance(payload["jobs"], str) else payload["jobs"]
    except (ValueError, TypeError):
        raise HarvestError("quote_jobs_invalid") from None
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        raise HarvestError("single_job_required")
    job = jobs[0]
    allowed = {"subjobs", "turnaround", "shipping", "shipping_service", "shipping_service_label",
               "shipping_from_id", "shipping_to_id", "shipping_factory", "shipping_signature"}
    if set(job) - allowed or job.get("shipping", "") != "":
        raise HarvestError("unexpected_shipping_or_job_context")
    if any(job.get(key) not in (None, "", False, 0) for key in ("shipping_from_id", "shipping_to_id", "shipping_factory", "shipping_signature")):
        raise HarvestError("address_or_delivery_context_present")
    if job.get("shipping_service", "03") != "03" or job.get("shipping_service_label", "Ground") != "Ground":
        raise HarvestError("shipping_service_changed")
    subjobs = job.get("subjobs")
    if (not isinstance(subjobs, list) or len(subjobs) != 1 or not isinstance(subjobs[0], dict)
            or set(subjobs[0]) - {"name", "quantity"} or subjobs[0].get("name", "") != ""
            or str(subjobs[0].get("quantity")) != str(candidate["quantity"])):
        raise HarvestError("quantity_or_job_name_mismatch")
    turnaround = job.get("turnaround")
    source_modes = {option.get('key') for rule in (catalog.get('production_rules') or [])
                    for option in rule.get('options', [])}
    initial_source_mode = allow_initializing and turnaround in source_modes | {None, ""}
    if turnaround != candidate["turnaround"] and not initial_source_mode:
        raise HarvestError("turnaround_mismatch")
    return {"id": payload["id"], "values": copy.deepcopy(payload["values"]), "job": copy.deepcopy(job),
            "configuration_bound": payload["values"] == expected and turnaround == candidate["turnaround"]}


class SharedQuoteGate:
    def __init__(self, rate):
        if not isinstance(rate, (int, float)) or not 0 < rate <= 6:
            raise HarvestError("quote_rate_outside_supported_bound")
        self.interval, self.lock, self.next_at = 1 / rate, asyncio.Lock(), 0.0

    async def wait(self):
        async with self.lock:
            delay = self.next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_at = time.monotonic() + self.interval


SERVICE_STATE_JS = r"""() => {
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
 const root=[...document.querySelectorAll('div[class*="_instant-quote_"]')].find(visible);
 if(!root)return null;
 const proofs=[...root.querySelectorAll('input[type=checkbox]')].filter(e=>[...e.labels||[]].some(l=>visible(l)&&/^email\s*proof$/i.test(l.innerText.trim())));
 const shipping=[...root.querySelectorAll('input[type=radio]')].filter(e=>['dropship','storepickup'].includes(e.value));
 return {proof_count:proofs.length,proof_checked:proofs.some(e=>e.checked),shipping_selected:shipping.filter(e=>e.checked).map(e=>e.value)};
}"""

# Project the response before crossing the browser boundary. No customer,
# address, debug, shipping-rate or other Inertia props leave the page here.
FETCH_QUOTE_JS = r"""async ({payload,headers}) => {
 try {
  const r=await fetch('/item/quote',{method:'POST',credentials:'same-origin',redirect:'error',headers,body:JSON.stringify(payload)});
  let data;try{data=await r.json()}catch(_){return {status:r.status,body:null};}
  const source=data?.props?.quoteResult;
  if(!source||typeof source!=='object')return {status:r.status,body:{props:{quoteResult:null}}};
  const project=x=>{if(!x||typeof x!=='object')return x;const o={};for(const k of ['unit_price','subtotal','price','qty','quantity'])if(k in x)o[k]=x[k];return o;};
  const safe={};if('quote' in source)safe.quote=Array.isArray(source.quote)?source.quote.map(project):null;
  if('price_break' in source)safe.price_break=Array.isArray(source.price_break)?source.price_break.map(x=>{
    if(!x||typeof x!=='object')return null;const o={};for(const k of ['qty','price','full_sheet_price'])if(k in x)o[k]=x[k];return o;
  }):null;
  return {status:r.status,body:{props:{quoteResult:safe}}};
 }catch(_){return {status:0,body:null};}
}"""


class B2SignHarvestManager:
    def __init__(self, store, browser_pool, *, requests_per_second=2, max_active_products=2,
                 bootstrap_timeout=45, quote_gate=None):
        self.root = Path(store.root) / "b2sign-harvests"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        if type(max_active_products) is not int or not 1 <= max_active_products <= 6:
            raise HarvestError("invalid_active_product_limit")
        self.pool, self.gate = browser_pool, quote_gate or SharedQuoteGate(requests_per_second)
        self.slots = asyncio.Semaphore(max_active_products)
        self.bootstrap_timeout = bootstrap_timeout
        self._tasks, self._closed, self._lock = {}, False, asyncio.Lock()
        self.bundle_url = load_contract()["bundle_url"]
        if self.bundle_url not in COMPATIBLE_RUNTIME_BUNDLES:
            raise HarvestError("pinned_source_bundle_missing_from_compatibility_review")

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise KeyError("harvest_not_found")
        return self.root / identifier

    @staticmethod
    def _write(path, value):
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def get(self, identifier):
        directory = self._path(identifier)
        try:
            report = json.loads((directory / "report.json").read_text())
        except FileNotFoundError:
            raise KeyError("harvest_not_found") from None
        if report["status"] in {"opening", "running"} and identifier not in self._tasks:
            report["status"] = "attention"
            report["warnings"] = sorted(set(report["warnings"] + ["harvest_interrupted"]))
        report["results"] = [json.loads(path.read_text()) for path in sorted((directory / "results").glob("*.json"))]
        return report

    def list(self):
        paths = sorted(self.root.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        reports = []
        for path in paths:
            report = json.loads(path.read_text())
            if report["status"] in {"opening", "running"} and path.parent.name not in self._tasks:
                report["status"] = "attention"
                report["warnings"] = sorted(set(report["warnings"] + ["harvest_interrupted"]))
            # The list drives dashboard totals. Quote rows belong to the detail
            # endpoint, where callers explicitly request a particular job.
            report.pop("results", None)
            reports.append(report)
        return reports

    async def start(self, catalog, candidates, *, attempt_budget=None, diagnosis=None):
        policy_binding = None
        if diagnosis is not None:
            selection = diagnosis.get("selection") or {}
            if (diagnosis.get("supplier_id") != "b2sign" or diagnosis.get("url") != catalog.get("url")
                    or selection.get("source_url") != catalog.get("url")
                    or selection.get("revision") != diagnosis.get("revision")):
                raise HarvestError("saved_policy_scope_mismatch")
            proof = [field for field in selection.get("fields", []) if field.get("label") == "Email Proof"]
            if (len(proof) != 1 or proof[0].get("include") is not False
                    or proof[0].get("fixed_values") != [] or proof[0].get("selected_values") != []):
                raise HarvestError("saved_policy_requires_unchecked_proof")
            if any(field.get("configurable") and field.get("label") != "Email Proof" and field.get("include") is not True
                   for field in selection.get("fields", [])):
                raise HarvestError("additional_hidden_policy_controls_unsupported")
            policy_binding = {"diagnosis_id": diagnosis["id"], "revision": diagnosis["revision"], "selection_sha256": _hash(selection)}
        safe, seen = [], set()
        for candidate in candidates:
            row = prepare_candidate(catalog, candidate)
            if row["candidate_id"] in seen:
                continue
            seen.add(row["candidate_id"])
            safe.append(row)
            if len(safe) > 100000:
                raise HarvestError("finite_manifest_limit_exceeded")
        if not safe:
            raise HarvestError("empty_quote_manifest")
        budget = len(safe) + max(10, len(safe) // 10) if attempt_budget is None else attempt_budget
        if type(budget) is not int or budget < len(safe) or budget > len(safe) * 3 + 20:
            raise HarvestError("invalid_attempt_budget")
        identifier = uuid.uuid4().hex
        directory = self._path(identifier)
        directory.mkdir(mode=0o700)
        (directory / "results").mkdir(mode=0o700)
        (directory / "attempts").mkdir(mode=0o700)
        manifest = {"catalog": copy.deepcopy(catalog), "candidates": safe}
        self._write(directory / "manifest.json", manifest)
        report = {"id": identifier, "status": "opening", "supplier_id": "b2sign", "url": catalog["url"], "title": catalog["title"],
                  "requested_count": len(safe), "completed_count": 0, "row_count": 0, "invalid_count": 0,
                  "quote_requests": 0, "attempt_budget": budget, "created_at": _now(), "updated_at": _now(),
                  "warnings": [], "contexts": [], "catalog_complete": False, "final_eligible": False,
                  "finite_requested_set_exhausted": False, "coverage": "exact_finite_manifest_only",
                  "shipping_status": "held_unselected", "proof_selection": "unchecked",
                  "proof_selection_basis": ("source_explicitly_does_not_offer_proof" if catalog.get("email_proof_available") is False else "read_scope_requires_observed_unchecked_UI"), "saved_policy_binding": policy_binding,
                  "verification_status": "pending_independent_canaries"}
        self._write(directory / "report.json", report)
        await self._launch(identifier)
        return self.get(identifier)

    async def _launch(self, identifier):
        async with self._lock:
            if self._closed:
                raise HarvestError("harvest_manager_closed")
            if identifier in self._tasks:
                raise HarvestError("harvest_already_running")
            self._tasks[identifier] = asyncio.create_task(self._run(identifier), name="b2sign-harvest-" + identifier)
            self._tasks[identifier].add_done_callback(lambda _: self._tasks.pop(identifier, None))

    async def resume(self, identifier):
        report = self.get(identifier)
        if report["finite_requested_set_exhausted"]:
            return report
        if report["quote_requests"] >= report["attempt_budget"]:
            raise HarvestError("attempt_budget_exhausted")
        await self._launch(identifier)
        return self.get(identifier)

    async def _run(self, identifier):
        directory = self._path(identifier)
        manifest = json.loads((directory / "manifest.json").read_text())
        report = json.loads((directory / "report.json").read_text())
        report["status"] = "queued"
        report["warnings"] = []
        self._write(directory / "report.json", report)
        try:
            async with self.slots:
                report["status"] = "running"
                self._write(directory / "report.json", report)
                remaining = [candidate for candidate in manifest["candidates"] if not (directory / "results" / (candidate["candidate_id"] + ".json")).exists()]
                if not remaining:
                    report.update(status="completed", finite_requested_set_exhausted=True)
                    return
                context_id = uuid.uuid4().hex
                report["contexts"].append({"id": context_id, "started_at": _now(), "contract_sha256": manifest["catalog"]["contract_sha256"]})
                self._write(directory / "report.json", report)
                async with self.pool.page("https://www.b2sign.com", headless=False) as page:
                    await self._collect(page, manifest["catalog"], remaining, report, directory, context_id)
                report.update(status="completed", finite_requested_set_exhausted=True)
        except asyncio.CancelledError:
            report["status"] = "closed"
            report["warnings"].append("harvest_cancelled")
            raise
        except (HarvestError, PilotError, CatalogError) as exc:
            report["status"] = "attention"
            report["warnings"].append(str(exc))
        except Exception:
            report["status"] = "error"
            report["warnings"].append("harvest_browser_error")
        finally:
            results = [json.loads(path.read_text()) for path in (directory / "results").glob("*.json")]
            report["completed_count"] = len(results)
            report["row_count"] = sum(row["status"] == "quoted" for row in results)
            report["invalid_count"] = sum(row["status"] == "source_unavailable" for row in results)
            report["updated_at"] = _now()
            report["contains_multiple_session_contexts"] = len(report["contexts"]) > 1
            self._write(directory / "report.json", report)

    async def _collect(self, page, catalog, candidates, report, directory, context_id):
        active = candidates[0]
        native_size_key = open_size_key(catalog)
        bootstrap_size_value = active["values"].get(native_size_key) if native_size_key else None
        bootstrap = True
        captured_headers = None
        job_template = None
        public_verified = False
        bundle_verified = False
        document_id = None
        fatal, bound_native = [], []
        requests, tasks = {}, set()
        vetted = False
        expected_origin = urlsplit(catalog["url"]).netloc
        expected_path = urlsplit(catalog["url"]).path

        async def guard(route):
            nonlocal vetted, captured_headers, job_template
            request = route.request
            p = urlsplit(request.url)
            if p.scheme != "https" or p.netloc != expected_origin or p.username or p.password:
                return await route.abort()
            if not vetted:
                try:
                    await validate_url(request.url)
                    vetted = True
                except Exception:
                    fatal.append("source_origin_validation_failed")
                    return await route.abort()
            if request.method in {"GET", "HEAD"}:
                if p.path != expected_path and not p.path.startswith(("/build/assets/", "/assets/", "/image/")):
                    return await route.abort()
                if request.is_navigation_request() and p.path != expected_path:
                    return await route.abort()
                return await route.continue_()
            if request.method != "POST" or p.path != "/item/quote" or p.query:
                return await route.abort()
            try:
                safe = validate_observed_request(request.post_data_json, catalog, active, allow_initializing=bootstrap)
                if report["quote_requests"] >= report["attempt_budget"]:
                    raise HarvestError("attempt_budget_exhausted")
                await self.gate.wait()
                report["quote_requests"] += 1
                self._write(directory / "report.json", report)
                attempt = {"id": uuid.uuid4().hex, "context_id": context_id, "candidate_id": active["candidate_id"],
                           "quantity": active["quantity"], "request": safe, "started_at": _now(),
                           "status": "request_started", "bootstrap": bootstrap}
                requests[request] = attempt
                self._write(directory / "attempts" / (attempt["id"] + ".json"), attempt)
                if bootstrap:
                    if safe["configuration_bound"]:
                        # Only protocol headers survive this local variable;
                        # cookies/auth/account values never enter reports.
                        raw = await request.all_headers()
                        names = {"content-type", "x-inertia", "x-inertia-version", "x-inertia-partial-data",
                                 "x-inertia-partial-component", "x-requested-with", "x-xsrf-token", "x-csrf-token", "accept"}
                        selected = {key: value for key, value in raw.items() if key.lower() in names}
                        if selected.get("x-inertia", "").lower() != "true" or not any(selected.get(key) for key in ("x-xsrf-token", "x-csrf-token")):
                            raise HarvestError("source_quote_protocol_headers_missing")
                        captured_headers = selected
                        job_template = safe["job"]
                return await route.continue_()
            except (HarvestError, PilotError) as exc:
                fatal.append(str(exc))
                return await route.abort()
            except Exception:
                fatal.append("quote_request_validation_failed")
                return await route.abort()

        async def receive(response):
            nonlocal bundle_verified
            try:
                if (urlsplit(response.url).netloc == expected_origin
                        and re.fullmatch(r"/build/assets/app-[a-z0-9]+\.js", urlsplit(response.url).path)):
                    body = await response.body() if response.status == 200 else b""
                    bundle_verified = verified_runtime_bundle(response.url, body)
                    if not bundle_verified:
                        fatal.append("source_bundle_changed")
                    else:
                        report["contexts"][-1]["runtime_bundle_sha256"] = hashlib.sha256(body).hexdigest()
                    return
                attempt = requests.pop(response.request, None)
                if attempt is None:
                    return
                record = {"http_status": response.status, "request": attempt["request"], "received_at": _now(), "attempt_id": attempt["id"]}
                if response.status == 200:
                    body = await response.json()
                    record["schema"] = quote_schema(body)
                    try:
                        record["quote"] = sanitize_quote(body, attempt["quantity"])
                    except PilotError as exc:
                        record["reason"] = str(exc)
                else:
                    record["reason"] = "bootstrap_quote_http_error"
                self._write(directory / "attempts" / (attempt["id"] + ".json"), {**attempt, **record, "status": "response_received"})
                if attempt["bootstrap"] and attempt["request"]["configuration_bound"]:
                    bound_native.append(record)
            except Exception:
                fatal.append("bootstrap_response_unreadable")

        def on_response(response):
            task = asyncio.create_task(receive(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def on_failed(request):
            attempt = requests.pop(request, None)
            if attempt:
                self._write(directory / "attempts" / (attempt["id"] + ".json"),
                            {**attempt, "status": "request_failed", "reason": "source_request_failed", "finished_at": _now()})

        def save(candidate, result, method, *, native_selection_bound=False, native_size_bound=False):
            row = {"candidate_id": candidate["candidate_id"], "candidate": candidate, "context_id": context_id,
                   "captured_at": _now(), "method": method, "status": "quoted" if result.get("quote") else "source_unavailable",
                   "native_selection_bound": native_selection_bound,
                   "native_size_bound": native_size_bound,
                   "verification_status": "pending_independent_canaries", "final_eligible": False, **result}
            self._write(directory / "results" / (candidate["candidate_id"] + ".json"), row)
            report["completed_count"] += 1
            report["row_count"] += bool(result.get("quote"))
            report["invalid_count"] += not bool(result.get("quote"))
            report["updated_at"] = _now()
            self._write(directory / "report.json", report)

        await page.route("**/*", guard)
        await page.add_init_script(PUBLIC_ITEM_INIT_JS)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)
        try:
            await page.goto(catalog["url"] + "?" + urlencode(dict(active["values"], _qty=str(active["quantity"]))), wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + self.bootstrap_timeout
            while time.monotonic() < deadline:
                if fatal:
                    raise HarvestError(fatal[0])
                latch = await page.evaluate(PUBLIC_ITEM_LATCH_JS)
                current = await page.evaluate(PUBLIC_ITEM_JS)
                if latch:
                    if document_id is None:
                        document_id = latch["document_id"]
                    elif document_id != latch["document_id"]:
                        raise HarvestError("bootstrap_document_changed")
                for item in [current, (latch or {}).get("first"), (latch or {}).get("last")]:
                    if item is not None:
                        if item.get("saleable") is False:
                            raise HarvestError("source_product_not_saleable")
                        live = parse_public_product(item, source_url=catalog["url"], source_bundle_sha256=PINNED_BUNDLE_SHA256)
                        if live["contract_sha256"] != catalog["contract_sha256"]:
                            changed = sorted(key for key in set(catalog["contract"]) | set(live["contract"])
                                             if catalog["contract"].get(key) != live["contract"].get(key))
                            report["live_contract_difference"] = {
                                "saved_sha256": catalog["contract_sha256"],
                                "live_sha256": live["contract_sha256"],
                                "changed_sections": changed,
                                "display_metadata_saved": catalog["contract"].get("display_metadata"),
                                "display_metadata_live": live["contract"].get("display_metadata"),
                            }
                            if (changed != ["display_metadata"] or
                                    set((catalog["contract"].get("display_metadata") or {})) |
                                    set((live["contract"].get("display_metadata") or {})) != {"hide_sqft"}):
                                raise HarvestError("live_product_contract_changed")
                            report["contexts"][-1]["display_metadata_only_change"] = True
                        public_verified = True
                service = await page.evaluate(SERVICE_STATE_JS)
                dom = await page.evaluate(QUOTE_DOM_JS)
                report["bootstrap_diagnostics"] = {"public_contract_verified": public_verified, "bundle_verified": bundle_verified,
                    "source_headers_captured": bool(captured_headers), "bound_native_quotes": len(bound_native),
                    "outstanding_requests": len(requests), "pending_responses": len(tasks), "dom": safe_dom_diagnostics(dom, active["quantity"])}
                if dom.get("login_required"):
                    raise HarvestError("supplier_login_required")
                if service and (service["proof_checked"] or service["shipping_selected"]):
                    raise HarvestError("source_service_selection_changed")
                proof_observed = service and (service["proof_count"] == 1 or catalog.get("email_proof_available") is False and service["proof_count"] == 0)
                if public_verified and bundle_verified and captured_headers and proof_observed and bound_native and not requests and not tasks:
                    baseline = bound_native[-1]
                    if not baseline.get("quote"):
                        raise HarvestError(baseline.get("reason", "bootstrap_quote_unavailable"))
                    amount = dom.get("subtotal") or ""
                    if dom.get("quantity") == str(active["quantity"]) and re.fullmatch(r"\$\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}", amount):
                        if Decimal(amount.replace("$", "").replace(",", "").strip()) != Decimal(baseline["quote"]["subtotal"]):
                            raise HarvestError("bootstrap_dom_quote_mismatch")
                        size_match = bootstrap_open_size_match(catalog, active, dom)
                        if size_match is False:
                            raise HarvestError("bootstrap_dom_size_mismatch")
                        if size_match is None:
                            await asyncio.sleep(.1)
                            continue
                        save(active, baseline, "ordinary_page_quote_with_dom_anchor",
                             native_selection_bound=True, native_size_bound=bool(native_size_key))
                        report["contexts"][-1].update(bootstrap_verified=True, protocol_header_names=sorted(captured_headers),
                                                      source_bundle_sha256=PINNED_BUNDLE_SHA256)
                        break
                await asyncio.sleep(.1)
            else:
                raise HarvestError("bootstrap_readiness_incomplete")
            bootstrap = False
            for active in candidates[1:]:
                if fatal:
                    raise HarvestError(fatal[0])
                if page.is_closed():
                    raise HarvestError("source_page_closed")
                latch = await page.evaluate(PUBLIC_ITEM_LATCH_JS)
                if not latch or latch["document_id"] != document_id:
                    raise HarvestError("source_document_changed")
                service = await page.evaluate(SERVICE_STATE_JS)
                if not service or service["proof_checked"] or service["shipping_selected"]:
                    raise HarvestError("source_service_selection_changed")
                for item in [(latch or {}).get("first"), (latch or {}).get("last"), await page.evaluate(PUBLIC_ITEM_JS)]:
                    if item is not None:
                        if item.get("saleable") is False:
                            raise HarvestError("source_product_not_saleable")
                        current = parse_public_product(item, source_url=catalog["url"], source_bundle_sha256=PINNED_BUNDLE_SHA256)
                        if current["contract_sha256"] != catalog["contract_sha256"]:
                            changed = sorted(key for key in set(catalog["contract"]) | set(current["contract"])
                                             if catalog["contract"].get(key) != current["contract"].get(key))
                            report["live_contract_difference"] = {
                                "saved_sha256": catalog["contract_sha256"],
                                "live_sha256": current["contract_sha256"],
                                "changed_sections": changed,
                                "display_metadata_saved": catalog["contract"].get("display_metadata"),
                                "display_metadata_live": current["contract"].get("display_metadata"),
                            }
                            if (changed != ["display_metadata"] or
                                    set((catalog["contract"].get("display_metadata") or {})) |
                                    set((current["contract"].get("display_metadata") or {})) != {"hide_sqft"}):
                                raise HarvestError("live_product_contract_changed")
                            report["contexts"][-1]["display_metadata_only_change"] = True
                payload = make_payload(catalog, active, job_template)
                validate_observed_request(payload, catalog, active)
                response = await page.evaluate(FETCH_QUOTE_JS, {"payload": payload, "headers": captured_headers})
                if fatal:
                    raise HarvestError(fatal[0])
                status = response["status"]
                evidence = {"http_status": status, "request": validate_observed_request(payload, catalog, active),
                            "schema": quote_schema(response.get("body")), "received_at": _now()}
                if status in (401, 403, 419, 429) or status == 0 or status >= 500:
                    # Stop and checkpoint instead of resetting budget, changing
                    # identity, or multiplying supplier pressure with retries.
                    raise HarvestError("source_access_or_rate_attention" if status in (401, 403, 419, 429) else "source_transport_attention")
                if status != 200:
                    raise HarvestError("quote_http_contract_unresolved")
                try:
                    evidence["quote"] = sanitize_quote(response.get("body"), active["quantity"])
                except PilotError as exc:
                    if str(exc) != "quote_unavailable":
                        raise HarvestError(str(exc)) from None
                    evidence["reason"] = "source_unavailable_for_exact_configuration"
                save(active, evidence, "same_context_source_quote_read",
                     native_size_bound=bool(native_size_key and active["values"].get(native_size_key) == bootstrap_size_value))
        finally:
            captured_headers = None
            page.remove_listener("response", on_response)
            page.remove_listener("requestfailed", on_failed)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self._closed = True
        identifiers = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for identifier in identifiers:
            report = json.loads((self._path(identifier) / "report.json").read_text())
            if report["status"] in {"opening", "running"}:
                report.update(status="closed", warnings=["harvest_cancelled"], updated_at=_now())
                self._write(self._path(identifier) / "report.json", report)
