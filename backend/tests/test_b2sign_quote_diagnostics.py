"""Privacy-safe quote shape diagnostics; no browser or supplier calls."""

import copy
import hashlib
import json

import pytest

from crawler.b2sign_pilot import PilotError, quote_schema, sanitize_quote


def test_missing_null_and_wrong_type_envelopes_are_distinguishable():
    missing = quote_schema({})
    null_props = quote_schema({'props': None})
    no_result = quote_schema({'props': {}})
    null_result = quote_schema({'props': {'quoteResult': None}})
    malformed_result = quote_schema({'props': {'quoteResult': 'PRIVATE_QUOTE'}})
    assert missing['props_present'] is False
    assert null_props['props_present'] is True and null_props['props_type'] == 'NoneType'
    assert no_result['quote_result_present'] is False
    assert null_result['quote_result_present'] is True
    assert null_result['quote_result_type'] == 'NoneType'
    assert malformed_result['quote_result_type'] == 'str'
    assert 'PRIVATE' not in json.dumps(malformed_result)
    assert quote_schema(['PRIVATE'])['body_type'] == 'list'
    for body in [{}, {'props': None}, {'props': {}}, {'props': {'quoteResult': None}},
                 {'props': {'quoteResult': 'PRIVATE_QUOTE'}}]:
        with pytest.raises(PilotError, match='quote_result_missing_or_ambiguous'):
            sanitize_quote(body, 1)


@pytest.mark.parametrize('value,kind,count', [(None, 'NoneType', None), ('PRIVATE', 'str', None),
                                               ([], 'list', 0), ([{'PRIVATE': 'PRIVATE'}], 'list', 1),
                                               ({}, 'dict', 0), (42, 'int', None)])
def test_error_container_presence_types_without_unknown_content(value, kind, count):
    out = quote_schema({'props': {'errors': value}})
    assert out['errors_present'] is True
    assert out['errors_type'] == kind and out['error_count'] == count
    assert out['quote_message_present'] is False
    assert out['quote_message_sha256'] is None
    assert 'PRIVATE' not in json.dumps(out)
    assert quote_schema({'props': {}})['errors_present'] is False


def test_quote_message_hash_length_and_unknown_error_keys_never_expose_private_text():
    message = 'PRIVATE_CUSTOMER@example.com at PRIVATE_ADDRESS: quote unavailable'
    body = {'props': {'errors': {'quote.message': message, 'PRIVATE_FIELD': 'PRIVATE_VALUE'},
                      'customer': {'email': 'PRIVATE_CUSTOMER'}, 'debug': 'PRIVATE_DEBUG'}}
    original = copy.deepcopy(body)
    out = quote_schema(body)
    assert out['quote_message_present'] is True and out['quote_message_type'] == 'str'
    assert out['quote_message_sha256'] == hashlib.sha256(message.encode()).hexdigest()
    assert out['quote_message_length'] == len(message)
    assert out['quote_message_category'] == 'unclassified'
    assert out['error_count'] == 2 and out['unknown_error_key_count'] == 1
    assert body == original
    assert 'PRIVATE' not in json.dumps(out) and '@' not in json.dumps(out)


@pytest.mark.parametrize('value,kind', [(None, 'NoneType'), (False, 'bool'), (['PRIVATE'], 'list'), ({'PRIVATE': 1}, 'dict')])
def test_wrong_quote_message_type_is_not_stringified_or_hashed(value, kind):
    out = quote_schema({'props': {'errors': {'quote.message': value}}})
    assert out['quote_message_present'] is True and out['quote_message_type'] == kind
    assert out['quote_message_length'] is None and out['quote_message_sha256'] is None
    assert out['quote_message_category'] is None
    assert 'PRIVATE' not in json.dumps(out)


@pytest.mark.parametrize('message,category', [
    ('Unauthenticated.', 'authentication_required'), ('  FORBIDDEN. ', 'authorization_denied'),
    ('Too Many Requests.', 'rate_limited'), ('The given data was invalid.', 'validation_error'),
    ('Out of stock.', 'stock_unavailable'), ('Price unavailable', 'price_unavailable'),
    ('Out of stock for PRIVATE_ACCOUNT', 'unclassified'), ('', 'empty'), ('   ', 'empty'),
])
def test_only_static_whole_message_categories(message, category):
    out = quote_schema({'props': {'errors': {'quote.message': message}}})
    assert out['quote_message_category'] == category
    assert 'PRIVATE' not in json.dumps(out)
    with pytest.raises(PilotError, match='quote_result_missing_or_ambiguous'):
        sanitize_quote({'props': {'errors': {'quote.message': message}}}, 1)


def test_supported_quote_legacy_schema_fields_and_acceptance_remain_unchanged():
    body = {'props': {'quoteResult': {'quote': [{'qty': [1], 'subtotal': 56.26, 'unit_price': 56.26,
                                              'PRIVATE_FIELD': 'PRIVATE_VALUE'}]}, 'errors': {}}}
    schema = quote_schema(body)
    assert schema['quote_result_type'] == 'dict' and schema['quote_count'] == 1
    assert schema['result_fields'] == {'quote': 'list'}
    assert schema['row_fields'] == {'qty': 'list', 'subtotal': 'float', 'unit_price': 'float'}
    assert schema['quote_field_present'] is True and schema['quote_field_type'] == 'list'
    assert 'PRIVATE' not in json.dumps(schema)
    assert sanitize_quote(body, 1)['subtotal'] == '56.26'


def test_opaque_hash_handles_non_ascii_and_malformed_unicode_without_serializing_message():
    message = 'PRIVATE\ud800é'
    out = quote_schema({'props': {'errors': {'quote.message': message}}})
    assert out['quote_message_length'] == len(message)
    assert out['quote_message_sha256'] == hashlib.sha256(message.encode('utf-8', errors='surrogatepass')).hexdigest()
    assert 'PRIVATE' not in json.dumps(out)
