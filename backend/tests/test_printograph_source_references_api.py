"""Authenticated API boundary only; fake manager, no browser or suppliers."""
from unittest.mock import AsyncMock

import pytest

from test_product_diagnosis_api import client, headers

SOURCE = "https://www.printograph.com/products/special-shapes/order"


@pytest.mark.asyncio
async def test_source_reference_endpoint_requires_auth_and_strict_payload(client):
    http, manager = client
    method = AsyncMock(return_value={"status": "observed"})
    manager.read_source_references = method
    path = "/api/v1/product-diagnoses/" + manager.value["id"] + "/source-references"
    body = {"revision": 1, "source_url": SOURCE}
    assert (await http.post(path, json=body)).status_code == 401
    for change in [{"revision": True}, {"revision": "1"}, {"revision": -1},
                   {"source_url": 1}, {"source_url": ""}, {"source_url": "x" * 769},
                   {"price": "PRIVATE"}]:
        response = await http.post(path, json={**body, **change}, headers=headers())
        assert response.status_code == 422
    method.assert_not_called()


@pytest.mark.asyncio
async def test_source_reference_endpoint_only_forwards_exact_revision_and_mapped_url(client):
    http, manager = client
    expected = {"status": "observed", "source_fetches_initiated": 0, "scripts": [], "navigation": []}
    manager.read_source_references = AsyncMock(return_value=expected)
    path = "/api/v1/product-diagnoses/" + manager.value["id"] + "/source-references"
    response = await http.post(path, json={"revision": 1, "source_url": SOURCE}, headers=headers())
    assert response.status_code == 200 and response.json() == expected
    manager.read_source_references.assert_awaited_once_with(manager.value["id"], 1, SOURCE)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [
    (LookupError("missing"), 404), (ValueError("diagnosis_revision_conflict"), 409),
    (ValueError("diagnosis_source_url_mismatch"), 422),
    (ValueError("source_script_version_ambiguous"), 422),
])
async def test_source_reference_read_failures_do_not_become_success(client, error, status):
    http, manager = client
    manager.read_source_references = AsyncMock(side_effect=error)
    path = "/api/v1/product-diagnoses/" + manager.value["id"] + "/source-references"
    response = await http.post(path, json={"revision": 1, "source_url": SOURCE}, headers=headers())
    assert response.status_code == status


@pytest.mark.asyncio
async def test_start_diagnosis_defaults_to_background_and_requires_explicit_boolean_focus(client):
    http, manager = client
    manager.start = AsyncMock(return_value={"status": "opening"})
    path = "/api/v1/product-diagnoses"
    response = await http.post(path, json={"url": SOURCE}, headers=headers())
    assert response.status_code == 200
    manager.start.assert_awaited_once_with(SOURCE, focus=False)
    manager.start.reset_mock()
    response = await http.post(path, json={"url": SOURCE, "focus": True}, headers=headers())
    assert response.status_code == 200
    manager.start.assert_awaited_once_with(SOURCE, focus=True)
    manager.start.reset_mock()
    for focus in [1, 0, "false", "true", None, []]:
        response = await http.post(path, json={"url": SOURCE, "focus": focus}, headers=headers())
        assert response.status_code == 422
    manager.start.assert_not_called()
