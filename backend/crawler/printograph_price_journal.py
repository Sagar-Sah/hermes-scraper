"""Passive owned-page evidence for closed reviewed Printograph families.

No import-time work, manager integration, source calls or completion writes.
The caller supplies already-observed controller/bundle captures and performs any
separately authorized native choice. This module only projects completed native
GET responses. A matched component is not a finalized product, payable quote,
production/coupon verification, or WooCommerce import.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from urllib.parse import urlsplit

from .price_contract import (
    BASE_FIELDS, NORMAL_CONTEXT_FLAGS, OPTION_FIELDS, SPECIAL_SHAPES_SOURCE_CONTRACT,
    SPECIAL_SHAPES_SOURCE_URL, STAGGERED_CUT_SOURCE_URL, calculate_source_total, normalize_price_context,
    round_source_component, STAGGERED_LITERAL_BASE_QUERY, staggered_literal_base_query,
)

SOURCE_URL = SPECIAL_SHAPES_SOURCE_URL
CONTRACT = dict(SPECIAL_SHAPES_SOURCE_CONTRACT)
TTL_SECONDS = 300
MAX_BODY = 512 * 1024
MAX_SESSION_BUFFER = 2 * 1024 * 1024
MAX_JOURNAL = 1024 * 1024
MAX_ROWS = 512
MAX_OPTIONS = 128
MAX_REQUESTS = 32
MAX_EPOCHS = 8
MAX_QUANTITY = 10_000_000
_KEYS = (*BASE_FIELDS['39'], *OPTION_FIELDS['39'], 'proofType', 'manualProofType',
         'designServices', 'fileDelivery', 'quantity', 'productId', 'productionTime')
_BASE_KEYS = (*BASE_FIELDS['39'], 'productionTime')
_MONEY = re.compile(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,8})?\Z')
_ID = re.compile(r'[0-9]{1,12}\Z')
_HEX_ID = re.compile(r'[a-f0-9]{32}\Z')


class JournalError(ValueError):
    """Only fixed safe enums are used; never wrap a source exception message."""


def _fail(reason):
    raise JournalError(reason)



@dataclass(frozen=True)
class PassiveFamily:
    product_type: str
    source_url: str
    base_fields: tuple
    option_fields: tuple
    manual_upload_only: bool = False

    @property
    def keys(self):
        return (*self.base_fields, *self.option_fields, 'proofType', 'manualProofType',
                'designServices', 'fileDelivery', 'quantity', 'productId', 'productionTime')


SPECIAL_FAMILY = PassiveFamily('39', SOURCE_URL, tuple(BASE_FIELDS['39']), tuple(OPTION_FIELDS['39']))
STAGGERED_FAMILY = PassiveFamily('38', STAGGERED_CUT_SOURCE_URL, tuple(BASE_FIELDS['38']), tuple(OPTION_FIELDS['38']), True)


def passive_family(value=None):
    # Identity-bound constants only; a caller-created lookalike is not policy.
    if value is None or value is SPECIAL_FAMILY:
        return SPECIAL_FAMILY
    if value is STAGGERED_FAMILY:
        return STAGGERED_FAMILY
    _fail('unsupported_passive_family')


def family_for_url(url):
    for value in (SPECIAL_FAMILY, STAGGERED_FAMILY):
        if type(url) is str and url == value.source_url:
            return value
    _fail('unsupported_passive_family')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _integer(value, *, maximum=999_999_999_999, empty=False, zero=False):
    if empty and value == '' and type(value) is str:
        return ''
    if type(value) not in (str, int) or not _ID.fullmatch(str(value)):
        _fail('invalid_numeric_identifier')
    number = int(value)
    if type(value) is str and str(number) != value:
        _fail('invalid_numeric_identifier')
    if number > maximum or number < (0 if zero else 1):
        _fail('numeric_identifier_bound')
    return str(number)


def _money(value, *, positive=False):
    if type(value) is not str or len(value) > 64 or not _MONEY.fullmatch(value):
        _fail('invalid_markup_price')
    plain = value.replace(',', '')
    try:
        number = Decimal(plain)
    except InvalidOperation:
        _fail('invalid_markup_price')
    if not number.is_finite() or number >= Decimal('1000000000') or number < 0 or (positive and not number):
        _fail('markup_price_bound')
    return {'markup_price': plain, 'source_cents': round_source_component(plain)}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_json_key')
        result[key] = value
    return result


def _parse_integer(text):
    if len(text) > 16:
        _fail('json_number_bound')
    return int(text)


def _parse_decimal(text):
    if len(text) > 64:
        _fail('json_number_bound')
    # Decimal does not expand exponent text. Unknown values are never formatted.
    try:
        return Decimal(text)
    except InvalidOperation:
        _fail('json_number_bound')


@dataclass(frozen=True)
class RequestProjection:
    kind: str
    product_id: str = ''
    option_id: str = ''
    quantity_filter: str = ''
    lookup: tuple = ()
    base_query_profile: str = ''

    def public(self):
        value = {'kind': self.kind}
        for key, item in (('product_id', self.product_id), ('source_option_id', self.option_id),
                          ('quantity_filter', self.quantity_filter)):
            if item:
                value[key] = item
        if self.lookup:
            value['numeric_query'] = dict(self.lookup)
        if self.base_query_profile == 'source_literal_pair_v1':
            value['base_price_query'] = dict(STAGGERED_LITERAL_BASE_QUERY)
        return value


def project_request(url, method='GET', resource_type='XHR', *, family=None, base_price_query=None):
    """Exact source-native GET classes only; no returned URL or query text."""
    family = passive_family(family)
    if method != 'GET' or resource_type not in {'XHR', 'Fetch'}:
        _fail('unsupported_request_kind')
    if (type(url) is not str or len(url) > 2048 or not url.isascii()
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url) or any(c in url for c in '%\\#')):
        _fail('invalid_source_url')
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'www.printograph.com' or parts.fragment:
        _fail('unsupported_source_origin')
    query = {}
    segments = parts.query.split('&') if parts.query else []
    if segments and segments[-1] == '':
        segments.pop()  # The reviewed product lookup builder can leave one trailing &.
    if len(segments) > 8:
        _fail('unsupported_query')
    for item in segments:
        pair = item.split('=')
        if len(pair) != 2 or not pair[0] or pair[0] in query:
            _fail('unsupported_query')
        query[pair[0]] = pair[1]
    if family is STAGGERED_FAMILY and parts.path == '/service/rest/v1/products/quantities':
        if set(query) != {'productType', 'includeTurnaround', *family.base_fields} or query['includeTurnaround'] != 'true':
            _fail('unsupported_query')
        numeric = {k: _integer(v) for k, v in query.items() if k != 'includeTurnaround'}
        if numeric['productType'] != family.product_type or numeric['orientation'] not in {'1','2'} or numeric['color'] not in {'1','3'}:
            _fail('unsupported_lookup_scope')
        return RequestProjection('quantity_domain', lookup=tuple(sorted(numeric.items())))
    if parts.path == '/service/rest/v1/products':
        if set(query) != {'productType', 'turnaround', *family.base_fields}:
            _fail('unsupported_query')
        numeric = {k: _integer(v) for k, v in query.items()}
        if (numeric['productType'] != family.product_type or numeric['turnaround'] not in {'1', '3'}
                or numeric['orientation'] not in {'1', '2'} or numeric['color'] not in ({'1','3'} if family is STAGGERED_FAMILY else {'1','2','3'})):
            _fail('unsupported_lookup_scope')
        return RequestProjection('product_identity', lookup=tuple(sorted(numeric.items())))
    match = re.fullmatch(r'/service/rest/v1/products/([0-9]{1,12})/(options|prices)', parts.path)
    if match:
        product_id = _integer(match[1])
        if match[2] == 'options':
            if query != {'requestValues': 'true', 'eddm': 'false'}:
                _fail('unsupported_query')
            return RequestProjection('option_identity', product_id)
        if query:
            if (family is not STAGGERED_FAMILY or not staggered_literal_base_query(base_price_query)
                    or parts.query != 'itemId=null&cid='):
                _fail('unsupported_query')
            return RequestProjection('base_price', product_id, base_query_profile='source_literal_pair_v1')
        return RequestProjection('base_price', product_id)
    match = re.fullmatch(r'/service/rest/v1/products/([0-9]{1,12})/options/([0-9]{1,12})/prices', parts.path)
    if match:
        if set(query) - {'quantities'}:
            _fail('unsupported_query')
        quantity = _integer(query['quantities'], maximum=MAX_QUANTITY) if query else ''
        return RequestProjection('option_price', _integer(match[1]), _integer(match[2]), quantity)
    _fail('unsupported_source_path')


def rejected_request_shape(url, method, resource_type):
    """Fixed public endpoint/query names and value classes only; never raw IDs."""
    try:
        if type(url) is not str or len(url) > 2048 or not url.isascii() or any(ord(c) < 33 for c in url):
            return None
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != 'www.printograph.com' or parts.fragment:
            return None
        path = parts.path
        if path == '/service/rest/v1/products': kind = 'product_identity'
        elif path == '/service/rest/v1/products/quantities': kind = 'quantity_domain'
        elif re.fullmatch(r'/service/rest/v1/products/[0-9]{1,12}/prices',path): kind = 'base_price'
        elif re.fullmatch(r'/service/rest/v1/products/[0-9]{1,12}/options',path): kind = 'option_identity'
        elif re.fullmatch(r'/service/rest/v1/products/[0-9]{1,12}/options/[0-9]{1,12}/prices',path): kind = 'option_price'
        else: return None
        allowed = {'productType','turnaround','shape','size','orientation','paper','color',
                   'includeTurnaround','requestValues','eddm','quantities','itemId','cid','priceGroup','designService'}
        pairs = parts.query.split('&') if parts.query else []
        classes, counts, unknown = {}, {}, 0
        for pair in pairs[:16]:
            key, sep, value = pair.partition('=')
            if key not in allowed:
                unknown += 1; continue
            counts[key] = counts.get(key,0) + 1
            label = ('missing_equals' if not sep else 'blank_literal' if value == '' else
                     'null_literal' if value == 'null' else 'true_literal' if value == 'true' else
                     'false_literal' if value == 'false' else 'integer' if _ID.fullmatch(value) else 'redacted')
            classes.setdefault(key,set()).add(label)
        return {'endpoint_kind':kind,'method_class':'GET' if method == 'GET' else 'other',
                'resource_type_class':resource_type if resource_type in {'XHR','Fetch'} else 'other',
                'query_value_classes':{key:sorted(value) for key,value in sorted(classes.items())},
                'duplicate_known_query_keys':sorted(key for key,count in counts.items() if count > 1),
                'unknown_query_key_count':unknown,'query_pairs_over_bound':len(pairs)>16}
    except Exception:
        return None

def project_body(spec, raw):
    """Parse only bounded numeric catalog/price fields; never return raw JSON."""
    if not isinstance(spec, RequestProjection) or type(raw) is not bytes or not 0 < len(raw) <= MAX_BODY:
        _fail('response_body_bound')
    try:
        data = json.loads(raw, object_pairs_hook=_unique, parse_int=_parse_integer,
                          parse_float=_parse_decimal, parse_constant=lambda _: _fail('invalid_json_number'))
    except JournalError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        _fail('invalid_response_json')
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        _fail('response_items_missing')
    items = data['items']
    limit = MAX_OPTIONS if spec.kind == 'option_identity' else MAX_ROWS
    if not 0 < len(items) <= limit or any(not isinstance(item, dict) for item in items):
        _fail('response_items_bound')
    if spec.kind == 'quantity_domain':
        values = [_integer(item.get('quantity'), maximum=MAX_QUANTITY) for item in items]
        if len(values) != len(set(values)):
            _fail('quantity_row_ambiguous')
        return {'kind':spec.kind, 'quantities':sorted(values,key=int),
                'observational_only':True, 'production_specific':False}
    if spec.kind == 'product_identity':
        if len(items) != 1:
            _fail('product_identity_ambiguous')
        return {'kind': spec.kind, 'product_id': _integer(items[0].get('id')), 'item_count': 1}
    if spec.kind == 'option_identity':
        mapping = {}
        for item in items:
            option_id = _integer(item.get('id'))
            type_data = item.get('type')
            if not isinstance(type_data, dict):
                _fail('option_type_missing')
            type_id = _integer(type_data.get('id'))
            if option_id in mapping and mapping[option_id] != type_id:
                _fail('option_identity_conflict')
            mapping[option_id] = type_id
        return {'kind': spec.kind, 'mapping': dict(sorted(mapping.items()))}
    if spec.kind not in {'base_price', 'option_price'}:
        _fail('unsupported_response_kind')
    rows = {}
    for item in items:
        quantity = _integer(item.get('quantity'), maximum=MAX_QUANTITY)
        if quantity in rows:
            _fail('quantity_row_ambiguous')
        rows[quantity] = _money(item.get('markupPrice'), positive=spec.kind == 'base_price')
    return {'kind': spec.kind, 'rows': dict(sorted(rows.items(), key=lambda row: int(row[0])))}


def project_context(capture, *, complete=True, family=None):
    """Use the existing pinned pure normalizer, without accessing source methods."""
    family = passive_family(family)
    if not isinstance(capture, dict):
        _fail('context_missing')
    if (capture.get('source_contract') != CONTRACT or capture.get('source_url') != family.source_url
            or str(capture.get('product_type')) != family.product_type or capture.get('store_type') != 'broker'):
        _fail('unsupported_context_scope')
    normalized = normalize_price_context(capture)
    context = normalized['context']
    selected = context['selections']
    reasons = set(normalized['reasons'])
    if not complete:
        for key in (*family.base_fields, 'quantity', 'productId', 'productionTime'):
            if selected.get(key) in {'', '0'}:
                reasons.discard('unselected_required_value:' + key)
                reasons.discard('unsupported_selected_option:' + key)
    unresolved = sorted(reasons)
    if not complete and family is STAGGERED_FAMILY:
        # Unknown bootstrap controls are recorded, never defaulted or priced.
        unknown_fields = {*family.base_fields, *family.option_fields, 'proofType','manualProofType',
                          'designServices','fileDelivery','quantity','productId','productionTime'}
        reasons = {r for r in reasons if not (
            r in {'staggered_manual_controls_not_retained','unsupported_staggered_service_scope',
                  'unsupported_staggered_production','unsupported_proof_mode', 'unsupported_manual_proof',
                  'unsupported_delivery_for_proof_mode','unsupported_design_package',
                  'coupon_state_not_observed'} or
            r.startswith('missing_or_invalid_selection:') and r.split(':',1)[1] in unknown_fields)}
        shape_ok = set(selected) <= set(family.keys)
    else:
        shape_ok = set(selected) == set(family.keys)
    if reasons or not shape_ok:
        _fail('context_not_supported')
    for key, value in selected.items():
        selected[key] = _integer(value, maximum=MAX_QUANTITY if key == 'quantity' else 999_999_999_999,
                                 empty=True, zero=not complete)
    # Design-service pricing has a different query contract and is deliberately out of scope.
    if selected.get('proofType') == '101':
        _fail('design_context_unsupported')
    nonquantity = {**context, 'selections': {k: v for k, v in selected.items() if k != 'quantity'}}
    base = {'source_contract': CONTRACT, 'source_url': family.source_url,
            'selections': {key: selected.get(key) for key in (*family.base_fields, 'productionTime', 'productId')}}
    if family is STAGGERED_FAMILY and 'base_price_query' in context:
        base['base_price_query'] = dict(context['base_price_query'])
    return {'context': context, 'context_hash': _hash(context), 'nonquantity_hash': _hash(nonquantity),
            'base_hash': _hash(base), 'selected': selected, **(
                {'bootstrap_unknown_reasons':unresolved,'bootstrap_only':True} if not complete and family is STAGGERED_FAMILY else {})}


@dataclass
class _Request:
    spec: RequestProjection
    epoch: int
    order: int
    observed_at: float
    response_order: int = 0
    finish_order: int = 0
    finished_at: float = 0
    status: int = 0
    mime: str = ''
    network_timing: bool = False
    wire_bytes: int = 0
    cached: bool = False
    done: bool = False
    reason: str = ''
    projection: dict = field(default_factory=dict)
    body_sha256: str = ''
    body_bytes: int = 0
    bound_nonquantity: str = ''
    bound_base: str = ''


@dataclass
class _Epoch:
    number: int
    revision_before: int
    before: dict
    field: str
    value: str
    requests: list = field(default_factory=list)
    reasons: set = field(default_factory=set)
    finished: bool = False
    result: dict = field(default_factory=dict)
    catalog_after: tuple | None = None


class PriceJournal:
    """One document, one owned diagnosis; no fetch, reload, retries or imports."""
    def __init__(self, diagnosis_id, *, clock=time.monotonic, family=None, allow_staggered_literal_base=False):
        if type(diagnosis_id) is not str or not _HEX_ID.fullmatch(diagnosis_id):
            _fail('invalid_diagnosis_id')
        self.family = passive_family(family)
        if (type(allow_staggered_literal_base) is not bool or
                allow_staggered_literal_base and self.family is not STAGGERED_FAMILY):
            _fail('unsupported_literal_base_scope')
        self._allow_staggered_literal_base = allow_staggered_literal_base
        self.diagnosis_id, self.clock = diagnosis_id, clock
        self.page_generation = os.urandom(16).hex()
        self._frame = self._loader = ''
        self._epochs, self._requests = {}, {}
        self._current = self._order = 0
        self._invalid = ''
        self._mappings, self._option_maps = {}, {}
        self._projection_bytes = 0

    def bind_document(self, frame_id, loader_id, source_url, observed_bundle_contract):
        if (self._frame or source_url != self.family.source_url or observed_bundle_contract != CONTRACT
                or type(frame_id) is not str or not 0 < len(frame_id) <= 128
                or type(loader_id) is not str or not 0 < len(loader_id) <= 128):
            _fail('document_scope_unproven')
        self._frame, self._loader = frame_id, loader_id

    def invalidate(self, reason):
        allowed = {'page_closed', 'document_changed', 'unattributed_control_change', 'adapter_closed',
                   'adapter_error', 'cancelled', 'bundle_changed'}
        self._invalid = reason if reason in allowed else 'adapter_error'

    def _tick(self):
        self._order += 1
        return self._order

    def begin_epoch(self, capture_before, revision_before, field_key, value):
        if self._invalid or not self._frame:
            _fail('journal_not_live')
        if type(revision_before) is not int or not 0 <= revision_before < 1_000_000_000:
            _fail('invalid_revision')
        if field_key not in set(self.family.keys) - {'productId'}:
            _fail('unsupported_selection_field')
        value = _integer(value, maximum=MAX_QUANTITY if field_key == 'quantity' else 999_999_999_999,
                         empty=True)
        if self.family is STAGGERED_FAMILY:
            allowed_radios = {'proofType':{'100'},'manualProofType':{'1','36','37','38'},'fileDelivery':{'5'}}
            if field_key == 'designServices' or field_key in allowed_radios and value not in allowed_radios[field_key]:
                _fail('unsupported_selection_field')
        before = project_context(capture_before, complete=False, family=self.family)
        if before['selected'].get(field_key) == value:
            _fail('selection_not_new')
        if self._current and not self._epochs[self._current].finished:
            _fail('prior_epoch_not_finished')
        if len(self._epochs) >= MAX_EPOCHS:
            _fail('epoch_budget_exceeded')
        self._current += 1
        self._epochs[self._current] = _Epoch(self._current, revision_before, before, field_key, value)
        return self._current

    def _catalog_dependency_key(self, projected):
        if (self.family is not STAGGERED_FAMILY or not self._allow_staggered_literal_base
                or not isinstance(projected, dict)
                or not staggered_literal_base_query(projected.get('context', {}).get('base_price_query'))):
            return None
        selected = projected.get('selected', {})
        values = []
        for key in (*self.family.base_fields, *self.family.option_fields, 'productId', 'productionTime'):
            value = selected.get(key)
            try:
                value = _integer(value, empty=key in self.family.option_fields)
            except JournalError:
                return None
            values.append(value)
        return (*values, 'source_literal_pair_v1')

    def _retain_catalog_after(self, epoch, capture, revision_after):
        # A sealed after-state is a permanent lifecycle barrier when unavailable,
        # private, unsettled, drifted, invalid, or unfinished. It is not repaired
        # by a later observation or by a late response completing after sealing.
        epoch.catalog_after = None
        if (self._invalid or epoch.reasons or type(revision_after) is not int
                or revision_after != epoch.revision_before + 1 or revision_after >= 1_000_000_000
                or any(not self._requests[identifier].done for identifier in epoch.requests)):
            return
        try:
            # Initial catalog discovery may precede known service/quantity state.
            # A subsequent completed quantity transition must have a fully valid
            # after-context, including the current retained service/coupon guards.
            after = project_context(capture, complete=epoch.field == 'quantity', family=self.family)
            if after['selected'].get(epoch.field) != epoch.value:
                return
            epoch.catalog_after = self._catalog_dependency_key(after)
        except JournalError:
            pass

    def _literal_base_profile(self, epoch):
        # Only the campaign-gated manager opts in. An exact controller string-null
        # and positively retained manual/proof/upload controls must precede the
        # native request. No inferred/default service values admit a response.
        context = epoch.before['context']
        if (self._allow_staggered_literal_base and self.family is STAGGERED_FAMILY
                and staggered_literal_base_query(context.get('base_price_query'))
                and context.get('manual_controls') is not None):
            return dict(STAGGERED_LITERAL_BASE_QUERY)
        return None

    def _candidate(self, url):
        # No queries/headers/body are read for unrelated endpoints. This parser is not a transport guard.
        try:
            parts = urlsplit(url)
            return (parts.hostname == 'www.printograph.com' and
                    (parts.path == '/service/rest/v1/products' or
                     self.family is STAGGERED_FAMILY and parts.path == '/service/rest/v1/products/quantities' or
                     re.fullmatch(r'/service/rest/v1/products/[^/]+/(?:prices|options)(?:/[^/]+/prices)?', parts.path)))
        except Exception:
            return False

    def request(self, identifier, frame_id, loader_id, url, method, resource_type, *, redirected=False):
        epoch = self._epochs.get(self._current)
        if self._invalid or not epoch or epoch.finished or not self._candidate(url):
            return False
        if frame_id != self._frame or loader_id != self._loader:
            return False
        if type(identifier) is not str or not 0 < len(identifier) <= 128 or identifier in self._requests:
            if identifier in self._requests:
                self.fail(identifier, 'redirected_response' if redirected else 'request_identity_ambiguous')
            epoch.reasons.add('request_identity_ambiguous'); return False
        if len(epoch.requests) >= MAX_REQUESTS:
            epoch.reasons.add('request_budget_exceeded'); return False
        try:
            if redirected:
                _fail('redirected_response')
            spec = project_request(url, method, resource_type, family=self.family,
                                   base_price_query=self._literal_base_profile(epoch))
        except JournalError as error:
            if self.family is STAGGERED_FAMILY:
                # Rejected events still consume the unchanged bounded request budget.
                shape=rejected_request_shape(url,method,resource_type)
                cost=len(_json(shape))
                if self._projection_bytes+cost>MAX_JOURNAL:
                    epoch.reasons.add('projection_budget_exceeded');return False
                self._projection_bytes+=cost
                ordinal = self._tick()
                epoch.requests.append(identifier)
                self._requests[identifier] = _Request(RequestProjection('rejected_request'), epoch.number, ordinal,
                    self.clock(), done=True, reason=str(error),
                    projection={'request_shape':shape})
            epoch.reasons.add(str(error)); return False
        self._requests[identifier] = _Request(spec, epoch.number, self._tick(), self.clock())
        epoch.requests.append(identifier)
        return True

    def response(self, identifier, *, status, mime, response_url, timing=None, from_disk=False,
                 from_service_worker=False, from_prefetch=False):
        record = self._requests.get(identifier)
        if not record or record.done or self._invalid:
            return
        try:
            profile = (dict(STAGGERED_LITERAL_BASE_QUERY)
                       if record.spec.base_query_profile == 'source_literal_pair_v1' else None)
            if project_request(response_url, family=self.family, base_price_query=profile) != record.spec:
                _fail('response_identity_mismatch')
        except JournalError:
            self.fail(identifier, 'response_identity_mismatch'); return
        record.response_order = self._tick()
        if type(status) is not int or status != 200 or mime != 'application/json':
            self.fail(identifier, 'response_not_successful_json'); return
        if any(type(flag) is not bool for flag in (from_disk, from_service_worker, from_prefetch)):
            self.fail(identifier, 'cache_provenance_unproven'); return
        record.status, record.mime = status, mime
        record.cached |= from_disk or from_service_worker or from_prefetch
        timing = timing if isinstance(timing, dict) else {}
        record.network_timing = all(type(timing.get(k)) in (int, float) and math.isfinite(timing[k]) and timing[k] >= 0
                                    for k in ('requestTime', 'receiveHeadersEnd'))

    def cache(self, identifier):
        record = self._requests.get(identifier)
        if record:
            record.cached = True

    def loading_finished(self, identifier, encoded_bytes):
        record = self._requests.get(identifier)
        if not record or record.done or self._invalid:
            return False
        record.finish_order, record.finished_at = self._tick(), self.clock()
        if type(encoded_bytes) not in (int, float) or not math.isfinite(encoded_bytes) or encoded_bytes <= 0:
            self.fail(identifier, 'network_transfer_unproven'); return False
        record.wire_bytes = int(encoded_bytes)
        if record.cached:
            self.fail(identifier, 'cache_only'); return False
        if record.status != 200 or not record.response_order or not record.network_timing:
            self.fail(identifier, 'cache_provenance_unproven'); return False
        return True

    def accept_body(self, identifier, raw):
        record = self._requests.get(identifier)
        if not record or record.done or self._invalid:
            return
        if not record.finish_order or record.cached or record.status != 200 or not record.network_timing:
            self.fail(identifier, 'cache_provenance_unproven'); return
        try:
            projection = project_body(record.spec, raw)
            cost = len(_json(projection))
            if self._projection_bytes + cost > MAX_JOURNAL:
                _fail('projection_budget_exceeded')
        except JournalError as error:
            self.fail(identifier, str(error)); return
        record.projection, record.done = projection, True
        record.body_bytes, record.body_sha256 = len(raw), hashlib.sha256(raw).hexdigest()
        self._projection_bytes += cost

    def fail(self, identifier, reason):
        record = self._requests.get(identifier)
        if record:
            allowed = {'response_not_successful_json', 'cache_provenance_unproven', 'network_transfer_unproven',
                       'cache_only', 'body_unavailable', 'body_decode_failed', 'response_body_bound',
                       'response_items_missing', 'response_items_bound', 'invalid_response_json',
                       'duplicate_json_key', 'json_number_bound', 'invalid_json_number',
                       'product_identity_ambiguous', 'option_type_missing', 'option_identity_conflict',
                       'quantity_row_ambiguous', 'invalid_numeric_identifier', 'numeric_identifier_bound',
                       'invalid_markup_price', 'markup_price_bound', 'projection_budget_exceeded',
                       'request_failed', 'cancelled', 'redirected_response', 'request_identity_ambiguous',
                       'response_identity_mismatch'}
            record.reason = reason if reason in allowed else 'body_unavailable'
            record.done = True

    def _fresh(self, record):
        age = self.clock() - record.finished_at
        return (record.done and not record.reason and not record.cached and record.finish_order > 0
                and bool(record.projection) and 0 <= age <= TTL_SECONDS)

    def _base_matches(self, record, context):
        query = dict(record.spec.lookup)
        selected = context['selected']
        return (query.get('productType') == self.family.product_type and query.get('turnaround') == selected['productionTime']
                and all(query.get(k) == selected[k] for k in self.family.base_fields))

    def _certify_context(self, epoch, context):
        selected, product_id = context['selected'], context['selected']['productId']
        # The initial catalog lookup can finish before quantity exposes manual
        # controls. Only this explicit literal profile may certify those earlier
        # still-live native catalog responses; persisted evidence never rehydrates.
        reuse_bootstrap = (self._allow_staggered_literal_base and self.family is STAGGERED_FAMILY
                           and staggered_literal_base_query(context['context'].get('base_price_query'))
                           and context['context'].get('manual_controls') is not None)
        epochs = {epoch.number}
        if reuse_bootstrap:
            dependency = self._catalog_dependency_key(context)
            epochs.update(number for number, prior in self._epochs.items()
                          if dependency is not None and number < epoch.number and prior.finished
                          and prior.catalog_after == dependency
                          and all(step.field == 'quantity'
                                  and self._catalog_dependency_key(step.before) == dependency
                                  and step.catalog_after == dependency
                                  for later, step in self._epochs.items()
                                  if number < later <= epoch.number))
        lookups = [r for r in self._requests.values() if r.epoch in epochs and self._fresh(r)
                   and r.spec.kind == 'product_identity' and self._base_matches(r, context)]
        if reuse_bootstrap:
            # Rebuild only from the eligible observed catalog records below.
            # A return to an earlier selection cannot resurrect a cached identity.
            self._mappings.pop(context['base_hash'], None)
            self._option_maps.pop(context['base_hash'], None)
        if any(r.projection['product_id'] != product_id for r in lookups):
            return None, 'product_identity_conflict'
        if lookups:
            mapping = min(lookups, key=lambda r: r.response_order)
            self._mappings[context['base_hash']] = mapping
        else:
            if reuse_bootstrap:
                return None, 'product_selection_mapping_missing'
            mapping = self._mappings.get(context['base_hash'])
            if not mapping or not self._fresh(mapping):
                return None, 'product_selection_mapping_missing'
        for record in self._requests.values():
            if (record.epoch in epochs and self._fresh(record) and record.spec.product_id == product_id
                    and record.order > mapping.finish_order):
                # Earlier epochs contribute only catalog identity, never prices.
                if record.epoch != epoch.number and record.spec.kind != 'option_identity':
                    continue
                if (record.spec.kind == 'base_price' and record.spec.base_query_profile !=
                        ('source_literal_pair_v1' if staggered_literal_base_query(context['context'].get('base_price_query')) else '')):
                    continue
                record.bound_base = context['base_hash']
                if record.spec.kind in {'base_price', 'option_price'}:
                    record.bound_nonquantity = context['nonquantity_hash']
                elif record.spec.kind == 'option_identity':
                    prior = self._option_maps.get(context['base_hash'])
                    if prior and self._fresh(prior) and prior.projection != record.projection:
                        return None, 'option_identity_conflict'
                    self._option_maps[context['base_hash']] = record
        return mapping, ''

    def _component(self, epoch, context, component):
        kind = 'base_price' if component['field'] == 'productPrice' else 'option_price'
        type_id = component['selected_value']
        option_id = ''
        if kind == 'option_price':
            mapping = self._option_maps.get(context['base_hash'])
            if not mapping or not self._fresh(mapping):
                return {'status': 'held', 'reason': 'option_mapping_missing'}
            ids = [key for key, value in mapping.projection['mapping'].items() if value == type_id]
            if len(ids) != 1:
                return {'status': 'held', 'reason': 'option_mapping_ambiguous'}
            option_id = ids[0]
        quantity = context['selected']['quantity']
        current_failures = [r for r in self._requests.values() if r.epoch == epoch.number
                            and r.spec.kind == kind and r.spec.product_id == context['selected']['productId']
                            and r.spec.option_id == option_id and r.reason
                            and not (r.cached and r.status == 200 and r.mime == 'application/json')]
        if current_failures:
            return {'status': 'held', 'reason': 'current_component_response_failed'}
        literal_scope = (self._allow_staggered_literal_base and self.family is STAGGERED_FAMILY
                         and staggered_literal_base_query(context['context'].get('base_price_query')))
        eligible = [r for r in self._requests.values() if self._fresh(r) and r.spec.kind == kind
                    and (not literal_scope or r.epoch == epoch.number)
                    and r.bound_nonquantity == context['nonquantity_hash']
                    and r.spec.product_id == context['selected']['productId'] and r.spec.option_id == option_id
                    and (not r.spec.quantity_filter or r.spec.quantity_filter == quantity)]
        amounts = {}
        for record in eligible:
            for q, row in record.projection['rows'].items():
                if record.spec.quantity_filter and q != record.spec.quantity_filter:
                    continue
                if q in amounts and amounts[q] != row['markup_price']:
                    return {'status': 'held', 'reason': 'fresh_tables_conflict'}
                amounts[q] = row['markup_price']
        rows = [r for r in eligible if quantity in r.projection['rows']]
        if not rows:
            return {'status': 'held', 'reason': 'fresh_exact_quantity_missing'}
        # Same-epoch response preferred only when it satisfies identical context/row gates.
        record = max(rows, key=lambda r: (r.epoch == epoch.number, r.finished_at, r.order))
        row = record.projection['rows'][quantity]
        if row['source_cents'] != component['amount']:
            return {'status': 'held', 'reason': 'controller_price_mismatch'}
        return {'status': 'observed_fresh_response' if record.epoch == epoch.number else 'reused_fresh_observed_table',
                'field': component['field'], 'selected_value': type_id, 'quantity': quantity, **row,
                'originating_epoch': record.epoch, 'request_ordinal': record.order,
                'response_completed_at_monotonic': record.finished_at,
                'table_projection_sha256': _hash(record.projection),
                'ttl_remaining_seconds': max(0, TTL_SECONDS - (self.clock() - record.finished_at)),
                'new_network_request_for_current_selection': record.epoch == epoch.number,
                **({'base_price_query':dict(STAGGERED_LITERAL_BASE_QUERY)}
                   if record.spec.base_query_profile == 'source_literal_pair_v1' else {})}

    def finalize_epoch(self, epoch_number, capture_after, revision_after):
        epoch = self._epochs.get(epoch_number)
        if not epoch or epoch_number != self._current or epoch.finished:
            _fail('epoch_not_current')
        reasons = set(epoch.reasons)
        context = None
        if self._invalid:
            reasons.add(self._invalid)
        try:
            context = project_context(capture_after, family=self.family)
            if (type(revision_after) is not int or revision_after != epoch.revision_before + 1 or revision_after >= 1_000_000_000
                    or context['selected'][epoch.field] != epoch.value):
                reasons.add('selection_revision_not_retained')
        except JournalError as error:
            reasons.add(str(error))
        if any(not self._requests[identifier].done for identifier in epoch.requests):
            reasons.add('native_requests_inflight')
        components = []
        if self._allow_staggered_literal_base and self.family is STAGGERED_FAMILY:
            self._retain_catalog_after(epoch, capture_after, revision_after)
        reconciliation = calculate_source_total(capture_after)
        if reconciliation['status'] != 'complete' or reconciliation['comparison'] != 'matched':
            reasons.add('arithmetic_unresolved')
        if not reasons and context:
            mapping, error = self._certify_context(epoch, context)
            if error:
                reasons.add(error)
            else:
                for component in reconciliation['components']:
                    result = self._component(epoch, context, component)
                    components.append(result)
                    if result['status'] == 'held':
                        reasons.add(result['reason'])
        # Never claim production, coupon, product-domain, import, or checkout completion.
        epoch.finished = True
        epoch.result = {'schema': 'printograph-passive-price-evidence/v1',
                        'status': 'held' if reasons else 'matched_fresh_observed_components',
                        'diagnosis_id': self.diagnosis_id, 'page_generation': self.page_generation,
                        'selection_epoch': epoch.number, 'revision_before': epoch.revision_before,
                        'revision_after': revision_after if type(revision_after) is int else None,
                        'source_contract': dict(CONTRACT), 'context_sha256': context['context_hash'] if context else None,
                        'nonquantity_context_sha256': context['nonquantity_hash'] if context else None,
                        'components': components, 'reasons': sorted(reasons),
                        'production_freshness': 'not_collected', 'coupon_freshness': 'not_collected',
                        'controller_write_provenance': 'not_intercepted',
                        'source_data_finalized': False, 'source_domain_complete': False,
                        'fresh_source_verified': False, 'woocommerce_import_ready': False,
                        'shipping_verified': False, 'checkout_verified': False}
        if (self._allow_staggered_literal_base and context
                and staggered_literal_base_query(context['context'].get('base_price_query'))):
            mapping = self._mappings.get(context['base_hash'])
            options = self._option_maps.get(context['base_hash'])
            epoch.result['catalog_identity_provenance'] = {
                'product_identity': ({'originating_epoch':mapping.epoch,'request_ordinal':mapping.order,
                                      'finish_ordinal':mapping.finish_order} if mapping else None),
                'option_identity': ({'originating_epoch':options.epoch,'request_ordinal':options.order,
                                     'finish_ordinal':options.finish_order} if options else None),
                'price_carry_forward_permitted':False,
                'completed_after_dependencies_known':epoch.catalog_after is not None,
                'completed_after_dependency_sha256':_hash(epoch.catalog_after) if epoch.catalog_after is not None else None,
                'source_requests_reissued':False}
        return json.loads(_json(epoch.result))

    def snapshot(self):
        events = []
        for record in self._requests.values():
            events.append({'epoch': record.epoch, 'request_ordinal': record.order,
                           'request': record.spec.public(), 'http_status': record.status,
                           'cache_only': record.cached, 'done': record.done,
                           'reason': record.reason, 'projection': record.projection,
                           'projection_sha256': _hash(record.projection) if record.projection else None,
                           'body_bytes': record.body_bytes, 'body_sha256': record.body_sha256 or None,
                           **({'rejected_request_shape':record.projection.get('request_shape')}
                              if self.family is STAGGERED_FAMILY and record.spec.kind=='rejected_request' else {}),
                           **({'quantity_projection':record.projection} if self.family is STAGGERED_FAMILY and record.spec.kind=='quantity_domain' else {})})
        return {'schema': 'printograph-private-passive-journal/v1', 'diagnosis_id': self.diagnosis_id,
                'page_generation': self.page_generation, 'source_url': self.family.source_url, 'source_contract': dict(CONTRACT),
                'historical_only_after_process_or_page_release': True,
                'events': events, 'epochs': [epoch.result for epoch in self._epochs.values() if epoch.finished],
                'invalidated': bool(self._invalid), 'invalid_reason': self._invalid or None,
                'source_data_finalized': False, 'woocommerce_import_ready': False}

    def persist(self, store_root):
        """Private evidence only. Persisted data cannot restore live/TTL eligibility."""
        root = Path(store_root)
        if not root.is_absolute() or '..' in root.parts:
            _fail('invalid_private_root')
        for part in (root, *root.parents):
            if part.is_symlink():
                _fail('private_path_symlink')
        if not root.is_dir():
            _fail('invalid_private_root')
        directory = root / 'printograph-price-journals'
        if not directory.exists():
            directory.mkdir(mode=0o700)
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
            _fail('private_directory_permissions')
        destination = directory / (self.diagnosis_id + '.json')
        if destination.is_symlink() or (destination.exists() and (not destination.is_file()
                                     or destination.stat().st_mode & 0o077)):
            _fail('private_destination_unsafe')
        raw = _json(self.snapshot())
        if len(raw) > MAX_JOURNAL:
            _fail('journal_persistence_bound')
        temporary = directory / (self.diagnosis_id + '.' + os.urandom(8).hex() + '.tmp')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {'file': destination.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                'bytes': len(raw), 'historical_evidence_only': True}


class OwnedPageCDPObserver:
    """Passive Chromium target session only; it never controls source transport."""
    def __init__(self, page, journal):
        self.page, self.journal = page, journal
        self._session = None
        self._tasks = set()
        self._closed = False
        self._callbacks = {}

    async def attach(self, observed_bundle_contract):
        if self._session or self._closed or self.page.url != self.journal.family.source_url:
            _fail('owned_page_scope_unproven')
        session = await self.page.context.new_cdp_session(self.page)
        try:
            tree = await session.send('Page.getFrameTree')
            frame = tree.get('frameTree', {}).get('frame', {})
            self.journal.bind_document(frame.get('id'), frame.get('loaderId'), frame.get('url'), observed_bundle_contract)
            self._session = session
            self._callbacks = {'Network.requestWillBeSent': self._request,
                               'Network.responseReceived': self._response,
                               'Network.requestServedFromCache': self._cache,
                               'Network.loadingFinished': self._finished,
                               'Network.loadingFailed': self._failed,
                               'Page.frameNavigated': self._navigated,
                               'Page.navigatedWithinDocument': self._within_document}
            for event, callback in self._callbacks.items():
                session.on(event, callback)
            self.page.on('close', self._page_closed)
            await session.send('Page.enable')
            await session.send('Network.enable', {'maxTotalBufferSize': MAX_SESSION_BUFFER,
                                                 'maxResourceBufferSize': MAX_BODY, 'maxPostDataSize': 0})
        except BaseException as error:
            self.journal.invalidate('adapter_error')
            for event, callback in self._callbacks.items():
                session.remove_listener(event, callback)
            self.page.remove_listener('close', self._page_closed)
            try:
                await session.detach()
            except Exception:
                pass
            self._session = None
            if isinstance(error, asyncio.CancelledError):
                raise
            raise JournalError('passive_attach_failed') from None
        return self

    def _page_closed(self, *_):
        self.journal.invalidate('page_closed')

    def _request(self, event):
        try:
            # Deliberately access no request header, postData, initiator, stack or extra-info fields.
            request = event.get('request', {})
            self.journal.request(event.get('requestId'), event.get('frameId'), event.get('loaderId'),
                                 request.get('url'), request.get('method'), event.get('type'),
                                 redirected='redirectResponse' in event)
        except Exception:
            self.journal.invalidate('adapter_error')

    def _response(self, event):
        try:
            response = event.get('response', {})
            self.journal.response(event.get('requestId'), status=response.get('status'), mime=response.get('mimeType'),
                                  response_url=response.get('url'),
                                  timing=response.get('timing'), from_disk=response.get('fromDiskCache', False),
                                  from_service_worker=response.get('fromServiceWorker', False),
                                  from_prefetch=response.get('fromPrefetchCache', False))
        except Exception:
            self.journal.invalidate('adapter_error')

    def _cache(self, event):
        self.journal.cache(event.get('requestId'))

    def _finished(self, event):
        try:
            identifier = event.get('requestId')
            if self.journal.loading_finished(identifier, event.get('encodedDataLength')) and not self._closed:
                task = asyncio.create_task(self._body(identifier))
                self._tasks.add(task); task.add_done_callback(self._tasks.discard)
        except Exception:
            self.journal.invalidate('adapter_error')

    def _failed(self, event):
        self.journal.fail(event.get('requestId'), 'request_failed')

    def _navigated(self, event):
        frame = event.get('frame', {})
        if frame.get('id') == self.journal._frame and (frame.get('loaderId') != self.journal._loader
                                                      or frame.get('url') != self.journal.family.source_url):
            self.journal.invalidate('document_changed')

    def _within_document(self, event):
        if event.get('frameId') == self.journal._frame and event.get('url') != self.journal.family.source_url:
            self.journal.invalidate('document_changed')

    async def _body(self, identifier):
        try:
            record = self.journal._requests.get(identifier)
            if (self._closed or self.journal._invalid or not record or record.done
                    or record.cached or not record.finish_order or record.status != 200):
                return
            result = await self._session.send('Network.getResponseBody', {'requestId': identifier})
            if self._closed:
                return
            body, encoded = result.get('body'), result.get('base64Encoded')
            if type(body) is not str or type(encoded) is not bool:
                self.journal.fail(identifier, 'body_decode_failed'); return
            if len(body) > (4 * ((MAX_BODY + 2) // 3) if encoded else MAX_BODY):
                self.journal.fail(identifier, 'response_body_bound'); return
            raw = base64.b64decode(body, validate=True) if encoded else body.encode('utf-8')
            if len(raw) > MAX_BODY:
                self.journal.fail(identifier, 'response_body_bound'); return
            self.journal.accept_body(identifier, raw)
        except asyncio.CancelledError:
            self.journal.fail(identifier, 'cancelled')
            raise
        except Exception:
            self.journal.fail(identifier, 'body_unavailable')

    async def drain(self, timeout=5):
        if type(timeout) not in (int, float) or not 0 < timeout <= 10:
            _fail('invalid_observer_deadline')
        pending = tuple(self._tasks)
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)
            except asyncio.TimeoutError:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                self.journal.invalidate('adapter_error')

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.journal.invalidate('adapter_closed')
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self.page.remove_listener('close', self._page_closed)
        if self._session:
            for event, callback in self._callbacks.items():
                self._session.remove_listener(event, callback)
            try:
                await self._session.detach()
            except Exception:
                pass
            self._session = None
