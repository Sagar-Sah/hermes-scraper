"""One-page, user-operated product diagnosis. No traversal or price verification.

Only duplicate pages belong to this manager. Browser contexts/profiles belong to
BrowserSessionPool; neither credentials nor browser storage are inspected here.
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
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .transport import RateLimiter, validate_url
from .b2sign_diagnosis import inspect_b2sign, merge_b2sign_snapshot
from .dps_pilot import DPS_DOM_JS
from .price_contract import CONTROLLER_PRICE_STATE_JS, SOURCE_CONTRACT, calculate_source_total
from .supplier_registry import SupplierRegistry


SUPPLIERS = {
    "printograph": {"name": "Printograph", "origin": "https://www.printograph.com", "entry_url": "https://www.printograph.com/user/login"},
    "b2sign": {"name": "B2Sign", "origin": "https://www.b2sign.com", "entry_url": "https://www.b2sign.com/"},
    "soardist": {"name": "Soardist", "origin": "https://soardistusa.com", "entry_url": "https://soardistusa.com/"},
    "fourover": {"name": "4over", "origin": "https://4over.com", "entry_url": "https://4over.com/customer/account/login/"},
    "printitprints": {"name": "Print It Prints", "origin": "https://printitprints.com", "entry_url": "https://printitprints.com/wp-admin/"},
}

# This script reads only rendered product content and finite/numeric controls.
# No hidden/text/password/file values, HTML, account identity, or storage APIs.
DIAGNOSIS_JS = r"""() => {
  const clean = (s, n=240) => String(s || '').replace(/\s+/g, ' ').trim().slice(0,n);
  const visible = e => !!e && !e.closest('[hidden],[aria-hidden="true"]') &&
    getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden' &&
    e.getClientRects().length > 0;
  const text = e => clean(e?.innerText || e?.textContent);
  const explicitLogout = [...document.querySelectorAll('a,button,input[type="submit"]')].some(e =>
    visible(e) && /^(?:log\s*out|sign\s*out)$/i.test(text(e) || e.getAttribute('aria-label') || e.value || ''));
  const explicitAccount = [...document.querySelectorAll('[data-authenticated="true"],[data-logged-in="true"]')]
    .some(e => visible(e) && /account|dashboard|signed in/i.test(text(e)));
  const login = [...document.querySelectorAll('input[type="password"]')].some(e => visible(e) &&
    !!e.closest('form,[role="dialog"],main'));
  const session_status = login ? 'login_required' : explicitLogout || explicitAccount ? 'signed_in' : 'unknown';
  const scopeSelectors = ['#order-form','#product_calculator_display','form.variations_form',
    '[data-pirc]','[data-pigf]','[data-piso]','.pirc-configurator','.pigf-configurator',
    '[id^="product-"]','.product .summary'];
  let roots = [...document.querySelectorAll(scopeSelectors.join(','))].filter(visible);
  if (!roots.length) roots = [document.querySelector('main') || document.querySelector('article')].filter(Boolean);
  const excluded = /(?:password|username|email|e-mail|login|log in|sign in|search|newsletter|subscribe|coupon|promo code|credit card|card number|payment|billing|upload|artwork file|captcha|csrf|token|verification code|security code|one.time|two.factor|\botp\b|\bpin\b|agree|privacy|terms)/i;
  const localLabel = e => {
    const linked = clean([...e.labels || []].map(text).join(' '));
    if (linked) return linked;
    const labelled = clean((e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>text(document.getElementById(id))).join(' '));
    if (labelled) return labelled;
    if (e.getAttribute('aria-label')) return clean(e.getAttribute('aria-label'));
    const wrap = e.closest('.form-group,.field,.form-row,.control-group,[data-field]');
    return text(wrap?.querySelector('label'));
  };
  const controls = [...new Set(roots.flatMap(r=>[...r.querySelectorAll('select,input[type="radio"],input[type="checkbox"],input[type="number"]')]))];
  const fields = [], groups = new Map();
  let omitted = 0;
  for (const e of controls) {
    if ((!visible(e) && !(['radio','checkbox'].includes(e.type) && [...e.labels || []].some(visible))) || e.closest('header,footer,nav,[role="search"],form[role="search"]')) continue;
    const form = e.closest('form');
    if (form?.querySelector('input[type="password"]')) continue;
    const kind = e.tagName === 'SELECT' ? 'select' : e.type;
    const ownLabel = localLabel(e);
    const container = e.closest('fieldset,.form-group,.field,.form-row,.control-group,[data-field]');
    const heading = container?.querySelector('legend,h2,h3,h4,h5,h6,.control-label,.field-label');
    const legend = visible(heading) ? text(heading) : '';
    // Printograph gives each foil-color radio a numbered name even though they
    // form one mutually exclusive customer choice.
    const foilGroup = kind === 'radio' && /^raisedFoilColor\d+$/.test(e.name || '') && !!e.closest('#order-form');
    const sourceName = foilGroup ? 'raisedFoilColor' : e.name || '';
    const label = foilGroup ? 'Foil Colors' : (kind === 'radio' || kind === 'checkbox') ? legend || ownLabel : ownLabel;
    if (!label || excluded.test([label,e.name,e.id,form?.getAttribute('action')].join(' '))) { omitted++; continue; }
    const group = (kind === 'radio' || kind === 'checkbox') && (sourceName || e.closest('fieldset'));
    const groupKey = group ? kind + ':' + (sourceName || legend) : null;
    if (groupKey && groups.has(groupKey)) {
      const field = groups.get(groupKey);
      if (field.choices.length < 500) field.choices.push({value:e.value,label:ownLabel || clean(e.value),disabled:e.matches(':disabled')});
      if (e.checked) field.selected_values.push(e.value);
      field.required ||= !!e.required;
      field.disabled &&= e.matches(':disabled');
      continue;
    }
    if (fields.length >= 200) { omitted++; continue; }
    const field = {label,kind,choices:[],selected_values:[],required:!!e.required,disabled:e.matches(':disabled'),multiple:kind==='checkbox'||kind==='select'&&e.multiple,
      configurable:true,source_key:sourceName || e.id || label,source_name:sourceName,source_id:e.id || ''};
    if (kind === 'select') {
      field.choices = [...e.options].slice(0,500).map(o=>({value:o.value,label:clean(o.label || o.textContent),disabled:!!o.disabled || !!o.closest('optgroup')?.disabled}));
      field.selected_values = [...e.selectedOptions].slice(0,500).map(o=>o.value);
      if (e.options.length > 500) omitted++;
    } else if (kind === 'number') {
      const numeric = s => /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?$/i.test(s || '') && Number.isFinite(Number(s));
      field.selected_values = numeric(e.value) ? [e.value] : [];
      for (const key of ['min','max','step']) {
        const v=e.getAttribute(key); field[key]=(numeric(v) || key==='step' && v==='any') ? v : null;
      }
    } else {
      field.choices = [{value:e.value,label:ownLabel || clean(e.value),disabled:e.matches(':disabled')}];
      if (e.checked) field.selected_values = [e.value];
    }
    fields.push(field); if (groupKey) groups.set(groupKey,field);
  }
  const turnaround = [...document.querySelectorAll('#radioBtn a.turnAround[data-toggle="turnaround"][data-title]')].filter(visible);
  if (turnaround.length && document.querySelector('#order-form')) {
    const section = turnaround[0].closest('.form-group,.row,section');
    const heading = [...(section?.querySelectorAll('h2,h3,h4,h5,h6,label,.control-label,.field-label,.customheaderlabel') || [])].find(visible);
    const label = text(heading);
    if (label) fields.push({label,kind:'radio',source_key:'productionTime',source_name:'productionTime',source_id:'radioBtn',
      required:false,multiple:false,disabled:turnaround.every(e=>e.getAttribute('aria-disabled')==='true'||e.classList.contains('disabled')),
      selected_values:turnaround.filter(e=>e.classList.contains('active')).map(e=>e.getAttribute('data-title')),
      choices:turnaround.map(e=>({value:e.getAttribute('data-title'),label:text(e),disabled:e.getAttribute('aria-disabled')==='true'||e.classList.contains('disabled')||/\bnot\s+available\b/i.test(text(e))}))});
    else omitted++;
  }
  const product = document.querySelector('[id^="product-"],main,article');
  const titleElement = product?.querySelector('h1.product_title,h1') || document.querySelector('h1');
  const title = visible(titleElement) ? text(titleElement) : '';
  const descriptionElement = [...document.querySelectorAll('.woocommerce-product-details__short-description,#tab-description,[itemprop="description"],.product-description')].find(visible);
  const description = descriptionElement ? clean(descriptionElement.innerText,8000) : '';
  const imageElements = product ? [...product.querySelectorAll('.woocommerce-product-gallery img,[itemprop="image"],.product-image img,.product-images img')].filter(visible) : [];
  const images = imageElements.slice(0,50).map(e=>({url:e.currentSrc || e.src || '',alt:clean(e.alt)}));
  const busy = [...document.querySelectorAll('#ajaxLoader,[aria-busy="true"],.loading-overlay,.loading-mask')].some(visible);
  const price_login_required = [...document.querySelectorAll('a,span,p,label,div')].some(e =>
    visible(e) && /(?:please\s+)?(?:log|sign)\s*in\s+to\s+(?:view|see|access)\s+(?:the\s+)?(?:price|prices|pricing)\b|unit price\s*:?\s*please\s+log\s*in/i.test(text(e)));
  return {title,fields,content:{title,description,images},session_status:price_login_required?'login_required':session_status,price_login_required,omitted,busy,document_ready:document.readyState!=='loading'};
}"""


_ID = re.compile(r"^[a-f0-9]{32}$")
_BLOCKED_PATH = re.compile(r"(?:^|/)(?:wp-admin|wp-login\.php|login|log-in|signin|sign-in|register|checkout|cart|shopping-cart(?:\.html)?|account|my-account|payment|orders?)(?:/|$)", re.I)
_BLOCKED_QUERY = re.compile(r"(?:password|passwd|token|secret|session|nonce|auth|key|checkout|add.?to.?cart|action|redirect|return.?url)", re.I)


def _blocked_product_path(path):
    return bool(_BLOCKED_PATH.search(path)) and not bool(re.fullmatch(r"/products/[a-zA-Z0-9_-]+/order/?", path))


def _public_url(url):
    """Queries/fragments may contain session artifacts; never persist them."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def _supplier_for(url):
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return next((key for key, item in SUPPLIERS.items() if urlsplit(item["origin"]).hostname.removeprefix("www.") == host), None)


def _printograph_bundle_key(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "static.printograph.com":
        return None
    if parts.path.endswith("/orderForm.js"):
        return "order_form_sha256"
    if parts.path.endswith("/chunk-common.js"):
        return "common_sha256"
    return None


def _loaded_bundle_contract(observed):
    """Only one actually loaded hash per required public script can match."""
    return {key: next(iter(values)) for key, values in observed.items()
            if key in SOURCE_CONTRACT and len(values) == 1}


def _is_dps(url):
    return (urlsplit(url).hostname or "").removeprefix("www.") == "digital-print-solutions.com"


def merge_dps_snapshot(raw, calculator):
    """Project the visible DPS calculator into the same reviewable field model.

    This is a current-page diagnosis only. Redirecting choices and conditional
    option trees still require the native calculator before any harvest.
    """
    raw = dict(raw)
    fields = list(raw.get("fields") or [])
    seen = {field.get("source_id") for field in fields}
    for key, label in (("width", "Width"), ("height", "Height"), ("quantity", "Quantity")):
        control = calculator.get(key)
        if not isinstance(control, dict) or control.get("kind") not in {"select", "number"}:
            continue
        source_id = control.get("id")
        if source_id in seen:
            continue
        field = {"label": label, "kind": control["kind"], "source_key": source_id,
                 "source_name": "", "source_id": source_id, "choices": [],
                 "selected_values": [str(control["value"])] if control.get("value") not in (None, "") else [],
                 "required": True, "disabled": bool(control.get("disabled")), "multiple": False,
                 "configurable": True}
        if control["kind"] == "select":
            field["choices"] = [{"value": str(choice.get("value", "")),
                                  "label": choice.get("label", ""), "disabled": bool(choice.get("disabled"))}
                                 for choice in control.get("choices", [])]
        else:
            field.update(min=control.get("minimum"), max=control.get("maximum"), step=control.get("step"))
        fields.append(field)
        seen.add(source_id)
    for option in calculator.get("options") or []:
        source_id = "dps-option-" + str(option.get("id", ""))
        if source_id in seen or not option.get("label"):
            continue
        choices = [{"value": str(choice.get("value", "")), "label": choice.get("label", ""),
                    "disabled": bool(choice.get("disabled") or choice.get("redirects"))}
                   for choice in option.get("choices", [])]
        fields.append({"label": option["label"], "kind": "select", "source_key": source_id,
                       "source_name": "", "source_id": source_id, "choices": choices,
                       "selected_values": [str(option["value"])] if option.get("value") not in (None, "") else [],
                       "required": True, "disabled": bool(option.get("disabled")), "multiple": False,
                       "configurable": True})
        seen.add(source_id)
    raw["fields"] = fields
    if calculator.get("login_prompt"):
        raw["price_login_required"] = True
    return raw


def _safe_error(exc):
    code = getattr(exc, "code", "")
    if code in {"invalid_url", "network_error", "browser_profile_in_use", "browser_unavailable"}:
        return code
    if isinstance(exc, asyncio.TimeoutError) or type(exc).__name__ == "TimeoutError":
        return "page_timeout"
    return "page_unavailable"


def normalize_snapshot(raw):
    """Bound the public projection; generated source IDs never become labels."""
    def clean(value, limit=240):
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    fields, keys, unsupported = [], set(), False
    for field in raw.get("fields", [])[:200]:
        if field.get("kind") not in {"select", "custom_select", "static", "radio", "checkbox", "number"}:
            continue
        label = clean(field.get("label"))
        if not label:
            continue
        raw_values = [field.get("source_name", ""), field.get("source_id", ""), field.get("source_key", "")] + [c.get("value", "") for c in field.get("choices", [])[:500]] + list(field.get("selected_values", [])[:500])
        if any(not isinstance(v, str) or len(v) > 2048 for v in raw_values):
            unsupported = True
            continue
        source = field.get("source_key", "")
        key = hashlib.sha256(f"{field['kind']}\0{source}\0{label}".encode()).hexdigest()[:20]
        if key in keys:
            key = hashlib.sha256(f"{key}\0{len(fields)}".encode()).hexdigest()[:20]
        keys.add(key)
        choices = [{"value": c.get("value", ""), "label": clean(c.get("label")), "disabled": bool(c.get("disabled"))} for c in field.get("choices", [])[:500]]
        selected = list(field.get("selected_values", [])[:500])
        out = {"key": key, "label": label, "kind": field["kind"], "choices": choices,
               "selected_values": selected, "required": bool(field.get("required")),
               "disabled": bool(field.get("disabled")), "multiple": field.get("multiple") is True or field["kind"] == "checkbox", "configurable": True,
               "source_name": field.get("source_name", ""), "source_id": field.get("source_id", "")}
        for metadata, allowed in (("binding_kind", {"display_label_only", "native_control"}),
                                  ("value_origin", {"display_label_surrogate", "native_value"})):
            if field.get(metadata) in allowed:
                out[metadata] = field[metadata]
        if field.get("warnings") == ["field_label_from_choices"]:
            out["warnings"] = ["field_label_from_choices"]
        if out["kind"] == "number":
            def numeric(value):
                return isinstance(value, str) and len(value) < 80 and bool(re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", value)) and abs(float(value)) != float("inf")
            out["selected_values"] = [v for v in selected if numeric(v)]
            for attr in ("min", "max", "step"):
                value = field.get(attr)
                out[attr] = value if numeric(value) or attr == "step" and value == "any" else None
        else:
            values = {choice["value"] for choice in choices}
            out["selected_values"] = [v for v in selected if v in values]
        fields.append(out)
    content = raw.get("content") or {}
    title = clean(raw.get("title"))
    images = []
    for item in content.get("images", [])[:50]:
        try:
            url = urlsplit(str(item.get("url", "")))
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                continue
            images.append({"url": _public_url(url.geturl()), "alt": clean(item.get("alt"))})
        except ValueError:
            continue
    content = {"title": title, "description": clean(content.get("description"), 8000), "images": images}
    for key, label in (("title", "Product title"), ("description", "Description"), ("images", "Images")):
        if content[key]:
            fields.append({"key": f"content_{key}", "label": label, "kind": "content", "choices": [], "selected_values": [], "required": False, "disabled": False, "multiple": False, "configurable": False})
    status = raw.get("session_status")
    warnings = ["diagnosis_limits_reached"] if raw.get("omitted") or unsupported else []
    if raw.get('price_login_required') is True:
        warnings.append('supplier_price_login_required')
    warnings += [w for w in raw.get("source_warnings", []) if w in {
        "field_label_from_choices", "custom_dropdown_choices_unavailable", "custom_dropdown_labels_ambiguous",
        "custom_dropdown_inspection_failed", "custom_dropdown_close_failed", "custom_dropdown_selection_changed",
        "custom_dropdown_inspection_incomplete", "unsupported_visible_attribute"}]
    return {"title": title, "fields": fields, "content": content,
            "pricing_access": "login_required" if raw.get('price_login_required') is True else "not_established",
            "session_status": status if status in {"signed_in", "login_required", "unknown"} else "unknown",
            "warnings": warnings}


class ProductDiagnosisManager:
    def __init__(self, store, browser_pool):
        self.store, self.pool = store, browser_pool
        self.registry = SupplierRegistry(store.root, SUPPLIERS)
        self.pool.origin_resolver = self.registry.canonical_origin
        self.root = Path(store.root) / "diagnoses"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._records, self._pages, self._leases, self._tasks = {}, {}, {}, {}
        self._choice_limiter = RateLimiter(store)
        self._supplier_sessions = {}
        self._page_hosts = {}
        self._bundle_hashes, self._bundle_tasks = {}, {}
        self._locks = {}
        self._lock = asyncio.Lock()
        self._diagnosis_slots = asyncio.Semaphore(3)
        self._closed = False

    def _path(self, diagnosis_id):
        if not _ID.fullmatch(str(diagnosis_id)):
            raise ValueError("invalid_diagnosis_id")
        return self.root / f"{diagnosis_id}.json"

    def _persist(self, record):
        target = self._path(record["id"])
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as stream:
                os.chmod(temp, 0o600)
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def get(self, diagnosis_id):
        path = self._path(diagnosis_id)
        if diagnosis_id not in self._records:
            if not path.exists():
                raise KeyError("diagnosis_not_found")
            record = json.loads(path.read_text())
            record["status"] = "closed"
            record["session_status"] = "closed"
            record["warnings"] = list(dict.fromkeys(record.get("warnings", []) + ["page_not_open"]))
            self._records[diagnosis_id] = record
        return copy.deepcopy(self._records[diagnosis_id])

    def suppliers(self):
        return [{"origin": item["origin"], "entry_url": item["entry_url"],
                 "website_url": item["website_url"], "custom": item["custom"],
                 **copy.deepcopy(self._supplier_sessions.get(key) or {"id": key, "supplier_id": key, "name": item["name"], "url": item["entry_url"], "status": "not_open", "session_status": "not_open"})}
                for key, item in self.registry.items().items()]

    async def add_supplier(self, name, website_url, login_url=None):
        if self._closed:
            raise ValueError("diagnosis_manager_closed")
        item = await self.registry.add(name, website_url, login_url)
        return next(card for card in self.suppliers() if card["id"] == item["id"])

    async def remove_supplier(self, key):
        if self._closed:
            raise ValueError("diagnosis_manager_closed")
        item = self.registry.get(key)
        if not item["custom"]:
            raise ValueError("builtin_supplier")
        await self.registry.remove(key)
        owner = f"supplier:{key}"
        async with self._locks.setdefault(owner, asyncio.Lock()):
            await self._release(owner)
        self._supplier_sessions.pop(key, None)

    def list(self):
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        records = []
        for path in paths:
            try:
                records.append(self.get(path.stem))
            except (ValueError, KeyError, OSError):
                continue
        return records

    async def _validated_target(self, url, *, login=False):
        try:
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.fragment or not parts.hostname or parts.username or parts.password:
                raise ValueError("invalid_product_url")
            from urllib.parse import parse_qsl
            if not login and (_blocked_product_path(parts.path) or any(_BLOCKED_QUERY.search(k) for k, _ in parse_qsl(parts.query))):
                raise ValueError("product_page_required")
            await validate_url(url)
        except ValueError:
            raise ValueError("invalid_product_url") from None
        supplier = self.registry.supplier_for(url)
        origin = self.registry.get(supplier)["origin"] if supplier else f"https://{parts.hostname}"
        host = urlsplit(origin).hostname
        hosts = {host, host.removeprefix("www.")} if host.startswith("www.") else {host, f"www.{host}"}
        # Only vet the counterpart when it is actually requested; unrelated
        # source/CDN hosts still undergo the same public-address validation.
        return origin, hosts, supplier

    async def _new_page(self, owner, origin, hosts):
        lease = self.pool.page(origin, headless=False)
        page = await lease.__aenter__()
        if self._closed:
            await lease.__aexit__(None, None, None)
            raise ValueError("diagnosis_manager_closed")
        self._pages[owner], self._leases[owner] = page, lease
        self._page_hosts[owner] = hosts
        vetted = set()

        def closed():
            if self._pages.get(owner) is not page:
                return
            if owner in self._records:
                record = self._records[owner]
                record.update(status="closed", session_status="closed")
                self._persist(record)
            elif owner.startswith("supplier:"):
                supplier = self._supplier_sessions.get(owner.split(":", 1)[1])
                if supplier:
                    supplier.update(status="closed", session_status="closed")
        page.on("close", closed)
        if origin == SUPPLIERS["printograph"]["origin"] and owner in self._records:
            bundle_hashes = self._bundle_hashes[owner] = {}
            bundle_tasks = self._bundle_tasks[owner] = set()

            async def hash_loaded_bundle(response):
                try:
                    key = _printograph_bundle_key(response.url)
                    if key and response.status == 200:
                        digest = hashlib.sha256(await response.body()).hexdigest()
                        bundle_hashes.setdefault(key, set()).add(digest)
                except Exception:
                    pass

            def queue_bundle_hash(response):
                if _printograph_bundle_key(response.url) is None:
                    return
                task = asyncio.create_task(hash_loaded_bundle(response))
                bundle_tasks.add(task)
                task.add_done_callback(bundle_tasks.discard)

            page.on("response", queue_bundle_hash)

        async def guard(route):
            request = route.request
            try:
                p = urlsplit(request.url)
                if p.scheme not in {"http", "https"}:
                    await route.abort()
                    return
                if request.is_navigation_request() and request.frame == page.main_frame:
                    if p.scheme != "https" or p.hostname not in hosts:
                        await route.abort()
                        return
                authority = (p.scheme, p.netloc)
                if authority not in vetted:
                    await validate_url(request.url)
                    vetted.add(authority)
                await route.continue_()
            except Exception:
                await route.abort()

        await page.route("**/*", guard)
        return page

    async def open_supplier(self, key):
        item = self.registry.get(key)
        async with self._locks.setdefault(f"supplier:{key}", asyncio.Lock()):
            if self._closed:
                raise ValueError("diagnosis_manager_closed")
            owner = f"supplier:{key}"
            page = self._pages.get(owner)
            try:
                if page is None or page.is_closed():
                    await self._release(owner)
                    origin, hosts, _ = await self._validated_target(item["entry_url"], login=True)
                    page = await self._new_page(owner, origin, hosts)
                    await page.goto(item["entry_url"], wait_until="domcontentloaded", timeout=45000)
                await page.bring_to_front()
                return await self.check_supplier(key)
            except Exception as exc:
                self._supplier_sessions[key] = {"id": key, "supplier_id": key, "name": item["name"], "url": item["entry_url"], "origin": item["origin"], "entry_url": item["entry_url"], "website_url": item["website_url"], "custom": item["custom"], "status": "attention", "session_status": "unknown", "warnings": [_safe_error(exc)]}
                return copy.deepcopy(self._supplier_sessions[key])

    async def check_supplier(self, key):
        item = self.registry.get(key)
        page = self._pages.get(f"supplier:{key}")
        result = {"id": key, "supplier_id": key, "name": item["name"], "url": item["entry_url"], "origin": item["origin"], "entry_url": item["entry_url"], "website_url": item["website_url"], "custom": item["custom"]}
        if page is None or page.is_closed():
            result.update(status="closed" if key in self._supplier_sessions else "not_open", session_status="closed" if key in self._supplier_sessions else "not_open")
        elif self.registry.supplier_for(page.url) != key:
            result.update(status="attention", session_status="unknown", warnings=["unexpected_source_origin"])
        else:
            try:
                raw = await page.evaluate(DIAGNOSIS_JS)
                status = normalize_snapshot(raw)["session_status"]
                result.update(status=status, session_status=status, url=_public_url(page.url))
            except Exception as exc:
                result.update(status="attention", session_status="unknown", warnings=[_safe_error(exc)])
        self._supplier_sessions[key] = result
        return copy.deepcopy(result)

    async def start(self, url):
        if self._closed:
            raise ValueError("diagnosis_manager_closed")
        origin, hosts, supplier = await self._validated_target(url)
        diagnosis_id = uuid.uuid4().hex
        record = {"id": diagnosis_id, "url": _public_url(url), "title": "", "status": "opening", "revision": 0,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "fields": [], "content": {}, "warnings": [], "coverage": "current_page_only",
                  "supplier_id": supplier, "session_status": "unknown", "selection": None}
        async with self._lock:
            self._records[diagnosis_id] = record
            self._persist(record)
            self._tasks[diagnosis_id] = asyncio.create_task(self._open_diagnosis(diagnosis_id, url, origin, hosts))
        return copy.deepcopy(record)

    async def _open_diagnosis(self, diagnosis_id, url, origin, hosts):
        async with self._diagnosis_slots:
            await self._open_diagnosis_in_slot(diagnosis_id, url, origin, hosts)

    async def _open_diagnosis_in_slot(self, diagnosis_id, url, origin, hosts):
        try:
            page = await self._new_page(diagnosis_id, origin, hosts)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.bring_to_front()
            async with self._lock:
                self._records[diagnosis_id]["status"] = "analyzing"
                self._persist(self._records[diagnosis_id])
            settled = await self._settle_initial(page)
            await self.refresh(diagnosis_id)
            if not settled:
                async with self._lock:
                    record = self._records[diagnosis_id]
                    record["warnings"] = list(dict.fromkeys(record["warnings"] + ["initial_snapshot_not_settled"]))
                    self._persist(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                record = self._records[diagnosis_id]
                record.update(status="attention", warnings=[_safe_error(exc)])
                self._persist(record)

    async def _settle_initial(self, page):
        """Wait only for local rendered state; never click, reload, or fetch.

        A stable snapshot is not proof of a complete dependent option domain.
        Empty/simple product pages time out with an explicit warning and remain
        available for the user's immediate Refresh action.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0
        previous = None
        placeholder = re.compile(r"^(?:[-–—]+|select(?:\s|$)|please select|choose(?:\s|$)|loading(?:\s|$))", re.I)
        while not page.is_closed():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                raw = await asyncio.wait_for(self._snapshot(page), remaining)
            except asyncio.TimeoutError:
                return False
            snapshot = normalize_snapshot(raw)
            controls = [f for f in snapshot["fields"] if f["configurable"]]
            usable = any(
                field["kind"] == "number" or any(
                    not choice["disabled"] and not placeholder.search(choice["label"])
                    for choice in field["choices"]
                ) for field in controls
            )
            ready = raw.get("document_ready") is True and not raw.get("busy") and (
                snapshot["session_status"] == "login_required" or usable
            )
            signature = json.dumps({"fields": snapshot["fields"], "title": snapshot["title"],
                                    "session_status": snapshot["session_status"]}, sort_keys=True)
            if ready and signature == previous:
                return True
            previous = signature if ready else None
            await asyncio.sleep(min(0.3, max(0, deadline - loop.time())))
        return False

    async def _snapshot(self, page, *, expand=False):
        raw = await page.evaluate(DIAGNOSIS_JS)
        if _supplier_for(page.url) == "b2sign":
            raw = merge_b2sign_snapshot(raw, await inspect_b2sign(page, expand=expand))
        elif _is_dps(page.url):
            raw = merge_dps_snapshot(raw, await page.evaluate(DPS_DOM_JS))
        return raw

    async def refresh(self, diagnosis_id):
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            page = self._pages.get(diagnosis_id)
            record = self._records[diagnosis_id]
            if page is None or page.is_closed():
                async with self._lock:
                    record.update(status="closed", session_status="closed")
                    record["warnings"] = list(dict.fromkeys(record.get("warnings", []) + ["page_not_open"]))
                    self._persist(record)
                return self.get(diagnosis_id)
            try:
                hosts = self._page_hosts[diagnosis_id]
                current = urlsplit(page.url)
                if current.scheme != "https" or current.hostname not in hosts:
                    raise ValueError("unexpected_source_origin")
                raw = normalize_snapshot(await self._snapshot(page, expand=True))
                raw["url"] = _public_url(page.url)
                raw["status"] = "login_required" if raw["session_status"] == "login_required" else "ready"
                if _blocked_product_path(current.path) and raw["session_status"] != "login_required":
                    raw.update(status="attention", fields=[], content={}, warnings=["product_page_required"])
                if not any(f["configurable"] for f in raw["fields"]) and raw["status"] != "login_required":
                    raw["warnings"].append("no_visible_configurable_controls")
                    if _supplier_for(page.url) == "b2sign" and raw["status"] != "login_required":
                        raw["status"] = "attention"
                if any(w.startswith('custom_dropdown_') or w == 'unsupported_visible_attribute' for w in raw["warnings"]):
                    raw["status"] = "attention"
                # A refreshed login page stays inspectable, but never claims a
                # verified product, price, or complete option domain.
                async with self._lock:
                    changed = any(record.get(k) != raw[k] for k in ("url", "title", "fields", "content", "session_status"))
                    if changed:
                        record["revision"] += 1
                        if record.get("selection") is not None:
                            record["selection"] = None
                            raw["warnings"].append("selection_cleared_after_page_change")
                    record.update(raw)
                    self._persist(record)
            except Exception as exc:
                async with self._lock:
                    record.update(status="attention", warnings=[_safe_error(exc)])
                    self._persist(record)
            return self.get(diagnosis_id)

    async def select_observed_choice(self, diagnosis_id, expected_revision, field_key, value):
        """Change one currently offered native dropdown choice in the open page.

        This is a bounded diagnosis action, not a pricing walk or a claim that
        the returned price covers other selections. Its source-triggered calls
        share the persisted origin limiter with the campaign workers.
        """
        self.get(diagnosis_id)
        if not isinstance(field_key, str) or not isinstance(value, str):
            raise ValueError("invalid_observed_choice")
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready" or page is None or page.is_closed():
                raise ValueError("diagnosis_not_ready")
            if record["supplier_id"] != "printograph":
                raise ValueError("observed_choice_supplier_unsupported")
            current = urlsplit(page.url)
            if current.scheme != "https" or current.hostname not in self._page_hosts[diagnosis_id]:
                raise ValueError("unexpected_source_origin")
            fields = [f for f in record["fields"] if f.get("key") == field_key]
            if len(fields) != 1 or fields[0].get("kind") not in {"select", "radio"}:
                raise ValueError("observed_choice_control_required")
            field = fields[0]
            if field.get("disabled"):
                raise ValueError("observed_choice_control_disabled")
            source_id = field.get("source_id", "")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", source_id):
                raise ValueError("observed_choice_control_id_invalid")
            choices = [c for c in field.get("choices", []) if c.get("value") == value and not c.get("disabled")]
            if not choices or value in field.get("selected_values", []):
                raise ValueError("choice_not_newly_available")
            origin = f"{current.scheme}://{current.netloc}"

            async def check():
                if self._closed or page.is_closed():
                    raise ValueError("diagnosis_not_ready")

            token = await self._choice_limiter.acquire(origin, 1, 1, check)
            started = time.monotonic()
            status = 500
            try:
                # The ID is taken from the saved rendered control, not from
                # caller input, and restricted above to a CSS-safe alphabet.
                if field["kind"] == "select":
                    await page.locator(f'select[id="{source_id}"]').select_option(value, timeout=10000)
                else:
                    name = field.get("source_name", "")
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", name) or not re.fullmatch(r"[0-9]{1,12}", value):
                        raise ValueError("observed_radio_identity_invalid")
                    selector = (f'#order-form input[type="radio"][name^="raisedFoilColor"][value="{value}"]'
                                if name == "raisedFoilColor" else
                                f'#order-form input[type="radio"][name="{name}"][value="{value}"]')
                    radio = page.locator(selector)
                    if await radio.count() != 1 or not await radio.is_enabled():
                        raise ValueError("observed_radio_unavailable")
                    await radio.evaluate('(e) => e.click()')
                # Printograph first displays the generic turnaround and then
                # recalculates finish-dependent text. Wait for that update.
                await asyncio.sleep(1.5)
                status = 200
            finally:
                await asyncio.to_thread(self._choice_limiter.release, origin, token,
                                        time.monotonic() - started, status, 30, 1)
        result = await self.refresh(diagnosis_id)
        selected = next((f.get("selected_values", []) for f in result.get("fields", [])
                         if f.get("key") == field_key), [])
        if (selected != [value] if field["kind"] == "radio" else value not in selected):
            raise ValueError("observed_choice_not_retained")
        return result

    async def read_scoped_price(self, diagnosis_id, expected_revision):
        """Read the current Printograph calculator without making a source call.

        The historical client bundle is not proven to be the loaded bundle by
        diagnosis alone, so this observation cannot certify a product or quote.
        """
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready" or page is None or page.is_closed():
                raise ValueError("diagnosis_not_ready")
            current = urlsplit(page.url)
            if record["supplier_id"] != "printograph" or current.scheme != "https" or current.hostname not in self._page_hosts[diagnosis_id]:
                raise ValueError("unexpected_source_origin")
            settled = await self._settle_initial(page)
            capture = await page.evaluate(CONTROLLER_PRICE_STATE_JS,
                                          {"source_contract": SOURCE_CONTRACT, "settled": settled})
            if not capture.get("observed"):
                return {"status": "unavailable", "reason": capture.get("reason", "controller_not_observed"),
                        "diagnosis_id": diagnosis_id, "revision": expected_revision}
            displayed = await page.evaluate("""() => {
              const root=document.querySelector('#order-form');
              const text=root?.innerText || '';
              const match=text.match(/Subtotal\\s*\\(excludes shipping\\)\\s*:\\s*(\\$[0-9,]+(?:\\.[0-9]{2})?)/i);
              return match ? match[1] : null;
            }""")
            if displayed is not None:
                capture["displayed_total"] = displayed
            reconciliation = calculate_source_total(capture)
            pending = tuple(self._bundle_tasks.get(diagnosis_id, ()))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            loaded_contract = _loaded_bundle_contract(self._bundle_hashes.get(diagnosis_id, {}))
            return {"status": "observed", "scope": "current_page_only", "diagnosis_id": diagnosis_id,
                    "revision": expected_revision,
                    "source_bundle_verified": loaded_contract == SOURCE_CONTRACT,
                    "loaded_contract": loaded_contract,
                    "capture": capture, "reconciliation": reconciliation}

    async def read_observed_gallery(self, diagnosis_id, expected_revision):
        """Read source-gallery image URLs from the current product page only."""
        self.get(diagnosis_id)
        async with self._locks.setdefault(diagnosis_id, asyncio.Lock()):
            record = self._records[diagnosis_id]
            page = self._pages.get(diagnosis_id)
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready" or page is None or page.is_closed():
                raise ValueError("diagnosis_not_ready")
            current = urlsplit(page.url)
            if (record["supplier_id"] != "fourover" or current.scheme != "https"
                    or current.hostname not in self._page_hosts[diagnosis_id]):
                raise ValueError("unexpected_source_origin")
            observed = await page.evaluate("""() => {
              const scope=document.querySelector('#maincontent') || document.querySelector('main');
              const imgs=[...(scope?.querySelectorAll('img') || [])];
              return imgs.slice(0,100).map(e=>({
                alt:String(e.alt||'').replace(/\\s+/g,' ').trim().slice(0,180),
                src:e.currentSrc || e.getAttribute('data-src') || e.src || '',
                gallery:!!e.closest('.fotorama,.gallery-placeholder,[data-gallery-role],.product.media')
              })).filter(e=>e.gallery && e.src);
            }""")
            images = []
            for image in observed:
                parsed = urlsplit(image["src"])
                if (parsed.scheme == "https" and parsed.hostname
                        and (parsed.hostname == "4over.com" or parsed.hostname.endswith(".4over.com"))):
                    item = {"url": image["src"], "alt": image["alt"]}
                    if item not in images:
                        images.append(item)
            return {"diagnosis_id": diagnosis_id, "revision": expected_revision,
                    "source_url": record["url"], "images": images[:30],
                    "scope": "current_rendered_product_gallery_only"}

    async def save_selection(self, diagnosis_id, expected_revision, selection):
        self.get(diagnosis_id)
        # The API validates selection policy. This boundary still requires a
        # finite JSON object and an exact revision under the same commit lock.
        if not isinstance(selection, dict):
            raise ValueError("invalid_selection")
        payload = json.dumps(selection, allow_nan=False)
        if len(payload) > 65536:
            raise ValueError("selection_too_large")
        async with self._lock:
            record = self._records[diagnosis_id]
            if record["revision"] != expected_revision:
                raise ValueError("diagnosis_revision_conflict")
            if record["status"] != "ready":
                raise ValueError("diagnosis_not_ready")
            record["selection"] = json.loads(payload)
            self._persist(record)
            return copy.deepcopy(record)

    async def _release(self, owner):
        self._pages.pop(owner, None)
        self._page_hosts.pop(owner, None)
        tasks = self._bundle_tasks.pop(owner, set())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bundle_hashes.pop(owner, None)
        lease = self._leases.pop(owner, None)
        if lease is not None:
            try:
                await lease.__aexit__(None, None, None)
            except Exception:
                pass

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for owner in list(self._leases):
            await self._release(owner)
        async with self._lock:
            for record in self._records.values():
                record.update(status="closed", session_status="closed")
                self._persist(record)
            for result in self._supplier_sessions.values():
                result.update(status="closed", session_status="closed")
