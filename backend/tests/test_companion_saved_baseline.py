"""Saved baseline reads must not trigger another site scan or approval."""
import os
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from crawler.autonomous_product_pipeline import StagingBaseline
from crawler.companion_autonomy_integration import autonomy_integration_router


class Environments:
    def __init__(self):
        self.calls = []

    async def environment_snapshot(self, environment):
        self.calls.append(environment)
        return {"environment": environment, "observation": len(self.calls)}


@pytest.mark.asyncio
async def test_latest_saved_capture_is_local_and_does_not_approve(tmp_path):
    env = Environments()
    baseline = StagingBaseline(tmp_path, env)
    assert baseline.latest_capture() is None
    first = await baseline.capture()
    second = await baseline.capture()
    os.utime(baseline.root / (first["id"] + ".json"), ns=(1, 1))
    assert baseline.latest_capture() == second
    assert len(env.calls) == 4
    assert baseline.status()["ready"] is False
    assert not (baseline.root / "approved.json").exists()


@pytest.mark.asyncio
async def test_latest_saved_capture_corruption_does_not_fall_back(tmp_path):
    baseline = StagingBaseline(tmp_path, Environments())
    first = await baseline.capture()
    path = baseline.root / ("a" * 64 + ".json")
    path.write_text('{}')
    os.utime(baseline.root / (first["id"] + ".json"), ns=(1, 1))
    with pytest.raises(ValueError, match="baseline_capture_hash_changed"):
        baseline.latest_capture()


@pytest.mark.asyncio
async def test_capture_filename_must_bind_the_embedded_identity(tmp_path):
    baseline = StagingBaseline(tmp_path, Environments())
    capture = await baseline.capture()
    wrong = 'a' * 64
    (baseline.root / (wrong + '.json')).write_bytes((baseline.root / (capture['id'] + '.json')).read_bytes())
    with pytest.raises(ValueError, match='baseline_capture_hash_changed'):
        baseline._capture(wrong)


@pytest.mark.asyncio
async def test_manifest_rehash_does_not_renew_a_changed_capture_identity(tmp_path):
    import json
    from crawler.companion_environments import digest
    baseline = StagingBaseline(tmp_path, Environments())
    capture = await baseline.capture()
    changed = {k: v for k, v in capture.items() if k != 'manifest_sha256'}
    changed['production'] = {'observation': 'changed-after-capture'}
    changed['manifest_sha256'] = digest(changed)
    (baseline.root / (capture['id'] + '.json')).write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='baseline_capture_hash_changed'):
        baseline._capture(capture['id'])


def test_symlink_capture_is_not_read(tmp_path):
    baseline = StagingBaseline(tmp_path, Environments())
    outside = tmp_path / "private.json"
    outside.write_text('{"secret":"must-not-be-read"}')
    (baseline.root / ("b" * 64 + ".json")).symlink_to(outside)
    with pytest.raises(ValueError, match="baseline_capture_storage_invalid"):
        baseline.latest_capture()


def test_saved_capture_route_requires_auth_and_has_no_remote_actions(tmp_path):
    baseline = StagingBaseline(tmp_path, Environments())
    service = SimpleNamespace(baseline=baseline)
    async def auth():
        raise HTTPException(401, "authentication_required")
    app = FastAPI()
    app.include_router(autonomy_integration_router(lambda: service, auth))
    with TestClient(app) as client:
        assert client.get('/api/v1/companion/autonomy/baseline/latest').status_code == 401
        assert client.get('/api/v1/companion/autonomy/baseline/alignment').status_code == 401
    app.dependency_overrides[auth] = lambda: None
    with TestClient(app) as client:
        assert client.get('/api/v1/companion/autonomy/baseline/latest').json() == {"capture": None}
        response = client.get('/api/v1/companion/autonomy/baseline/alignment')
        assert response.status_code == 409
        assert response.json()["detail"] == "baseline_environment_capture_required"
    assert baseline.environments.calls == []


def test_alignment_route_is_bound_to_requested_saved_capture(tmp_path, monkeypatch):
    import asyncio
    from crawler import companion_baseline_alignment
    baseline = StagingBaseline(tmp_path, Environments())
    first = asyncio.run(baseline.capture())
    second = asyncio.run(baseline.capture())
    monkeypatch.setattr(companion_baseline_alignment, 'build_alignment_plan', lambda capture: {'capture_id': capture['id'], 'ready': False})
    app = FastAPI()
    app.include_router(autonomy_integration_router(lambda: SimpleNamespace(baseline=baseline), lambda: None))
    with TestClient(app) as client:
        response = client.get('/api/v1/companion/autonomy/baseline/alignment', params={'capture_id': first['id']})
        assert response.status_code == 200 and response.json()['capture_id'] == first['id']
        response = client.get('/api/v1/companion/autonomy/baseline/alignment', params={'capture_id': second['id']})
        assert response.status_code == 200 and response.json()['capture_id'] == second['id']
        assert client.get('/api/v1/companion/autonomy/baseline/alignment', params={'capture_id': '../private'}).status_code == 409
    assert len(baseline.environments.calls) == 4
