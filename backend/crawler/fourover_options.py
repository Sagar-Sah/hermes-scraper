"""Inspect declared Magento option bindings without executing source JavaScript.

Native controls describe the supplied document only. A complete binding means
that the one source declaration agrees in both directions; it makes no price,
service, production, fulfillment or whole-product completion claim.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup
from .fourover_bulk import child_configuration_limit


_ID = re.compile(r"[1-9][0-9]{0,12}\Z")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_PRIVATE = re.compile(
    r"secret|token|cookie|password|authorization|credential|session|nonce|"
    r"form.?key|api.?key|private.?key|access.?key|account|customer|header|"
    r"price|currency|amount|subtotal|grand.?total|tax", re.I
)
_PATH = re.compile(r"[A-Za-z0-9_./*#:\[\]-]{1,160}\Z")
_MAX_ATTRIBUTES = 32


class _Invalid(ValueError):
    pass


def _id(value):
    if type(value) not in (str, int) or not _ID.fullmatch(str(value)):
        raise _Invalid("invalid_numeric_identifier")
    return str(value)


def _label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 300
            or any(ord(char) < 32 and char not in "\t\r\n" for char in value)):
        raise _Invalid("invalid_source_label")
    return value.strip()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Invalid("duplicate_json_key")
        result[key] = value
    return result


def _native_controls(soup):
    rows, rejected = [], 0
    selects = soup.select('select[name^="super_attribute["]')
    for select in selects[:64]:
        matched = re.fullmatch(r"super_attribute\[([1-9][0-9]{0,12})\]", select.get("name", ""))
        if not matched:
            rejected += 1
            continue
        identifier = matched.group(1)
        control_id = select.get("id")
        label = soup.find("label", attrs={"for": control_id}) if control_id else None
        if label is None:
            parent = select.find_parent("label")
            label = parent if parent is not None else None
        try:
            label_text = _label(label.get_text(" ", strip=True)) if label else None
        except _Invalid:
            label_text = None
        choices, seen, placeholders = [], set(), 0
        native_options = select.find_all("option")
        for option in native_options[:2001]:
            value = option.get("value", "")
            if value == "":
                placeholders += 1
                continue
            try:
                value = _id(value)
                choice = _label(option.get_text(" ", strip=True))
                if value in seen:
                    raise _Invalid("duplicate_native_option")
            except _Invalid:
                rejected += 1
                continue
            seen.add(value)
            choices.append({"value": value, "label": choice,
                            "disabled": option.has_attr("disabled"),
                            "selected": option.has_attr("selected")})
        rows.append({"attribute_id": identifier,
                     "id": control_id if control_id == "attribute" + identifier else None,
                     "name": "super_attribute[" + identifier + "]", "label": label_text,
                     "disabled": select.has_attr("disabled"), "choices": choices,
                     "placeholder_count": placeholders,
                     "choices_truncated": len(native_options) > 2001})
    return rows, rejected, len(selects)


def _bind(candidate, children, maximum_children):
    raw_attributes, index = candidate.get("attributes"), candidate.get("index")
    if (not isinstance(raw_attributes, dict) or not 1 <= len(raw_attributes) <= _MAX_ATTRIBUTES
            or not isinstance(index, dict)):
        raise _Invalid("invalid_attribute_index_schema")
    child_set = set(children)
    if set(index) != child_set:
        raise _Invalid("declared_child_set_mismatch")
    attributes, definitions, codes = [], {}, set()
    for key, raw in raw_attributes.items():
        identifier = _id(key)
        if not isinstance(raw, dict) or _id(raw.get("id")) != identifier:
            raise _Invalid("attribute_identifier_mismatch")
        code = raw.get("code")
        if (not isinstance(code, str) or not _CODE.fullmatch(code)
                or _PRIVATE.search(code) or code in codes):
            raise _Invalid("invalid_or_duplicate_attribute_code")
        codes.add(code)
        label = _label(raw.get("label"))
        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= maximum_children:
            raise _Invalid("invalid_options_schema")
        options, by_id = [], {}
        for option in raw_options:
            if not isinstance(option, dict):
                raise _Invalid("invalid_option_schema")
            value = _id(option.get("id"))
            products = option.get("products")
            if not isinstance(products, list) or len(products) > maximum_children:
                raise _Invalid("missing_option_product_membership")
            products = [_id(product) for product in products]
            if (value in by_id or len(set(products)) != len(products)
                    or not set(products) <= child_set):
                raise _Invalid("duplicate_or_unknown_option_binding")
            safe = {"id": value, "label": _label(option.get("label")), "products": products}
            by_id[value] = safe
            options.append(safe)
        attribute = {"id": identifier, "code": code, "label": label, "options": options}
        attributes.append(attribute)
        definitions[identifier] = (attribute, by_id)
    rows, signatures = [], set()
    expected = {(attribute, option): set() for attribute, (_, choices) in definitions.items()
                for option in choices}
    for child in children:
        values = index[child]
        if not isinstance(values, dict) or set(values) != set(definitions):
            raise _Invalid("incomplete_child_attributes")
        fields = []
        for attribute_id, (attribute, options) in definitions.items():
            value = _id(values[attribute_id])
            if value not in options:
                raise _Invalid("undeclared_attribute_value")
            expected[attribute_id, value].add(child)
            fields.append({"attribute_id": attribute_id, "code": attribute["code"],
                           "label": attribute["label"], "value": value,
                           "choice": options[value]["label"]})
        signature = tuple((field["attribute_id"], field["value"]) for field in fields)
        if signature in signatures:
            raise _Invalid("duplicate_child_configuration")
        signatures.add(signature)
        rows.append({"child_id": child, "fields": fields})
    for attribute_id, (_, choices) in definitions.items():
        for value, option in choices.items():
            if set(option["products"]) != expected[attribute_id, value]:
                raise _Invalid("option_product_membership_mismatch")
    return attributes, rows


def inspect_option_contract(html, children, *, source_url=None):
    """Return whitelisted source declarations or an explicit incomplete result.

    ``children`` is the product's already parsed childSkuConfig list. SKUs are
    never inspected. Unknown/scalar account and price material is never returned.
    Only declarative JSON is parsed; executable scripts are not interpreted.
    """
    result = {"schema": "hermes-fourover-option-contract/v1", "status": "unsupported",
              "reason_code": "option_contract_missing", "binding_complete": False,
              "native_select_scope": "supplied_document_dom_only_not_full_domain",
              "native_selects": [], "attributes": [], "rows": [],
              "diagnostic": {}, "source_data_complete": False,
              "catalog_complete": False, "final_eligible": False}
    maximum_children = child_configuration_limit(source_url)
    try:
        if not isinstance(html, str) or len(html.encode("utf-8")) > 8 * 1024 * 1024:
            raise _Invalid("invalid_or_oversized_document")
        if not isinstance(children, list) or not 1 <= len(children) <= maximum_children:
            raise _Invalid("invalid_declared_children")
        child_ids = [_id(row.get("child_id")) if isinstance(row, dict)
                     else _id(None) for row in children]
        if len(set(child_ids)) != len(child_ids):
            raise _Invalid("duplicate_declared_children")
    except (_Invalid, UnicodeError) as exc:
        result["reason_code"] = str(exc) if isinstance(exc, _Invalid) else "invalid_document_encoding"
        return result
    soup = BeautifulSoup(html, "html.parser")
    native, rejected, native_count = _native_controls(soup)
    result["native_selects"] = native
    initializers = [script.get_text() for script in soup.select(
        'script[type="text/x-magento-init"], script[type="application/json"]')]
    initializers.extend(node.get("data-mage-init", "") for node in soup.select("[data-mage-init]"))
    diagnostic = {"declared_child_count": len(child_ids), "initializer_count": len(initializers),
                  "invalid_json_count": 0, "candidate_count": 0,
                  "rejected_native_choice_count": rejected, "sensitive_branches_omitted": 0,
                  "native_select_count": native_count, "native_selects_truncated": native_count > 64,
                  "module_summaries": []}
    result["diagnostic"] = diagnostic
    if len(initializers) > 256:
        result.update(status="partial", reason_code="initializer_limit")
        return result
    candidates, budget = [], [200000]

    def walk(node, path=(), depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 40:
            raise _Invalid("declaration_structure_limit")
        if isinstance(node, dict):
            if "attributes" in node and "index" in node:
                candidates.append(node)
            if path and ("/js/" in path[-1] or path[-1] in {"spConfig", "jsonConfig"}):
                if len(diagnostic["module_summaries"]) < 64:
                    diagnostic["module_summaries"].append({
                        "path": "/".join(key if _PATH.fullmatch(key) else "<other>" for key in path),
                        "keys": [key for key in node if _CODE.fullmatch(key) and not _PRIVATE.search(key)][:32],
                        "key_count": len(node)})
            for key, value in node.items():
                if _PRIVATE.search(key):
                    diagnostic["sensitive_branches_omitted"] += 1
                    continue
                walk(value, path + (key,), depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, path + ("[]",), depth + 1)

    try:
        for text in initializers:
            try:
                parsed = json.loads(text, object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                diagnostic["invalid_json_count"] += 1
                if '"attributes"' in text and '"index"' in text:
                    raise _Invalid("unreadable_option_contract")
                continue
            walk(parsed)
        diagnostic["candidate_count"] = len(candidates)
        if len(candidates) > 1:
            result["reason_code"] = "ambiguous_option_contract"
        elif candidates:
            attributes, rows = _bind(candidates[0], child_ids, maximum_children)
            result.update(status="declared_binding_complete", reason_code="validated_declared_binding",
                          binding_complete=True, attributes=attributes, rows=rows)
    except (_Invalid, RecursionError) as exc:
        diagnostic["candidate_count"] = len(candidates)
        result.update(status="partial", reason_code=str(exc) if isinstance(exc, _Invalid)
                      else "declaration_structure_limit")
    return result
