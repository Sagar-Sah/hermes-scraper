"""Connector catalog inventory fixtures. No HTTP/browser or real data access."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from crawler.companion_autonomy_integration import IntegratedAutonomousCompanion
from crawler.companion_environments import CompanionEnvironmentRegistry, private_save


BASE = 'https://printitprints.com'
TOKEN = 'catalog-fixture-private-token-' + 'x' * 45


class Store:
    def __init__(self, root):
        self.root = root
    def profiles(self, kind, secrets=False):
        return []


def item(identifier):
    return {'id': identifier, 'name': f'Product {identifier}', 'permalink': f'{BASE}/shop/product-{identifier}/',
            'categories': [{'id': 1, 'name': 'Standard Print'}]}


def envelope(page, rows):
    total = len(rows)
    pages = max(1, (total + 99) // 100)
    return {'schema': 'hermes-companion-catalog/v1', 'site_origin': BASE, 'environment': 'production',
            'items': deepcopy(rows[(page - 1) * 100:page * 100]), 'page': page, 'total': total,
            'pages': pages, 'next_cursor': page + 1 if page < pages else None}


async def build(tmp_path, rows=None, *, capability=True, mutate=None, raw_override=None):
    rows = [item(i) for i in range(1, 207)] if rows is None else rows
    public_calls, connector_calls, responses = [], [], {}
    async def public(url):
        public_calls.append(url)
        parts = urlsplit(url)
        page = int(parse_qs(parts.query)['page'][0])
        if parts.path.endswith('/categories'):
            values = [{'id': 1, 'name': 'Standard Print', 'slug': 'standard-print', 'parent': 0}] if page == 1 else []
        else:
            values = rows[(page - 1) * 100:page * 100]
        return 200, url, json.dumps(values).encode(), {}
    service = IntegratedAutonomousCompanion(Store(tmp_path), SimpleNamespace(pairs={}, campaigns=SimpleNamespace(list=lambda: [])),
                                           SimpleNamespace(), fetch=public)
    async def handshake(cfg, method, path, payload):
        assert method == 'GET' and path == '/capabilities' and cfg['role'] == 'read'
        return {'site_origin': cfg['base_url'], 'environment': cfg['environment'], 'role': cfg['role'],
                'connector_version': '0.2.1' if capability else '0.2.0', 'published_catalog': capability,
                'inventory': True, 'private_not_a_capability': TOKEN}
    async def raw(cfg, method, path, payload):
        assert cfg['role'] == 'read' and cfg['environment'] == 'production'
        assert cfg['token'] == TOKEN and method == 'GET' and payload is None
        connector_calls.append(path)
        page = int(parse_qs(urlsplit(path).query)['page'][0])
        document = envelope(page, rows)
        if mutate:
            document = mutate(page, document)
        data = json.dumps(document, indent=3, ensure_ascii=False).encode() + b'\n'
        url = BASE + '/wp-json/hermes-companion/v1' + path
        responses[page] = data
        if raw_override:
            return await raw_override(cfg, page, url, data)
        return 200, url, data, {'content-type': 'application/json'}
    service.environments = CompanionEnvironmentRegistry(service.root, transport=handshake, raw_transport=raw)
    await service.environments.configure('production', BASE, TOKEN)
    return service, public_calls, connector_calls, responses


@pytest.mark.asyncio
async def test_verified_read_connector_full_catalog_and_exact_raw_endpoint_evidence(tmp_path):
    service, public, connector, responses = await build(tmp_path)
    try:
        cycle = service.create_cycle(category_slugs=['standard-print'])
        record = await service.inventory(cycle['id'])
        assert record['inventory']['complete'] is True
        assert record['inventory']['catalog_count'] == 206
        assert record['inventory']['pages'] == 3
        assert connector == ['/catalog?page=1', '/catalog?page=2', '/catalog?page=3']
        assert all(urlsplit(url).path.endswith('/products/categories') for url in public)
        evidence = [row for row in record['inventory']['evidence_pages'] if row['resource'] == 'products']
        assert len(evidence) == 3
        for row in evidence:
            data = responses[row['page']]
            assert (service.root / row['path']).read_bytes() == data
            assert row['sha256'] == hashlib.sha256(data).hexdigest()
            assert row['url'] == BASE + '/wp-json/hermes-companion/v1/catalog?page=' + str(row['page'])
            assert row['transport'] == 'companion_production_read_catalog'
            assert row['bytes'] == len(data)
        assert service.environments.status('production')['capabilities']['published_catalog'] is True
        assert TOKEN not in json.dumps(record)
        assert TOKEN not in json.dumps(service.environments.status())
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_old_connector_retains_public_catalog_and_zero_products_stops_at_page_one(tmp_path):
    service, public, connector, _ = await build(tmp_path / 'old', rows=[item(9532)], capability=False)
    try:
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is True
        assert any(urlsplit(url).path.endswith('/products') for url in public)
        assert connector == []
    finally:
        await service.close()
    empty, public, connector, _ = await build(tmp_path / 'empty', rows=[])
    try:
        record = await empty.inventory(empty.create_cycle()['id'])
        assert record['inventory']['complete'] is True
        assert record['inventory']['catalog_count'] == 0
        assert connector == ['/catalog?page=1']
    finally:
        await empty.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    lambda d: d.update(items=d['items'][:-1]),
    lambda d: d.pop('items'),
    lambda d: d.update(schema='unexpected/v1'),
    lambda d: d.update(site_origin='https://unapproved.invalid'),
    lambda d: d.update(environment='staging'),
    lambda d: d.update(page=2),
    lambda d: d.update(pages=1),
    lambda d: d.update(next_cursor=None),
    lambda d: d.update(total=True),
    lambda d: d.update(private_customer_profile=TOKEN),
    lambda d: d['items'][0].update(cost='1.00'),
    lambda d: d['items'][0].update(id=True),
    lambda d: d['items'][0].update(permalink='https://unapproved.invalid/shop/product/'),
    lambda d: d['items'][0]['categories'].append(d['items'][0]['categories'][0]),
    lambda d: d['items'][1].update(id=d['items'][0]['id']),
])
async def test_partial_identity_changed_or_private_catalog_response_never_completes_or_falls_back(tmp_path, mutation):
    def mutate(page, document):
        mutation(document)
        return document
    service, public, connector, _ = await build(tmp_path, mutate=mutate)
    try:
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is False
        assert record['status'] == 'inventory_failed'
        assert len(connector) == 1
        assert not any(urlsplit(url).path.endswith('/products') for url in public)
        assert TOKEN not in json.dumps(record)
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['changed_total', 'repeated_ids', 'missing_last_page'])
async def test_missing_repeated_and_changing_pages_remain_unknown(tmp_path, kind):
    def mutate(page, document):
        if page == 3:
            if kind == 'changed_total':
                document['total'] = 205
                document['items'] = document['items'][:5]
            elif kind == 'repeated_ids':
                document['items'] = [item(i) for i in range(1, 7)]
            else:
                document['items'] = []
        return document
    service, public, connector, _ = await build(tmp_path, mutate=mutate)
    try:
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is False
        assert len(connector) == 3
        assert not any(urlsplit(url).path.endswith('/products') for url in public)
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['http_failure', 'redirect', 'nonraw_transport', 'revoked_connection'])
async def test_connector_failures_and_revocation_do_not_use_public_fallback(tmp_path, kind):
    service_ref = []
    async def override(cfg, page, url, data):
        if kind == 'http_failure':
            return 500, url, b'{"message":"private error that must not enter status"}', {}
        if kind == 'redirect':
            return 200, 'https://unapproved.invalid/', data, {}
        if kind == 'nonraw_transport':
            return 200, url, data.decode(), {}
        if page == 1:
            service = service_ref[0]
            config = service.environments._config('production')
            config['connected'] = False
            private_save(service.environments._path('production'), config)
        return 200, url, data, {}
    service, public, connector, _ = await build(tmp_path, raw_override=override)
    service_ref.append(service)
    try:
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is False
        assert not any(urlsplit(url).path.endswith('/products') for url in public)
        assert 'private error' not in json.dumps(record)
        assert len(connector) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_catalog_raw_http_preserves_actual_response_and_read_header(monkeypatch):
    seen = []
    raw = b'{ "schema": "fixture", "items": [] }\n'
    async def handle(request):
        seen.append(request)
        return httpx.Response(200, content=raw, headers={'content-type': 'application/json'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    cfg = {'base_url': BASE, 'token': TOKEN}
    response = await CompanionEnvironmentRegistry._http_raw(cfg, 'GET', '/catalog?page=1', None)
    assert response[0:3] == (200, BASE + '/wp-json/hermes-companion/v1/catalog?page=1', raw)
    assert seen[0].headers['x-hermes-companion-token'] == TOKEN
    assert 'authorization' not in seen[0].headers
    assert seen[0].method == 'GET'


@pytest.mark.asyncio
async def test_failed_capability_recheck_retains_explicit_lane_as_unverified(tmp_path):
    service, public, connector, _ = await build(tmp_path)
    async def signed_out(*args):
        raise ValueError('companion_connector_http_401')
    service.environments.transport = signed_out
    try:
        status = await service.environments.configure('production', BASE)
        assert status['connected'] is False
        assert status['capabilities']['published_catalog'] is True
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is False
        assert record['inventory']['reason'] == 'companion_catalog_connection_unverified'
        assert connector == []
        assert not any(urlsplit(url).path.endswith('/products') for url in public)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_misbound_read_connection_blocks_connector_and_other_site_uses_its_own_public_lane(tmp_path):
    service, public, connector, _ = await build(tmp_path / 'wrong-role')
    try:
        cfg = service.environments._config('production')
        cfg['role'] = 'release'
        private_save(service.environments._path('production'), cfg)
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is False
        assert record['inventory']['reason'] == 'companion_catalog_connection_identity_changed'
        assert connector == []
        assert not any(urlsplit(url).path.endswith('/products') for url in public)
    finally:
        await service.close()
    other, _, connector, _ = await build(tmp_path / 'other-origin', rows=[])
    try:
        assert other.environments.published_catalog_connection('https://other.invalid') is None
        with pytest.raises(ValueError, match='published_catalog_not_supported'):
            await other.environments.published_catalog_page('https://other.invalid', 1)
        assert connector == []
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_damaged_saved_connector_configuration_cannot_silently_change_inventory_source(tmp_path):
    service, public, connector, _ = await build(tmp_path)
    try:
        service.environments._path('production').write_text('{damaged private configuration')
        record = await service.inventory(service.create_cycle()['id'])
        assert record['inventory']['complete'] is False
        assert record['inventory']['reason'] == 'companion_catalog_connection_invalid'
        assert connector == []
        assert not any(urlsplit(url).path.endswith('/products') for url in public)
    finally:
        await service.close()
