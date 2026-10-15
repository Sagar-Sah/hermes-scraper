"""Read embedded PrintItPrints CSV tables without executing supplier JavaScript.

Retail arithmetic is a Python translation of one exact public calculator script.
An unrecognized calculator leaves retail prices unresolved; embedded base prices
are retained separately and never presented as customer quotes.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .printograph import AdapterError, matches


# Public Club Flyers HTML, captured 2026-09-26; entire inline calculator,
# including its membership lists, row selection, display and price function.
CALCULATOR_SHA256 = "948b63c3764d3781dad5c369d1b0b3e6c7f8f3811ec3df80e5b459e1f85bafb5"
PRICE_COLUMNS = {"UnitPrice", "TotalPrice"}
ALIASES = {"STOCK": "stock", "COLORSPEC": "colorspec", "RUNSIZE": "quantity", "TURNAROUND": "production"}
FILTER_ALIASES = {"paper": "stock", "color": "colorspec", "runsize": "quantity", "turnaround": "production"}
REASONS = ["embedded_table_scope_only", "calculated_retail_not_cart_verified", "shipping_and_tax_not_quoted"]


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AdapterError("schema_changed", f"Invalid {name} in embedded CSV table")
    text = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text):
        raise AdapterError("schema_changed", f"Invalid {name} in embedded CSV table")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise AdapterError("schema_changed", f"Invalid {name} in embedded CSV table") from exc
    if not amount.is_finite() or amount < 0 or not math.isfinite(float(amount)):
        raise AdapterError("schema_changed", f"Nonfinite {name} in embedded CSV table")
    return amount


def _quantity(value):
    result = _number(value, "quantity")
    if result <= 0 or result != result.to_integral_value() or result > 2**53 - 1:
        raise AdapterError("schema_changed", "Quantity must be a positive exact integer")
    return int(result)


def _key(value):
    return ALIASES.get(value, re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_"))


def _json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_json_constant(value):
    raise ValueError("Nonstandard JSON numeric constant")


def _declaration(scripts, name):
    found = []
    for text in scripts:
        match = re.match(r"\s*const\s+" + re.escape(name) + r"\s*=\s*", text)
        if not match:
            continue
        try:
            value, end = json.JSONDecoder(object_pairs_hook=_json_unique, parse_constant=_invalid_json_constant).raw_decode(text[match.end():])
        except ValueError as exc:
            raise AdapterError("schema_changed", f"Invalid JSON declaration: {name}") from exc
        if not re.fullmatch(r"\s*;?\s*", text[match.end() + end:]):
            raise AdapterError("schema_changed", f"Executable or ambiguous suffix in {name} declaration")
        found.append(value)
    if len(found) != 1:
        raise AdapterError("unsupported_source", f"Expected exactly one isolated {name} JSON declaration")
    return found[0]


def _currencies(soup):
    values = set()
    for node in soup.select('meta[itemprop="priceCurrency"], meta[property="product:price:currency"]'):
        if node.get("content"):
            values.add(node["content"])
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("priceCurrency"), str):
                values.add(value["priceCurrency"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(node.string or node.get_text()))
        except ValueError:
            continue
    return values


@dataclass
class EmbeddedProduct:
    title: str
    product_id: str
    rows: list[dict]
    headers: list[str]
    domains: dict[str, list[dict]]
    currency: str | None
    images: list[str]
    calculator_hash: str | None
    calculator_verified: bool
    markup_member: bool | None


def parse_product_html(html: str, url: str) -> EmbeddedProduct:
    """Parse a saved/public HTML response; never eval page code or fetch assets."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"printitprints.com", "www.printitprints.com"} or parsed.username or parsed.password:
        raise AdapterError("unsupported_source", "PrintItPrints requires its HTTPS product URL")
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#product_calculator_display") is None:
        raise AdapterError("unsupported_source", "No embedded PrintItPrints calculator found")
    scripts = [node.string or node.get_text() for node in soup.find_all("script", src=False)]
    rows = _declaration(scripts, "csvdata")
    product_id = _declaration(scripts, "current_product_id_is_this")
    if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
        raise AdapterError("schema_changed", "Embedded product ID must be a positive integer")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise AdapterError("schema_changed", "Embedded CSV data must be a nonempty row array")
    headers = list(dict.fromkeys(key for row in rows for key in row if key and key not in PRICE_COLUMNS))
    if "RUNSIZE" not in headers:
        raise AdapterError("schema_changed", "Embedded table does not establish a quantity column")
    keys = [_key(header) for header in headers]
    if not all(keys) or len(set(keys)) != len(keys):
        raise AdapterError("schema_changed", "Embedded attribute names are ambiguous after normalization")
    normalized = []
    seen = set()
    values = defaultdict(dict)
    for row in rows:
        item = {}
        for header in headers:
            value = row.get(header)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
                raise AdapterError("schema_changed", "Embedded option values must be scalar")
            value = "" if value is None else str(value).strip()
            item[header] = value
            if value:
                values[_key(header)][value] = {"id": value, "label": value}
        _quantity(item["RUNSIZE"])
        item["TotalPrice"] = format(_number(row.get("TotalPrice"), "base total price"), "f")
        item["UnitPrice"] = format(_number(row.get("UnitPrice"), "embedded unit price"), "f")
        signature = tuple(item[header] for header in headers)
        if signature in seen:
            # The observed UI requires exactly one matching row, even when two
            # duplicate records happen to contain the same price.
            raise AdapterError("ambiguous_table", "Duplicate configurations do not establish a unique displayed price")
        seen.add(signature)
        normalized.append(item)
    calculators = [text for text in scripts if "function profit_for_printing(" in text]
    calculator_hash = hashlib.sha256(calculators[0].encode()).hexdigest() if len(calculators) == 1 else None
    verified = calculator_hash == CALCULATOR_SHA256 and headers[-1] == "RUNSIZE"
    member = None
    if verified:
        # Exact script pin protects every expression, threshold and list. Only
        # literal JSON integer arrays are parsed; no generic JS interpreter.
        identifiers = {7426}
        for name in ["business_product_ids_are"] + [f"marketing_product_ids_are_part_{i}" for i in range(1, 5)]:
            match = re.search(r"const\s+" + name + r"\s*=\s*(\[[\d\s,]+\])\s*;", calculators[0])
            if not match:
                raise AdapterError("schema_changed", "Pinned calculator membership list is unavailable")
            identifiers.update(json.loads(match.group(1)))
        member = product_id in identifiers
    currencies = _currencies(soup)
    if len(currencies) > 1:
        raise AdapterError("currency_required", "Page contains conflicting currency declarations")
    currency = next(iter(currencies), None)
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise AdapterError("currency_required", "Currency must be an explicit three-letter code")
    title = soup.select_one(".product_title") or soup.find("title")
    images = []
    for node in soup.select('meta[property="og:image"], .woocommerce-product-gallery__image img'):
        value = node.get("content") or node.get("data-large_image") or node.get("src")
        if value:
            value = urljoin(url, value)
            if urlsplit(value).scheme in {"http", "https"} and value not in images:
                images.append(value)
    return EmbeddedProduct(title.get_text(" ", strip=True) if title else "PrintItPrints product",
        str(product_id), normalized, headers, {key: list(value.values()) for key, value in values.items()},
        currency, images, calculator_hash, verified, member)


def retail_total(base_total, markup_member: bool) -> tuple[str, float]:
    """Mirror pinned JS Number arithmetic and toFixed(2), including boundaries."""
    base = float(_number(base_total, "base total price"))
    percent = 0
    if markup_member and base > 0:
        for upper, rate in [(25, 200), (50, 150), (100, 100), (250, 85), (600, 70),
                            (1000, 65), (1500, 50), (2000, 45)]:
            if base <= upper:
                percent = rate
                break
        else:
            percent = 40 if base < 3001 else 35
    value = base + base * (percent / 100)
    if not math.isfinite(value):
        raise AdapterError("schema_changed", "Calculated retail total is nonfinite")
    return format(Decimal.from_float(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"), value


def _records(page: EmbeddedProduct, spec: dict, evidence_id: str):
    limit = spec.get("max_configurations")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise AdapterError("invalid_selection", "max_configurations must be a positive integer")
    currency = page.currency or spec.get("source_metadata", {}).get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise AdapterError("currency_required", "No explicit source currency found")
    filters = {}
    for key, requested in (spec.get("selection_filters") or {}).items():
        name = FILTER_ALIASES.get(str(key).casefold(), _key(str(key).upper()))
        if name not in page.domains or not any(matches(choice, requested) for choice in page.domains[name]):
            raise AdapterError("invalid_selection", f"No embedded choice matches {key}")
        filters[name] = requested
    # Empty options are skipped by the source UI. Count matching row subsets
    # through indexes, so sparse rows cannot masquerade as uniquely priced rows.
    indexes = defaultdict(set)
    for i, row in enumerate(page.rows):
        for header in page.headers:
            if row[header]: indexes[(header, row[header])].add(i)
    emitted = 0
    for index, row in enumerate(page.rows):
        if any(not matches({"id": row[header], "label": row[header]}, filters[_key(header)])
               for header in page.headers if _key(header) in filters):
            continue
        if limit is not None and emitted >= limit:
            break
        sets = sorted((indexes[(header, row[header])] for header in page.headers if row[header]), key=len)
        candidates = set(sets[0])
        for group in sets[1:]:
            candidates.intersection_update(group)
            if len(candidates) == 1: break
        unique = len(candidates) == 1
        quantity = _quantity(row["RUNSIZE"])
        subtotal = unit = display_unit = None
        if page.calculator_verified and unique:
            subtotal, unrounded = retail_total(row["TotalPrice"], bool(page.markup_member))
            unit = format(Decimal(subtotal) / quantity, "f")
            display_unit = format(Decimal.from_float(unrounded / quantity).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), "f")
        reasons = list(REASONS)
        if not page.calculator_verified: reasons.append("retail_calculator_contract_unrecognized")
        if not unique: reasons.append("sparse_configuration_not_unique_in_source_calculator")
        if limit is not None and limit < len(page.rows): reasons.append("configured_configuration_limit")
        emitted += 1
        yield {
            "product_url": spec["url"], "title": page.title, "source_product_id": page.product_id,
            "supplier": "PrintItPrints", "configuration": {
                _key(header): {"value": row[header], "label": row[header]} for header in page.headers
                if header not in {"RUNSIZE", "TURNAROUND"}},
            "quantity": quantity, "currency": currency, "subtotal": subtotal, "unit_price": unit,
            "provenance": "source_table", "verification_status": "pending", "evidence_ids": [evidence_id],
            "production": {"value": row.get("TURNAROUND", ""), "label": row.get("TURNAROUND", ""), "status": "source_option"},
            "images": page.images, "coverage_status": "partial", "coverage_reasons": reasons,
            "pricing_scope": "embedded_table_calculated_retail" if subtotal is not None else "embedded_base_only_retail_unresolved",
            "tax_basis": "not_observed", "shipping": {"status": "not_quoted"},
            "pricing_components": {"embedded_base_total": row["TotalPrice"], "embedded_base_unit_price": row["UnitPrice"],
                                   "calculated_display_unit_price": display_unit},
            "pricing_context": {"supplier": "printitprints.com", "calculator_sha256": page.calculator_hash,
                "calculator_contract_matched": page.calculator_verified, "markup_membership": page.markup_member,
                "source_price_context": "public_embedded_table"},
            "source_row_index": index, "cart_verified": False,
        }


def records_from_html(html: str, spec: dict, evidence_id: str):
    """Offline import hook for HTML already stored as immutable source evidence."""
    if not evidence_id:
        raise AdapterError("evidence_required", "Saved HTML extraction requires a response evidence ID")
    yield from _records(parse_product_html(html, spec["url"]), spec, evidence_id)


class PrintItPrintsAdapter:
    name = "printitprints"
    capabilities = {"embedded_csv_variations": True, "source_pinned_retail_calculator": True,
                    "exact_shipping": False, "cart_writes": False, "requires_woocommerce_variations": False}

    async def extract(self, spec, transport, emit):
        mode = (spec.get("source_metadata") or {}).get("document_transport", "http")
        if mode == "browser":
            from ..browser_document import capture_product_document
            response = await capture_product_document(transport, spec["url"])
        elif mode == "http":
            response = await transport.fetch(spec["url"], context={"adapter": self.name, "lane": "discovery"})
        else:
            raise AdapterError("invalid_selection", "document_transport must be http or browser")
        if response.status in {401, 403}:
            raise AdapterError("access_required", "Source did not permit this product HTML request", response_id=response.response_id)
        if response.status >= 400 or not isinstance(response.data, str):
            raise AdapterError("source_error", f"Expected product HTML; received HTTP {response.status}")
        page = parse_product_html(response.data, spec["url"])
        await emit("capabilities", self.capabilities)
        await emit("discovery", {"title": page.title, "supplier": "PrintItPrints", "source_product_id": page.product_id,
            "domains": page.domains, "embedded_rows": len(page.rows), "calculator_contract_matched": page.calculator_verified})
        count = 0
        for record in _records(page, spec, response.response_id):
            count += 1
            yield record
        await emit("coverage", {"status": "partial", "rows": count, "embedded_rows": len(page.rows),
            "embedded_table_exhausted": not (spec.get("max_configurations") and count < len(page.rows)),
            "count_is_exact": False, "verification_complete": False,
            "reasons": REASONS + ([] if page.calculator_verified else ["retail_calculator_contract_unrecognized"]),
            "shipping_status": "not_quoted"})
