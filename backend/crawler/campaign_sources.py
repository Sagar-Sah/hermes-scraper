"""Bounded public supplier-page collection for Hermes campaigns.

Only explicitly supplied supported HTTPS page URLs are fetched. Discovered
links are evidence, never an implicit crawl frontier. Reports retain sanitized
public projections and the received-body hash, not cookies, account envelopes,
raw HTML, or authenticated session state. Completion means supplied pages were
collected; it does not certify supplier mapping, prices, or catalog coverage.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .pacing import retry_after_seconds
from .store import canonical, uid
from .tls import tls_context
from .transport import ExtractionError, RateLimiter, validate_url

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_ALLOWED_HOSTS = {"b2sign.com": "b2sign", "www.b2sign.com": "b2sign",
                  "soardistusa.com": "soardist", "www.soardistusa.com": "soardist",
                  "4over.com": "4over", "www.4over.com": "4over",
                  "digital-print-solutions.com": "dps", "www.digital-print-solutions.com": "dps"}
_DPS_PRODUCT_PATHS = {
    "/vinyl-decals/3-mil-high-tack-vinyl-material", "/low-tack-vinyl",
    "/vinyl-decals/crack-and-peel-stickers", "/vinyl-decals/clear-decals",
    "/vinyl-decals/white-reflective", "/vinyl-decals/calendered-vinyl",
    "/vinyl-decals/hi-performance",
}
_PRIVATE = re.compile(r"account|customer|session|cookie|password|passwd|secret|token|authorization|debug|csrf", re.I)
_ITEM_KEYS = {"id", "title", "feature", "description", "url", "image", "images", "files", "price", "attrs", "fields", "saleable"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _page_url(url):
    if not isinstance(url, str) or not url or len(url) > 4096 or url != url.strip():
        raise ValueError("Use an exact public supplier page URL")
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or p.hostname not in _ALLOWED_HOSTS or p.port not in (None, 443)
                or p.username is not None or p.password is not None or p.fragment
                or "\\" in url or any(ord(c) < 33 for c in url)):
            raise ValueError
        parts = unquote(p.path).lower().split("/")
        if any(v in {"login", "logout", "account", "customer", "cart", "checkout", "admin", "api", "graphql", "wp-json"} for v in parts):
            raise ValueError
        if p.path.lower().endswith((".json", ".xml")):
            raise ValueError
        if any(_PRIVATE.search(k) for k, _ in parse_qsl(p.query, keep_blank_values=True)):
            raise ValueError
        if _ALLOWED_HOSTS[p.hostname] == "dps" and (p.path.rstrip("/") not in _DPS_PRODUCT_PATHS or p.query):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Use a supported public HTTPS supplier product/category page without credentials or private parameters; DPS is limited to the seven reviewed decal pages") from None
    return p


def _clean(value, depth=0):
    """Defense in depth inside allowed public fields; never copy outer props."""
    if depth > 30:
        return None
    if isinstance(value, dict):
        return {k: _clean(v, depth + 1) for k, v in value.items()
                if isinstance(k, str) and not _PRIVATE.search(k)}
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return None


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pages, self.scripts, self.forms, self.anchors = [], [], [], []
        self.canonical_urls, self._title_seen, self._select, self._option = [], False, None, None
        self.title, self._title, self._script, self._form, self._anchor = "", False, None, None, None
        self.specifications, self._cells, self._cell = [], [], None
        self._term, self._definition = None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._cells = []
        elif tag in {"th", "td"}:
            self._cell = ""
        elif tag == "dt":
            self._term = ""
        elif tag == "dd":
            self._definition = ""
        if a.get("data-page"):
            self.pages.append(a["data-page"])
        if tag == "link" and "canonical" in a.get("rel", "").lower().split() and a.get("href"):
            self.canonical_urls.append(a["href"])
        if tag == "title" and not self._title_seen:
            self._title = True
            self._title_seen = True
        elif tag == "script":
            self._script = {"attrs": a, "text": ""}
        elif tag == "form":
            self._form = {"method": a.get("method", "get").lower(), "action": a.get("action", ""), "controls": []}
            self.forms.append(self._form)
        elif tag in {"input", "select", "textarea"} and self._form is not None:
            typ = a.get("type", tag).lower()
            # Retain structure, never form values (CSRF/customer fields included).
            control = {"type": typ, "name": a.get("name", "") if not _PRIVATE.search(a.get("name", "")) else "[private field]"}
            self._form["controls"].append(control)
            if tag == "select" and control["name"] != "[private field]":
                control["options"] = []
                self._select = control
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "label": "", "selected": "selected" in a, "disabled": "disabled" in a}
        elif tag == "a" and a.get("href"):
            self._anchor = {"url": a["href"], "title": "", "relation": "html_anchor"}

    def handle_data(self, data):
        if self._option is not None:
            self._option["label"] += data
        if self._cell is not None:
            self._cell += data
        if self._term is not None and self._definition is None:
            self._term += data
        if self._definition is not None:
            self._definition += data
        if self._title:
            self.title += data
        if self._script is not None:
            self._script["text"] += data
        if self._anchor is not None:
            self._anchor["title"] += data

    def handle_endtag(self, tag):
        if tag == "option" and self._option is not None:
            self._option["label"] = " ".join(self._option["label"].split())
            self._select["options"].append(self._option)
            self._option = None
        elif tag == "select":
            self._select, self._option = None, None
        if tag in {"th", "td"} and self._cell is not None:
            self._cells.append(" ".join(self._cell.split()))
            self._cell = None
        elif tag == "tr" and len(self._cells) >= 2:
            self.specifications.append({"label": self._cells[0], "values": self._cells[1:]})
            self._cells = []
        elif tag == "dd" and self._definition is not None:
            if self._term:
                self.specifications.append({"label": " ".join(self._term.split()), "values": [" ".join(self._definition.split())]})
            self._term, self._definition = None, None
        if tag == "title":
            self._title = False
        elif tag == "script" and self._script is not None:
            self.scripts.append(self._script)
            self._script = None
        elif tag == "form":
            self._form = None
        elif tag == "a" and self._anchor is not None:
            self.anchors.append(self._anchor)
            self._anchor = None


def _decode_page(value):
    candidates = [value, unquote(value)]
    try:
        decoded = base64.b64decode(value, validate=True).decode()
        candidates += [decoded, unquote(decoded)]
    except (ValueError, UnicodeError):
        pass
    for candidate in candidates:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except ValueError:
            pass
    return None


def _menu(nodes):
    if not isinstance(nodes, list):
        return []
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        item = {k: node[k] for k in ("title", "url", "type") if isinstance(node.get(k), str)}
        children = _menu(node.get("items", node.get("children")))
        if children:
            item["children"] = children
        if item:
            result.append(item)
    return result


def _item(value):
    return _clean({k: v for k, v in value.items() if k in _ITEM_KEYS}) if isinstance(value, dict) else None


def _collection(value):
    if not isinstance(value, dict):
        return None
    out = {k: value[k] for k in ("id", "title", "url", "type") if isinstance(value.get(k), (str, int))}
    children = value.get("children", [])
    out["children"] = [_item(child) for child in children if isinstance(child, dict)] if isinstance(children, list) else []
    return out


def _links(nodes, final_url, relation):
    supplier = _ALLOWED_HOSTS[urlsplit(final_url).hostname]
    found = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("url"), str):
            absolute = urljoin(final_url, node["url"])
            try:
                p = _page_url(absolute)
            except ValueError:
                pass
            else:
                if _ALLOWED_HOSTS[p.hostname] == supplier:
                    found.append({"url": absolute, "title": str(node.get("title", "")).strip(), "relation": relation,
                                  "source_type": node.get("type"), "mapping_verified": False})
        found += _links(node.get("children", []), final_url, relation)
    return found


def _json_products(parsed):
    products = []
    for script in parsed.scripts:
        a = script["attrs"]
        if a.get("type", "").lower() not in {"application/ld+json", "application/json"}:
            continue
        try:
            data = json.loads(script["text"])
        except ValueError:
            continue
        if a.get("type", "").lower() == "application/ld+json":
            candidates = data if isinstance(data, list) else [data]
            expanded = []
            for entry in candidates:
                if isinstance(entry, dict):
                    expanded.append(entry)
                    expanded.extend(entry.get("@graph", []) if isinstance(entry.get("@graph"), list) else [])
            for entry in expanded:
                if not isinstance(entry, dict):
                    continue
                typ = entry.get("@type", [])
                if typ == "Product" or isinstance(typ, list) and "Product" in typ:
                    products.append(_clean({k: entry[k] for k in ("@type", "@id", "name", "url", "productID", "productId", "sku", "description", "image", "brand", "offers") if k in entry}))
        elif (re.match(r"^ProductJson(?:-|$)", a.get("id", ""), re.I) or "data-product-json" in a):
            if isinstance(data, dict) and data.get("id") is not None and isinstance(data.get("title"), str) and isinstance(data.get("variants"), list):
                products.append(_clean({k: data[k] for k in ("id", "title", "handle", "type", "description", "images", "options", "variants") if k in data}))
    # Shopify's public analytics product metadata sometimes contains variants
    # outside ProductJson. Parse JSON only; never evaluate JS or copy the outer
    # analytics/customer envelope. It must later bind to an explicit product ID.
    for script in parsed.scripts:
        text = script["text"]
        for match in re.finditer(r"(?:ShopifyAnalytics\.meta|(?:var|let|const)\s+meta)\s*=\s*(\{)", text):
            try:
                envelope, _ = json.JSONDecoder().raw_decode(text[match.start(1):])
            except ValueError:
                continue
            product = envelope.get("product") if isinstance(envelope, dict) else None
            if isinstance(product, dict) and product.get("id") is not None and isinstance(product.get("variants"), list):
                products.append({"representation": "shopify_public_product_metadata", **_clean({k: product[k] for k in ("id", "title", "handle", "type", "vendor", "variants") if k in product})})
    unique = {canonical(p): p for p in products}
    return list(unique.values())


def _same_product_url(left, right):
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (a.scheme == b.scheme == "https" and (a.hostname or "").removeprefix("www.") == (b.hostname or "").removeprefix("www.")
                and a.path.rstrip("/") == b.path.rstrip("/") and not a.username and not a.password
                and all(k == "variant" for k, _ in parse_qsl(a.query)))
    except (ValueError, TypeError):
        return False


def _product_id(product):
    value = product.get("id", product.get("productID", product.get("productId")))
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).removeprefix("gid://shopify/Product/")
    return None


def _bound_products(candidates, url):
    matched = [p for p in candidates if isinstance(p.get("url"), str) and _same_product_url(p["url"], url)]
    if matched:
        ids = {_product_id(p) for p in matched} - {None}
        if len(ids) > 1:
            return []
        matched += [p for p in candidates if p not in matched and p.get("url") is None and _product_id(p) in ids]
        return matched
    if len(candidates) == 1 and candidates[0].get("url") is None and candidates[0].get("representation") != "shopify_public_product_metadata":
        return candidates
    return []


def _page_blocks(value):
    if not isinstance(value, dict):
        return None
    return _clean({k: value[k] for k in ("id", "title", "url", "body") if k in value})


def _block_links(value, url):
    links = []
    if isinstance(value, list):
        for child in value:
            links += _block_links(child, url)
    elif isinstance(value, dict):
        for key in ("url", "href", "link"):
            target = value.get(key)
            if isinstance(target, str):
                links += _links([{"url": target, "title": value.get("title", value.get("label", value.get("text", ""))), "type": value.get("type")}], url, "public_page_block")
        for key, child in value.items():
            if key == "html" and isinstance(child, str):
                parsed = _HTML(); parsed.feed(child)
                links += _links(parsed.anchors, url, "public_page_block")
            elif isinstance(child, (dict, list)):
                links += _block_links(child, url)
    return links


def _fourover_content(body, url):
    """Public product text/specs, scoped away from navigation and accounts."""
    soup = BeautifulSoup(body, "html.parser")
    titles = soup.select(".page-title-wrapper.product h1.page-title")
    if len(titles) != 1:
        return None
    title = " ".join(titles[0].get_text(" ", strip=True).split())[:500]
    descriptions = soup.select(".product.attribute.description")
    paragraphs = []
    if len(descriptions) == 1:
        paragraphs = [" ".join(p.get_text(" ", strip=True).split())[:4000]
                      for p in descriptions[0].select("p")[:20]]
        paragraphs = [p for p in paragraphs if p]
    specs = []
    tables = soup.select("#product-attribute-specs-table")
    if len(tables) == 1:
        for row in tables[0].find_all("li", recursive=False):
            label = row.find("span", class_="label", recursive=False)
            values = row.find("ul", class_="data", recursive=False)
            if label is not None and values is not None:
                specs.append({"label": label.get_text(" ", strip=True)[:200],
                              "values": [v.get_text(" ", strip=True)[:500] for v in values.find_all("li", recursive=False)[:500]],
                              "evidence_type": "public_product_specification_not_combination_availability"})
    images = []
    for img in soup.select(".product.media img")[:50]:
        image_url = urljoin(url, img.get("src", ""))
        parsed = urlsplit(image_url)
        if parsed.scheme == "https" and (parsed.hostname or "").removeprefix("www.") == "4over.com" and not parsed.username and not parsed.password:
            images.append({"url": image_url, "alt": img.get("alt", "")[:500]})
    return {"title": title, "description_paragraphs": paragraphs, "specifications": specs,
            "images": images, "source_url": url, "pricing_verified": False,
            "option_combinations_verified": False}


def _dps_content(body, url, products, canonical_url):
    """Known product controls only; never WebForms state or account values.

    DPS publishes SEO Offer amounts even when its actual price panels require
    login. Those amounts are retained by the generic public JSON projection,
    but are not quote evidence or a price table for these dimension controls.
    """
    if not canonical_url or len(products) != 1:
        raise ExtractionError("DPS public product identity is absent or ambiguous", "source_schema_unresolved")
    soup = BeautifulSoup(body, "html.parser")
    product = products[0]
    if not isinstance(product.get("name"), str) or not product["name"].strip():
        raise ExtractionError("DPS public product identity is absent or ambiguous", "source_schema_unresolved")
    controls = []
    selectors = [("height", "Height", "#CustProductHeightHolder"),
                 ("width", "Width", "#CustProductWidthHolder"),
                 ("size", "Size", "#CustProductStaticSizes"),
                 ("quantity", "Quantity", "#CustQuantityList, #QuantityList"),
                 ("turnaround", "Production", "#TurnaroundOption")]
    for key, label, selector in selectors:
        nodes = soup.select(selector)
        if len(nodes) != 1 or nodes[0].name not in {"select", "input"}:
            continue
        node = nodes[0]
        if node.name == "input" and node.get("type", "text").lower() not in {"text", "number"}:
            continue
        control = {"key": key, "label": label, "kind": node.name,
                   "disabled": node.has_attr("disabled"), "unit": "inch" if key in {"width", "height"} else None}
        if node.name == "select":
            control["choices"] = [{"value": opt.get("value", opt.get_text()),
                                    "label": " ".join(opt.get_text(" ", strip=True).split())[:500],
                                    "selected": opt.has_attr("selected"), "disabled": opt.has_attr("disabled")}
                                   for opt in node.select("option")[:2000]]
        else:
            # Only explicit numeric bounds, never current form/user values.
            for attr in ("min", "max", "step"):
                value = node.get(attr)
                if isinstance(value, str) and re.fullmatch(r"(?:\d+(?:\.\d+)?|any)", value):
                    control[attr] = value
        controls.append(control)
    addon_selects = soup.select("#ProductOptionsHolder select")
    for node in addon_selects[:100]:
        wrapper = node.find_parent(class_="option-group")
        label_node = wrapper.select_one("label") if wrapper else None
        label = label_node.get_text(" ", strip=True)[:200] if label_node else "Product option"
        if _PRIVATE.search(node.get("name", "")) or _PRIVATE.search(node.get("id", "")):
            continue
        controls.append({"key": "addon_" + str(len(controls)), "label": label, "kind": "select",
                         "disabled": node.has_attr("disabled"),
                         "choices": [{"value": opt.get("value", opt.get_text()),
                                      "label": opt.get_text(" ", strip=True)[:500],
                                      "selected": opt.has_attr("selected"), "disabled": opt.has_attr("disabled")}
                                     for opt in node.select("option")[:2000]]})
    login = any(re.search(r"login required|please login|must log in", n.get_text(" ", strip=True), re.I)
                for n in soup.select("#disabledOverlayLoginText, .loginRequired"))
    warnings = ["current_document_options_not_complete_configuration_domain"]
    if soup.select_one("#ProductOptionsHolder") and not addon_selects:
        warnings.append("product_addons_loaded_dynamically_not_observed")
    if not any(c["key"] == "quantity" for c in controls):
        warnings.append("quantity_domain_not_observed")
    if product.get("offers"):
        warnings.append("public_seo_offer_has_no_bound_configuration")
    if login:
        warnings.append("supplier_login_required_for_displayed_quote")
    image_values = product.get("image", [])
    if isinstance(image_values, str):
        image_values = [image_values]
    elif isinstance(image_values, dict):
        image_values = [image_values.get("url", image_values.get("contentUrl"))]
    images = []
    for value in image_values if isinstance(image_values, list) else []:
        if not isinstance(value, str):
            continue
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.query
                and parsed.hostname in {"lag.azureedge.net", "web2printdata.blob.core.windows.net", "www.digital-print-solutions.com", "digital-print-solutions.com"}):
            images.append({"url": value})
    return {"title": product["name"], "source_url": url, "description": product.get("description"),
            "controls": controls, "images": images, "pricing_access": "login_requested" if login else "not_established",
            "pricing_verified": False, "quote_rows": 0, "option_combinations_verified": False,
            "quantity_domain_complete": False, "warnings": warnings}


def parse_public_page(body, url):
    """Project only public product/category evidence; never retain the raw envelope."""
    parsed = _HTML()
    parsed.feed(body.decode("utf-8", errors="replace"))
    if any(c["type"] == "password" for f in parsed.forms for c in f["controls"]):
        raise ExtractionError("Supplier page requires a normal login", "access_required")
    title = " ".join(parsed.title.split())
    if re.search(r"^(?:just a moment|access denied|verify (?:you are|your browser)|attention required)", title, re.I):
        raise ExtractionError("Supplier returned an access challenge", "access_required")
    supplier = _ALLOWED_HOSTS[urlsplit(url).hostname]
    out = {"supplier": supplier, "title": title, "item": None, "coll": None, "menus": [], "observed_links": [], "warnings": [],
           "data_scope": "public_page_metadata_only", "prices_verified": False,
           "public_price_basis": "public_listing_not_verified_authenticated_supplier_cost"}
    if supplier == "b2sign":
        envelopes = [_decode_page(value) for value in parsed.pages]
        envelopes = [v for v in envelopes if isinstance(v, dict)]
        if len(envelopes) != 1 or not isinstance(envelopes[0].get("props"), dict):
            raise ExtractionError("Public B2Sign page data is absent or ambiguous", "source_schema_unresolved")
        props = envelopes[0]["props"]
        out.update(item=_item(props.get("item")), coll=_collection(props.get("coll")), menus=_menu(props.get("menus")), page=_page_blocks(props.get("page")))
        if not out["title"]:
            out["title"] = (out.get("item") or out.get("coll") or out.get("page") or {}).get("title", "")
        out["observed_links"] += _block_links(out.get("page"), url)
        if out["item"] is None and out["coll"] is None and out["page"] is None and not out["menus"]:
            raise ExtractionError("Page has no supported public product or category data", "source_schema_unresolved")
        out["observed_links"] += _links([out["item"]] if out["item"] else [], url, "item")
        out["observed_links"] += _links(out["coll"].get("children", []) if out["coll"] else [], url, "collection_child")
        out["observed_links"] += _links(out["menus"], url, "menu")
    else:
        candidates = _json_products(parsed)
        bound = _bound_products(candidates, url)
        out["product_representations"] = bound
        out["product_json"] = max(bound, key=lambda p: (p.get("@type") == "Product", len(p.get("offers", [])), len(p)), default=None)
        out["public_variant_metadata"] = [p for p in bound if isinstance(p.get("variants"), list)]
        out["canonical_url"] = next((urljoin(url, v) for v in parsed.canonical_urls if _same_product_url(urljoin(url, v), url)), None)
        if candidates and not bound:
            out["warnings"].append("ambiguous_product_json_not_retained")
        elif not candidates:
            out["warnings"].append("product_json_not_observed")
        out["pricing_access"] = "login_requested" if any(re.search(r"login to (?:view|see) price", a["title"], re.I) for a in parsed.anchors) else "not_established"
        specification_label = re.compile(r"^(?:sizes?|stocks?|materials?|colou?rs?(?:pec)?|coatings?|finishes?|finishing|quantity|quantities|runsizes?|run sizes?|turnaround(?: time)?|production(?: time)?|bleed|resolution|width|height|thickness|envelopes?)\s*[:*]?\s*$", re.I)
        out["observed_specifications"] = [entry for entry in parsed.specifications
                                          if specification_label.fullmatch(entry["label"]) and any(entry["values"])]
        out["forms"] = [{"method": f["method"], "action": urljoin(url, f["action"]).split("?")[0], "controls": f["controls"]} for f in parsed.forms]
        out["scripts"] = []
        for script in parsed.scripts:
            src = script["attrs"].get("src")
            if isinstance(src, str):
                p = urlsplit(urljoin(url, src))
                if p.scheme == "https" and p.username is None and p.password is None:
                    out["scripts"].append(f"{p.scheme}://{p.netloc}{p.path}")
        if supplier == "4over":
            out["product_content"] = _fourover_content(body, url)
            if out["product_content"]:
                out["observed_specifications"] += out["product_content"]["specifications"]
        elif supplier == "dps":
            out["product_content"] = _dps_content(body, url, bound, out["canonical_url"])
            out["pricing_access"] = out["product_content"]["pricing_access"]
            out["warnings"] += out["product_content"]["warnings"]
            # The whole WebForms page wraps cart/account/consent inputs. Only
            # our allowlisted product controls above belong in a DPS report.
            out["forms"] = []
    out["observed_links"] += _links(parsed.anchors, url, "html_anchor")
    out["observed_links"] = list({(x["url"], x["relation"]): x for x in out["observed_links"]}.values())
    return out


class SourceCollector:
    """Explicit lifecycle only: constructing or listing never makes a source call."""
    def __init__(self, store, *, http_transport=None):
        self.store, self.http_transport = store, http_transport
        self.root = Path(store.root) / "source-collections"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.limiter, self._tasks = RateLimiter(store), {}
        self._closed = False

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Unknown source collection")
        return self.root / f"{job_id}.json"

    def _save(self, job):
        job["updated_at"] = _now()
        target = self._path(job["id"])
        tmp = target.with_name(target.name + f".{uid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(job, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)

    def _load(self, job_id):
        try:
            return json.loads(self._path(job_id).read_text())
        except FileNotFoundError:
            raise ValueError("Unknown source collection") from None

    def _owner_active(self, job_id):
        self._path(job_id)
        fd = os.open(self.root / f"{job_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def get(self, job_id, *, include_data=False):
        job = self._load(job_id)
        job["worker_active"] = self._owner_active(job_id)
        if job["status"] == "running" and not job["worker_active"]:
            job["status"] = "interrupted"
        job["completed_pages"] = sum(x["status"] == "completed" for x in job["pages"])
        job["pending_pages"] = sum(x["status"] in {"pending", "fetching"} for x in job["pages"])
        job["failed_pages"] = sum(x["status"] == "failed" for x in job["pages"])
        job["planned_pages"] = len(job["pages"])
        job["resumable"] = job["status"] != "completed"
        if include_data:
            for page in job["pages"]:
                if page.get("evidence"):
                    page["public_data"] = json.loads(self.store.get_object(page["evidence"]["object_path"]))["public_data"]
        return job

    status = get

    def list(self, limit=20):
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.get(p.stem) for p in paths[:max(1, min(int(limit), 100))]]

    async def start(self, urls):
        if self._closed:
            raise ValueError("Source collector is stopped")
        if not isinstance(urls, list) or not 1 <= len(urls) <= 1000:
            raise ValueError("Supply 1–1000 exact public product/category URLs")
        unique = list(dict.fromkeys(urls)) if all(isinstance(u, str) for u in urls) else []
        if not unique:
            raise ValueError("Supply exact public supplier URLs")
        for url in unique:
            _page_url(url)
            await validate_url(url)
        job = {"schema": "hermes-public-source-collection/v1", "id": uid(), "status": "queued",
               "created_at": _now(), "updated_at": _now(), "requests_per_second": 1, "source_concurrency": 1,
               "max_response_bytes": MAX_RESPONSE_BYTES, "http_attempts": 0, "input_count": len(urls),
               "coverage": "explicit_supplied_pages_only", "catalog_complete": False, "mapping_verified": False,
               "error": None, "pages": [{"url": u, "status": "pending", "attempts": 0, "evidence": None} for u in unique]}
        self._save(job)
        self._schedule(job["id"])
        return self.get(job["id"])

    def _schedule(self, job_id):
        if self._closed:
            raise ValueError("Source collector is stopped")
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        task = asyncio.create_task(self._run(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda done: self._tasks.pop(job_id, None) if self._tasks.get(job_id) is done else None)

    async def resume(self, job_id):
        job = self._load(job_id)
        if job["status"] != "completed":
            self._schedule(job_id)
        return self.get(job_id)

    async def pause(self, job_id):
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            if self._owner_active(job_id):
                raise ValueError("Source collection is owned by another running worker")
            job = self._load(job_id)
            if job["status"] != "completed":
                job["status"] = "paused"
                self._save(job)
        return self.get(job_id)

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job_id):
        lock = os.open(self.root / f"{job_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # Another collector owns this durable job.
            job = self._load(job_id)
            job["status"], job["error"] = "running", None
            for page in job["pages"]:
                if page["status"] != "completed":
                    page["status"] = "pending"
            self._save(job)
            try:
                async with httpx.AsyncClient(verify=tls_context(), transport=self.http_transport,
                                             follow_redirects=False, timeout=httpx.Timeout(30),
                                             limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
                    for page in job["pages"]:
                        if page["status"] == "completed":
                            continue
                        page["status"] = "fetching"
                        self._save(job)
                        try:
                            evidence = await self._fetch(job, page, client)
                        except ExtractionError as exc:
                            page["status"] = "failed"
                            page["error"] = {"code": exc.code, "message": str(exc)}
                            job["status"], job["error"] = "attention", page["error"]
                            self._save(job)
                            return
                        page["status"], page["evidence"] = "completed", evidence
                        page.pop("error", None)
                        self._save(job)  # Durable response before advancing to next URL.
                job["status"] = "completed"
                self._save(job)
            except asyncio.CancelledError:
                job["status"] = "paused"
                self._save(job)
                raise
            except Exception:
                job["status"], job["error"] = "attention", {"code": "collector_error", "message": "Source collection stopped; completed page evidence is preserved"}
                self._save(job)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)

    async def _fetch(self, job, page, client):
        current, redirects, supplier = page["url"], [], _ALLOWED_HOSTS[urlsplit(page["url"]).hostname]
        async def check():
            if self._closed:
                raise asyncio.CancelledError()
        for _ in range(6):
            try:
                p = _page_url(current)
            except ValueError as exc:
                raise ExtractionError(str(exc), "invalid_redirect") from None
            if _ALLOWED_HOSTS[p.hostname] != supplier:
                raise ExtractionError("Cross-supplier redirects are not allowed", "invalid_redirect")
            await validate_url(current)
            origin = {"b2sign": "https://www.b2sign.com", "soardist": "https://soardistusa.com", "4over": "https://4over.com",
                      "dps": "https://www.digital-print-solutions.com"}[supplier]
            token = await self.limiter.acquire(origin, 1, 1, check)
            started, status, retry_after = time.monotonic(), 0, 30
            try:
                await check()
                job["http_attempts"] += 1
                page["attempts"] += 1
                self._save(job)
                # Request constructed directly: no client cookies, auth, profiles,
                # referer, browser headers, or prior Set-Cookie values are copied.
                request = httpx.Request("GET", current, headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": "Hermes-PublicSourceCollector/1.0"})
                response = await client.send(request, stream=True, follow_redirects=False)
                try:
                    status = response.status_code
                    page["last_http_status"] = status
                    retry_after = retry_after_seconds(response.headers.get("retry-after"))
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ExtractionError("Supplier redirect has no destination", "invalid_redirect")
                        target = urljoin(current, location)
                        try:
                            target_parts = _page_url(target)
                        except ValueError:
                            raise ExtractionError("Supplier redirected outside a supported public page", "invalid_redirect") from None
                        if _ALLOWED_HOSTS[target_parts.hostname] != supplier:
                            raise ExtractionError("Cross-supplier redirects are not allowed", "invalid_redirect")
                        redirects.append({"url": current, "status": status, "location": target})
                        current = target
                        continue
                    if status in {401, 403}:
                        raise ExtractionError(f"Supplier requires access (HTTP {status})", "access_required")
                    if status == 429:
                        raise ExtractionError("Supplier rate limit reached; collection stopped", "rate_limited")
                    if status >= 500:
                        raise ExtractionError(f"Supplier unavailable (HTTP {status})", "source_unavailable")
                    if status != 200:
                        raise ExtractionError(f"Supplier page returned HTTP {status}", "source_http_error")
                    content_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ExtractionError("Expected an ordinary public HTML page", "source_schema_unresolved")
                    try:
                        size_hint = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        size_hint = 0
                    if size_hint > MAX_RESPONSE_BYTES:
                        raise ExtractionError("Public page exceeds 32 MB limit", "response_too_large")
                    chunks, count = [], 0
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > MAX_RESPONSE_BYTES:
                            raise ExtractionError("Public page exceeds 32 MB limit", "response_too_large")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                finally:
                    await response.aclose()
                    client.cookies.clear()
            except httpx.HTTPError:
                raise ExtractionError("Public supplier page request failed", "network_error") from None
            finally:
                # Synchronous release cannot be abandoned at a cancellation point.
                self.limiter.release(origin, token, time.monotonic() - started, status, retry_after, 1)
            projection = parse_public_page(body, current)
            evidence = {"requested_url": page["url"], "url": current, "status": status, "content_type": content_type,
                        "received_bytes": len(body), "received_body_sha256": hashlib.sha256(body).hexdigest(),
                        "captured_at": _now(), "redirects": redirects, "public_data": projection,
                        "stored_body": "public_projection_only_raw_envelope_not_retained"}
            payload = canonical(evidence).encode()
            key = self.store.put_object(payload, "public-source.json")
            local = Path(self.store.root) / key
            if local.exists():
                local.chmod(0o600)
            return {"object_path": key, "sha256": hashlib.sha256(payload).hexdigest(),
                    **{k: v for k, v in evidence.items() if k != "public_data"},
                    "observed_links": projection["observed_links"],
                    "public_item": {k: v for k, v in (projection.get("item") or {}).items() if k in {"id", "title", "url"}},
                    "warnings": projection["warnings"]}
        raise ExtractionError("Supplier redirect limit reached", "invalid_redirect")
