"""Truthful status summaries, recoverable event history and export provenance."""

import json
import time

import pytest

from crawler.store import Event, Product, Run
from test_api import auth, service


@pytest.mark.asyncio
async def test_mixed_run_separates_complete_from_finished_and_scheduled_retry(service):
    client, store = service
    retry_at = time.time() + 60
    with store.session() as session:
        run = Run(name="Mixed outcome", status="running", spec={"adapter": "browser"})
        session.add(run)
        session.flush()
        rid = run.id
        for status in ("completed", "partial", "needs_attention", "running", "queued"):
            session.add(
                Product(
                    run_id=rid,
                    title=status,
                    url="https://fixture.invalid/" + status,
                    status=status,
                    row_count=10,
                    coverage_status="complete" if status == "completed" else "partial",
                    error="Source mismatch" if status == "needs_attention" else None,
                    lease_until=retry_at if status == "queued" else 0,
                )
            )
    result = (await client.get(f"/api/v1/runs/{rid}", headers=auth())).json()
    assert result["status"] == "running"
    assert result["completed_products"] == 1
    assert result["finished_attempts"] == 3
    assert result["partial_products"] == 1
    assert result["active_products"] == 2
    assert result["attention_products"] == result["error_count"] == 1
    assert result["configuration_total"] is None and result["eta_seconds"] is None
    assert next(p for p in result["products"] if p["status"] == "queued")["retry_at"]
    assert all(
        p["retry_at"] is None for p in result["products"] if p["status"] != "queued"
    )


@pytest.mark.asyncio
async def test_event_history_recovers_old_error_and_incremental_cursor_does_not_skip(
    service,
):
    client, store = service
    with store.session() as session:
        run = Run(name="Event history", status="running")
        session.add(run)
        session.flush()
        rid = run.id
        session.add(
            Event(
                run_id=rid,
                type="product_error",
                payload={"message": "Old retained failure"},
            )
        )
        session.flush()
        first_id = session.query(Event).filter_by(run_id=rid).first().id
        session.add_all(
            Event(run_id=rid, type="progress", payload={"rows": i}) for i in range(600)
        )
    url = f"/api/v1/runs/{rid}/events"
    latest = (await client.get(url, headers=auth())).json()
    assert len(latest["items"]) == 250 and latest["has_older"]
    assert all(e["type"] != "product_error" for e in latest["items"])
    attention = (await client.get(url + "?category=attention", headers=auth())).json()
    assert attention["items"][0]["payload"]["message"] == "Old retained failure"
    older = (
        await client.get(url + f"?before={latest['next_before']}", headers=auth())
    ).json()
    assert older["items"][-1]["id"] < latest["items"][0]["id"]
    assert older["has_older"]
    observed = []
    after = first_id
    while True:
        page = (await client.get(url + f"?after={after}", headers=auth())).json()
        observed.extend(e["id"] for e in page["items"])
        after = page["next_after"]
        if not page["has_newer"]:
            break
    assert len(observed) == len(set(observed)) == 600
    assert observed[0] == first_id + 1
    assert (
        await client.get(url + "?after=1&before=2", headers=auth())
    ).status_code == 422


@pytest.mark.asyncio
async def test_export_includes_immutable_manifest_and_explicit_stale_row_count(service):
    client, store = service
    with store.session() as session:
        run = Run(name="Resumed export", status="running")
        session.add(run)
        session.flush()
        rid = run.id
        product = Product(
            run_id=rid,
            title="Retained product",
            status="running",
            row_count=20,
            coverage_status="partial",
            url="https://fixture.invalid/product",
        )
        session.add(product)
        session.flush()
        pid = product.id
        folder = store.root / "exports" / rid / pid / "snapshot-fixture"
        folder.mkdir(parents=True)
        csv = folder / "combined.csv"
        csv.write_text("quantity,subtotal\n100,10.00\n")
        manifest = {
            "generation": "snapshot-fixture",
            "created_at": "2026-09-26T00:00:00+00:00",
            "row_count": 10,
            "verification_claim": "Per-row evidence only",
            "items": [{"name": "combined.csv", "path": str(csv)}],
        }
        (folder / "manifest.json").write_text(json.dumps(manifest))
        product.export_manifest = manifest
    result = (await client.get(f"/api/v1/runs/{rid}/exports", headers=auth())).json()[
        "items"
    ]
    assert len(result) == 2
    assert all(
        f["snapshot_row_count"] == 10 and f["current_row_count"] == 20 and f["stale"]
        for f in result
    )
    assert all(
        f["current_coverage_status"] == "partial" and f["product_status"] == "running"
        for f in result
    )
    item = next(f for f in result if f["name"].endswith("manifest.json"))
    assert "/snapshot-fixture/manifest.json" in item["url"]
    downloaded = await client.get(item["url"], headers=auth())
    assert downloaded.status_code == 200 and downloaded.json() == manifest


def scope_rows():
    base = {
        "title": "Business Cards",
        "product_url": "https://www.printograph.com/products/business-cards/order",
        "source_product_id": "1",
        "configuration": {"size": {"value": "1", "label": "Standard"}},
        "quantity": 100,
        "subtotal": "10.00",
        "unit_price": "0.10",
        "currency": "USD",
        "provenance": "direct_quote",
        "verification_status": "source_observed",
        "coverage_status": "partial",
        "evidence_ids": [],
        "images": [],
    }
    legacy = {**base, "id": "legacy-price"}
    current = {
        **base,
        "id": "scoped-price",
        "record_schema_version": 2,
        "pricing_context": {
            "schema_version": 2,
            "service_context": {
                "version": 2,
                "status": "observed",
                "groups": {
                    "proofType": {"status": "selected", "values": ["31"]},
                    "fileDeliveryForIop": {"status": "selected", "values": ["5"]},
                },
            },
        },
    }
    return legacy, current


@pytest.mark.asyncio
async def test_rows_default_current_preserve_explicit_legacy_and_unknown_counts(
    service,
):
    from crawler.store import ProductRecordScope

    client, store = service
    legacy, current = scope_rows()
    with store.session() as session:
        run = Run(name="Scope migration", status="running", spec={"adapter": "browser"})
        session.add(run)
        session.flush()
        rid = run.id
        product = Product(
            run_id=rid, status="running", epoch=1, url=legacy["product_url"]
        )
        session.add(product)
        session.flush()
        pid = product.id
    store.append_rows(pid, 1, [legacy, current])
    url = f"/api/v1/runs/{rid}"
    before = (await client.get(url, headers=auth())).json()
    assert before["record_scope"] == "not_assessed"
    assert before["scoped_row_count"] is None and before["legacy_row_count"] is None
    assert before["row_count"] == 2
    # Filtering is safe before the durable scope count is initialized.
    visible = (await client.get(url + "/rows", headers=auth())).json()
    assert visible["total"] == 1
    assert [r["id"] for r in visible["items"]] == ["scoped-price"]
    history = (await client.get(url + "/rows?scope=legacy", headers=auth())).json()
    assert history["total"] == 1 and history["items"][0]["id"] == "legacy-price"
    assert history["items"][0]["record_scope"] == "legacy_unscoped"
    all_rows = (await client.get(url + "/rows?scope=all", headers=auth())).json()
    assert all_rows["total"] == 2
    with store.session() as session:
        session.add(ProductRecordScope(product_id=pid, legacy_row_count=1))
    after = (await client.get(url, headers=auth())).json()
    assert after["record_scope"] == "assessed"
    assert after["legacy_row_count"] == after["scoped_row_count"] == 1
    assert (await client.get(url + "/rows?offset=1", headers=auth())).json()[
        "items"
    ] == []
    assert (await client.get(url + "/rows?search=scoped", headers=auth())).json()[
        "total"
    ] == 1
    assert (
        await client.get(url + "/rows?scope=invalid", headers=auth())
    ).status_code == 422


@pytest.mark.asyncio
async def test_historical_export_is_explicit_and_scoped_freshness_uses_source_count(
    service,
):
    client, store = service
    with store.session() as session:
        run = Run(
            name="Historical export", status="running", spec={"adapter": "browser"}
        )
        session.add(run)
        session.flush()
        rid = run.id
        product = Product(
            run_id=rid,
            title="Cards",
            status="running",
            row_count=20,
            url="https://www.printograph.com/products/business-cards/order",
        )
        session.add(product)
        session.flush()
        pid = product.id
        folder = store.root / "exports" / rid / pid / "old-snapshot"
        folder.mkdir(parents=True)
        csv = folder / "combined.csv"
        csv.write_text("quantity,subtotal\n100,10.00\n")
        product.export_manifest = {"row_count": 20, "items": [{"path": str(csv)}]}
    url = f"/api/v1/runs/{rid}/exports"
    visible = (await client.get(url, headers=auth())).json()
    assert visible["items"] == [] and visible["withheld_historical_snapshots"] == 1
    audit = (await client.get(url + "?scope=all", headers=auth())).json()["items"]
    assert len(audit) == 1 and audit[0]["record_scope"] == "not_assessed"
    with store.session() as session:
        product = session.get(Product, pid)
        product.export_manifest = {
            **product.export_manifest,
            "row_count": 5,
            "source_row_count": 20,
            "legacy_row_count": 15,
            "scoped_row_count": 5,
            "record_scope": "service_context_v2",
        }
    scoped = (await client.get(url, headers=auth())).json()["items"]
    assert len(scoped) == 1 and scoped[0]["snapshot_row_count"] == 5
    assert scoped[0]["current_row_count"] == 20 and scoped[0]["stale"] is False
    assert scoped[0]["legacy_row_count"] == 15


@pytest.mark.asyncio
async def test_queued_retry_metadata_matches_current_schedule(service):
    client, store = service
    retry_at = time.time() + 60
    with store.session() as session:
        run = Run(name="Retry", status="running")
        session.add(run)
        session.flush()
        rid = run.id
        product = Product(
            run_id=rid,
            status="queued",
            lease_until=retry_at,
            url="https://fixture.invalid/product",
        )
        session.add(product)
        session.flush()
        session.add(
            Event(
                run_id=rid,
                type="product_retry_scheduled",
                payload={
                    "product_id": product.id,
                    "retry_at": retry_at,
                    "attempt": 2,
                    "code": "network_error",
                    "message": "Source connection reset",
                },
            )
        )
    data = (await client.get(f"/api/v1/runs/{rid}", headers=auth())).json()["products"][
        0
    ]
    assert (
        data["retry"]["attempt"] == 2
        and data["retry"]["message"] == "Source connection reset"
    )
