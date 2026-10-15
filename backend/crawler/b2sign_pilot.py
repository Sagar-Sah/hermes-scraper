"""A bounded, ordinary-page B2Sign quote pilot, not a catalog harvester.

Only the reviewed Pole Banner public contract is supported. Every quantity gets
its own duplicate page: the source otherwise caches unit prices without always
requesting a new quote. No cookies, account props, addresses or full Inertia
responses are read into reports. No cart/order/upload actions are permitted.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .b2sign_diagnosis import inspect_b2sign
from .transport import validate_url


ORIGIN = "https://www.b2sign.com"
APPROVED_QUANTITIES = (1, 10, 100, 101)
MAX_QUOTES_PER_PAGE = 6
CONTRACT_FILE = Path(__file__).with_name("contracts") / "b2sign-pole-banner.json"


class PilotError(ValueError):
    """A safe machine reason, never an upstream exception or response body."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def contract_projection(value):
    if isinstance(value, dict):
        return {key: contract_projection(item) for key, item in value.items() if key != "image"}
    if isinstance(value, list):
        return [contract_projection(item) for item in value]
    return value


def load_contract(path=None):
    data = json.loads(Path(path or CONTRACT_FILE).read_text())
    if (data.get("schema") != 1 or data.get("item_id") != 31
            or data.get("source_url") != ORIGIN + "/pole-banner-set"
            or not re.fullmatch(r"[0-9a-f]{64}", data.get("bundle_sha256", ""))):
        raise PilotError("unsupported_source_contract")
    return data


def _single(field):
    values = field.get("selected_values")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise PilotError("selection_unresolved")
    return values[0]


def _field(fields, *, label=None, source_id=None):
    found = [field for field in fields if (
        field.get("label") == label if label is not None else field.get("source_id") == source_id)]
    if len(found) != 1:
        raise PilotError("selection_field_missing_or_ambiguous")
    return found[0]


def bind_diagnosis(diagnosis, contract):
    """Resolve only the reviewed baseline; labels are mapped through source data."""
    if diagnosis.get("supplier_id") != "b2sign" or diagnosis.get("url") != contract["source_url"]:
        raise PilotError("unsupported_product")
    selection = diagnosis.get("selection")
    if (not isinstance(selection, dict) or selection.get("revision") != diagnosis.get("revision")
            or selection.get("source_url") != diagnosis["url"]):
        raise PilotError("saved_selection_required")
    fields = diagnosis.get("fields", [])
    policy_fields = selection.get("fields", [])
    proof = _field(fields, label="Email Proof")
    proof_policy = _field(policy_fields, label="Email Proof")
    if (proof.get("selected_values") != [] or proof_policy.get("include") is not False
            or proof_policy.get("fixed_values") != []):
        raise PilotError("unchecked_email_proof_required")
    shipping = _field(fields, label="Shipping")
    if shipping.get("selected_values") != []:
        raise PilotError("shipping_must_remain_unselected")
    # This pilot exercises the current baseline, not any hidden replacement
    # values or arbitrary combinations from the saved selection policy.
    for field in policy_fields:
        if field.get("configurable") and field.get("label") != "Email Proof" and field.get("include") is not True:
            raise PilotError("additional_hidden_controls_unsupported")
    attrs = {item["key"]: item for item in contract["attrs"]["attrs"]}
    values = {}
    for key in ("hardware", "sides", "material", "polepocket", "grommet", "windslit"):
        label = _single(_field(fields, source_id="attr-" + key))
        choices = [option for option in attrs[key]["options"]
                   if option.get("visible") is not False and option.get("label") == label]
        if len(choices) != 1:
            raise PilotError("source_option_binding_ambiguous")
        values[key] = choices[0]["key"]
    if values["hardware"] not in ("p", "0"):
        raise PilotError("hardware_only_baseline_unsupported")
    size = re.fullmatch(r'(\d+)\s*"\s*[x×]\s*(\d+)\s*"', _single(_field(fields, label="Size")))
    if not size:
        raise PilotError("size_binding_unresolved")
    dimensions = {item["key"]: item["component_props"]["range"] for item in attrs["_size"]["options"]}
    width, height = size.groups()
    if width not in dimensions["width"] or height not in dimensions["height"]:
        raise PilotError("size_outside_source_domain")
    values["_size"] = width + "x" + height
    # Both are explicit source defaults/actions and must also match each actual
    # transmitted request. Neither is inferred from an absent DOM control.
    values["pole_width"] = width
    defaults = [option["key"] for option in attrs["hem"]["options"] if option.get("default") is True]
    if defaults != ["0"]:
        raise PilotError("source_hem_default_changed")
    values["hem"] = "0"
    if _single(_field(fields, label="Turnaround")) != "v1":
        raise PilotError("regular_baseline_required")
    return values


def expected_turnaround(quantity, contract):
    # Only the source's simple, ordered quantity rules are evaluated. Clock/
    # holiday eligibility of additional rush choices is read from the page.
    for rule in contract["attrs"]["turnaround"]:
        match = rule.get("match")
        if (not isinstance(match, list) or len(match) != 1 or match[0].get("key") != "_qty"
                or match[0].get("op") != ">" or type(match[0].get("value")) is not int):
            raise PilotError("turnaround_rule_unsupported")
        if quantity > match[0]["value"]:
            option = rule["options"][0]
            if option.get("visible") is not None:
                raise PilotError("default_turnaround_is_conditional")
            return {"value": option["key"], "label": option["label"]}
    raise PilotError("turnaround_unresolved")


def _money(value):
    if isinstance(value, bool) or value is None:
        raise PilotError("invalid_quote_money")
    raw = str(value)
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        raise PilotError("invalid_quote_money")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise PilotError("invalid_quote_money") from None
    if not amount.is_finite() or amount > Decimal("1000000000"):
        raise PilotError("invalid_quote_money")
    return format(amount, "f")


def sanitize_request(payload, contract, quantity, values):
    """Reject private/extra context before transmission; preserve safe raw IDs.

    Transient initialization requests may be safe but not yet configuration-
    bound. Such requests are counted and cannot supply an accepted quote.
    """
    if not isinstance(payload, dict) or set(payload) != {"id", "values", "jobs"} or payload["id"] != contract["item_id"]:
        raise PilotError("unexpected_quote_request")
    posted = payload["values"]
    allowed = set(values) | {"_qty"}
    if (not isinstance(posted, dict) or not set(posted) <= allowed
            or any(not isinstance(value, str) or len(value) > 100 for value in posted.values())):
        raise PilotError("unexpected_quote_values")
    for key, value in posted.items():
        if value != (str(quantity) if key == "_qty" else values[key]):
            raise PilotError("quote_values_changed")
    jobs = payload["jobs"]
    if isinstance(jobs, str):
        try:
            jobs = json.loads(jobs)
        except (ValueError, TypeError):
            raise PilotError("invalid_quote_jobs") from None
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        raise PilotError("single_quote_job_required")
    job = jobs[0]
    allowed_job = {"subjobs", "turnaround", "shipping", "shipping_service", "shipping_service_label",
                   "shipping_from_id", "shipping_to_id", "shipping_factory", "shipping_signature"}
    if not set(job) <= allowed_job:
        raise PilotError("unexpected_quote_job_context")
    if job.get("shipping", "") != "" or any(job.get(key) not in (None, "", False, 0) for key in
            ("shipping_from_id", "shipping_to_id", "shipping_factory", "shipping_signature")):
        raise PilotError("shipping_or_address_context_present")
    if job.get("shipping_service", "03") != "03" or job.get("shipping_service_label", "Ground") != "Ground":
        raise PilotError("shipping_service_changed")
    subjobs = job.get("subjobs")
    if (not isinstance(subjobs, list) or len(subjobs) != 1 or not isinstance(subjobs[0], dict)
            or set(subjobs[0]) - {"name", "quantity"} or subjobs[0].get("name", "") != ""
            or str(subjobs[0].get("quantity")) != str(quantity)):
        raise PilotError("quantity_or_job_context_changed")
    expected = expected_turnaround(quantity, contract)
    turnaround = job.get("turnaround")
    if turnaround not in (None, "", expected["value"]):
        raise PilotError("turnaround_changed")
    return {"item_id": payload["id"], "values": copy.deepcopy(posted), "quantity": quantity,
            "turnaround": turnaround, "shipping": "", "shipping_service": "03",
            "configuration_bound": set(posted) >= set(values) and turnaround == expected["value"]}


def sanitize_quote(body, quantity):
    """Project only known product-pricing fields; never save other Inertia props."""
    props = body.get("props") if isinstance(body, dict) else None
    result = props.get("quoteResult") if isinstance(props, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("quote"), list) or len(result["quote"]) != 1:
        raise PilotError("quote_result_missing_or_ambiguous")
    row = result["quote"][0]
    if not isinstance(row, dict):
        raise PilotError("quote_unavailable")
    for key in ("qty", "quantity"):
        if key in row:
            returned_quantity = row[key]
            # The live source returns one quantity per subjob in qty[]. A
            # scalar is also supported, but neither form may hide other jobs.
            if isinstance(returned_quantity, list):
                if len(returned_quantity) != 1:
                    raise PilotError("response_quantity_mismatch")
                returned_quantity = returned_quantity[0]
            if type(returned_quantity) not in (int, str) or str(returned_quantity) != str(quantity):
                raise PilotError("response_quantity_mismatch")
    safe = {"unit_price": _money(row.get("unit_price")), "subtotal": _money(row.get("subtotal"))}
    if Decimal(safe["unit_price"]) <= 0 or Decimal(safe["subtotal"]) <= 0:
        raise PilotError("nonpositive_quote")
    if "price" in row:
        if not isinstance(row["price"], list) or len(row["price"]) != 1:
            raise PilotError("subjob_prices_unsupported")
        safe["subjob_prices"] = [_money(value) for value in row["price"]]
    safe["price_break_present"] = result.get("price_break") is not None
    safe.update(sanitize_price_breaks(result.get("price_break")))
    safe["quantity"] = quantity
    return safe


def sanitize_price_breaks(value):
    """The pinned price_break renderer reads qty, price and full_sheet_price.

    Numeric tiers are retained in full, but no extrapolation or scope beyond
    this exact quote is asserted. Other response keys are never persisted.
    """
    if value is None:
        return {"price_break_status": "not_exposed_in_response", "price_break_tiers": []}
    try:
        if not isinstance(value, list) or len(value) > 10000:
            raise PilotError("unsupported_price_break_schema")
        tiers = []
        for tier in value:
            if not isinstance(tier, dict):
                raise PilotError("unsupported_price_break_schema")
            qty = tier.get("qty")
            if type(qty) is int and qty > 0:
                pass
            elif (isinstance(qty, list) and len(qty) == 2
                    and all(type(number) is int and number > 0 for number in qty) and qty[0] <= qty[1]):
                pass
            else:
                raise PilotError("unsupported_price_break_schema")
            row = {"qty": copy.deepcopy(qty), "price": _money(tier.get("price"))}
            if "full_sheet_price" in tier:
                if type(tier["full_sheet_price"]) is not bool:
                    raise PilotError("unsupported_price_break_schema")
                row["full_sheet_price"] = tier["full_sheet_price"]
            tiers.append(row)
        return {"price_break_status": "source_numeric_tiers", "price_break_tiers": tiers}
    except PilotError:
        return {"price_break_status": "unresolved_schema", "price_break_tiers": []}


def quote_schema(body):
    """Known structure plus an opaque fingerprint of the source quote error.

    The source renders ``props.errors['quote.message']`` beside its calculator.
    Preserve its presence/type and, for strings, hash/length without retaining
    arbitrary message text, error keys, debug output, or other Inertia props.
    Literal categories are diagnostic only; they never change quote acceptance.
    """
    props = body.get("props") if isinstance(body, dict) else None
    result = props.get("quoteResult") if isinstance(props, dict) else None
    rows = result.get("quote") if isinstance(result, dict) else None
    row = rows[0] if isinstance(rows, list) and rows else None
    errors = props.get("errors") if isinstance(props, dict) else None
    message = errors.get("quote.message") if isinstance(errors, dict) else None
    # Exact whole-message matches only. Unknown prose, even when containing one
    # of these phrases, must not be echoed or treated as a supplier diagnosis.
    categories = {
        "unauthenticated": "authentication_required",
        "unauthorized": "authorization_denied",
        "forbidden": "authorization_denied",
        "too many requests": "rate_limited",
        "the given data was invalid": "validation_error",
        "not found": "not_found",
        "service unavailable": "service_unavailable",
        "out of stock": "stock_unavailable",
        "price unavailable": "price_unavailable",
    }
    category = None
    if isinstance(message, str):
        literal = message.strip().casefold().removesuffix(".")
        category = categories.get(literal, "empty" if not message.strip() else "unclassified")
    known = {"unit_price", "subtotal", "price", "qty", "quantity", "price_break", "quote"}
    describe = lambda value: {key: type(item).__name__ for key, item in value.items() if key in known} if isinstance(value, dict) else {}
    return {"quote_result_type": type(result).__name__, "quote_count": len(rows) if isinstance(rows, list) else None,
            "result_fields": describe(result), "row_fields": describe(row),
            "body_type": type(body).__name__,
            "props_present": isinstance(body, dict) and "props" in body,
            "props_type": type(props).__name__,
            "quote_result_present": isinstance(props, dict) and "quoteResult" in props,
            "quote_field_present": isinstance(result, dict) and "quote" in result,
            "quote_field_type": type(rows).__name__,
            "errors_present": isinstance(props, dict) and "errors" in props,
            "errors_type": type(errors).__name__,
            "error_count": len(errors) if isinstance(errors, (dict, list)) else None,
            "unknown_error_key_count": sum(key != "quote.message" for key in errors) if isinstance(errors, dict) else None,
            "quote_message_present": isinstance(errors, dict) and "quote.message" in errors,
            "quote_message_type": type(message).__name__,
            "quote_message_length": len(message) if isinstance(message, str) else None,
            "quote_message_sha256": hashlib.sha256(message.encode("utf-8", errors="surrogatepass")).hexdigest() if isinstance(message, str) else None,
            "quote_message_category": category}


PUBLIC_ITEM_JS = r"""() => {
 const e=document.querySelector('[data-page]'); if(!e)return null;
 let p;try {p=JSON.parse(e.getAttribute('data-page'));} catch(_){
   try {p=JSON.parse(decodeURIComponent(atob(e.getAttribute('data-page'))));}catch(_){return null;}}
 const item=p?.props?.item;if(!item)return null;
 return {id:item.id,url:item.url,title:item.title,attrs:item.attrs,saleable:item.saleable};
}"""

# Inertia may consume/remove the initial data-page attribute during hydration.
# Capture only the public product contract in the page before that can happen;
# no other bootstrap props are returned to Python or saved in the latch.
PUBLIC_ITEM_LATCH_KEY = "__hermesB2PublicContract"
PUBLIC_ITEM_INIT_JS = """(() => {
 const read = (""" + PUBLIC_ITEM_JS + """);
 const state={document_id:crypto.randomUUID(),first:null,last:null};
 Object.defineProperty(window,""" + json.dumps(PUBLIC_ITEM_LATCH_KEY) + """,{value:state});
 const capture=()=>{const item=read();if(item){if(!state.first)state.first=item;state.last=item;}};
 new MutationObserver(capture).observe(document,{childList:true,subtree:true,attributes:true,attributeFilter:['data-page']});
 capture();
})();"""
PUBLIC_ITEM_LATCH_JS = "() => window[" + json.dumps(PUBLIC_ITEM_LATCH_KEY) + "] || null"

QUOTE_DOM_JS = r"""() => {
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
 const roots=[...document.querySelectorAll('div[class*="_instant-quote_"]')].filter(visible);
 const qty=[...new Set(roots.flatMap(r=>[...r.querySelectorAll('div[class*="_quantity_"] input')]))].filter(visible);
 const e=qty.length===1?qty[0]:null;
 const subtotals=e?.closest('div[class*="_quantity_"]')?.parentElement?.querySelectorAll('div[class*="_subtotal_"]')||[];
 const subtotal=subtotals.length===1?subtotals[0]:null;
 const sizeInputs=[...document.querySelectorAll('#attr-_size input')].filter(visible);
 const pairIsFeetInches=(a,b)=>a?.closest('div[class*="_feet_"]')&&b?.closest('div[class*="_inches_"]');
 let sizeValues=null;
 if(sizeInputs.length===2) sizeValues=sizeInputs.map(input=>input.value);
 else if(sizeInputs.length===4&&pairIsFeetInches(sizeInputs[0],sizeInputs[1])
     &&pairIsFeetInches(sizeInputs[2],sizeInputs[3])){
   const axis=(feet,inches)=>{const f=Number(feet.value),i=Number(inches.value);
     return Number.isFinite(f)&&Number.isFinite(i)&&f>=0&&i>=0&&i<12
       ?String(Math.round((12*f+i)*100)/100):null};
   const values=[axis(sizeInputs[0],sizeInputs[1]),axis(sizeInputs[2],sizeInputs[3])];
   if(values.every(value=>value!==null)) sizeValues=values;
 }
 const password=[...document.querySelectorAll('input[type=password]')].some(visible);
 return {quantity:e&&/^\d+$/.test(e.value)?e.value:null,subtotal:subtotal?.innerText?.trim()||null,
   quantity_count:qty.length,subtotal_count:subtotals.length,
   size_inputs:sizeValues,
   size_input_count:sizeInputs.length,
   login_required:password};
}"""


def safe_dom_diagnostics(raw, quantity):
    """Bounded amount-only diagnostics from the scoped quote subtotal cell."""
    text = raw.get("subtotal")
    safe_text = text if isinstance(text, str) and len(text) <= 64 and re.fullmatch(r"(?:USD\s*|US\$\s*)?[\d$.,()\-\s]+", text) else None
    size_inputs = raw.get("size_inputs")
    safe_size = (size_inputs if isinstance(size_inputs, list) and len(size_inputs) == 2
                 and all(isinstance(part, str) and re.fullmatch(r"\d{1,4}(?:\.\d{1,2})?", part)
                         for part in size_inputs) else None)
    result = {"requested_quantity": quantity, "quantity": raw.get("quantity") if isinstance(raw.get("quantity"), str) and re.fullmatch(r"\d{1,4}", raw["quantity"]) else None,
            "quantity_count": raw.get("quantity_count") if type(raw.get("quantity_count")) is int else None,
            "subtotal_count": raw.get("subtotal_count") if type(raw.get("subtotal_count")) is int else None,
            "subtotal_numeric_currency_text": safe_text,
            "subtotal_text_status": "amount_only" if safe_text is not None else "missing" if text is None or text == "" else "unsupported_text"}
    if "size_input_count" in raw or "size_inputs" in raw:
        result["size_input_count"] = raw.get("size_input_count") if type(raw.get("size_input_count")) is int else None
        result["size_inputs"] = safe_size
    return result


def bind_dom(snapshot, quote_dom, values, quantity, contract):
    if not snapshot.get("root_found") or quote_dom.get("quantity") != str(quantity):
        raise PilotError("dom_quantity_not_ready")
    if set(snapshot.get("warnings", [])) - {"field_label_from_choices"}:
        raise PilotError("unhandled_product_controls")
    fields = snapshot.get("fields", [])
    if _field(fields, label="Shipping").get("selected_values") != []:
        raise PilotError("dom_shipping_changed")
    if _field(fields, label="Email Proof").get("selected_values") != []:
        raise PilotError("dom_email_proof_changed")
    expected = expected_turnaround(quantity, contract)
    turn = _field(fields, label="Turnaround")
    if _single(turn) != expected["value"]:
        raise PilotError("dom_turnaround_not_ready")
    choices = [choice for choice in turn.get("choices", []) if choice.get("value") == expected["value"]]
    if len(choices) != 1 or choices[0].get("disabled") or choices[0].get("label") != expected["label"]:
        raise PilotError("dom_turnaround_changed")
    attrs = {item["key"]: item for item in contract["attrs"]["attrs"]}
    for key in ("hardware", "sides", "material", "polepocket", "grommet", "windslit"):
        labels = [option["label"] for option in attrs[key]["options"]
                  if option.get("key") == values[key] and option.get("visible") is not False]
        if len(labels) != 1 or _single(_field(fields, source_id="attr-" + key)) != labels[0]:
            raise PilotError("dom_option_changed")
    size = _single(_field(fields, label="Size"))
    if not re.fullmatch(re.escape(values["_size"].split("x")[0]) + r'\s*"\s*[x×]\s*' + re.escape(values["_size"].split("x")[1]) + r'\s*"', size):
        raise PilotError("dom_size_changed")
    raw = quote_dom.get("subtotal") or ""
    if not re.fullmatch(r"\$\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}", raw):
        raise PilotError("dom_subtotal_unresolved")
    return {"quantity": quantity, "subtotal": _money(raw.replace("$", "").replace(",", "").strip()),
            "turnaround": expected, "shipping": "unselected", "email_proof": False}


class B2SignPilotManager:
    def __init__(self, store, browser_pool, source_contract_dir=None, *, timeout_seconds=45):
        self.root = Path(store.root) / "pricing-pilots"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.pool = browser_pool
        self.contract = load_contract(Path(source_contract_dir) / "b2sign-pole-banner.json" if source_contract_dir else None)
        self.timeout_seconds = timeout_seconds
        self._tasks = {}
        self._closed = False
        self._lock = asyncio.Lock()

    def _write(self, report):
        target = self.root / (report["id"] + ".json")
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def get(self, pilot_id):
        if not isinstance(pilot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", pilot_id):
            raise KeyError("pilot_not_found")
        try:
            result = json.loads((self.root / (pilot_id + ".json")).read_text())
        except FileNotFoundError:
            raise KeyError("pilot_not_found") from None
        if result["status"] in ("opening", "running") and pilot_id not in self._tasks:
            result["status"] = "attention"
            result["warnings"] = sorted(set(result["warnings"] + ["pilot_interrupted"]))
        return result

    def list(self):
        paths = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:100]
        return [self.get(path.stem) for path in paths]

    async def start(self, diagnosis, quantities=None):
        quantities = list(APPROVED_QUANTITIES if quantities is None else quantities)
        if (not quantities or len(quantities) > 4
                or any(type(value) is not int or value not in APPROVED_QUANTITIES for value in quantities)
                or len(set(quantities)) != len(quantities)):
            raise PilotError("pilot_quantities_outside_approved_scope")
        values = bind_diagnosis(diagnosis, self.contract)
        async with self._lock:
            if self._closed:
                raise PilotError("pilot_manager_closed")
            if any(not task.done() for task in self._tasks.values()):
                raise PilotError("pilot_already_running")
            report = {"schema": 1, "id": uuid.uuid4().hex, "status": "opening", "created_at": _now(),
                      "updated_at": _now(), "supplier_id": "b2sign", "url": self.contract["source_url"],
                      "title": self.contract["title"], "diagnosis_id": diagnosis["id"],
                      "diagnosis_revision": diagnosis["revision"], "selection_fingerprint": _hash(diagnosis["selection"]),
                      "quantities": quantities, "values": values, "source_contract_sha256": _hash(self.contract),
                      "source_bundle_sha256": self.contract["bundle_sha256"], "quote_request_limit": len(quantities) * MAX_QUOTES_PER_PAGE,
                      "quote_requests": 0, "quotes": [], "observations": [], "warnings": [],
                      "coverage": "approved_quantity_pilot_only", "catalog_complete": False,
                      "final_eligible": False, "shipping": {"status": "held", "reason": "shipping_destination_and_method_unselected"},
                      "tax": {"status": "unresolved"}, "verification": "fresh_supplier_quote_and_dom_comparison_only"}
            self._write(report)
            task = asyncio.create_task(self._run(report), name="b2sign-pilot-" + report["id"])
            self._tasks[report["id"]] = task
            task.add_done_callback(lambda _: self._tasks.pop(report["id"], None))
            return copy.deepcopy(report)

    async def _run(self, report):
        try:
            report["status"] = "running"
            for index, quantity in enumerate(report["quantities"]):
                if index:
                    await asyncio.sleep(1)
                self._write(report)
                observation = await self._quantity(report, quantity)
                report["observations"].append(observation)
                report["updated_at"] = _now()
                self._write(report)
                if observation["status"] != "matched":
                    raise PilotError("quote_dom_math_mismatch")
            report["status"] = "completed"
            # These sampled comparisons locate changes between samples, not
            # the exact unsampled breakpoint or a reusable pricing formula.
            ordered = sorted(report["observations"], key=lambda item: item["quantity"])
            report["sample_comparisons"] = [{"from_quantity": left["quantity"], "to_quantity": right["quantity"],
                "unit_price_changed": Decimal(left["quote"]["unit_price"]) != Decimal(right["quote"]["unit_price"]),
                "production_changed": left["dom"]["turnaround"] != right["dom"]["turnaround"],
                "exact_price_break": "not_established"} for left, right in zip(ordered, ordered[1:])]
        except asyncio.CancelledError:
            report["status"] = "closed"
            report["warnings"].append("pilot_cancelled")
            raise
        except PilotError as exc:
            report["status"] = "attention"
            report["warnings"].append(str(exc))
        except Exception:
            report["status"] = "error"
            report["warnings"].append("pilot_browser_error")
        finally:
            report["updated_at"] = _now()
            report["warnings"] = sorted(set(report["warnings"]))
            self._write(report)

    async def _quantity(self, report, quantity):
        contract, values = self.contract, report["values"]
        url = contract["source_url"] + "?" + urlencode({**values, "_qty": str(quantity)})
        async with self.pool.page(ORIGIN, headless=False) as page:
            captured = []
            pending = set()
            request_contexts = {}
            fatal = []
            bundle_verified = False
            item_validated = False
            document_id = None
            page_quotes = 0
            vetted = set()

            async def guard(route):
                nonlocal page_quotes
                request = route.request
                parsed = urlsplit(request.url)
                same = parsed.scheme == "https" and parsed.hostname == "www.b2sign.com" and parsed.port in (None, 443)
                if not same or parsed.username is not None or parsed.password is not None:
                    if request.is_navigation_request():
                        fatal.append("unexpected_navigation_or_login_required")
                    return await route.abort()
                authority = (parsed.scheme, parsed.netloc)
                if authority not in vetted:
                    try:
                        await validate_url(request.url)
                    except Exception:
                        fatal.append("source_origin_validation_failed")
                        return await route.abort()
                    vetted.add(authority)
                if request.method in ("GET", "HEAD"):
                    if request.is_navigation_request() and parsed.path != "/pole-banner-set":
                        fatal.append("unexpected_navigation_or_login_required")
                        return await route.abort()
                    if parsed.path != "/pole-banner-set" and not parsed.path.startswith(("/build/assets/", "/assets/", "/image/")):
                        return await route.abort()
                    return await route.continue_()
                if not same or request.method != "POST" or parsed.path != "/item/quote" or parsed.query:
                    # Includes telemetry writes, cart, checkout, uploads and any
                    # unreviewed pricing route. None is required for this pilot.
                    return await route.abort()
                page_quotes += 1
                report["quote_requests"] += 1
                if page_quotes > MAX_QUOTES_PER_PAGE:
                    fatal.append("quote_request_budget_exhausted")
                    return await route.abort()
                try:
                    safe = sanitize_request(request.post_data_json, contract, quantity, values)
                except PilotError as exc:
                    fatal.append(str(exc))
                    return await route.abort()
                except Exception:
                    fatal.append("quote_request_unreadable")
                    return await route.abort()
                safe.update(sequence=report["quote_requests"], requested_at=_now())
                request_contexts[request] = safe
                return await route.continue_()

            async def receive(response):
                nonlocal bundle_verified
                try:
                    if response.url == contract["bundle_url"] and response.status == 200:
                        bundle_verified = hashlib.sha256(await response.body()).hexdigest() == contract["bundle_sha256"]
                        if not bundle_verified:
                            fatal.append("source_bundle_changed")
                        return
                    safe = request_contexts.pop(response.request, None)
                    if safe is None:
                        return
                    evidence = {"id": uuid.uuid4().hex, "route": "/item/quote", "http_status": response.status,
                                "request": safe, "received_at": _now(), "body_scope": "allowlisted_product_quote_fields"}
                    if response.status != 200:
                        evidence["reason"] = "quote_http_error"
                    else:
                        try:
                            # Only this projection is retained. Inertia props
                            # can also contain customer/address/debug data.
                            body = await response.json()
                            evidence["schema"] = quote_schema(body)
                            evidence["quote"] = sanitize_quote(body, quantity)
                        except PilotError as exc:
                            evidence["reason"] = str(exc)
                        except Exception:
                            evidence["reason"] = "quote_response_unreadable"
                    captured.append(evidence)
                    report["quotes"].append(evidence)
                    self._write(report)
                except Exception:
                    fatal.append("quote_capture_failed")

            def on_response(response):
                task = asyncio.create_task(receive(response))
                pending.add(task)
                task.add_done_callback(pending.discard)

            await page.route("**/*", guard)
            await page.add_init_script(PUBLIC_ITEM_INIT_JS)
            page.on("response", on_response)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                deadline = asyncio.get_running_loop().time() + self.timeout_seconds
                previous = None
                stable_since = None
                last_reason = "fresh_configuration_quote_unavailable"
                while asyncio.get_running_loop().time() < deadline:
                    if fatal:
                        raise PilotError(fatal[0])
                    if page.is_closed():
                        raise PilotError("pilot_page_closed")
                    latch = await page.evaluate(PUBLIC_ITEM_LATCH_JS)
                    if latch:
                        if document_id is None:
                            document_id = latch["document_id"]
                        elif document_id != latch["document_id"]:
                            raise PilotError("pilot_document_changed")
                    item = await page.evaluate(PUBLIC_ITEM_JS)
                    for observed_item in [item, (latch or {}).get("first"), (latch or {}).get("last")]:
                        if observed_item is None:
                            continue
                        if (observed_item.get("id") != contract["item_id"]
                                or observed_item.get("url") != "/pole-banner-set"
                                or contract_projection(observed_item.get("attrs")) != contract["attrs"]):
                            raise PilotError("live_product_contract_changed")
                        item_validated = True
                    raw_dom = await page.evaluate(QUOTE_DOM_JS)
                    report["last_dom_diagnostic"] = safe_dom_diagnostics(raw_dom, quantity)
                    if raw_dom.get("login_required"):
                        raise PilotError("supplier_login_required")
                    bound_captures = [capture for capture in captured if capture["request"]["configuration_bound"]]
                    newest_bound = max(bound_captures, key=lambda capture: capture["request"]["sequence"], default=None)
                    readiness = {"item_current_present": item is not None, "item_contract_validated": item_validated,
                                 "item_bootstrap_captured": bool(latch and latch.get("first")),
                                 "bundle_verified": bundle_verified, "dom_bound": False, "busy": None,
                                 "pending_response_tasks": len(pending), "outstanding_quote_requests": len(request_contexts),
                                 "bound_quote_count": len(bound_captures), "latest_bound_quote_accepted": bool(newest_bound and newest_bound.get("quote")),
                                 "stable_for_ms": 0}
                    report["last_readiness_diagnostic"] = readiness
                    try:
                        snapshot = await inspect_b2sign(page)
                        readiness["busy"] = bool(snapshot.get("busy"))
                        dom = bind_dom(snapshot, raw_dom, values, quantity, contract)
                    except PilotError as exc:
                        previous = None
                        last_reason = str(exc)
                        readiness["waiting_for"] = last_reason
                        await asyncio.sleep(0.1)
                        continue
                    readiness["dom_bound"] = True
                    # A newer rejected response must not let an older valid
                    # quote become the apparent final source observation.
                    eligible = [newest_bound] if newest_bound and newest_bound.get("quote") else []
                    if not item_validated or not bundle_verified or not eligible or snapshot.get("busy"):
                        if not item_validated:
                            last_reason = "public_product_contract_unavailable"
                        elif not bundle_verified:
                            last_reason = "source_bundle_verification_unavailable"
                        elif not eligible:
                            last_reason = newest_bound.get("reason", "fresh_configuration_quote_unavailable") if newest_bound else "fresh_configuration_quote_unavailable"
                        else:
                            last_reason = "source_quote_panel_busy"
                        readiness["waiting_for"] = last_reason
                        previous = None
                        await asyncio.sleep(0.1)
                        continue
                    latest = max(eligible, key=lambda capture: capture["request"]["sequence"])
                    state = _canonical([dom, latest["id"]])
                    if previous != state:
                        previous, stable_since = state, asyncio.get_running_loop().time()
                    readiness["stable_for_ms"] = int((asyncio.get_running_loop().time() - stable_since) * 1000)
                    if readiness["stable_for_ms"] >= 600 and not pending and not request_contexts:
                        quote = latest["quote"]
                        derived = (Decimal(quote["unit_price"]) * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        matched = Decimal(dom["subtotal"]) == Decimal(quote["subtotal"]) == derived
                        if "subjob_prices" in quote:
                            matched = matched and Decimal(quote["subjob_prices"][0]) == Decimal(quote["subtotal"])
                        readiness["waiting_for"] = None
                        return {"quantity": quantity, "status": "matched" if matched else "mismatch",
                                "quote": quote, "dom": dom, "evidence_id": latest["id"],
                                "source_context_sha256": _hash(latest["request"]),
                                "display_formula": "unit_price * quantity (shipping unselected, regular production)",
                                "calculated_display_subtotal": format(derived, ".2f"),
                                "currency": "USD", "currency_basis": "supplier_US_store_dollar_display",
                                "final_eligible": False}
                    if request_contexts:
                        last_reason = "quote_requests_incomplete"
                    elif pending:
                        last_reason = "quote_response_capture_pending"
                    else:
                        last_reason = "quote_dom_stability_pending"
                    readiness["waiting_for"] = last_reason
                    await asyncio.sleep(0.1)
                bound_captures = [capture for capture in captured if capture["request"]["configuration_bound"]]
                newest_bound = max(bound_captures, key=lambda capture: capture["request"]["sequence"], default=None)
                if newest_bound and newest_bound.get("reason"):
                    last_reason = newest_bound["reason"]
                if not bundle_verified:
                    last_reason = "source_bundle_verification_unavailable"
                raise PilotError(last_reason)
            finally:
                page.remove_listener("response", on_response)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    async def close(self):
        self._closed = True
        active_ids = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        # A task cancelled before its coroutine first executes has no finally
        # block opportunity; its persisted opening report still needs closure.
        for pilot_id in active_ids:
            report = self.get(pilot_id)
            if report["status"] == "attention" and "pilot_interrupted" in report["warnings"]:
                report["status"] = "closed"
                report["warnings"] = [warning for warning in report["warnings"] if warning != "pilot_interrupted"] + ["pilot_cancelled"]
                report["updated_at"] = _now()
                self._write(report)
