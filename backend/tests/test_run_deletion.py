"""Deleting a run removes its own rows and export files, not shared objects."""

import pytest

from crawler.store import Event, Product, Run
from test_api import auth, service


def make_run(store):
    with store.session() as session:
        run = Run(name="Disposable run", spec={"adapter": "fixture"})
        session.add(run)
        session.flush()
        rid = run.id
        session.add(Product(run_id=rid, url="https://fixture.invalid/1", title="One"))
        session.add(Event(run_id=rid, type="run_created", payload={}))
        return rid


@pytest.mark.asyncio
async def test_delete_run_removes_run_products_events_and_exports(service):
    client, store = service
    run_id = make_run(store)
    export_dir = store.root / "exports" / run_id
    export_dir.mkdir(parents=True)
    (export_dir / "rows.csv").write_text("id\n1\n")

    response = await client.delete(f"/api/v1/runs/{run_id}", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"deleted": run_id}

    assert (await client.get(f"/api/v1/runs/{run_id}", headers=auth())).status_code == 404
    assert not export_dir.exists()
    with store.session() as session:
        assert session.get(Run, run_id) is None
        assert list(session.query(Product).filter(Product.run_id == run_id)) == []
        assert list(session.query(Event).filter(Event.run_id == run_id)) == []


@pytest.mark.asyncio
async def test_delete_unknown_run_is_404(service):
    client, _ = service
    response = await client.delete("/api/v1/runs/missing", headers=auth())
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_run_requires_auth(service):
    client, store = service
    run_id = make_run(store)
    response = await client.delete(f"/api/v1/runs/{run_id}")
    assert response.status_code == 401
