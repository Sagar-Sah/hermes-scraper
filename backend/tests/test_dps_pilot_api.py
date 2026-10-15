from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import crawler.app as api


@pytest.mark.asyncio
async def test_dps_pilot_api_auth_scope_and_missing_evidence(monkeypatch):
    def get(key):
        raise KeyError('pilot_not_found')

    pilots = SimpleNamespace(start=AsyncMock(return_value={'id': 'a' * 32, 'status': 'opening'}),
                             get=get, list=lambda: [])
    monkeypatch.setattr(api.settings, 'api_token', 'dps-test-token')
    monkeypatch.setattr(api.app.state, 'dps_pilots', pilots, raising=False)
    url = 'https://www.digital-print-solutions.com/low-tack-vinyl'
    headers = {'Authorization': 'Bearer dps-test-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://fixture') as client:
        path = '/api/v1/dps-pilots'
        assert (await client.post(path, json={'url': url})).status_code == 401
        for quantities in ([True], ['10'], [], [1, 2, 3, 4, 5]):
            assert (await client.post(path, json={'url': url, 'quantities': quantities}, headers=headers)).status_code == 422
        assert (await client.post(path, json={'url': url, 'copy_cookies': True}, headers=headers)).status_code == 422
        response = await client.post(path, json={'url': url}, headers=headers)
        assert response.status_code == 200
        pilots.start.assert_awaited_once_with(url, quantities=None)
        assert (await client.get(path, headers=headers)).json() == {'items': []}
        assert (await client.get(path + '/missing', headers=headers)).status_code == 404
        assert (await client.get(path + '/missing')).status_code == 401
