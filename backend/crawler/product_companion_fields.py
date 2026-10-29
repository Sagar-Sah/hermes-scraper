"""Evidence-backed purchase-field overlap checks; never removes controls.

CSV schema uniqueness cannot detect two independent customer quantity widgets.
These observations retain DOM identity, purchase/side/set scope and visibility.
Only concurrent editable controls with the same binding are a proven conflict.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re

from bs4 import BeautifulSoup


SCHEMA = "hermes-companion-field-inventory/v1"
MAX_NODES = 256
ROOTS = "form.cart,form.variations_form,#product_addtocart_form,[data-product-calculator],#product_calculator_display,.product_calculator_display,.pirc-options,.pigf-builder"
LAYER_SCOPE_ATTRIBUTES = ("data-layer", "data-layer-id", "data-layer-key", "data-artwork-layer", "data-artwork-layer-id")
SCOPE_ATTRIBUTES = ("data-side", "data-position", "data-location", "data-artwork-side", "data-set-id", "data-set", "data-artwork-set", "data-job", "data-variation-id", "data-variation-row") + LAYER_SCOPE_ATTRIBUTES
OWNER_REGIONS = "[id^=product-],[data-product_id],[data-product-id],.product"
NON_PRIMARY_REGIONS = ".related,.upsells,.up-sells,.cross-sells,.products,[data-companion-nonprimary]"
# Reviewed public UI markers whose source controller explicitly writes these axes.
# Only attribute presence is read, never hidden payload values or nonce fields.
PUBLIC_CONTROL_BINDINGS = {"data-pigf-coroplast-quantity": "pigf_run_size",
                           "data-pigf-flute": "pigf_flute", "data-pigf-stakes": "pigf_stakes_per_sign"}

# Public product controls only: no entered values, uploaded URLs, addresses,
# account storage or private JS state. Only explicitly named public Woo product
# and variation identifiers are read from hidden fields to establish ownership.
CAPTURE_RENDERED_FIELDS_JS = r"""() => {
 const roots=[...document.querySelectorAll(ROOTS)].filter(n=>!n.parentElement?.closest(ROOTS));
 const all=[],headings=[],limit=256;
 const primaryId=(String(document.body?.className||'').match(/\bpostid-(\d+)\b/)||[])[1]||'';
 const text=n=>String(n?.textContent||'').replace(/\s+/g,' ').trim().slice(0,180);
 // aria-hidden changes accessibility exposure, not visual presentation. An
 // aria-hidden native dropdown can still be a second customer-facing field.
 // Likewise inert disables interaction without making the element disappear.
 const visible=n=>{for(let e=n;e;e=e.parentElement){const s=getComputedStyle(e);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||s.opacity==='0')return false;}return [...n.getClientRects()].some(r=>r.width>0&&r.height>0);};
 const scope=(n,root)=>{const out=[],boundary=root.closest('[id^=product-],.product')||root.closest('main,[role=main]')||(primaryId?document.body:root);for(let e=n;e;e=e.parentElement){for(const a of SCOPE_ATTRS){if(e.hasAttribute(a)){const v=String(e.getAttribute(a));out.push(a+':'+(LAYER_SCOPE_ATTRS.includes(a)&&!/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(v)?'unverified-layer-scope:'+String([...document.querySelectorAll('['+a+']')].indexOf(e)):v.slice(0,100)));}}if(e.tagName==='FIELDSET'){const l=e.querySelector(':scope>legend');if(l)out.push('legend:'+text(l));}if(e.matches('.pips-size-qty-row,[data-variation-row]'))out.push('row:'+String([...document.querySelectorAll('.pips-size-qty-row,[data-variation-row]')].indexOf(e)));if(e===boundary)break;}const variation=root.querySelector('input[name=variation_id]')?.value;if(/^[1-9][0-9]{0,11}$/.test(String(variation||'')))out.push('variation_id:'+variation);return out;};
 const owner=root=>{
  const region=root.closest(OWNER_REGIONS),form=root.closest('form'),signals=[],sources=[];
  if(region){for(const value of [region.getAttribute('data-product_id'),region.getAttribute('data-product-id'),(region.id.match(/^product-(\d+)$/)||[])[1],(String(region.className||'').match(/\bpost-(\d+)\b/)||[])[1]])if(/^[1-9][0-9]{0,11}$/.test(String(value||''))){signals.push(String(value));sources.push('product_region:'+(region.id||region.tagName.toLowerCase()));}}
  // Read only these named public Woo product identifiers, never other hidden
  // fields or entered values. They establish ownership, not quote semantics.
  if(form)for(const node of form.querySelectorAll('[name="product_id"],[name="add-to-cart"]')){if(node.closest('form')===form&&/^[1-9][0-9]{0,11}$/.test(String(node.value||''))){signals.push(String(node.value));sources.push('public_woo_identifier:'+node.name);}}
  const ids=[...new Set(signals)],excluded=!!root.closest(NON_PRIMARY_REGIONS);
  if(excluded)return {product_id:ids.length===1?ids[0]:'',verified:false,primary:false,basis:'non_primary_product_region'};
  if(ids.length>1)return {product_id:'',verified:false,primary:false,basis:'conflicting_product_identifiers'};
  if(ids.length===1)return {product_id:ids[0],verified:true,primary:ids[0]===primaryId,basis:'explicit_product_region_or_form_id',sources:[...new Set(sources)]};
  if(primaryId&&!region&&root.closest('main,[role=main]'))return {product_id:primaryId,verified:true,primary:true,basis:'primary_body_and_main_purchase_region',sources:['body.postid-'+primaryId,'main_purchase_region']};
  return {product_id:'',verified:false,primary:false,basis:'product_owner_unverified'};
 };
 const binding=n=>n.getAttribute('data-calculator-field')||n.getAttribute('data-field')||Object.entries(BOUND_CONTROL_ATTRS).find(([attribute])=>n.hasAttribute(attribute))?.[1]||'';
 const label=n=>{const explicit=n.getAttribute('aria-label');if(explicit)return explicit.slice(0,180);const ids=(n.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);if(ids.length)return ids.map(id=>text(document.getElementById(id))).join(' ').slice(0,180);if(n.labels?.length)return [...n.labels].map(text).join(' ').slice(0,180);return n.getAttribute('data-label')||'';};
 roots.forEach((root,ri)=>{
  const form=root.closest('form')||root,formKey='purchase-'+ri,productOwner=owner(root);
  [...root.querySelectorAll('select,input[type=number],input[type=range],[role=combobox],[role=spinbutton]')].forEach((n,i)=>{
   if(all.length>=limit)return;
   if(n.tagName==='INPUT'&&!['number','range'].includes(n.type))return;
   if(n.closest('[data-companion-ignore],nav,header,footer'))return;
   all.push({dom_ref:formKey+'/control-'+i,tag:n.tagName.toLowerCase(),type:n.type||n.getAttribute('role')||'',id:n.id||'',name:n.getAttribute('name')||'',label:label(n),
    form_key:formKey,product_owner:productOwner,scopes:scope(n,form),visible:visible(n),enabled:!n.matches(':disabled,[aria-disabled=true]'),editable:!n.readOnly&&!n.closest('[inert]'),multiple:!!n.multiple,
    declared_purpose:n.getAttribute('data-companion-purpose')||'',binding:binding(n),proxy_for:n.getAttribute('data-companion-proxy-for')||''});
  });
  [...root.querySelectorAll('h1,h2,h3,h4,legend,[role=heading]')].forEach((n,i)=>{
   if(headings.length>=limit||n.closest('button,[role=button],[role=tab],summary,nav'))return;
   headings.push({dom_ref:formKey+'/heading-'+i,tag:n.tagName.toLowerCase(),label:text(n),form_key:formKey,product_owner:productOwner,scopes:scope(n,form),visible:visible(n)});
  });
 });
 return {schema:'hermes-companion-field-inventory/v1',observation_kind:'rendered_dom',url:location.href,
  product_id:primaryId,
  observed_at:new Date().toISOString(),viewport:{width:innerWidth,height:innerHeight},
  truncated:all.length>=limit||headings.length>=limit,controls:all,headings,
  scope:'current_rendered_purchase_state_only',private_values_collected:false};
}""".replace("BOUND_CONTROL_ATTRS", json.dumps(PUBLIC_CONTROL_BINDINGS)).replace("LAYER_SCOPE_ATTRS", json.dumps(LAYER_SCOPE_ATTRIBUTES)).replace("SCOPE_ATTRS", json.dumps(SCOPE_ATTRIBUTES)).replace("OWNER_REGIONS", json.dumps(OWNER_REGIONS)).replace("NON_PRIMARY_REGIONS", json.dumps(NON_PRIMARY_REGIONS)).replace("ROOTS", json.dumps(ROOTS))


def _text(value, limit=180):
    return " ".join(str(value or "").split())[:limit]


def _purpose(record):
    declared = _text(record.get("declared_purpose"), 80).casefold()
    name = _text(record.get("name"), 160).casefold()
    named_axis = _text(record.get("binding"), 160).casefold() or name
    label = _text(record.get("label")).casefold()
    # Units per artwork and number of sets are distinct dimensions. Never
    # collapse them into a single quantity axis merely because both are numeric.
    if re.search(r"\b(?:sets|set count|number of sets)\b", declared + " " + label) or name in {"sets", "set_count", "piad_print_set_count"}:
        return "sets", "declared_or_named_set_axis"
    if re.search(r"pieces?\s+per\s+(?:artwork\s+)?set|quantity\s+per\s+(?:artwork\s+)?set", declared + " " + label):
        return "pieces_per_set", "explicit_quantity_scope"
    if re.sub(r"[ -]+", "_", declared) in {"quantity_per_set", "quantity_per_artwork_set", "pieces_per_set", "pieces_per_artwork_set"}:
        return "pieces_per_set", "explicit_quantity_scope"
    if declared:
        return re.sub(r"[^a-z0-9]+", "_", declared).strip("_"), "declared_purpose"
    # Preserve recognized role qualifiers, e.g. garment color versus ink color.
    cleaned = re.sub(r"^(?:print_option_|attribute_pa_|attribute_)", "", named_axis)
    cleaned = re.sub(r"[\[\] -]+", "_", cleaned).strip("_")
    aliases = {"qty": "quantity", "runsize": "quantity", "run_size": "quantity", "quantity": "quantity",
               "pi_csv_pieces": "quantity", "pigf_run_size": "quantity", "size": "size", "width": "width", "height": "height",
               "stock": "stock", "paper_stock": "stock", "substrate": "stock", "coating": "coating",
               "color": "color", "colour": "color", "colorspec": "colorspec", "printing": "colorspec",
               "garment_color": "garment_color", "ink_color": "ink_color", "decoration_color": "decoration_color"}
    if cleaned in aliases:
        return aliases[cleaned], "named_product_axis"
    if re.fullmatch(r"super_attribute_\d+", cleaned):
        # The supplier attribute ID is binding evidence; its associated label
        # supplies only the human-readable purpose, never cross-ID equivalence.
        return re.sub(r"[^a-z0-9]+", "_", label).strip("_") or cleaned, "attribute_id_and_label"
    simple_label = re.sub(r"[^a-z0-9]+", " ", label).strip()
    if simple_label in {"quantity", "qty", "run size", "size", "width", "height", "stock", "coating", "color", "colour"}:
        return aliases.get(simple_label.replace(" ", "_"), simple_label), "label_only"
    if cleaned:
        # Unknown vocabulary is still a real named scalar binding. Do not
        # restrict duplicate detection to cards, dimensions or known finishes.
        return cleaned, "named_axis_identity"
    return "", "unclassified"


def _scope(scopes):
    result = []
    for value in scopes if isinstance(scopes, list) else []:
        value = _text(value, 160)
        if value.startswith("legend:"):
            legend = value.removeprefix("legend:").casefold()
            if not re.search(r"\b(?:front|back|left sleeve|right sleeve|set\s+\w+|(?:artwork\s+)?layer\s+\w+|variation)\b", legend):
                continue
            value = "legend:" + legend
        result.append(value)
    return tuple(sorted(set(result)))


def normalize_field_inventory(raw: dict) -> dict:
    if (not isinstance(raw, dict) or raw.get("schema") != SCHEMA
            or raw.get("observation_kind") not in {"rendered_dom", "static_html"}):
        raise ValueError("field_inventory_schema_unverified")
    controls, headings = [], []
    for kind, output in (("controls", controls), ("headings", headings)):
        rows = raw.get(kind)
        if not isinstance(rows, list) or len(rows) > MAX_NODES:
            raise ValueError("field_inventory_out_of_bounds")
        for record in rows:
            if not isinstance(record, dict) or not any(record.get("visible") is value for value in (True, False, None)):
                raise ValueError("field_inventory_record_invalid")
            item = {key: _text(record.get(key), 180) for key in ("dom_ref", "tag", "type", "id", "name", "label", "form_key", "declared_purpose", "binding", "proxy_for")}
            if not item["dom_ref"] or not item["form_key"]:
                raise ValueError("field_inventory_scope_missing")
            item.update(scopes=list(_scope(record.get("scopes"))), visible=record.get("visible"),
                        enabled=record.get("enabled") is True, editable=record.get("editable") is True,
                        multi_value=record.get("multiple") is True or item["name"].endswith("[]"))
            owner = record.get("product_owner") or {}
            owner = owner if isinstance(owner, dict) else {}
            owner_id = str(owner.get("product_id", "")) if isinstance(owner, dict) else ""
            owner_basis = owner.get("basis") if isinstance(owner, dict) else ""
            verified_owner = (re.fullmatch(r"[1-9][0-9]{0,11}", owner_id) is not None and owner.get("verified") is True
                              and owner_basis in {"explicit_product_region_or_form_id", "primary_body_and_main_purchase_region"})
            item["product_owner"] = {"product_id": owner_id if verified_owner else "", "verified": verified_owner,
                "primary": verified_owner and owner.get("primary") is True and owner_id == str(raw.get("product_id", "")),
                "basis": _text(owner_basis, 80), "sources": [_text(value, 160) for value in owner.get("sources", [])[:8]] if isinstance(owner.get("sources"), list) else []}
            item["purpose"], item["purpose_basis"] = _purpose(item) if kind == "controls" else ("product_options" if re.sub(r"[^a-z]", "", item["label"].casefold()) in {"productoptions", "productconfiguration"} else "", "heading_text")
            # An identical public scalar binding is strong evidence. Distinct
            # native Woo quantity and RunSize names remain different bindings.
            item["binding_key"] = ("declared:" + item["binding"] if item["binding"] else "name:" + item["name"] if item["name"] else "")
            output.append(item)
    viewport = raw.get("viewport")
    viewport = {key: viewport[key] for key in ("width", "height") if type(viewport.get(key)) is int and 0 < viewport[key] <= 100000} if isinstance(viewport, dict) else None
    result = {"schema": SCHEMA, "observation_kind": raw["observation_kind"], "url": _text(raw.get("url"), 4096),
              "product_id": _text(raw.get("product_id"), 16),
              "observed_at": _text(raw.get("observed_at"), 60), "viewport": viewport,
              "scope": "current_rendered_purchase_state_only" if raw["observation_kind"] == "rendered_dom" else "static_html_purchase_markup_only",
              "truncated": raw.get("truncated") is True, "controls": controls, "headings": headings,
              "private_values_collected": False}
    result["projection_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def static_field_inventory(html: str, *, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    root_candidates = soup.select(ROOTS)
    root_ids = {id(node) for node in root_candidates}
    roots = [node for node in root_candidates if not any(id(parent) in root_ids for parent in node.parents)]
    owner_region_ids = {id(node) for node in soup.select(OWNER_REGIONS)}
    scope_boundary_ids = {id(node) for node in soup.select("[id^=product-],.product")}
    non_primary_ids = {id(node) for node in soup.select(NON_PRIMARY_REGIONS)}
    body_class = " ".join(soup.body.get("class", [])) if soup.body else ""
    primary_match = re.search(r"\bpostid-(\d+)\b", body_class)
    primary_id = primary_match.group(1) if primary_match else ""
    controls, headings = [], []
    def product_owner(root):
        ancestors = [root, *root.parents]
        region = next((node for node in ancestors if id(node) in owner_region_ids), None)
        form = root if root.name == "form" else root.find_parent("form")
        identifiers, sources = set(), []
        if region:
            region_id = re.fullmatch(r"product-(\d+)", region.get("id", ""))
            class_id = re.search(r"\bpost-(\d+)\b", " ".join(region.get("class", [])))
            for value in (region.get("data-product_id"), region.get("data-product-id"),
                          region_id.group(1) if region_id else None, class_id.group(1) if class_id else None):
                if re.fullmatch(r"[1-9][0-9]{0,11}", str(value or "")):
                    identifiers.add(str(value)); sources.append("product_region:" + (region.get("id") or region.name))
        if form:
            for node in form.select('[name="product_id"],[name="add-to-cart"]'):
                value = str(node.get("value", ""))
                if node.find_parent("form") is form and re.fullmatch(r"[1-9][0-9]{0,11}", value):
                    identifiers.add(value); sources.append("public_woo_identifier:" + node["name"])
        if any(id(node) in non_primary_ids for node in ancestors):
            return {"verified": False, "primary": False, "basis": "non_primary_product_region"}
        if len(identifiers) > 1:
            return {"verified": False, "primary": False, "basis": "conflicting_product_identifiers"}
        if identifiers:
            identifier = next(iter(identifiers))
            return {"product_id": identifier, "verified": True, "primary": identifier == primary_id,
                    "basis": "explicit_product_region_or_form_id", "sources": sorted(set(sources))}
        if primary_id and region is None and any(node.name == "main" or node.get("role") == "main" for node in ancestors):
            return {"product_id": primary_id, "verified": True, "primary": True,
                    "basis": "primary_body_and_main_purchase_region", "sources": ["body.postid-" + primary_id, "main_purchase_region"]}
        return {"verified": False, "primary": False, "basis": "product_owner_unverified"}
    def visibility(node):
        for ancestor in [node, *node.parents]:
            if not getattr(ancestor, "attrs", None):
                continue
            if (ancestor.has_attr("hidden")
                    or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|opacity\s*:\s*0(?:\s*[;!]|\s*$))", ancestor.get("style", ""), re.I)):
                return False
        return None  # External/responsive CSS has not been evaluated.
    def scopes(node, root):
        result = []
        root_ancestors = [root, *root.parents]
        boundary = next((ancestor for ancestor in root_ancestors if id(ancestor) in scope_boundary_ids), None)
        boundary = boundary or next((ancestor for ancestor in root_ancestors if ancestor.name == "main" or ancestor.get("role") == "main"), None)
        boundary = boundary or (soup.body if primary_id else root)
        for ancestor in [node, *node.parents]:
            for name in SCOPE_ATTRIBUTES:
                if ancestor.has_attr(name):
                    value = str(ancestor.get(name))
                    if name in LAYER_SCOPE_ATTRIBUTES and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
                        # Layer attributes can hold serialized artwork data. Keep
                        # an opaque DOM witness, never an uploaded URL or payload.
                        value = "unverified-layer-scope:" + str(soup.select("[" + name + "]").index(ancestor))
                    result.append(name + ":" + _text(value, 100))
            if ancestor.name == "fieldset":
                legend = ancestor.find("legend", recursive=False)
                if legend:
                    result.append("legend:" + legend.get_text(" ", strip=True))
            if "pips-size-qty-row" in ancestor.get("class", []) or ancestor.has_attr("data-variation-row"):
                result.append("row:" + str(soup.select(".pips-size-qty-row,[data-variation-row]").index(ancestor)))
            if ancestor is boundary:
                break
        variation = root.select_one('input[name="variation_id"]')
        if variation and re.fullmatch(r"[1-9][0-9]{0,11}", str(variation.get("value", ""))):
            result.append("variation_id:" + variation["value"])
        return result
    for ri, root in enumerate(roots):
        form_key = f"purchase-{ri}"
        owner = product_owner(root)
        for index, node in enumerate(root.select("select,input[type=number],input[type=range],[role=combobox],[role=spinbutton]")):
            if len(controls) >= MAX_NODES:
                break
            label_nodes = soup.find_all("label", attrs={"for": node.get("id")}) if node.get("id") else []
            label = node.get("aria-label") or " ".join(item.get_text(" ", strip=True) for item in label_nodes) or (node.find_parent("label").get_text(" ", strip=True) if node.find_parent("label") else "")
            controls.append({"dom_ref": f"{form_key}/control-{index}", "tag": node.name, "type": node.get("type", ""),
                "id": node.get("id", ""), "name": node.get("name", ""), "label": label, "form_key": form_key,
                "scopes": scopes(node, root), "product_owner": owner, "visible": visibility(node),
                "enabled": not node.has_attr("disabled") and node.get("aria-disabled") != "true" and not any(p.has_attr("disabled") for p in node.parents if p.name == "fieldset"),
                "editable": not node.has_attr("readonly") and not any(p.has_attr("inert") for p in [node, *node.parents]), "multiple": node.has_attr("multiple"), "declared_purpose": node.get("data-companion-purpose", ""),
                "binding": node.get("data-calculator-field") or node.get("data-field")
                    or next((axis for attribute, axis in PUBLIC_CONTROL_BINDINGS.items() if node.has_attr(attribute)), ""),
                "proxy_for": node.get("data-companion-proxy-for", "")})
        for index, node in enumerate(root.select("h1,h2,h3,h4,legend,[role=heading]")):
            if len(headings) >= MAX_NODES:
                break
            if node.find_parent(["button", "summary", "nav"]) or node.find_parent(attrs={"role": re.compile(r"^(?:button|tab)$")}):
                continue
            headings.append({"dom_ref": f"{form_key}/heading-{index}", "tag": node.name, "label": node.get_text(" ", strip=True),
                             "form_key": form_key, "scopes": scopes(node, root), "product_owner": owner, "visible": visibility(node)})
    return normalize_field_inventory({"schema": SCHEMA, "observation_kind": "static_html", "url": url,
        "product_id": primary_id,
        "observed_at": datetime.now(timezone.utc).isoformat(), "controls": controls, "headings": headings,
        "truncated": len(controls) >= MAX_NODES or len(headings) >= MAX_NODES})


def duplicate_field_findings(inventory: dict | None) -> dict:
    coverage = {"all_conditional_states": "unknown", "other_viewports": "unknown"}
    if not inventory:
        return {"status": "unknown", "reason": "field_observation_unavailable", "findings": [], "inactive_observations": [],
                "unclassified_active_observations": [], "coverage": coverage, "automatic_changes_allowed": False}
    controls, headings = inventory.get("controls", []), inventory.get("headings", [])
    findings, inactive, unclassified, groups = [], [], [], defaultdict(list)
    for field in controls:
        if not field.get("purpose"):
            # An editable public control with no known axis must not silently
            # disappear from a supposedly complete rendered-state check.
            if field.get("visible") is not False and field.get("enabled") and field.get("editable"):
                unclassified.append({"dom_ref": field["dom_ref"], "form_key": field["form_key"],
                    "label": field.get("label", ""), "scopes": field.get("scopes", []),
                    "reason": "public_purchase_axis_unclassified"})
            continue
        if field.get("visible") is False or not field.get("enabled") or not field.get("editable"):
            inactive.append({"dom_ref": field["dom_ref"], "reason": "hidden_disabled_or_readonly", "purpose": field["purpose"]})
            continue
        purpose = "quantity" if field["purpose"] in {"quantity", "pieces_per_set"} else field["purpose"]
        groups[(field["form_key"], tuple(field.get("scopes", [])), purpose)].append(field)
    for heading in headings:
        if heading.get("purpose") and heading.get("visible") is not False:
            groups[(heading["form_key"], tuple(heading.get("scopes", [])), "heading:" + heading["purpose"])].append(heading)
    for (form_key, scopes, purpose), fields in groups.items():
        if len(fields) < 2:
            continue
        bindings = {field.get("binding_key") for field in fields}
        concurrent = all(field.get("visible") is True for field in fields)
        confirmed = (not purpose.startswith("heading:") and concurrent and len(bindings) == 1 and "" not in bindings
                     and len({field.get("purpose") for field in fields}) == 1
                     and all(not field.get("multi_value") and not field.get("proxy_for") for field in fields)
                     and not any(":unverified-layer-scope:" in value for value in scopes))
        reason = "same_binding_in_same_purchase_scope" if confirmed else "repeated_heading_requires_layout_review" if purpose.startswith("heading:") else "overlapping_purpose_requires_binding_review"
        refs = [field["dom_ref"] for field in fields]
        identity = hashlib.sha256(json.dumps([form_key, scopes, purpose, refs]).encode()).hexdigest()[:16]
        findings.append({"id": identity, "kind": "duplicate_bound_controls" if confirmed else "repeated_product_options_heading" if purpose.startswith("heading:") else "ambiguous_field_overlap",
            "status": "mismatch" if confirmed else "unknown", "purpose": purpose.removeprefix("heading:"), "reason": reason,
            "purchase_scope": {"form_key": form_key, "scopes": list(scopes)}, "observations": fields,
            "evidence": {"url": inventory.get("url"), "projection_sha256": inventory.get("projection_sha256"),
                         "observed_at": inventory.get("observed_at"), "observation_kind": inventory.get("observation_kind"), "viewport": inventory.get("viewport")},
            "proposed_correction": {"action": "review_redundant_heading" if purpose.startswith("heading:") else "review_redundant_purchase_field",
                "instruction": "Keep the working bound field or necessary scoped fields; remove or hide only a verified redundant presentation after review.",
                "requires_review": True, "automatic": False}})
    # A standalone calculator and Woo cart form can both belong to the primary
    # product while retaining separate DOM roots. Surface their overlap without
    # pretending two independent purchase flows have equivalent live bindings.
    cross_form_groups = defaultdict(list)
    for field in controls:
        owner = field.get("product_owner") or {}
        if (not field.get("purpose") or field.get("visible") is False or not field.get("enabled") or not field.get("editable")
                or not owner.get("verified") or not owner.get("primary")):
            continue
        purpose = "quantity" if field["purpose"] in {"quantity", "pieces_per_set"} else field["purpose"]
        cross_form_groups[(owner["product_id"], tuple(field.get("scopes", [])), purpose)].append(field)
    for (owner_id, scopes, purpose), fields in cross_form_groups.items():
        if len({field["form_key"] for field in fields}) < 2:
            continue
        refs = [field["dom_ref"] for field in fields]
        identity = hashlib.sha256(json.dumps([owner_id, scopes, purpose, refs]).encode()).hexdigest()[:16]
        findings.append({"id": identity, "kind": "cross_form_purchase_field_overlap", "status": "unknown", "purpose": purpose,
            "reason": "same_primary_product_multiple_purchase_containers",
            "purchase_scope": {"form_key": "multiple-primary-purchase-containers", "scopes": list(scopes), "product_id": owner_id},
            "observations": fields,
            "evidence": {"url": inventory.get("url"), "projection_sha256": inventory.get("projection_sha256"),
                         "observed_at": inventory.get("observed_at"), "observation_kind": inventory.get("observation_kind"), "viewport": inventory.get("viewport")},
            "proposed_correction": {"action": "review_cross_container_field_binding",
                "instruction": "Verify how each container binds this primary product field, including pieces versus sets. Preserve intentional purchase flows; hide or remove only a proven redundant control after review.",
                "requires_review": True, "automatic": False}})
    unverified_scopes = [{"dom_ref": field["dom_ref"], "reason": "layer_scope_identity_unverified"} for field in controls
                         if field.get("visible") is not False and field.get("enabled") and field.get("editable")
                         and any(":unverified-layer-scope:" in value for value in field.get("scopes", []))]
    observed_active = any(field.get("purpose") and field.get("visible") is True and field.get("enabled") and field.get("editable") for field in controls)
    status = "mismatch" if any(item["status"] == "mismatch" for item in findings) else "unknown" if findings or unclassified or unverified_scopes or inventory.get("truncated") or inventory.get("observation_kind") != "rendered_dom" or not observed_active else "match"
    return {"status": status, "reason": "current_rendered_state_only" if status == "match" else "review_field_findings" if findings else "unclassified_active_purchase_controls" if unclassified else "layer_scope_identity_unverified" if unverified_scopes else "rendered_complete_observation_required",
            "scope": inventory.get("scope"), "findings": findings, "inactive_observations": inactive,
            "unclassified_active_observations": unclassified, "unverified_scope_observations": unverified_scopes, "coverage": coverage,
            "projection_sha256": inventory.get("projection_sha256"), "automatic_changes_allowed": False}
