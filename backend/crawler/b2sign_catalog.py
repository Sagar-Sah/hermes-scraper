"""Offline public B2Sign contract inventory and finite source-state candidates.

No prices, source requests, quantity guesses or pricing-independence claims are
made here. The pinned source's Am/De, Fi and Lr/kp rules govern this subset.
Unsupported widgets/rules and missing runtime context remain explicit gaps.
Hidden controls retain their initialized request values; visibility is never a
reason to drop a pricing-key dimension. Candidates still need fresh UI/request
binding and source quotes before use as product rows.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup


PINNED_BUNDLE_SHA256 = "c41d0a9ae2290bab7005b6bece2d124153cad0b682851ae106ab00b657c1b4d3"
SOURCE_OFFSETS = {"size_values": 142819, "compare": 142999, "conditions": 144408,
                  "size_widget": 170123, "change_actions": 194258, "production": 199310,
                  "source_initialization": 228428, "auxiliary_sqft_display": 238451}
_MISSING = object()
_DISPLAY_COMPONENTS = {"divider", "price_break"}
_FINITE_COMPONENTS = {"select", "button", "hidden"}
_TENT_WALL_ACTIONS = [
    {"type":"change", "match":[{"key":"tent_full_wall","logic":"and","op":"!=","value":"0"}],
     "change":[{"key":"tent_full_wall_velcro","value":"cfw"}]},
    {"type":"change", "match":[{"key":"tent_full_wall","logic":"and","op":"=","value":"0"}],
     "change":[{"key":"tent_full_wall_velcro","value":"nfw"}]},
]


class CatalogError(ValueError):
    pass


def _reviewed_derived_fields(item_id, source_url, fields, bundle_hash):
    """One observed action-only request key; it is not an independent option.

    Pinned Lr assigns this key into the full request values object even though
    the product has no corresponding attribute. Do not generalize that fact
    into permission for arbitrary extra keys or changed action expressions.
    """
    if (item_id != 35 or urlsplit(source_url).path != '/event-tent-canopy-graphic'
            or bundle_hash != PINNED_BUNDLE_SHA256
            or any(field['key']=='tent_full_wall_velcro' for field in fields)):
        return []
    source=next((field for field in fields if field['key']=='tent_full_wall'),None)
    if (not source or source.get('component')!='select' or source.get('visible',True) is not True
            or source.get('disabled',False) is not False or source.get('action')!=_TENT_WALL_ACTIONS):
        return []
    return [{'key':'tent_full_wall_velcro','source_field':'tent_full_wall',
             'binding':'source_change_action','initialization':'absent_until_action',
             'actions':copy.deepcopy(_TENT_WALL_ACTIONS)}]


def derived_request_values(catalog, values):
    """Recompute reviewed derived keys; absence is never substituted for a value."""
    contract=catalog['contract']
    bindings=contract.get('derived_request_fields',[])
    expected=_reviewed_derived_fields(contract['item_id'],contract['source_url'],
                                    contract['attributes'],contract['source_bundle_sha256'])
    if bindings!=expected:
        raise CatalogError('unreviewed_derived_request_contract')
    result={}
    for binding in bindings:
        matched=[]
        for action in binding['actions']:
            match=evaluate_condition(values,action['match'])
            if match is None: raise CatalogError('derived_request_dependency_unresolved')
            if match: matched.append(action['change'][0]['value'])
        if len(matched)!=1: raise CatalogError('derived_request_dependency_ambiguous')
        result[binding['key']]=matched[0]
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _text(value):
    return " ".join(BeautifulSoup(str(value or ""), "html.parser").get_text(" ").split())[:500]


def _safe_source_tree(value):
    """Keep public rule data, excluding decorative media and content bodies."""
    if isinstance(value, dict):
        return {key: _safe_source_tree(item) for key, item in value.items()
                if key not in {"image", "images", "description", "notice", "adpos_key"}}
    if isinstance(value, list):
        return [_safe_source_tree(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise CatalogError("non_json_public_contract")


def _public_item(payload):
    if isinstance(payload, str):
        if len(payload) > 10_000_000:
            raise CatalogError("public_document_too_large")
        try:
            payload = json.loads(payload)
        except ValueError:
            element = BeautifulSoup(payload, "html.parser").select_one("[data-page]")
            if element is None:
                raise CatalogError("public_product_bootstrap_missing") from None
            raw = element.get("data-page", "")
            try:
                payload = json.loads(raw)
            except ValueError:
                try:
                    payload = json.loads(unquote(base64.b64decode(raw, validate=True).decode()))
                except (ValueError, UnicodeError):
                    raise CatalogError("public_product_bootstrap_invalid") from None
    if not isinstance(payload, dict):
        raise CatalogError("public_product_missing")
    if "props" in payload:
        payload = payload.get("props")
    if not isinstance(payload, dict):
        raise CatalogError("public_product_missing")
    item = payload.get("item", payload)
    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
        raise CatalogError("public_product_id_missing")
    return item


def _default(field):
    choices = [choice["key"] for choice in field.get("options", []) if choice.get("default") is True]
    if len(choices) > 1:
        return _MISSING
    if choices:
        return choices[0]
    if field.get("component") == "size":
        axes = [option for option in field.get("options", []) if option.get("key") in ("width", "height")]
        axes.sort(key=lambda option: option["key"] == "height")
        if axes and all(option.get("component_props", {}).get("range") for option in axes):
            return "x".join(str(option["component_props"]["range"][0]) for option in axes)
    if field.get("component") in _FINITE_COMPONENTS:
        return ""  # Fi's explicit empty initial value, not a free-price claim.
    return _MISSING


def parse_public_product(payload, *, source_url, source_bundle_sha256):
    """Accept public item JSON, Inertia props or HTML; return no account props."""
    parts = urlsplit(source_url)
    if (parts.scheme != "https" or parts.hostname not in {"b2sign.com", "www.b2sign.com"}
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.query or parts.fragment or parts.path.startswith(("/cart", "/checkout", "/user", "/item/"))):
        raise CatalogError("unsupported_public_product_url")
    if not isinstance(source_bundle_sha256, str) or not re.fullmatch("[0-9a-f]{64}", source_bundle_sha256):
        raise CatalogError("source_bundle_hash_required")
    item = _public_item(payload)
    if not isinstance(item.get("url"), str):
        raise CatalogError("public_product_url_missing")
    item_url = urlsplit(urljoin(source_url, item["url"]))
    if (item_url.hostname not in {"b2sign.com", "www.b2sign.com"} or item_url.scheme != "https"
            or item_url.port not in (None, 443) or item_url.path != parts.path or item_url.query or item_url.fragment or item_url.username or item_url.password):
        raise CatalogError("product_source_url_mismatch")
    attrs = item.get("attrs")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except ValueError:
            raise CatalogError("public_attributes_invalid") from None
    if not isinstance(attrs, dict) or not isinstance(attrs.get("attrs"), list):
        raise CatalogError("public_attributes_missing")
    if len(attrs["attrs"]) > 100:
        raise CatalogError("attribute_limit_exceeded")
    dimensions, rules, gaps, keys = [], [], [], set()
    for index, raw in enumerate(attrs["attrs"]):
        if not isinstance(raw, dict):
            raise CatalogError("attribute_schema_invalid")
        key = raw.get("key")
        component = raw.get("component")
        if component in _DISPLAY_COMPONENTS and not key:
            key = f"display:{index}"
        if not isinstance(key, str) or not key or len(key) > 160 or key in keys:
            raise CatalogError("attribute_key_missing_or_duplicate")
        keys.add(key)
        # Only attribute schema is retained; no sibling item/account fields.
        field = _safe_source_tree({name: raw[name] for name in
            ("key", "label", "component", "options", "visible", "disabled", "component_props", "action") if name in raw})
        field["key"] = key
        options = field.get("options", [])
        if not isinstance(options, list) or len(options) > 10000 or any(not isinstance(option, dict) for option in options):
            raise CatalogError("option_schema_invalid")
        for option in options:
            if type(option.get("key")) not in (str, int, float) or isinstance(option.get("key"), bool):
                raise CatalogError("option_key_missing")
        props = field.get("component_props") or {}
        if not isinstance(props, dict):
            raise CatalogError("component_properties_invalid")
        dimension = {"key": key, "label": _text(raw.get("label")), "component": component,
                     "visible": copy.deepcopy(field.get("visible", True)), "disabled": copy.deepcopy(field.get("disabled", False)),
                     "choices": [], "domain_kind": "unsupported", "request_value": component not in _DISPLAY_COMPONENTS}
        if component in _FINITE_COMPONENTS:
            dimension["domain_kind"] = "finite_options"
            dimension["choices"] = [{"value": option["key"], "label": _text(option.get("label")),
                "visible": copy.deepcopy(option.get("visible", True)), "disabled": copy.deepcopy(option.get("disabled", False)),
                "default": option.get("default") is True} for option in options]
        elif component == "size":
            axes = [option for option in options if option["key"] in ("width", "height")]
            dimension["axes"] = [{"key": axis["key"], "label": _text(axis.get("label")),
                "values": copy.deepcopy(axis.get("component_props", {}).get("range")),
                "domain_kind": "finite_presets" if axis.get("component_props", {}).get("range") else "open_numeric"} for axis in axes]
            dimension["domain_kind"] = "finite_size_presets" if axes and all(axis["domain_kind"] == "finite_presets" for axis in dimension["axes"]) and not props.get("plugin") else "open_or_dynamic_size"
        elif component == "input":
            dimension["domain_kind"] = "open_numeric" if props.get("format") == "number" else "open_text"
            dimension["bounds"] = {name: props[name] for name in ("min", "max", "step") if name in props}
        elif component in _DISPLAY_COMPONENTS:
            dimension["domain_kind"] = "display_only"
        if dimension["domain_kind"] in {"unsupported", "open_or_dynamic_size", "open_numeric", "open_text"}:
            gaps.append({"reason": dimension["domain_kind"], "field": key})
        dimensions.append(dimension)
        rules.append(field)
    production = _safe_source_tree(attrs.get("turnaround"))
    if production is not None and not isinstance(production, list):
        raise CatalogError("production_rules_invalid")
    # The pinned Vp flag only hides the auxiliary per-square-foot display and
    # its /item/quote-sqft call. It is not a product option or ordinary quote
    # input. Exempt only the observed exact boolean-true shape, not other flags.
    display_metadata=({"hide_sqft":True} if source_bundle_sha256==PINNED_BUNDLE_SHA256
                      and isinstance(item.get('fields'),dict)
                      and set(item['fields'])=={'hide_sqft'} and item['fields']['hide_sqft'] is True else {})
    if item.get("fields") not in (None, [], {}) and not display_metadata:
        gaps.append({"reason": "additional_product_fields_unresolved"})
    if source_bundle_sha256 != PINNED_BUNDLE_SHA256:
        gaps.append({"reason": "unreviewed_source_bundle"})
    contract = {"item_id": item["id"], "source_url": source_url, "attributes": rules, "production_rules": production,
                "email_proof_available": attrs.get("designproof"), "source_bundle_sha256": source_bundle_sha256}
    if display_metadata: contract['display_metadata']=display_metadata
    derived=_reviewed_derived_fields(item['id'],source_url,rules,source_bundle_sha256)
    if derived: contract['derived_request_fields']=derived
    return {"schema": 1, "supplier_id": "b2sign", "source_product_id": str(item["id"]),
            "url": source_url, "title": _text(item.get("title")), "dimensions": dimensions,
            "quantity_domain": {"kind": "open_numeric", "values": None, "status": "explicit_scope_required"},
            "production_rules": production, "email_proof_available": attrs.get("designproof"),
            "contract": contract, "contract_sha256": _digest(contract), "gaps": gaps,
            "source_offsets": SOURCE_OFFSETS.copy(), "source_binding_status": "requires_fresh_runtime_binding",
            "catalog_complete": False, "prices_verified": False}


def _number(value):
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, bool):
        return Decimal(int(value))
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _compare(left, right, op):
    if op in {"inside", "outside", "ss>", "ss<", "ls>", "ls<", "area=", "area>", "area<", "area>=", "area<="}:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        a, b = [_number(part) for part in left.split("x")], [_number(part) for part in right.split("x")]
        if any(value is None for value in a + b):
            return None  # Deliberately don't turn malformed measurements into 0.
        if len(a) != 2:
            return False
        a.sort(reverse=True)
        b.sort(reverse=True)
        if op in {"inside", "outside"}:
            if len(b) != 2:
                return False
            return a[0] <= b[0] and a[1] <= b[1] if op == "inside" else a[0] > b[0] and a[1] > b[1]
        if len(b) != 1:
            return False
        value = a[1] if op.startswith("ss") else a[0] if op.startswith("ls") else a[0] * a[1]
        return _compare(value, b[0], op[2:] if op.startswith(("ss", "ls")) else op[4:])
    if not (isinstance(left, str) and isinstance(right, str)):
        left, right = _number(left), _number(right)
        if left is None or right is None:
            return None
    if op in ("=", "!=", "<>", "<", ">", "<=", ">="):
        return {"=": lambda: left == right, "!=": lambda: left != right, "<>": lambda: left != right,
                "<": lambda: left < right, ">": lambda: left > right, "<=": lambda: left <= right, ">=": lambda: left >= right}[op]()
    if op in ("starts_with", "ends_with", "contains"):
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        return left.startswith(right) if op == "starts_with" else left.endswith(right) if op == "ends_with" else right in left
    # JS regex and unknown operators require an explicit source-compatible
    # interpreter, not an approximation with Python regex syntax.
    return None


def _and(left, right):
    return False if left is False or right is False else None if left is None or right is None else True


def _or(left, right):
    return True if left is True or right is True else None if left is None or right is None else False


def evaluate_condition(values, rule):
    """Source-compatible supported comparisons, unknown when context is absent.

    Unknown is intentionally more conservative than De's null fallback; it must
    never make an unsupported/missing-context branch appear proven available.
    """
    if isinstance(rule, bool) or rule is None:
        return rule
    rules = rule if isinstance(rule, list) else [rule]
    result = True
    for index, term in enumerate(rules):
        if not isinstance(term, dict):
            return None
        if term.get("op") == "group":
            current = evaluate_condition(values, term.get("value"))
        elif not isinstance(term.get("key"), str) or term["key"] not in values:
            current = None
        else:
            left, right, op = values[term["key"]], term.get("value"), term.get("op", "=")
            if op == "in":
                choices = right if isinstance(right, list) else [right]
                current = any((type(left) is type(choice) or type(left) in (int, float) and type(choice) in (int, float)) and left == choice for choice in choices)
            elif op == "exists":
                current = str(left) in right if isinstance(right, dict) else False
            else:
                current = _compare(left, right, op) if not isinstance(right, (dict, list)) else None
        logic = str(term.get("logic", "and")).lower()
        if index == 0 and logic == "or":
            logic = "and"
        if index == 0 and logic == "or_not":
            # Pinned De rewrites first-position or_not to and_not. Its switch
            # has no and_not case, so this term leaves the accumulator alone.
            logic = "and_not"
        if logic == "and_not":
            if index == 0 and current is None:
                result = False
            continue
        if logic in ("not", "ant_not", "or_not"):
            current = None if current is None else not current
        if logic in ("and", "not", "ant_not"):
            result = _and(result, current)
        elif logic in ("or", "or_not"):
            result = _or(result, current)
        else:
            return None
    return result


def size_bound_holds_without_shipping(rules, values, size, bound):
    """Prove an explicit size fits every published B2 shipping size limit.

    This is an availability intersection, not a shipping selection or quote.
    Only the two exact shipping radio values in the pinned UI are supported;
    any additional condition or ambiguous rule leaves the bound unresolved.
    """
    if not isinstance(rules, list) or bound not in {'min', 'max'}:
        return None
    if not rules:
        return True

    def fits(scope):
        matches = []
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get('value'), str):
                return None
            matched = evaluate_condition(scope, rule.get('match', []))
            if matched is None:
                return None
            if matched:
                matches.append(rule['value'])
        if len(matches) != 1:
            return None
        return _compare(matches[0], size, 'inside') if bound == 'min' else _compare(size, matches[0], 'inside')

    result = fits(values)
    if result is not None:
        return result
    if '_shipping' in values:
        # Some board products publish two dropship maximums selected by the
        # candidate size itself: short side >30 uses 46x38, long side >46
        # uses 48x30. A smaller size matches neither rule. Accept that case
        # only when it fits *both* published dropship limits; overlapping
        # matches must satisfy every applicable limit. This proves a safe
        # shipped subset without treating a missing rule as unlimited size.
        if bound == 'max' and values['_shipping'] == 'dropship':
            scoped = []
            for rule in rules:
                match = rule.get('match') if isinstance(rule, dict) else None
                if not isinstance(match, list) or not isinstance(rule.get('value'), str):
                    return None
                if (len(match) == 1 and match[0].get('key') == '_shipping'
                        and match[0].get('value') == 'storepickup'):
                    continue
                if (len(match) != 2 or match[0].get('key') != '_shipping'
                        or match[0].get('op') != '=' or match[0].get('value') != 'dropship'
                        or match[0].get('logic') != 'and'
                        or match[1].get('key') != '_size'
                        or match[1].get('op') not in {'ss>', 'ls>'}
                        or match[1].get('logic') != 'and'):
                    return None
                scoped.append(rule)
            if len(scoped) == 2:
                applicable = [rule for rule in scoped
                              if evaluate_condition(values, rule['match']) is True]
                considered = applicable or scoped
                fits_all = [_compare(size, rule['value'], 'inside') for rule in considered]
                if all(fit is True for fit in fits_all):
                    return True
                if applicable and any(fit is False for fit in fits_all):
                    return False
        return None
    modes = set()
    for rule in rules:
        match = rule.get('match') if isinstance(rule, dict) else None
        if not isinstance(match, list) or len(match) != 1:
            return None
        term = match[0]
        if (not isinstance(term, dict) or term.get('key') != '_shipping'
                or term.get('op', '=') != '=' or term.get('logic', 'and') != 'and'
                or term.get('value') not in {'dropship', 'storepickup'}):
            return None
        modes.add(term['value'])
    if modes != {'dropship', 'storepickup'}:
        return None
    resolved = [fits(dict(values, _shipping=mode)) for mode in ('dropship', 'storepickup')]
    if any(item is None for item in resolved):
        return None
    return all(resolved)


def production_choices(catalog, values, quantity, context=None):
    """Ordered first-matching source rule; unknown earlier rules block guessing."""
    scope = dict(context or {}, _qty=quantity)
    scope.update(values)
    rules = catalog["production_rules"]
    if not isinstance(rules, list):
        return {"choices": [], "status": "production_rules_missing"}
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or not isinstance(rule.get("options"), list):
            return {"choices": [], "status": "production_rule_unsupported"}
        match = evaluate_condition(scope, rule.get("match", True))
        if match is None:
            return {"choices": [], "status": "production_match_unresolved"}
        if not match:
            continue
        choices = []
        for option in rule["options"]:
            if not isinstance(option, dict) or not isinstance(option.get("key"), str):
                return {"choices": [], "status": "production_option_unsupported"}
            visible = option.get("visible", True)
            if visible is False:
                continue
            enabled = evaluate_condition(scope, visible)
            if option.get("disabled") is True:
                enabled = False
            choices.append({"value": option["key"], "label": _text(option.get("label")),
                            "available": enabled, "rule_index": index})
        if any(choice["value"] in {"v-1", "v-2"} and choice["available"] is True for choice in choices):
            for choice in choices:
                if choice["value"] not in {"v-1", "v-2"}:
                    choice["available"] = False
        if len({choice["value"] for choice in choices}) != len(choices):
            return {"choices": [], "status": "production_option_ids_ambiguous"}
        return {"choices": choices, "status": "resolved" if all(choice["available"] is not None for choice in choices) else "production_availability_unresolved"}
    return {"choices": [], "status": "no_matching_production_rule"}


class CatalogEnumeration:
    """Streaming finite candidates; .summary() reports all known coverage gaps."""
    def __init__(self, catalog, *, quantities, context=None, explicit_sizes=None, max_candidates=100000):
        if (not isinstance(quantities, (list, tuple)) or not quantities
                or any(type(quantity) is not int or quantity <= 0 for quantity in quantities)
                or len(set(quantities)) != len(quantities)):
            raise CatalogError("explicit_positive_quantities_required")
        if type(max_candidates) is not int or max_candidates <= 0:
            raise CatalogError("positive_candidate_limit_required")
        self.catalog, self.quantities = copy.deepcopy(catalog), list(quantities)
        self.context, self.limit = copy.deepcopy(context or {}), max_candidates
        self.explicit_sizes = copy.deepcopy(explicit_sizes or {})
        size_fields = {field['key']: field for field in catalog['contract']['attributes']
                       if field.get('component') == 'size'}
        if (not isinstance(self.explicit_sizes, dict) or set(self.explicit_sizes) - set(size_fields)
                or any((field.get('component_props') or {}).get('plugin')
                       or all(isinstance(option.get('component_props', {}).get('range'), list)
                              and option['component_props']['range'] for option in field.get('options', []))
                       for key, field in size_fields.items() if key in self.explicit_sizes)
                or any(not isinstance(sizes, list) or not 1 <= len(sizes) <= 100
                       or len(set(map(str, sizes))) != len(sizes)
                       or any(not isinstance(size, str) or not re.fullmatch(r'\d+(?:\.\d+)?x\d+(?:\.\d+)?', size)
                              or any(_number(part) <= 0 for part in size.split('x')) for size in sizes)
                       for sizes in self.explicit_sizes.values())):
            raise CatalogError('invalid_explicit_size_scope')
        if set(self.context) - {"_time", "_business_day", "_shipping", "_channel_letter_turnaround"}:
            raise CatalogError("unsupported_context_keys")
        if ("_time" in self.context and (not isinstance(self.context["_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", self.context["_time"]))
                or "_business_day" in self.context and type(self.context["_business_day"]) is not bool
                or "_shipping" in self.context and (not isinstance(self.context["_shipping"], str) or self.context["_shipping"] not in {"", "storepickup", "dropship"})
                or "_channel_letter_turnaround" in self.context and (not isinstance(self.context["_channel_letter_turnaround"], str) or len(self.context["_channel_letter_turnaround"]) > 100)):
            raise CatalogError("invalid_public_runtime_context")
        self.gaps = copy.deepcopy(catalog["gaps"])
        for key in self.explicit_sizes:
            self.gaps = [gap for gap in self.gaps if not (gap.get('field') == key
                         and gap.get('reason') == 'open_or_dynamic_size')]
            self.gaps.append({'reason': 'explicit_size_scope_only', 'field': key})
        self.count = self.visited = 0
        self.exhausted = self.limited = False
        self._iterator = self._generate()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def _gap(self, reason, field=None):
        item = {"reason": reason, **({"field": field} if field is not None else {})}
        if item not in self.gaps:
            self.gaps.append(item)

    def summary(self):
        return {"candidates_emitted": self.count, "states_visited": self.visited, "explicit_quantities": self.quantities,
                "finite_scope_exhausted": self.exhausted and not self.limited and not self.gaps,
                "enumeration_stopped": self.limited, "gaps": copy.deepcopy(self.gaps),
                "coverage": "source_initialized_finite_attributes_at_explicit_quantities",
                "hidden_state_history_exhausted": False, "catalog_complete": False, "prices_verified": False}

    def _domain(self, field, values, visible):
        component = field.get("component")
        if component in _DISPLAY_COMPONENTS:
            return [_MISSING]
        if component in _FINITE_COMPONENTS:
            available = []
            for option in field.get("options", []):
                if option.get("component") == "divider":
                    continue
                if option.get("component") == "custom":
                    self._gap("custom_option_requires_calculator", field["key"])
                    continue
                # The source can insert unnamed synthetic select entries (for
                # example `_v4` in Reflective Aluminum Sign's hole punches).
                # They have no customer-facing choice and return no quote.
                if (visible and component == "select"
                        and re.fullmatch(r"_v\d+", str(option.get("key", "")))
                        and not _text(option.get("label"))):
                    continue
                showing = evaluate_condition(values, option.get("visible", True))
                disabled = evaluate_condition(values, option.get("disabled", False))
                if showing is None or disabled is None:
                    self._gap("option_availability_unresolved", field["key"])
                    continue
                if showing and not disabled:
                    available.append(option)
            keys = [option["key"] for option in available]
            if len({_digest(key) for key in keys}) != len(keys):
                self._gap("ambiguous_active_option_values", field["key"])
                return []
            if visible and component != "hidden":
                return keys
            current = values.get(field["key"], _MISSING)
            if current in keys:
                return [current]
            defaults = [option["key"] for option in available if option.get("default")]
            if len(defaults) > 1:
                self._gap("ambiguous_initial_default", field["key"])
                return []
            return defaults or [""]
        if component == "size":
            props = field.get("component_props") or {}
            if props.get("plugin"):
                self._gap("dynamic_size_plugin", field["key"])
                return []
            axes = [option for option in field.get("options", []) if option.get("key") in ("width", "height")]
            axes.sort(key=lambda option: option["key"] == "height")
            if len({axis["key"] for axis in axes}) != len(axes) or len(axes) != len(field.get("options", [])):
                self._gap("unsupported_size_axes", field["key"])
                return []
            ranges = [axis.get("component_props", {}).get("range") for axis in axes]
            if not axes or any(not isinstance(items, list) or not items for items in ranges):
                scoped = self.explicit_sizes.get(field['key'])
                if not scoped or len(axes) != 2:
                    self._gap("open_size_dimension", field["key"])
                    return []
                available = []
                for size in scoped:
                    valid = True
                    for bound in ('min', 'max'):
                        allowed = size_bound_holds_without_shipping(props.get(bound, []), values, size, bound)
                        if allowed is None:
                            # A later attribute may choose the conditional bound.
                            # Admit only this explicit size provisionally; the
                            # completed row is checked again in _walk.
                            continue
                        if not allowed:
                            valid = False; break
                    if valid: available.append(size)
                return available if visible else [values.get(field['key'], available[0])] if available else []
            if any(any(type(value) not in (str, int, float) or _number(value) is None or _number(value) <= 0 for value in items) for items in ranges):
                self._gap("unsupported_size_presets", field["key"])
                return []
            if not visible:
                default = values.get(field["key"], _default(field))
                return [] if default is _MISSING else [default]
            def sizes():
                for dimensions in itertools.product(*ranges):
                    value = "x".join(str(number) for number in dimensions)
                    allowed = True
                    for bound in ("min", "max"):
                        if bound not in props:
                            continue
                        if not isinstance(props[bound], str) or len(dimensions) != 2:
                            self._gap("conditional_size_bound_unresolved", field["key"])
                            allowed = False
                            break
                        fit = _compare(props[bound], value, "inside") if bound == "min" else _compare(value, props[bound], "inside")
                        if fit is not True:
                            allowed = False
                    if allowed:
                        yield value
            return sizes()
        self._gap("unsupported_or_open_dimension", field["key"])
        return []

    def _actions(self, field, values):
        result = values.copy()
        for action in field.get("action", []):
            if not isinstance(action, dict):
                self._gap("unsupported_change_action", field["key"])
                return None
            if action.get("type") == "click":
                continue  # Pinned click handler changes only decorative media.
            if action.get("type") != "change" or not isinstance(action.get("change"), list):
                self._gap("unsupported_change_action", field["key"])
                return None
            match = evaluate_condition(result, action.get("match", True))
            if match is None:
                self._gap("change_action_match_unresolved", field["key"])
                return None
            if not match:
                continue
            for change in action["change"]:
                key, value = change.get("key"), change.get("value")
                if key in {"_image", "_description"}:
                    continue
                known_keys={item["key"] for item in self.catalog["contract"]["attributes"]}
                derived_keys={item['key'] for item in self.catalog['contract'].get('derived_request_fields',[])}
                if key not in known_keys|derived_keys or not isinstance(value, str):
                    self._gap("unsupported_change_target", field["key"])
                    return None
                reference = re.fullmatch(r"\{\$([a-zA-Z0-9_]+)\}", value)
                if reference:
                    if reference[1] not in result:
                        self._gap("change_reference_unresolved", field["key"])
                        return None
                    value = result[reference[1]]
                result[key] = value
        return result

    def _walk(self, fields, index, values, visibility):
        self.visited += 1
        if self.visited > self.limit * 50:
            self.limited = True
            self._gap("enumeration_visit_limit")
            return
        if index == len(fields):
            for field in fields:
                if field.get('component') != 'size' or field['key'] not in self.explicit_sizes:
                    continue
                size = values.get(field['key'])
                props = field.get('component_props') or {}
                for bound in ('min', 'max'):
                    allowed = size_bound_holds_without_shipping(props.get(bound, []), values, size, bound)
                    if allowed is None:
                        self._gap('conditional_size_bound_unresolved', field['key'])
                        return
                    if not allowed:
                        return
            # Actions may alter an earlier field. Reject values that no longer
            # satisfy its visible option domain instead of publishing a reset.
            for field in fields:
                visible = evaluate_condition(values, field.get("visible", True))
                if visible is None or visible != visibility[field["key"]]:
                    self._gap("visibility_changed_after_dependency", field["key"])
                    return
                disabled = evaluate_condition(values, field.get("disabled", False))
                if disabled is None:
                    self._gap("attribute_availability_unresolved", field["key"])
                    return
                if disabled:
                    if values.get(field["key"]) != "":
                        self._gap("disabled_value_requires_source_reset", field["key"])
                        return
                    continue
                if field.get("component") in _FINITE_COMPONENTS and visible:
                    if values.get(field["key"]) not in self._domain(field, values, True):
                        self._gap("changed_value_requires_source_reset", field["key"])
                        return
            yield values, visibility
            return
        field = fields[index]
        visible = evaluate_condition(values, field.get("visible", True))
        disabled = evaluate_condition(values, field.get("disabled", False))
        if visible is None or disabled is None:
            self._gap("attribute_availability_unresolved", field["key"])
            return
        domain = [""] if disabled else self._domain(field, values, visible)
        for choice in domain:
            if self.limited:
                return
            selected = values.copy()
            if choice is not _MISSING:
                selected[field["key"]] = choice
            if visible and not disabled:
                selected = self._actions(field, selected)
                if selected is None:
                    continue
            yield from self._walk(fields, index + 1, selected, dict(visibility, **{field["key"]: visible}))

    def _generate(self):
        if self.catalog["contract"]["source_bundle_sha256"] != PINNED_BUNDLE_SHA256:
            self.exhausted = True
            return
        fields = self.catalog["contract"]["attributes"]
        defaults = {field["key"]: value for field in fields if (value := _default(field)) is not _MISSING}
        for quantity in self.quantities:
            initial = {**self.context, **defaults, "_qty": quantity}
            for values, visibility in self._walk(fields, 0, initial, {}):
                production = production_choices(self.catalog, values, quantity, self.context)
                if production["status"] != "resolved":
                    self._gap(production["status"])
                for option in production["choices"]:
                    if option["available"] is not True:
                        continue
                    if self.count >= self.limit:
                        self.limited = True
                        self._gap("candidate_limit")
                        return
                    request_values = {field["key"]: values[field["key"]] for field in fields
                                      if field.get("component") not in _DISPLAY_COMPONENTS and field["key"] in values}
                    derived=derived_request_values(self.catalog,values)
                    if any(key not in values or values[key]!=value for key,value in derived.items()):
                        self._gap('derived_request_state_unresolved')
                        continue
                    request_values.update(derived)
                    scope = {"source_url": self.catalog["url"], "source_product_id": self.catalog["source_product_id"],
                             "values": request_values, "quantity": quantity, "turnaround": option["value"],
                             "runtime_context": self.context, "contract_sha256": self.catalog["contract_sha256"]}
                    self.count += 1
                    yield {**scope, "candidate_key": _digest(scope), "visible": visibility,
                           "production": option, "status": "source_rule_candidate_requires_quote", "final_eligible": False,
                           "shipping_status": "unresolved", "proof_status": "explicit_selection_required",
                           "source_binding_status": "requires_fresh_runtime_binding",
                           "prices_verified": False, "pricing_independence": "not_established"}
        self.exhausted = True


def enumerate_catalog(catalog, *, quantities, context=None, explicit_sizes=None, max_candidates=100000):
    return CatalogEnumeration(catalog, quantities=quantities, context=context,
                              explicit_sizes=explicit_sizes, max_candidates=max_candidates)
